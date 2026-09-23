//! Integrity checks over what is already on disk.
//!
//! `verify` answers the questions you actually have after a long backfill: are
//! there holes, are timestamps monotonic, are there duplicates, do the OHLC
//! invariants hold, and how much of the expected history is present?

use std::time::Instant;

use serde::Serialize;

use crate::error::Result;
use crate::kucoin::{Candle, Timeframe};
use crate::storage::{parquet_store, Store};
use crate::util::format_ts;

/// A stretch of missing bars.
#[derive(Debug, Clone, Copy, Serialize)]
pub struct Gap {
    /// Last bar before the hole.
    pub after: i64,
    /// First bar after the hole.
    pub before: i64,
    /// Number of missing bars.
    pub missing: i64,
}

impl Gap {
    /// Human readable description.
    pub fn describe(&self) -> String {
        format!(
            "{} missing between {} and {}",
            self.missing,
            format_ts(self.after),
            format_ts(self.before)
        )
    }
}

/// How much to report.
#[derive(Debug, Clone, Copy)]
pub struct VerifyOptions {
    /// Maximum number of gaps to report (largest first).
    pub max_gaps: usize,
    /// Only list gaps at least this many bars wide.
    pub min_gap_bars: i64,
}

impl Default for VerifyOptions {
    fn default() -> Self {
        VerifyOptions {
            max_gaps: 20,
            min_gap_bars: 2,
        }
    }
}

/// Verification result for one `(symbol, timeframe)` series.
#[derive(Debug, Clone, Serialize)]
pub struct VerifyReport {
    /// Trading pair.
    pub symbol: String,
    /// Timeframe.
    pub timeframe: Timeframe,
    /// Files inspected.
    pub files: usize,
    /// Total rows.
    pub rows: u64,
    /// Oldest bar.
    pub first_candle: Option<i64>,
    /// Newest bar.
    pub last_candle: Option<i64>,
    /// Bars missing inside `[first, last]`.
    pub missing_bars: i64,
    /// Bars expected between the first and last bar.
    pub expected_bars: i64,
    /// Percentage of the expected range that is present.
    pub coverage_pct: f64,
    /// Duplicate timestamps (always zero after a merge, so any value is a bug).
    pub duplicates: u64,
    /// Rows that appear out of order.
    pub out_of_order: u64,
    /// Rows violating OHLC invariants.
    pub invalid_rows: u64,
    /// Rows whose timestamp is not aligned to the grid.
    pub misaligned_rows: u64,
    /// Detected gaps, largest first.
    pub gaps: Vec<Gap>,
    /// Threshold used when deciding which holes to list.
    ///
    /// Holes narrower than this are still counted in
    /// [`VerifyReport::missing_bars`], so the summary says so explicitly instead
    /// of claiming "no gaps" next to a coverage below 100%.
    pub min_gap_bars: i64,
    /// Files skipped because they were empty.
    pub empty_files: usize,
    /// Wall-clock duration.
    pub elapsed_ms: u64,
}

impl VerifyReport {
    /// True when nothing anomalous was found.
    pub fn is_clean(&self) -> bool {
        self.gaps.is_empty()
            && self.duplicates == 0
            && self.invalid_rows == 0
            && self.misaligned_rows == 0
    }

    /// Human readable summary (multi-line when gaps are present).
    pub fn render(&self) -> String {
        let mut out = format!(
            "{} {}: {} files, {} rows, {} .. {}, {:.2}% complete",
            self.symbol,
            self.timeframe.slug(),
            self.files,
            self.rows,
            self.first_candle
                .map(format_ts)
                .unwrap_or_else(|| "-".into()),
            self.last_candle
                .map(format_ts)
                .unwrap_or_else(|| "-".into()),
            self.coverage_pct
        );
        if self.duplicates > 0 {
            out.push_str(&format!(", {} duplicates", self.duplicates));
        }
        if self.out_of_order > 0 {
            out.push_str(&format!(", {} out-of-order rows", self.out_of_order));
        }
        if self.invalid_rows > 0 {
            out.push_str(&format!(", {} invalid rows", self.invalid_rows));
        }
        if self.misaligned_rows > 0 {
            out.push_str(&format!(", {} misaligned rows", self.misaligned_rows));
        }
        if self.empty_files > 0 {
            out.push_str(&format!(", {} empty files", self.empty_files));
        }
        if self.gaps.is_empty() {
            if self.missing_bars > 0 {
                out.push_str(&format!(
                    ", {} bars missing in total (every hole is narrower than {} bars)",
                    self.missing_bars, self.min_gap_bars
                ));
            } else {
                out.push_str(", no gaps");
            }
        } else {
            out.push_str(&format!(
                ", {} bars missing in total, {} largest gaps:",
                self.missing_bars,
                self.gaps.len()
            ));
            for gap in self.gaps.iter().take(5) {
                out.push_str(&format!("\n    {}", gap.describe()));
            }
            if self.gaps.len() > 5 {
                out.push_str(&format!("\n    … and {} more", self.gaps.len() - 5));
            }
        }
        out
    }
}

