//! Parquet storage layer: path layout plus read/write helpers.
//!
//! [`Store`] is the single entry point used by the collector and the verifier,
//! so the on-disk contract lives in exactly one place.

pub mod layout;
pub mod parquet_store;

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

pub use layout::{LayoutOptions, PartitionRule, Period};
pub use parquet_store::{FileInfo, MergeStats, WriteOptions, HISTORY_FLOOR_KEY};

use crate::error::{Error, Result};
use crate::kucoin::{Candle, Timeframe};

/// One partition file on disk together with its time coverage.
#[derive(Debug, Clone)]
pub struct FileCoverage {
    /// Absolute path.
    pub path: PathBuf,
    /// Period the file belongs to.
    pub period: Period,
    /// Oldest bar in the file.
    pub min: i64,
    /// Newest bar in the file.
    pub max: i64,
    /// Row count.
    pub rows: i64,
    /// History floor the file recorded when it was written, if it carries one.
    pub floor: Option<i64>,
}

impl FileCoverage {
    /// True when the file spans `[start, end]` (both inclusive).
    pub fn covers(&self, start: i64, end: i64) -> bool {
        self.min <= start && self.max >= end
    }
}

/// Everything a series looks like on disk, read in a single pass over footers.
#[derive(Debug, Clone, Default)]
pub struct SeriesScan {
    /// Every partition file, oldest first.
    pub files: Vec<FileCoverage>,
    /// Union of the coverage of every file, per period.
    pub coverage: BTreeMap<Period, (i64, i64)>,
    /// Recorded history floor: the claim of the file holding the series' oldest
    /// bars, i.e. of `files[0]`.
    ///
    /// Files created by the collector carry that claim in their footer, so the
    /// knowledge "nothing older than this exists upstream" lives in the data and
    /// survives copying the directory to another machine. `None` means the series
    /// has no recorded floor — a directory written before floors were recorded, or
    /// a series never collected — and the collector then walks down to find it.
    pub floor: Option<i64>,
}

impl SeriesScan {
    /// Oldest bar stored for the series, if any.
    pub fn oldest(&self) -> Option<i64> {
        self.files.iter().map(|f| f.min).min()
    }

    /// Total number of stored bars.
    pub fn rows(&self) -> u64 {
        self.files.iter().map(|f| f.rows.max(0) as u64).sum()
    }
}

/// Facade over the data directory.
#[derive(Debug, Clone)]
pub struct Store {
    data_dir: PathBuf,
    layout: LayoutOptions,
    write: WriteOptions,
}

impl Store {
    /// Build a store rooted at `data_dir`.
    pub fn new(data_dir: impl Into<PathBuf>, layout: LayoutOptions, write: WriteOptions) -> Self {
        Store {
            data_dir: data_dir.into(),
            layout,
            write,
        }
    }

    /// Data root.
    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    /// Layout settings.
    pub fn layout(&self) -> &LayoutOptions {
        &self.layout
    }

    /// Parquet write settings.
    pub fn write_options(&self) -> &WriteOptions {
        &self.write
    }

    /// Effective partition rule for a timeframe.
    pub fn rule_for(&self, tf: Timeframe) -> PartitionRule {
        self.layout.rule_for(tf)
    }

    /// Partition containing `ts`.
    pub fn period_of(&self, ts: i64, tf: Timeframe) -> Period {
        Period::containing(ts, self.rule_for(tf))
    }

    /// Path of a partition file.
    pub fn path(&self, symbol: &str, tf: Timeframe, period: Period) -> PathBuf {
        self.layout.path(&self.data_dir, symbol, tf, period)
    }

    /// Path relative to the data root.
    pub fn relative_path(&self, symbol: &str, tf: Timeframe, period: Period) -> PathBuf {
        self.layout.relative_path(symbol, tf, period)
    }

    /// Directory holding every partition of a series.
    pub fn series_dir(&self, symbol: &str, tf: Timeframe) -> PathBuf {
        self.layout.series_dir(&self.data_dir, symbol, tf)
    }

