//! Read-only inventory of what is already stored on disk.
//!
//! Everything here is answered from Parquet footers and file sizes, so `status`
//! stays fast even with thousands of series and never touches the network. Gap
//! hunting is a different, heavier job: see [`crate::verify`].

use serde::Serialize;

use crate::error::{Error, Result};
use crate::kucoin::{Candle, Timeframe};
use crate::storage::{parquet_store, Store};
use crate::util::{format_ts, human_bytes};

/// One `(symbol, timeframe)` series as it exists on disk.
#[derive(Debug, Clone, Serialize)]
pub struct SeriesStatus {
    /// Trading pair.
    pub symbol: String,
    /// Timeframe.
    pub timeframe: Timeframe,
    /// Number of partition files.
    pub files: usize,
    /// Total bars stored.
    pub bars: u64,
    /// Oldest bar.
    pub first: Option<i64>,
    /// Newest bar.
    pub last: Option<i64>,
    /// Where the next run resumes: the bar right after the last stored one.
    ///
    /// Only finished bars are ever written, so the series always ends on a complete
    /// bar and continuing straight after it is exactly right — for `1m` that is the
    /// next minute, for `1mon` the next month.
    pub next_sync_from: Option<i64>,
    /// Size on disk.
    pub bytes: u64,
}

impl SeriesStatus {
    /// True when the series has no data at all.
    pub fn is_empty(&self) -> bool {
        self.bars == 0
    }
}

/// Inventory of a whole data directory.
#[derive(Debug, Clone, Default, Serialize)]
pub struct StatusReport {
    /// One entry per series, sorted by symbol then timeframe.
    pub series: Vec<SeriesStatus>,
}

impl StatusReport {
    /// Total bars across every series.
    pub fn total_bars(&self) -> u64 {
        self.series.iter().map(|s| s.bars).sum()
    }

    /// Total size on disk.
    pub fn total_bytes(&self) -> u64 {
        self.series.iter().map(|s| s.bytes).sum()
    }

    /// Number of symbols covered.
    pub fn symbols(&self) -> usize {
        let mut names: Vec<&str> = self.series.iter().map(|s| s.symbol.as_str()).collect();
        names.sort_unstable();
        names.dedup();
        names.len()
    }

    /// Human readable table plus a totals line.
    pub fn render(&self) -> String {
        if self.series.is_empty() {
            return "  no data collected yet\n".to_string();
        }
        let mut out = format!(
            "  {:<12} {:<5} {:>12} {:<17} {:<17} {:<17} {:>6} {:>10}\n",
            "SYMBOL", "TF", "BARS", "FIRST", "LAST", "NEXT SYNC FROM", "FILES", "SIZE"
        );
        for s in &self.series {
            let fmt = |v: Option<i64>| {
                v.map(|t| format_ts(t).replace(" UTC", ""))
                    .unwrap_or_else(|| "-".to_string())
            };
            out.push_str(&format!(
                "  {:<12} {:<5} {:>12} {:<17} {:<17} {:<17} {:>6} {:>10}\n",
                s.symbol,
                s.timeframe.slug(),
                s.bars,
                fmt(s.first),
                fmt(s.last),
                fmt(s.next_sync_from),
                s.files,
                human_bytes(s.bytes)
            ));
        }
        out.push_str(&format!(
            "  totals: {} series over {} symbols, {} bars, {}",
            self.series.len(),
            self.symbols(),
            self.total_bars(),
            human_bytes(self.total_bytes())
        ));
        out
    }
}

/// Build the inventory for the selected series.
///
/// `symbols` and `timeframes` act as filters; an empty filter means "everything
/// on disk".
pub fn inventory(
    store: &Store,
    symbols: &[String],
    timeframes: &[Timeframe],
) -> Result<StatusReport> {
    inventory_at(store, symbols, timeframes, crate::util::now_unix())
}

/// [`inventory`] with an explicit "now", so tests are not clock dependent.
pub fn inventory_at(
    store: &Store,
    symbols: &[String],
    timeframes: &[Timeframe],
    now: i64,
) -> Result<StatusReport> {
    let selected: Vec<(String, Timeframe)> = if symbols.is_empty() {
        crate::verify::discover_series(store)?
    } else {
        let mut out = Vec::new();
        for symbol in symbols {
            let stored = store.stored_timeframes(symbol)?;
            if stored.is_empty() {
                out.push((symbol.clone(), Timeframe::M1));
            }
            for tf in stored {
                out.push((symbol.clone(), tf));
            }
        }
        out
    };

    let mut series = Vec::new();
    for (symbol, tf) in selected {
        if !timeframes.is_empty() && !timeframes.contains(&tf) {
            continue;
        }
        series.push(series_status_at(store, &symbol, tf, now)?);
    }
    series.sort_by(|a, b| {
        a.symbol
            .cmp(&b.symbol)
            .then_with(|| a.timeframe.cmp(&b.timeframe))
    });
    Ok(StatusReport { series })
}

