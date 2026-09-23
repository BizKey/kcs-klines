//! Per-series state persisted between runs.
//!
//! The state file is what turns "scan backwards until KuCoin stops answering"
//! into a one-off cost: the discovered start of history is cached, so later
//! updates start from the newest local bar instead of re-walking the decade.
//!
//! State lives **outside** the data directory on purpose, so it is never
//! uploaded to the dataset repository.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};
use crate::kucoin::Timeframe;

/// The one fact about a series that the Parquet files cannot tell us.
///
/// Everything else about what is stored — bar counts, ranges, coverage per
/// partition — is read from the Parquet footers, so it is deliberately not
/// duplicated here. What files cannot express is *"nothing older than this
/// exists upstream"*, which is a measurement made by probing the exchange
/// (~4 requests). Caching it is the difference between a repeat run costing
/// nothing and costing those probes again, every time, for every series.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct SeriesState {
    /// Oldest bar this pair has upstream, as verified by a probe.
    ///
    /// The collector never trusts it over the data: if the files hold bars older
    /// than this, the earlier value wins. A stale cache can therefore only ever
    /// make the scan look *deeper*, never hide data.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub history_start: Option<i64>,
}

/// Reads and writes [`SeriesState`] files.
#[derive(Debug, Clone)]
pub struct StateStore {
    dir: PathBuf,
}

impl StateStore {
    /// Create a state store rooted at `dir` (created lazily on first write).
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        StateStore { dir: dir.into() }
    }

    /// State directory.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// File backing one series.
    pub fn path(&self, symbol: &str, tf: Timeframe) -> PathBuf {
        self.dir
            .join(format!("{}_{}.json", sanitize(symbol), tf.slug()))
    }

    /// Load state, returning defaults when nothing was written yet.
    ///
    /// A corrupt state file is reported as a warning rather than an error: the
    /// collector can always rebuild it from the Parquet files plus one scan.
    pub fn load(&self, symbol: &str, tf: Timeframe) -> Result<SeriesState> {
        let path = self.path(symbol, tf);
        if !path.exists() {
            return Ok(SeriesState::default());
        }
        let text = std::fs::read_to_string(&path).map_err(|e| Error::io(&path, e))?;
        match serde_json::from_str::<SeriesState>(&text) {
            Ok(s) => Ok(s),
            Err(e) => {
                tracing::warn!(
                    path = %path.display(),
                    error = %e,
                    "ignoring corrupt state file"
                );
                Ok(SeriesState::default())
            }
        }
    }

    /// Persist state atomically.
    pub fn save(&self, symbol: &str, tf: Timeframe, state: &SeriesState) -> Result<()> {
        let path = self.path(symbol, tf);
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| Error::io(parent, e))?;
        }
        let body = serde_json::to_string_pretty(state)?;
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, body).map_err(|e| Error::io(&tmp, e))?;
        std::fs::rename(&tmp, &path).map_err(|e| Error::io(&path, e))?;
        Ok(())
    }
}

fn sanitize(symbol: &str) -> String {
    symbol
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_and_defaults() {
        let dir = tempfile::tempdir().unwrap();
        let store = StateStore::new(dir.path());
        assert_eq!(
            store.load("BTC-USDT", Timeframe::M1).unwrap(),
            SeriesState::default()
        );

        let state = SeriesState {
            history_start: Some(1_500_000_000),
        };
        store.save("BTC-USDT", Timeframe::M1, &state).unwrap();
        assert_eq!(store.load("BTC-USDT", Timeframe::M1).unwrap(), state);
        // No temp file left behind.
        let leftovers: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty());
    }

    #[test]
    fn corrupt_state_is_ignored() {
        let dir = tempfile::tempdir().unwrap();
        let store = StateStore::new(dir.path());
        std::fs::write(store.path("ETH-USDT", Timeframe::H1), "{not json").unwrap();
        assert_eq!(
            store.load("ETH-USDT", Timeframe::H1).unwrap(),
            SeriesState::default()
        );
    }

    #[test]
    fn paths_are_sanitised_and_unique_per_timeframe() {
        let dir = tempfile::tempdir().unwrap();
        let store = StateStore::new(dir.path());
        assert_eq!(
            store.path("BTC-USDT", Timeframe::M1).file_name().unwrap(),
            "BTC-USDT_1m.json"
        );
        assert_eq!(
            store.path("weird/pair", Timeframe::H1).file_name().unwrap(),
            "weird_pair_1h.json"
        );
        assert_ne!(
            store.path("BTC-USDT", Timeframe::M1),
            store.path("BTC-USDT", Timeframe::H1)
        );
    }
}