    /// Merge fresh candles into the file of `period`, rewriting only on change.
    ///
    /// `floor` is recorded in the footer so the next run — on this machine or on
    /// any other — knows how deep the history goes without probing.
    pub fn merge(
        &self,
        symbol: &str,
        tf: Timeframe,
        period: Period,
        candles: &[Candle],
        floor: Option<i64>,
    ) -> Result<(PathBuf, MergeStats)> {
        let path = self.path(symbol, tf, period);
        let stats = parquet_store::merge_into_file(&path, candles, symbol, tf, &self.write, floor)?;
        Ok((path, stats))
    }

    /// Read a whole series (files, per-period coverage and the recorded floor) in
    /// one pass over the Parquet footers.
    pub fn scan(&self, symbol: &str, tf: Timeframe) -> Result<SeriesScan> {
        let dir = self.series_dir(symbol, tf);
        if !dir.exists() {
            return Ok(SeriesScan::default());
        }
        let mut paths = Vec::new();
        collect_parquet_files(&dir, &mut paths)?;
        paths.sort();

        let rule = self.rule_for(tf);
        let mut scan = SeriesScan::default();
        for path in paths {
            let Some(info) = parquet_store::file_info(&path)? else {
                tracing::warn!(path = %path.display(), "skipping empty parquet file");
                continue;
            };
            scan.coverage
                .entry(Period::containing(info.min, rule))
                .and_modify(|(lo, hi)| {
                    *lo = (*lo).min(info.min);
                    *hi = (*hi).max(info.max);
                })
                .or_insert((info.min, info.max));
            scan.files.push(FileCoverage {
                path,
                period: Period::containing(info.min, rule),
                min: info.min,
                max: info.max,
                rows: info.rows,
                floor: info.floor,
            });
        }
        scan.files.sort_by_key(|f| (f.min, f.max));
        scan.floor = scan.files.first().and_then(|f| f.floor);
        Ok(scan)
    }

    /// Every `.parquet` file of a series, ordered chronologically.
    pub fn scan_series(&self, symbol: &str, tf: Timeframe) -> Result<Vec<FileCoverage>> {
        Ok(self.scan(symbol, tf)?.files)
    }

    /// Union of the coverage of every file of a series, per period.
    ///
    /// Multiple files may fall into the same period if the partition rule was
    /// changed between runs, so ranges are merged rather than overwritten.
    pub fn coverage_by_period(
        &self,
        symbol: &str,
        tf: Timeframe,
    ) -> Result<BTreeMap<Period, (i64, i64)>> {
        Ok(self.scan(symbol, tf)?.coverage)
    }

    /// Rewrite the oldest file of a series so it records `floor`.
    ///
    /// The collector calls this when the floor it learned during the walk is not
    /// the one the files already carry — after the very first collection, when
    /// migrating a directory written before floors were recorded, or when the walk
    /// tightened a probe's day-accurate answer to the first bar that actually
    /// exists for this timeframe. Doing it once per series is what keeps every
    /// later run — on any machine — free of probing and of empty-window fetches;
    /// the bar data itself is untouched.
    pub fn stamp_floor(&self, symbol: &str, tf: Timeframe, floor: i64) -> Result<Option<PathBuf>> {
        let Some(oldest) = self.scan(symbol, tf)?.files.into_iter().next() else {
            return Ok(None);
        };
        if oldest.floor == Some(floor) {
            return Ok(None);
        }
        let candles = parquet_store::read_candles(&oldest.path)?;
        parquet_store::write_atomic(&oldest.path, &candles, symbol, tf, &self.write, Some(floor))?;
        Ok(Some(oldest.path))
    }

    /// Total number of bars stored for a series (footer reads only).
    pub fn total_rows(&self, symbol: &str, tf: Timeframe) -> Result<u64> {
        Ok(self.scan(symbol, tf)?.rows())
    }

    /// Symbols present on disk (directory names under `<exchange>/<market>`).
    pub fn stored_symbols(&self) -> Result<Vec<String>> {
        let root = self
            .data_dir
            .join(&self.layout.exchange)
            .join(&self.layout.market);
        let mut out = Vec::new();
        let entries = match fs::read_dir(&root) {
            Ok(e) => e,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
            Err(e) => return Err(Error::io(&root, e)),
        };
        for entry in entries {
            let entry = entry.map_err(|e| Error::io(&root, e))?;
            if entry.file_type().map_err(|e| Error::io(&root, e))?.is_dir() {
                if let Some(name) = entry.file_name().to_str() {
                    out.push(name.to_string());
                }
            }
        }
        out.sort();
        Ok(out)
    }