/// Inspect one series.
pub fn verify_series(
    store: &Store,
    symbol: &str,
    tf: Timeframe,
    opts: VerifyOptions,
) -> Result<VerifyReport> {
    let started = Instant::now();
    let files = store.scan_series(symbol, tf)?;

    let mut report = VerifyReport {
        symbol: symbol.to_string(),
        timeframe: tf,
        files: files.len(),
        rows: 0,
        first_candle: None,
        last_candle: None,
        missing_bars: 0,
        expected_bars: 0,
        coverage_pct: 100.0,
        duplicates: 0,
        out_of_order: 0,
        invalid_rows: 0,
        misaligned_rows: 0,
        gaps: Vec::new(),
        min_gap_bars: opts.min_gap_bars,
        empty_files: 0,
        elapsed_ms: 0,
    };

    let mut prev: Option<i64> = None;
    for file in &files {
        let candles: Vec<Candle> = parquet_store::read_candles(&file.path)?;
        if candles.is_empty() {
            report.empty_files += 1;
            continue;
        }
        for c in &candles {
            report.rows += 1;
            report.first_candle = Some(report.first_candle.map_or(c.time, |v: i64| v.min(c.time)));
            report.last_candle = Some(report.last_candle.map_or(c.time, |v: i64| v.max(c.time)));

            if c.validate_invariants().is_err() {
                report.invalid_rows += 1;
            }
            // `align` is grid alignment for fixed intervals and "the 1st of the
            // month" for the calendar month, so this check works for both.
            if tf.align(c.time) != c.time {
                report.misaligned_rows += 1;
            }
            if let Some(previous) = prev {
                if c.time == previous {
                    report.duplicates += 1;
                } else if c.time < previous {
                    report.out_of_order += 1;
                } else {
                    // Ask the timeframe what the very next bar should be instead of
                    // assuming a fixed step: months are 28-31 days long.
                    let expected = tf.shift(previous, 1);
                    if c.time != expected {
                        let missing = tf.slots_between(previous, c.time) - 1;
                        if missing > 0 {
                            report.missing_bars += missing;
                            if missing >= opts.min_gap_bars {
                                report.gaps.push(Gap {
                                    after: previous,
                                    before: c.time,
                                    missing,
                                });
                            }
                        }
                    }
                }
            }
            prev = Some(c.time);
        }
    }

    if let (Some(first), Some(last)) = (report.first_candle, report.last_candle) {
        report.expected_bars = tf.slots_between(first, last) + 1;
        if report.expected_bars > 0 {
            report.coverage_pct =
                (report.rows as f64 / report.expected_bars as f64 * 100.0).min(100.0);
        }
    }

    report.gaps.sort_by_key(|g| std::cmp::Reverse(g.missing));
    report.gaps.truncate(opts.max_gaps.max(1));
    report.elapsed_ms = started.elapsed().as_millis() as u64;
    Ok(report)
}

