//! Parquet persistence: schema, atomic writes, merges and cheap metadata reads.
//!
//! Files are written with Zstd compression and chunk-level statistics, which is
//! what lets the collector answer "how far does this file go?" by reading only
//! the footer instead of the whole file.
//!
//! The footer also carries the series' *history floor* under
//! [`HISTORY_FLOOR_KEY`], so the dataset describes itself and no metadata has to
//! live outside `data_dir` (where it would not survive a move to another machine
//! or a round trip through a dataset repository).

use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow::array::{Array, ArrayRef, Float64Array, Int64Array, StringArray};
use arrow::datatypes::{DataType, Field, Schema, SchemaRef};
use arrow::record_batch::RecordBatch;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::{EnabledStatistics, WriterProperties};

use crate::error::{Error, Result};
use crate::kucoin::{Candle, Timeframe};

/// Column holding the bar open time (unix seconds, UTC).
pub const TIME_COL: &str = "time";
/// Column holding the base volume.
pub const VOLUME_COL: &str = "volume";
/// Column holding the quote volume.
pub const TURNOVER_COL: &str = "turnover";
/// Column holding the symbol (optional, off in some layouts).
pub const SYMBOL_COL: &str = "symbol";
/// Column holding the timeframe slug (optional).
pub const TIMEFRAME_COL: &str = "timeframe";

/// Footer key holding the oldest bar the exchange has for this series.
///
/// Value: unix seconds. The key is written into every file the collector
/// creates, which makes the floor a property of the *data* rather than of the
/// machine that collected it: a directory copied (or downloaded from a dataset
/// repository) onto a fresh host still knows how deep the history goes, and a
/// repeat run costs no request at all.
pub const HISTORY_FLOOR_KEY: &str = "kcs.history_start";

/// How files are written.
#[derive(Debug, Clone)]
pub struct WriteOptions {
    /// Zstd compression level (`1` fast … `19` small).
    pub zstd_level: i32,
    /// Maximum rows per Parquet row group.
    pub max_row_group_rows: usize,
    /// Store the quote volume column.
    pub include_turnover: bool,
    /// Store redundant `symbol` / `timeframe` columns (dictionary encoded, so
    /// almost free) to make single-file reads self-describing.
    pub include_metadata_columns: bool,
}

impl Default for WriteOptions {
    fn default() -> Self {
        WriteOptions {
            zstd_level: 3,
            max_row_group_rows: 65_536,
            include_turnover: true,
            include_metadata_columns: true,
        }
    }
}

/// Result of merging fresh candles into a partition file.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct MergeStats {
    /// Rows present before the merge.
    pub rows_before: u64,
    /// Rows present after the merge.
    pub rows_after: u64,
    /// Rows that only exist in the new batch.
    pub rows_added: u64,
    /// Rows whose timestamp already existed (value refreshed).
    pub rows_refreshed: u64,
    /// Whether the file content actually changed (and was therefore rewritten).
    pub changed: bool,
}

impl MergeStats {
    /// Net new bars.
    pub fn net_new(&self) -> i64 {
        self.rows_after as i64 - self.rows_before as i64
    }
}

/// Arrow schema used for every kline file.
pub fn schema(opts: &WriteOptions) -> SchemaRef {
    let mut fields = vec![
        Field::new(TIME_COL, DataType::Int64, false),
        Field::new("open", DataType::Float64, false),
        Field::new("high", DataType::Float64, false),
        Field::new("low", DataType::Float64, false),
        Field::new("close", DataType::Float64, false),
        Field::new(VOLUME_COL, DataType::Float64, false),
    ];
    if opts.include_turnover {
        fields.push(Field::new(TURNOVER_COL, DataType::Float64, false));
    }
    if opts.include_metadata_columns {
        fields.push(Field::new(SYMBOL_COL, DataType::Utf8, false));
        fields.push(Field::new(TIMEFRAME_COL, DataType::Utf8, false));
    }
    Arc::new(Schema::new(fields))
}