/// Inventory of a single series, as of the current clock.
pub fn series_status(store: &Store, symbol: &str, tf: Timeframe) -> Result<SeriesStatus> {
    series_status_at(store, symbol, tf, crate::util::now_unix())
}

/// [`series_status`] with an explicit "now".
pub fn series_status_at(
    store: &Store,
    symbol: &str,
    tf: Timeframe,
    now: i64,
) -> Result<SeriesStatus> {
    let files = store.scan_series(symbol, tf)?;
    let bars: u64 = files.iter().map(|f| f.rows.max(0) as u64).sum();
    let first = files.iter().map(|f| f.min).min();
    let last = files.iter().map(|f| f.max).max();
    let bytes = files
        .iter()
        .filter_map(|f| std::fs::metadata(&f.path).ok())
        .map(|m| m.len())
        .sum();
    Ok(SeriesStatus {
        symbol: symbol.to_string(),
        timeframe: tf,
        files: files.len(),
        bars,
        first,
        last,
        next_sync_from: last.map(|last| resume_point(tf, last, now)),
        bytes,
    })
}

/// Where a run would pick the series up again.
fn resume_point(tf: Timeframe, last: i64, _now: i64) -> i64 {
    tf.shift(last, 1)
}

/// The newest `count` bars of a series, oldest first.
pub fn tail(store: &Store, symbol: &str, tf: Timeframe, count: usize) -> Result<Vec<Candle>> {
    read_edge(store, symbol, tf, count, Edge::Newest)
}

/// The oldest `count` bars of a series, oldest first.
pub fn head(store: &Store, symbol: &str, tf: Timeframe, count: usize) -> Result<Vec<Candle>> {
    read_edge(store, symbol, tf, count, Edge::Oldest)
}

#[derive(Clone, Copy)]
enum Edge {
    Oldest,
    Newest,
}