    /// Timeframes present on disk for a symbol.
    pub fn stored_timeframes(&self, symbol: &str) -> Result<Vec<Timeframe>> {
        let dir = self
            .data_dir
            .join(&self.layout.exchange)
            .join(&self.layout.market)
            .join(symbol);
        let mut out = Vec::new();
        let entries = match fs::read_dir(&dir) {
            Ok(e) => e,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
            Err(e) => return Err(Error::io(&dir, e)),
        };
        for entry in entries {
            let entry = entry.map_err(|e| Error::io(&dir, e))?;
            if !entry.file_type().map_err(|e| Error::io(&dir, e))?.is_dir() {
                continue;
            }
            if let Some(name) = entry.file_name().to_str() {
                if let Ok(tf) = name.parse::<Timeframe>() {
                    out.push(tf);
                }
            }
        }
        out.sort();
        out.dedup();
        Ok(out)
    }

    /// Every parquet file under the data root, relative paths included.
    pub fn all_files(&self) -> Result<Vec<(PathBuf, PathBuf)>> {
        let mut files: Vec<PathBuf> = Vec::new();
        if !self.data_dir.exists() {
            return Ok(Vec::new());
        }
        collect_parquet_files(&self.data_dir, &mut files)?;
        files.sort();
        let mut out = Vec::with_capacity(files.len());
        for abs in files {
            let rel = abs
                .strip_prefix(&self.data_dir)
                .map_err(|_| Error::Other(format!("{} escapes the data dir", abs.display())))?
                .to_path_buf();
            out.push((rel, abs));
        }
        Ok(out)
    }
}