fn writer_properties(opts: &WriteOptions, floor: Option<i64>) -> Result<WriterProperties> {
    let level = ZstdLevel::try_new(opts.zstd_level)?;
    let mut builder = WriterProperties::builder()
        .set_compression(Compression::ZSTD(level))
        .set_max_row_group_row_count(Some(opts.max_row_group_rows.max(1)))
        .set_statistics_enabled(EnabledStatistics::Chunk)
        .set_created_by(format!("kcs-klines/{}", env!("CARGO_PKG_VERSION")));
    if let Some(floor) = floor {
        builder = builder.set_key_value_metadata(Some(vec![KeyValue::new(
            HISTORY_FLOOR_KEY.to_string(),
            floor.to_string(),
        )]));
    }
    Ok(builder.build())
}

/// Build a `RecordBatch` for `candles` (which must be sorted ascending).
fn to_batch(
    candles: &[Candle],
    schema: SchemaRef,
    opts: &WriteOptions,
    symbol: &str,
    tf: Timeframe,
) -> Result<RecordBatch> {
    let times: Vec<i64> = candles.iter().map(|c| c.time).collect();
    let mut columns: Vec<ArrayRef> = vec![
        Arc::new(Int64Array::from(times)),
        Arc::new(Float64Array::from(
            candles.iter().map(|c| c.open).collect::<Vec<_>>(),
        )),
        Arc::new(Float64Array::from(
            candles.iter().map(|c| c.high).collect::<Vec<_>>(),
        )),
        Arc::new(Float64Array::from(
            candles.iter().map(|c| c.low).collect::<Vec<_>>(),
        )),
        Arc::new(Float64Array::from(
            candles.iter().map(|c| c.close).collect::<Vec<_>>(),
        )),
        Arc::new(Float64Array::from(
            candles.iter().map(|c| c.volume).collect::<Vec<_>>(),
        )),
    ];
    if opts.include_turnover {
        columns.push(Arc::new(Float64Array::from(
            candles.iter().map(|c| c.turnover).collect::<Vec<_>>(),
        )));
    }
    if opts.include_metadata_columns {
        columns.push(Arc::new(StringArray::from(vec![symbol; candles.len()])));
        columns.push(Arc::new(StringArray::from(vec![tf.slug(); candles.len()])));
    }
    RecordBatch::try_new(schema, columns).map_err(Error::from)
}

/// Write `candles` to `path`, replacing any existing file.
///
/// The write goes to a sibling `.tmp` file that is fsynced and then renamed, so
/// a crash can never leave a half-written Parquet file behind.
///
/// `floor` is recorded in the footer as [`HISTORY_FLOOR_KEY`] when set (see
/// [`merge_into_file`] for how a series combines the values of its files).
pub fn write_atomic(
    path: &Path,
    candles: &[Candle],
    symbol: &str,
    tf: Timeframe,
    opts: &WriteOptions,
    floor: Option<i64>,
) -> Result<()> {
    ensure_parent_dir(path)?;
    let schema = schema(opts);
    let tmp = tmp_path(path);

    {
        let file = File::create(&tmp).map_err(|e| Error::io(&tmp, e))?;
        let props = writer_properties(opts, floor)?;
        let mut writer = ArrowWriter::try_new(file, schema.clone(), Some(props))?;
        let chunk = opts.max_row_group_rows.max(1);
        for block in candles.chunks(chunk) {
            let batch = to_batch(block, schema.clone(), opts, symbol, tf)?;
            writer.write(&batch)?;
        }
        writer.close()?;
    }

    // Durability: flush the data before the rename makes it visible.
    if let Ok(f) = File::open(&tmp) {
        let _ = f.sync_all();
    }
    fs::rename(&tmp, path).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        Error::io(path, e)
    })?;
    sync_dir(path);
    Ok(())
}

fn tmp_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "out.parquet".to_string());
    path.with_file_name(format!("{name}.{}.tmp", std::process::id()))
}

fn ensure_parent_dir(path: &Path) -> Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| Error::io(parent, e))?;
        }
    }
    Ok(())
}

/// Best-effort directory fsync so the rename survives a power loss.
fn sync_dir(path: &Path) {
    if let Some(parent) = path.parent() {
        if let Ok(dir) = File::open(parent) {
            let _ = dir.sync_all();
        }
    }
}