fn read_edge(
    store: &Store,
    symbol: &str,
    tf: Timeframe,
    count: usize,
    edge: Edge,
) -> Result<Vec<Candle>> {
    if count == 0 {
        return Ok(Vec::new());
    }
    let files = store.scan_series(symbol, tf)?;
    if files.is_empty() {
        return Err(Error::Data(format!(
            "no local data for {symbol} {}",
            tf.slug()
        )));
    }

    // Walk partitions from the requested edge inwards until enough bars are read.
    let ordered: Vec<_> = match edge {
        Edge::Oldest => files.iter().collect(),
        Edge::Newest => files.iter().rev().collect(),
    };
    let mut collected: Vec<Candle> = Vec::new();
    for file in ordered {
        let mut candles = parquet_store::read_candles(&file.path)?;
        match edge {
            Edge::Oldest => collected.append(&mut candles),
            Edge::Newest => {
                candles.extend(std::mem::take(&mut collected));
                collected = candles;
            }
        }
        if collected.len() >= count {
            break;
        }
    }
    Ok(match edge {
        Edge::Oldest => collected.into_iter().take(count).collect(),
        Edge::Newest => {
            let skip = collected.len().saturating_sub(count);
            collected.into_iter().skip(skip).collect()
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::{LayoutOptions, WriteOptions};

    fn store(dir: &std::path::Path) -> Store {
        Store::new(dir, LayoutOptions::default(), WriteOptions::default())
    }

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

    fn seed(s: &Store, symbol: &str, tf: Timeframe, start: i64, count: i64) {
        let iv = tf.fixed_seconds().unwrap_or(30 * 86_400);
        let candles: Vec<Candle> = (0..count)
            .map(|i| candle(start + i * iv, 1.0 + i as f64))
            .collect();
        let period = s.period_of(start, tf);
        s.merge(symbol, tf, period, &candles).unwrap();
    }

    #[test]
    fn inventory_reports_what_is_stored() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let jan = crate::util::date_to_unix("2024-01-01").unwrap();
        seed(&s, "BTC-USDT", Timeframe::H1, jan, 100);
        seed(&s, "ETH-USDT", Timeframe::D1, jan, 10);

        let report = inventory(&s, &[], &[]).unwrap();
        assert_eq!(report.series.len(), 2);
        assert_eq!(report.symbols(), 2);
        assert_eq!(report.total_bars(), 110);
        assert!(report.total_bytes() > 0);

        let btc = report
            .series
            .iter()
            .find(|s| s.symbol == "BTC-USDT")
            .expect("BTC present");
        assert_eq!(btc.bars, 100);
        assert_eq!(btc.first, Some(jan));
        assert_eq!(btc.last, Some(jan + 99 * 3600));
        assert_eq!(btc.files, 1);
        // The run resumes one bar after the last stored one.
        assert_eq!(btc.next_sync_from, Some(jan + 100 * 3600));

        let rendered = report.render();
        assert!(rendered.contains("SYMBOL"), "{rendered}");
        assert!(rendered.contains("BTC-USDT"), "{rendered}");
        assert!(rendered.contains("2024-01-01 00:00:00"), "{rendered}");
        assert!(
            rendered.contains("totals: 2 series over 2 symbols, 110 bars"),
            "{rendered}"
        );
    }

    #[test]
    fn filters_narrow_the_inventory() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let jan = crate::util::date_to_unix("2024-01-01").unwrap();
        seed(&s, "BTC-USDT", Timeframe::H1, jan, 10);
        seed(&s, "BTC-USDT", Timeframe::D1, jan, 5);
        seed(&s, "ETH-USDT", Timeframe::H1, jan, 7);

        let only_tf = inventory(&s, &[], &[Timeframe::D1]).unwrap();
        assert_eq!(only_tf.series.len(), 1);
        assert_eq!(only_tf.series[0].symbol, "BTC-USDT");
        assert_eq!(only_tf.series[0].bars, 5);

        let only_symbol = inventory(&s, &["ETH-USDT".to_string()], &[]).unwrap();
        assert_eq!(only_symbol.series.len(), 1);
        assert_eq!(only_symbol.series[0].symbol, "ETH-USDT");

        // A symbol with nothing on disk is reported as empty rather than silently
        // dropped.
        let missing = inventory(&s, &["NOPE-USDT".to_string()], &[]).unwrap();
        assert_eq!(missing.series.len(), 1);
        assert!(missing.series[0].is_empty());
        assert_eq!(missing.series[0].first, None);
    }

    #[test]
    fn resume_point_is_the_bar_after_the_last_stored_one() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let jan = crate::util::date_to_unix("2024-01-01").unwrap();
        seed(&s, "OLD-USDT", Timeframe::H1, jan, 24 * 20); // 1-20 January
        let last_bar = jan + (24 * 20 - 1) * 3600; // 2024-01-20 23:00

        let view = series_status(&s, "OLD-USDT", Timeframe::H1).unwrap();
        assert_eq!(view.next_sync_from, Some(last_bar + 3600));

        // Per timeframe: a daily series resumes on the next day, not the next hour.
        seed(&s, "OLD-USDT", Timeframe::D1, jan, 10);
        let daily = series_status(&s, "OLD-USDT", Timeframe::D1).unwrap();
        assert_eq!(daily.last, Some(jan + 9 * 86_400));
        assert_eq!(daily.next_sync_from, Some(jan + 10 * 86_400));

        // And a monthly series resumes on the next month.
        seed(&s, "OLD-USDT", Timeframe::Mon1, jan, 3);
        let monthly = series_status(&s, "OLD-USDT", Timeframe::Mon1).unwrap();
        assert_eq!(
            monthly.last,
            Some(crate::util::date_to_unix("2024-03-01").unwrap())
        );
        assert_eq!(
            monthly.next_sync_from,
            Some(crate::util::date_to_unix("2024-04-01").unwrap())
        );
    }

    #[test]
    fn empty_store_renders_a_clear_message() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let report = inventory(&s, &[], &[]).unwrap();
        assert!(report.series.is_empty());
        assert!(report.render().contains("no data collected yet"));
    }

    #[test]
    fn head_and_tail_read_the_edges() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let jan = crate::util::date_to_unix("2024-01-01").unwrap();
        // Two monthly partitions, so the edge readers have to cross files.
        seed(&s, "BTC-USDT", Timeframe::M15, jan, 96 * 31); // all of January
        let feb = crate::util::date_to_unix("2024-02-01").unwrap();
        seed(&s, "BTC-USDT", Timeframe::M15, feb, 96 * 29); // all of February

        let first3 = head(&s, "BTC-USDT", Timeframe::M15, 3).unwrap();
        assert_eq!(first3.len(), 3);
        assert_eq!(first3[0].time, jan);
        assert_eq!(first3[1].time, jan + 900);
        assert!(first3[0].time < first3[2].time);

        let last3 = tail(&s, "BTC-USDT", Timeframe::M15, 3).unwrap();
        assert_eq!(last3.len(), 3);
        assert_eq!(last3[2].time, feb + (96 * 29 - 1) * 900);
        assert!(last3[0].time < last3[2].time);

        assert!(tail(&s, "BTC-USDT", Timeframe::M15, 0).unwrap().is_empty());
        // More than exists: everything, still ordered.
        let all = tail(&s, "BTC-USDT", Timeframe::M15, 10_000).unwrap();
        assert_eq!(all.len(), 96 * (31 + 29));
        assert!(all.windows(2).all(|w| w[0].time < w[1].time));

        assert!(tail(&s, "NOPE-USDT", Timeframe::M15, 3).is_err());
    }
}