/// Every `(symbol, timeframe)` pair present on disk.
pub fn discover_series(store: &Store) -> Result<Vec<(String, Timeframe)>> {
    let mut out = Vec::new();
    for symbol in store.stored_symbols()? {
        for tf in store.stored_timeframes(&symbol)? {
            out.push((symbol.clone(), tf));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::{LayoutOptions, WriteOptions};

    fn candle(time: i64, price: f64) -> Candle {
        Candle {
            time,
            open: price,
            high: price * 1.01,
            low: price * 0.99,
            close: price,
            volume: 1.0,
            turnover: price,
        }
    }

    fn store(dir: &std::path::Path) -> Store {
        Store::new(dir, LayoutOptions::default(), WriteOptions::default())
    }

    fn hour_aligned(ts: i64) -> i64 {
        crate::util::align_down(ts, 3600)
    }

    #[test]
    fn clean_series_reports_no_gaps() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = hour_aligned(1_600_000_000);
        let candles: Vec<Candle> = (0..500).map(|i| candle(start + i * 3600, 10.0)).collect();
        let period = s.period_of(start, Timeframe::H1);
        s.merge("BTC-USDT", Timeframe::H1, period, &candles)
            .unwrap();

        let report =
            verify_series(&s, "BTC-USDT", Timeframe::H1, VerifyOptions::default()).unwrap();
        assert_eq!(report.rows, 500);
        assert!(report.is_clean(), "{report:?}");
        assert_eq!(report.expected_bars, 500);
        assert!((report.coverage_pct - 100.0).abs() < f64::EPSILON);
    }

    #[test]
    fn detects_gaps_and_coverage() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = hour_aligned(1_600_000_000);
        let mut candles: Vec<Candle> = (0..100).map(|i| candle(start + i * 3600, 10.0)).collect();
        // Hole of 10 bars, then more data.
        candles.extend((110..200).map(|i| candle(start + i * 3600, 10.0)));
        let period = s.period_of(start, Timeframe::H1);
        s.merge("BTC-USDT", Timeframe::H1, period, &candles)
            .unwrap();

        let report =
            verify_series(&s, "BTC-USDT", Timeframe::H1, VerifyOptions::default()).unwrap();
        assert_eq!(report.rows, 190);
        assert_eq!(report.gaps.len(), 1);
        assert_eq!(report.gaps[0].missing, 10);
        assert_eq!(report.missing_bars, 10);
        assert_eq!(report.expected_bars, 200);
        assert!((report.coverage_pct - 95.0).abs() < 0.01);
        assert!(!report.is_clean());
        assert!(report.render().contains("10 missing"));
    }

    #[test]
    fn tiny_gaps_can_be_filtered_out() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = hour_aligned(1_600_000_000);
        let mut candles: Vec<Candle> = (0..50).map(|i| candle(start + i * 3600, 10.0)).collect();
        candles.extend((51..100).map(|i| candle(start + i * 3600, 10.0)));
        let period = s.period_of(start, Timeframe::H1);
        s.merge("BTC-USDT", Timeframe::H1, period, &candles)
            .unwrap();

        let report = verify_series(
            &s,
            "BTC-USDT",
            Timeframe::H1,
            VerifyOptions {
                min_gap_bars: 5,
                ..Default::default()
            },
        )
        .unwrap();
        assert!(report.gaps.is_empty(), "{report:?}");
        assert_eq!(report.missing_bars, 1, "still counted in the totals");
    }

    #[test]
    fn detects_invalid_and_misaligned_rows() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = hour_aligned(1_600_000_000);
        let mut candles: Vec<Candle> = (0..10).map(|i| candle(start + i * 3600, 10.0)).collect();
        // Impossible bar (high below open) and an unaligned timestamp.
        candles.push(Candle {
            time: start + 10 * 3600,
            open: 10.0,
            high: 5.0,
            low: 1.0,
            close: 10.0,
            volume: 1.0,
            turnover: 1.0,
        });
        candles.push(candle(start + 11 * 3600 + 5, 10.0));
        let period = s.period_of(start, Timeframe::H1);
        s.merge("BTC-USDT", Timeframe::H1, period, &candles)
            .unwrap();

        let report =
            verify_series(&s, "BTC-USDT", Timeframe::H1, VerifyOptions::default()).unwrap();
        assert_eq!(report.invalid_rows, 1);
        assert_eq!(report.misaligned_rows, 1);
        assert!(!report.is_clean());
    }

    #[test]
    fn summary_distinguishes_small_holes_from_no_holes() {
        // A hole below `min_gap_bars` must not make the summary claim "no gaps"
        // while also reporting less than full coverage.
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = hour_aligned(1_600_000_000);
        let mut candles: Vec<Candle> = (0..10).map(|i| candle(start + i * 3600, 10.0)).collect();
        candles.extend((11..20).map(|i| candle(start + i * 3600, 10.0)));
        let period = s.period_of(start, Timeframe::H1);
        s.merge("BTC-USDT", Timeframe::H1, period, &candles)
            .unwrap();

        let report =
            verify_series(&s, "BTC-USDT", Timeframe::H1, VerifyOptions::default()).unwrap();
        assert!(report.gaps.is_empty());
        assert_eq!(report.missing_bars, 1);
        assert_eq!(report.min_gap_bars, 2);
        let text = report.render();
        assert!(text.contains("1 bars missing in total"), "{text}");
        assert!(text.contains("narrower than 2 bars"), "{text}");
        assert!(!text.contains("no gaps"), "{text}");

        let clean = verify_series(&s, "BTC-USDT", Timeframe::M1, VerifyOptions::default()).unwrap();
        assert!(clean.render().contains("no gaps"), "{}", clean.render());
    }

    #[test]
    fn monthly_gaps_are_counted_in_months() {
        // One missing month must be reported as exactly one missing bar — not as
        // ~30 days of missing daily bars.
        let dir = tempfile::tempdir().unwrap();
        let s = Store::new(
            dir.path(),
            LayoutOptions {
                partition: crate::storage::PartitionRule::Single,
                ..Default::default()
            },
            WriteOptions::default(),
        );
        let candles: Vec<Candle> = [
            "2024-01-01",
            "2024-02-01",
            "2024-04-01",
            "2024-05-01",
            "2024-06-01",
        ]
        .iter()
        .map(|d| candle(crate::util::date_to_unix(d).unwrap(), 10.0))
        .collect();
        let period = s.period_of(candles[0].time, Timeframe::Mon1);
        s.merge("BTC-USDT", Timeframe::Mon1, period, &candles)
            .unwrap();

        let report = verify_series(
            &s,
            "BTC-USDT",
            Timeframe::Mon1,
            VerifyOptions {
                min_gap_bars: 1,
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(report.rows, 5);
        assert_eq!(report.expected_bars, 6);
        assert_eq!(report.missing_bars, 1, "March 2024 is one bar short");
        assert_eq!(report.gaps.len(), 1);
        assert_eq!(report.gaps[0].missing, 1);
        assert_eq!(report.misaligned_rows, 0);
        assert!((report.coverage_pct - 83.33).abs() < 0.1, "{report:?}");

        // With the default threshold the single-month hole is counted but not
        // listed, and the summary says so.
        let default_threshold =
            verify_series(&s, "BTC-USDT", Timeframe::Mon1, VerifyOptions::default()).unwrap();
        assert!(default_threshold.gaps.is_empty());
        assert_eq!(default_threshold.missing_bars, 1);
        assert!(default_threshold.render().contains("narrower than 2 bars"));

        // A monthly bar that is not the 1st of a month is flagged as misaligned.
        let off_grid = Candle {
            time: crate::util::date_to_unix("2024-07-15").unwrap(),
            ..candle(crate::util::date_to_unix("2024-07-01").unwrap(), 10.0)
        };
        let s2 = Store::new(
            dir.path(),
            LayoutOptions {
                partition: crate::storage::PartitionRule::Single,
                ..Default::default()
            },
            WriteOptions::default(),
        );
        let period = s2.period_of(off_grid.time, Timeframe::Mon1);
        s2.merge("ETH-USDT", Timeframe::Mon1, period, &[off_grid])
            .unwrap();
        let report =
            verify_series(&s2, "ETH-USDT", Timeframe::Mon1, VerifyOptions::default()).unwrap();
        assert_eq!(report.misaligned_rows, 1);
    }

    #[test]
    fn discovers_series_on_disk() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = hour_aligned(1_600_000_000);
        let candles = vec![candle(start, 1.0)];
        for (symbol, tf) in [("BTC-USDT", Timeframe::H1), ("ETH-USDT", Timeframe::M1)] {
            let period = s.period_of(start, tf);
            s.merge(symbol, tf, period, &candles).unwrap();
        }
        let mut series = discover_series(&s).unwrap();
        series.sort();
        assert_eq!(
            series,
            vec![
                ("BTC-USDT".to_string(), Timeframe::H1),
                ("ETH-USDT".to_string(), Timeframe::M1)
            ]
        );
    }

    #[test]
    fn empty_store_verifies_cleanly() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let report =
            verify_series(&s, "NOPE-USDT", Timeframe::M1, VerifyOptions::default()).unwrap();
        assert_eq!(report.rows, 0);
        assert!(report.is_clean());
        assert_eq!(report.coverage_pct, 100.0);
    }

    #[test]
    fn gaps_across_file_boundaries_are_detected() {
        let dir = tempfile::tempdir().unwrap();
        // Force monthly partitioning so the hole spans two files.
        let s = Store::new(
            dir.path(),
            LayoutOptions {
                partition: crate::storage::PartitionRule::Month,
                ..Default::default()
            },
            WriteOptions::default(),
        );
        let jan = crate::util::date_to_unix("2024-01-31").unwrap();
        let mar = crate::util::date_to_unix("2024-03-01").unwrap();
        for (start, count) in [(jan, 10), (mar, 10)] {
            let candles: Vec<Candle> = (0..count).map(|i| candle(start + i * 3600, 5.0)).collect();
            let period = s.period_of(start, Timeframe::H1);
            s.merge("BTC-USDT", Timeframe::H1, period, &candles)
                .unwrap();
        }
        let report =
            verify_series(&s, "BTC-USDT", Timeframe::H1, VerifyOptions::default()).unwrap();
        assert_eq!(report.files, 2);
        assert_eq!(report.gaps.len(), 1);
        // February 2024 has 29 days, so the hole is over 690 hourly bars.
        assert!(report.gaps[0].missing > 690, "{report:?}");
        assert!(report.coverage_pct < 5.0, "{report:?}");
    }
}