/// Read every candle from a Parquet file, sorted ascending by time.
///
/// Unknown extra columns are ignored and a missing `turnover` column is read as
/// `0.0`, so files written by an older version stay readable.
pub fn read_candles(path: &Path) -> Result<Vec<Candle>> {
    let file = File::open(path).map_err(|e| Error::io(path, e))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.schema().clone();
    let idx = ColumnIndex::from_schema(&schema).ok_or_else(|| {
        Error::Data(format!(
            "{}: schema has no `{TIME_COL}` column",
            path.display()
        ))
    })?;
    let reader = builder.build()?;

    let mut out: Vec<Candle> = Vec::new();
    for batch in reader {
        let batch = batch?;
        let times = downcast::<Int64Array>(&batch, idx.time, path, TIME_COL)?;
        let open = downcast::<Float64Array>(&batch, idx.open, path, "open")?;
        let high = downcast::<Float64Array>(&batch, idx.high, path, "high")?;
        let low = downcast::<Float64Array>(&batch, idx.low, path, "low")?;
        let close = downcast::<Float64Array>(&batch, idx.close, path, "close")?;
        let volume = downcast::<Float64Array>(&batch, idx.volume, path, VOLUME_COL)?;
        let turnover = idx
            .turnover
            .map(|i| downcast::<Float64Array>(&batch, i, path, TURNOVER_COL))
            .transpose()?;
        out.reserve(batch.num_rows());
        for row in 0..batch.num_rows() {
            out.push(Candle {
                time: times.value(row),
                open: open.value(row),
                high: high.value(row),
                low: low.value(row),
                close: close.value(row),
                volume: volume.value(row),
                turnover: turnover.map(|t| t.value(row)).unwrap_or(0.0),
            });
        }
    }
    out.sort_unstable_by_key(|c| c.time);
    out.dedup_by_key(|c| c.time);
    Ok(out)
}

struct ColumnIndex {
    time: usize,
    open: usize,
    high: usize,
    low: usize,
    close: usize,
    volume: usize,
    turnover: Option<usize>,
}

impl ColumnIndex {
    fn from_schema(schema: &SchemaRef) -> Option<Self> {
        let find = |name: &str| schema.index_of(name).ok();
        Some(ColumnIndex {
            time: find(TIME_COL)?,
            open: find("open")?,
            high: find("high")?,
            low: find("low")?,
            close: find("close")?,
            volume: find(VOLUME_COL)?,
            turnover: find(TURNOVER_COL),
        })
    }
}

fn downcast<'a, T: 'static>(
    batch: &'a RecordBatch,
    index: usize,
    path: &Path,
    name: &str,
) -> Result<&'a T> {
    batch
        .column(index)
        .as_any()
        .downcast_ref::<T>()
        .ok_or_else(|| {
            Error::Data(format!(
                "{}: column `{name}` has unexpected type {:?}",
                path.display(),
                batch.column(index).data_type()
            ))
        })
}

/// Everything the collector needs to know about one file, from the footer only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileInfo {
    /// Row count.
    pub rows: i64,
    /// Oldest bar in the file.
    pub min: i64,
    /// Newest bar in the file.
    pub max: i64,
    /// History floor recorded when the file was written, when it carries one.
    pub floor: Option<i64>,
}

/// Read a file's [`FileInfo`] from its footer.
///
/// Chunk statistics make this a single footer read. The rare file written
/// without statistics falls back to a full scan; `None` means the file holds no
/// rows at all.
pub fn file_info(path: &Path) -> Result<Option<FileInfo>> {
    let file = File::open(path).map_err(|e| Error::io(path, e))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let meta = builder.metadata().clone();
    let rows = meta.file_metadata().num_rows();
    let floor = floor_from(&meta);

    let (min, max) = match stats_range(&meta) {
        Some(range) => range,
        None => {
            let candles = read_candles(path)?;
            match (candles.first(), candles.last()) {
                (Some(first), Some(last)) => (first.time, last.time),
                _ => return Ok(None),
            }
        }
    };
    Ok(Some(FileInfo {
        rows,
        min,
        max,
        floor,
    }))
}