/// Recursively collect `*.parquet` files (ignoring our `*.tmp` scratch files).
fn collect_parquet_files(dir: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(Error::io(dir, e)),
    };
    for entry in entries {
        let entry = entry.map_err(|e| Error::io(dir, e))?;
        let path = entry.path();
        let file_type = entry.file_type().map_err(|e| Error::io(&path, e))?;
        if file_type.is_dir() {
            collect_parquet_files(&path, out)?;
        } else if path.extension().map(|e| e == "parquet").unwrap_or(false) {
            out.push(path);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candle(time: i64) -> Candle {
        Candle {
            time,
            open: 1.0,
            high: 1.0,
            low: 1.0,
            close: 1.0,
            volume: 1.0,
            turnover: 1.0,
        }
    }

    fn store(dir: &Path) -> Store {
        Store::new(dir, LayoutOptions::default(), WriteOptions::default())
    }

    #[test]
    fn scan_series_reports_coverage_in_order() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let jan = s.period_of(1_704_067_200, Timeframe::H1); // 2024-01-01
        let feb = s.period_of(1_706_745_600, Timeframe::H1); // 2024-02-01
        assert_eq!(jan.slug(), "2024");
        assert_eq!(feb.slug(), "2024");

        let m = s.period_of(1_704_067_200, Timeframe::M1);
        s.merge(
            "BTC-USDT",
            Timeframe::M1,
            m,
            &[candle(1_704_067_200), candle(1_704_067_260)],
            None,
        )
        .unwrap();
        let m2 = s.period_of(1_706_745_600, Timeframe::M1);
        s.merge(
            "BTC-USDT",
            Timeframe::M1,
            m2,
            &[candle(1_706_745_600)],
            None,
        )
        .unwrap();

        let files = s.scan_series("BTC-USDT", Timeframe::M1).unwrap();
        assert_eq!(files.len(), 2);
        // January file comes first.
        assert!(files[0].min < files[1].min);
        assert_eq!(files[0].rows, 2);
        assert_eq!(files[0].period.slug(), "2024-01");
        assert_eq!(files[1].period.slug(), "2024-02");

        let coverage = s.coverage_by_period("BTC-USDT", Timeframe::M1).unwrap();
        assert_eq!(coverage.len(), 2);
        assert_eq!(s.stored_symbols().unwrap(), vec!["BTC-USDT".to_string()]);
        assert_eq!(
            s.stored_timeframes("BTC-USDT").unwrap(),
            vec![Timeframe::M1]
        );
    }

    #[test]
    fn total_rows_counts_stored_bars() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        assert_eq!(s.total_rows("BTC-USDT", Timeframe::M1).unwrap(), 0);
        let start = 1_704_067_200; // 2024-01-01
        for (offset, count) in [(0, 3), (31 * 86_400, 2)] {
            let candles: Vec<Candle> = (0..count)
                .map(|i| candle(start + offset + i * 60))
                .collect();
            let period = s.period_of(start + offset, Timeframe::M1);
            s.merge("BTC-USDT", Timeframe::M1, period, &candles, None)
                .unwrap();
        }
        assert_eq!(s.total_rows("BTC-USDT", Timeframe::M1).unwrap(), 5);
    }

    #[test]
    fn missing_directories_are_not_errors() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        assert!(s
            .scan_series("NOPE-USDT", Timeframe::M1)
            .unwrap()
            .is_empty());
        assert!(s.stored_symbols().unwrap().is_empty());
        assert!(s.stored_timeframes("NOPE-USDT").unwrap().is_empty());
        assert!(s.all_files().unwrap().is_empty());
    }

    #[test]
    fn scan_reports_coverage_floor_and_rows() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let start = 1_704_067_200; // 2024-01-01
        let listed = 1_500_000_000;
        for offset in [0, 31 * 86_400] {
            let period = s.period_of(start + offset, Timeframe::M1);
            s.merge(
                "BTC-USDT",
                Timeframe::M1,
                period,
                &[candle(start + offset)],
                Some(listed),
            )
            .unwrap();
        }

        let scan = s.scan("BTC-USDT", Timeframe::M1).unwrap();
        assert_eq!(scan.files.len(), 2);
        assert_eq!(scan.coverage.len(), 2);
        assert_eq!(scan.floor, Some(listed));
        assert_eq!(scan.oldest(), Some(start));
        assert_eq!(scan.rows(), 2);

        // A series with no recorded floor reports none, and does not invent one.
        let bare = tempfile::tempdir().unwrap();
        let s = store(bare.path());
        let period = s.period_of(start, Timeframe::M1);
        s.merge("ETH-USDT", Timeframe::M1, period, &[candle(start)], None)
            .unwrap();
        let scan = s.scan("ETH-USDT", Timeframe::M1).unwrap();
        assert_eq!(scan.floor, None);
        assert_eq!(scan.oldest(), Some(start));

        // Stamping records the floor without touching the bars.
        let path = s.stamp_floor("ETH-USDT", Timeframe::M1, listed).unwrap();
        assert!(path.is_some());
        let scan = s.scan("ETH-USDT", Timeframe::M1).unwrap();
        assert_eq!(scan.floor, Some(listed));
        assert_eq!(scan.rows(), 1);
        assert_eq!(scan.oldest(), Some(start));
        // Already recorded at least as deep: nothing to do.
        assert!(s
            .stamp_floor("ETH-USDT", Timeframe::M1, listed)
            .unwrap()
            .is_none());
    }

    #[test]
    fn all_files_lists_relative_paths() {
        let dir = tempfile::tempdir().unwrap();
        let s = store(dir.path());
        let p = s.period_of(1_704_067_200, Timeframe::M1);
        s.merge("ETH-USDT", Timeframe::M1, p, &[candle(1_704_067_200)], None)
            .unwrap();
        let files = s.all_files().unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(
            files[0].0,
            PathBuf::from("kucoin/spot/ETH-USDT/1m/2024/2024-01.parquet")
        );
        assert!(files[0].1.exists());
    }

    #[test]
    fn coverage_helper_detects_full_span() {
        let cover = FileCoverage {
            path: PathBuf::from("x.parquet"),
            period: Period::Single,
            min: 100,
            max: 200,
            rows: 2,
            floor: None,
        };
        assert!(cover.covers(100, 200));
        assert!(cover.covers(150, 150));
        assert!(!cover.covers(99, 200));
        assert!(!cover.covers(100, 201));
    }
}