/// `(min_time, max_time)` from the row-group statistics, if they are usable.
fn stats_range(meta: &parquet::file::metadata::ParquetMetaData) -> Option<(i64, i64)> {
    let mut min: Option<i64> = None;
    let mut max: Option<i64> = None;
    for rg in 0..meta.num_row_groups() {
        let row_group = meta.row_group(rg);
        for ci in 0..row_group.num_columns() {
            let column = row_group.column(ci);
            if column.column_descr().name() != TIME_COL {
                continue;
            }
            if let Some(stats) = column.statistics() {
                if let Some(v) = stats.min_bytes_opt().and_then(i64_from_le_bytes) {
                    min = Some(min.map_or(v, |m: i64| m.min(v)));
                }
                if let Some(v) = stats.max_bytes_opt().and_then(i64_from_le_bytes) {
                    max = Some(max.map_or(v, |m: i64| m.max(v)));
                }
            }
        }
    }
    match (min, max) {
        (Some(lo), Some(hi)) => Some((lo, hi)),
        _ => None,
    }
}

/// The history floor recorded in a footer, if the file carries one.
fn floor_from(meta: &parquet::file::metadata::ParquetMetaData) -> Option<i64> {
    meta.file_metadata()
        .key_value_metadata()?
        .iter()
        .find(|kv| kv.key == HISTORY_FLOOR_KEY)
        .and_then(|kv| kv.value.as_deref())
        .and_then(|v| v.parse::<i64>().ok())
}

fn i64_from_le_bytes(bytes: &[u8]) -> Option<i64> {
    if bytes.len() < 8 {
        return None;
    }
    let mut buf = [0u8; 8];
    buf.copy_from_slice(&bytes[..8]);
    Some(i64::from_le_bytes(buf))
}

/// Merge `new_candles` into the partition file at `path`.
///
/// The file is rewritten **only** when the content actually changes, which keeps
/// mtimes (and therefore any downstream file-sync tooling) stable. On timestamp
/// collisions the fresh candle wins, which is how a still-forming last bar gets
/// corrected on the next run.
///
/// The recorded history floor is combined with the one already in the file by
/// taking the older value: the floor only ever moves *down* as we learn more, and
/// a rewrite must never lose what an earlier run established.
pub fn merge_into_file(
    path: &Path,
    new_candles: &[Candle],
    symbol: &str,
    tf: Timeframe,
    opts: &WriteOptions,
    floor: Option<i64>,
) -> Result<MergeStats> {
    let existing = if path.exists() {
        read_candles(path)?
    } else {
        Vec::new()
    };
    let rows_before = existing.len() as u64;

    let mut fresh: Vec<Candle> = new_candles.to_vec();
    // Stable sort + dedup keeps the first occurrence of a repeated timestamp,
    // so the outcome does not depend on sort internals.
    fresh.sort_by_key(|c| c.time);
    fresh.dedup_by_key(|c| c.time);

    if fresh.is_empty() && rows_before == 0 {
        return Ok(MergeStats::default());
    }

    let added = count_new(&existing, &fresh);
    let merged = merge_sorted(&existing, &fresh);
    let rows_after = merged.len() as u64;
    let refreshed = fresh.len() as u64 - added;

    if merged == existing {
        return Ok(MergeStats {
            rows_before,
            rows_after,
            rows_added: 0,
            rows_refreshed: refreshed,
            changed: false,
        });
    }

    let recorded = match (existing_floor(path)?, floor) {
        (Some(old), Some(new)) => Some(old.min(new)),
        (old, new) => old.or(new),
    };
    write_atomic(path, &merged, symbol, tf, opts, recorded)?;
    Ok(MergeStats {
        rows_before,
        rows_after,
        rows_added: added,
        rows_refreshed: refreshed,
        changed: true,
    })
}

/// The history floor already recorded in `path`, if the file exists.
fn existing_floor(path: &Path) -> Result<Option<i64>> {
    if !path.exists() {
        return Ok(None);
    }
    Ok(file_info(path)?.and_then(|info| info.floor))
}

/// Count how many of `fresh` are not present in `existing` (both sorted).
fn count_new(existing: &[Candle], fresh: &[Candle]) -> u64 {
    let mut i = 0usize;
    let mut added = 0u64;
    for c in fresh {
        while i < existing.len() && existing[i].time < c.time {
            i += 1;
        }
        if i >= existing.len() || existing[i].time != c.time {
            added += 1;
        }
    }
    added
}

/// Merge two ascending, de-duplicated candle slices; `fresh` wins on ties.
pub fn merge_sorted(existing: &[Candle], fresh: &[Candle]) -> Vec<Candle> {
    let mut out = Vec::with_capacity(existing.len() + fresh.len());
    let (mut i, mut j) = (0usize, 0usize);
    while i < existing.len() && j < fresh.len() {
        match existing[i].time.cmp(&fresh[j].time) {
            std::cmp::Ordering::Less => {
                out.push(existing[i]);
                i += 1;
            }
            std::cmp::Ordering::Greater => {
                out.push(fresh[j]);
                j += 1;
            }
            std::cmp::Ordering::Equal => {
                out.push(fresh[j]);
                i += 1;
                j += 1;
            }
        }
    }
    out.extend_from_slice(&existing[i..]);
    out.extend_from_slice(&fresh[j..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 2020-01-01T00:00:00Z.
    const DAY0: i64 = 1_577_836_800;

    fn candle(time: i64, close: f64) -> Candle {
        Candle {
            time,
            open: close,
            high: close,
            low: close,
            close,
            volume: 1.0,
            turnover: close,
        }
    }

    #[test]
    fn merge_prefers_fresh_on_collision() {
        let old = vec![candle(60, 1.0), candle(120, 2.0)];
        let new = vec![candle(120, 9.0), candle(180, 3.0)];
        let merged = merge_sorted(&old, &new);
        assert_eq!(merged.len(), 3);
        assert_eq!(merged[1].close, 9.0, "fresh candle must win");
        assert_eq!(merged[2].time, 180);
    }

    #[test]
    fn merge_handles_empty_sides() {
        let a = vec![candle(60, 1.0)];
        assert_eq!(merge_sorted(&[], &a), a);
        assert_eq!(merge_sorted(&a, &[]), a);
        assert!(merge_sorted(&[], &[]).is_empty());
    }

    #[test]
    fn count_new_counts_only_missing() {
        let old = vec![candle(60, 1.0), candle(120, 2.0)];
        let fresh = vec![candle(120, 2.0), candle(180, 3.0), candle(240, 4.0)];
        assert_eq!(count_new(&old, &fresh), 2);
        assert_eq!(count_new(&[], &fresh), 3);
        assert_eq!(count_new(&old, &[]), 0);
    }

    #[test]
    fn roundtrip_preserves_data_and_schema() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("nested/2024-01.parquet");
        let opts = WriteOptions::default();
        let candles: Vec<Candle> = (0..5_000)
            .map(|i| candle(1_700_000_000 + i * 60, 100.0 + i as f64))
            .collect();
        write_atomic(&path, &candles, "BTC-USDT", Timeframe::M1, &opts, None).unwrap();
        assert!(path.exists());

        let read = read_candles(&path).unwrap();
        assert_eq!(read.len(), candles.len());
        assert_eq!(read[0], candles[0]);
        assert_eq!(read[4_999], candles[4_999]);

        assert_eq!(
            file_info(&path).unwrap(),
            Some(FileInfo {
                rows: 5_000,
                min: candles[0].time,
                max: candles[4_999].time,
                floor: None,
            })
        );
        // No temporary files left behind.
        let leftovers: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".tmp"))
            .collect();
        assert!(leftovers.is_empty(), "found {leftovers:?}");
    }

    #[test]
    fn merge_into_file_is_idempotent_and_detects_changes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("2024-01.parquet");
        let opts = WriteOptions::default();
        let first = vec![candle(60, 1.0), candle(120, 2.0)];
        let stats = merge_into_file(&path, &first, "BTC-USDT", Timeframe::M1, &opts, None).unwrap();
        assert!(stats.changed);
        assert_eq!(stats.rows_after, 2);
        assert_eq!(stats.rows_added, 2);

        // Same data again: nothing changes, file is not rewritten.
        let mtime = std::fs::metadata(&path).unwrap().modified().unwrap();
        let stats = merge_into_file(&path, &first, "BTC-USDT", Timeframe::M1, &opts, None).unwrap();
        assert!(!stats.changed);
        assert_eq!(stats.rows_added, 0);
        assert_eq!(stats.rows_refreshed, 2);
        assert_eq!(std::fs::metadata(&path).unwrap().modified().unwrap(), mtime);

        // A corrected last bar changes the file without adding rows.
        let fixed = vec![candle(120, 7.0)];
        let stats = merge_into_file(&path, &fixed, "BTC-USDT", Timeframe::M1, &opts, None).unwrap();
        assert!(stats.changed);
        assert_eq!(stats.rows_added, 0);
        assert_eq!(stats.rows_after, 2);
        assert_eq!(read_candles(&path).unwrap()[1].close, 7.0);
    }

    #[test]
    fn the_history_floor_travels_in_the_footer() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("2024-01.parquet");
        let opts = WriteOptions::default();
        let candles = vec![candle(1_704_067_200, 1.0), candle(1_704_067_260, 2.0)];
        write_atomic(
            &path,
            &candles,
            "BTC-USDT",
            Timeframe::M1,
            &opts,
            Some(DAY0),
        )
        .unwrap();

        // Read back through the footer, exactly as the collector does when the
        // directory came from somewhere else (another machine, a dataset repo).
        let info = file_info(&path).unwrap().unwrap();
        assert_eq!(info.floor, Some(DAY0));
        assert_eq!(info.rows, 2);
        // Arrow keeps its own footer entry alongside ours.
        assert!(read_candles(&path).unwrap().len() == 2);

        // Writing without a floor records nothing, and the file stays readable.
        let plain = dir.path().join("plain.parquet");
        write_atomic(&plain, &candles, "BTC-USDT", Timeframe::M1, &opts, None).unwrap();
        assert_eq!(file_info(&plain).unwrap().unwrap().floor, None);
    }

    #[test]
    fn a_rewrite_never_loses_an_older_recorded_floor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("2024-01.parquet");
        let opts = WriteOptions::default();
        let listed = 1_500_000_000; // the real start of history
        let later = 1_704_067_200; // what a `--start` run happens to know

        // First run knows only a late floor…
        merge_into_file(
            &path,
            &[candle(1_704_067_200, 1.0)],
            "BTC-USDT",
            Timeframe::M1,
            &opts,
            Some(later),
        )
        .unwrap();
        assert_eq!(file_info(&path).unwrap().unwrap().floor, Some(later));

        // …and a later run that learned the real listing date must move it down.
        merge_into_file(
            &path,
            &[candle(1_704_067_320, 2.0)],
            "BTC-USDT",
            Timeframe::M1,
            &opts,
            Some(listed),
        )
        .unwrap();
        assert_eq!(file_info(&path).unwrap().unwrap().floor, Some(listed));

        // A run that knows only the late floor again must not raise it back.
        merge_into_file(
            &path,
            &[candle(1_704_067_380, 3.0)],
            "BTC-USDT",
            Timeframe::M1,
            &opts,
            Some(later),
        )
        .unwrap();
        assert_eq!(file_info(&path).unwrap().unwrap().floor, Some(listed));
    }

    #[test]
    fn reads_files_without_optional_columns() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("minimal.parquet");
        let opts = WriteOptions {
            include_turnover: false,
            include_metadata_columns: false,
            ..Default::default()
        };
        let candles = vec![candle(60, 1.5), candle(120, 2.5)];
        write_atomic(&path, &candles, "ETH-USDT", Timeframe::M1, &opts, None).unwrap();
        let read = read_candles(&path).unwrap();
        assert_eq!(read.len(), 2);
        assert_eq!(read[0].turnover, 0.0, "missing column reads as zero");
    }

    #[test]
    fn schema_has_expected_columns() {
        let opts = WriteOptions::default();
        let s = schema(&opts);
        for name in [
            TIME_COL,
            "open",
            "high",
            "low",
            "close",
            VOLUME_COL,
            TURNOVER_COL,
        ] {
            assert!(s.index_of(name).is_ok(), "missing {name}");
        }
        assert_eq!(
            s.field(s.index_of(TIME_COL).unwrap()).data_type(),
            &DataType::Int64
        );
        let s = schema(&WriteOptions {
            include_turnover: false,
            include_metadata_columns: false,
            ..Default::default()
        });
        assert_eq!(s.fields().len(), 6);
    }
}
