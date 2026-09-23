//! TOML configuration plus environment overrides.
//!
//! Everything has a sensible default, so `kcs-klines backfill --symbol BTC-USDT`
//! works without any config file at all.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};
use crate::kucoin::{KucoinConfig, RetryPolicy, Timeframe};
use crate::storage::{LayoutOptions, PartitionRule, WriteOptions};
use crate::util::parse_start_spec;

/// Default config file name looked up in the current directory.
pub const DEFAULT_CONFIG_NAME: &str = "kcs-klines.toml";

/// Top-level configuration.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Paths, timeouts, concurrency.
    #[serde(default)]
    pub general: GeneralConfig,
    /// KuCoin public rate pool shape.
    #[serde(default)]
    pub rate_limit: RateLimitConfig,
    /// What to collect.
    #[serde(default)]
    pub collection: CollectionConfig,
    /// How files are laid out and written.
    #[serde(default)]
    pub storage: StorageConfig,
    /// Optional liveness ping.
    #[serde(default)]
    pub notifications: NotificationsConfig,
    /// Logging.
    #[serde(default)]
    pub logging: LoggingConfig,
}

/// General runtime settings.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct GeneralConfig {
    /// Where Parquet files live.
    pub data_dir: PathBuf,
    /// Where the cached history floor of each series lives.
    ///
    /// One number per series: the oldest bar the exchange has. Everything else is
    /// read from the Parquet files, and the cache is dropped if the files contradict
    /// it.
    pub state_dir: PathBuf,
    /// KuCoin REST base URL.
    pub base_url: String,
    /// Per-request HTTP timeout in seconds.
    pub request_timeout_secs: u64,
    /// How many (symbol, timeframe) pairs are collected in parallel.
    pub concurrency: usize,
    /// Log verbosity: `error`, `warn`, `info`, `debug`, `trace`.
    pub log_level: String,
}

impl Default for GeneralConfig {
    fn default() -> Self {
        GeneralConfig {
            data_dir: PathBuf::from("./data"),
            state_dir: PathBuf::from("./state"),
            base_url: "https://api.kucoin.com".to_string(),
            request_timeout_secs: 30,
            concurrency: 4,
            log_level: "info".to_string(),
        }
    }
}

/// KuCoin public rate pool shape.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct RateLimitConfig {
    /// Weight available per window (VIP0 public pool: 2000).
    pub weight_limit: f64,
    /// Window length in seconds.
    pub window_secs: u64,
    /// Minimum spacing between two HTTP requests, in milliseconds.
    pub min_interval_ms: u64,
    /// Retries for transient failures (429/5xx/network).
    pub max_retries: u32,
    /// Base delay for exponential backoff, in milliseconds.
    pub base_backoff_ms: u64,
    /// Upper bound for a single backoff delay, in seconds.
    pub max_backoff_secs: u64,
}

impl Default for RateLimitConfig {
    fn default() -> Self {
        RateLimitConfig {
            weight_limit: 2000.0,
            window_secs: 30,
            min_interval_ms: 50,
            max_retries: 6,
            base_backoff_ms: 500,
            max_backoff_secs: 30,
        }
    }
}

/// What to collect.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct CollectionConfig {
    /// Explicit symbol list. When empty, every pair listed by /api/v2/symbols is
    /// collected, with no filtering.
    pub symbols: Vec<String>,
    /// Timeframes to collect.
    pub timeframes: Vec<Timeframe>,
    /// History start: `listing`, unix seconds, `YYYY-MM-DD` or RFC 3339.
    pub start: String,
    /// Bars re-read at the resume boundary.
    ///
    /// A still-forming bar is never stored, so there is nothing to correct; this only
    /// helps when the exchange retroactively revises a recently closed bar. `1`
    /// continues right after the last stored bar.
    pub overlap_candles: i64,
    /// How many consecutive empty windows end the backwards history scan.
    pub max_empty_windows: u32,
    /// Abort the run on a candle that violates OHLC invariants.
    ///
    /// Off by default: such bars are stored verbatim (KuCoin really serves a few,
    /// e.g. BTC-USDT 1h on 2017-11-29 with `high < low`) and counted in the run
    /// report, because dropping them would leave holes that do not exist upstream.
    pub strict_validation: bool,
}

impl Default for CollectionConfig {
    fn default() -> Self {
        CollectionConfig {
            symbols: Vec::new(),
            timeframes: vec![Timeframe::H1, Timeframe::D1],
            start: "listing".to_string(),
            overlap_candles: 1,
            max_empty_windows: 3,
            strict_validation: false,
        }
    }
}

impl CollectionConfig {
    /// Parsed [`CollectionConfig::start`].
    pub fn start_spec(&self) -> Result<Option<i64>> {
        parse_start_spec(&self.start)
    }
}

/// How files are laid out and written.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct StorageConfig {
    /// Global partition rule: `auto`, `month`, `year` or `single`.
    pub partition: PartitionRule,
    /// Per-timeframe overrides, e.g. `{ "1h" = "month" }`.
    #[serde(default)]
    pub partition_overrides: BTreeMap<String, PartitionRule>,
    /// `zstd`, `snappy` or `uncompressed` (only `zstd` is implemented).
    pub compression: String,
    /// Zstd level.
    pub zstd_level: i32,
    /// Maximum rows per Parquet row group.
    pub max_row_group_rows: usize,
    /// Store the quote-volume column.
    pub include_turnover: bool,
    /// Store `symbol` / `timeframe` columns for self-describing files.
    pub include_metadata_columns: bool,
}

impl Default for StorageConfig {
    fn default() -> Self {
        StorageConfig {
            partition: PartitionRule::Auto,
            partition_overrides: BTreeMap::new(),
            compression: "zstd".to_string(),
            zstd_level: 3,
            max_row_group_rows: 65_536,
            include_turnover: true,
            include_metadata_columns: true,
        }
    }
}

impl StorageConfig {
    /// Build layout options, validating the `partition_overrides` keys.
    pub fn layout_options(&self) -> Result<LayoutOptions> {
        let mut overrides = Vec::with_capacity(self.partition_overrides.len());
        for (key, rule) in &self.partition_overrides {
            let tf: Timeframe = key
                .parse()
                .map_err(|e| Error::Config(format!("storage.partition_overrides: {e}")))?;
            overrides.push((tf, *rule));
        }
        Ok(LayoutOptions {
            exchange: "kucoin".to_string(),
            market: "spot".to_string(),
            partition: self.partition,
            partition_overrides: overrides,
        })
    }

    /// Build Parquet write options.
    pub fn write_options(&self) -> Result<WriteOptions> {
        if !self.compression.eq_ignore_ascii_case("zstd") {
            return Err(Error::Config(format!(
                "storage.compression = `{}` is not supported; only `zstd` is implemented",
                self.compression
            )));
        }
        if !(-7..=22).contains(&self.zstd_level) {
            return Err(Error::Config(format!(
                "storage.zstd_level = {} is out of range (-7..=22)",
                self.zstd_level
            )));
        }
        if self.max_row_group_rows == 0 {
            return Err(Error::Config(
                "storage.max_row_group_rows must be >= 1".into(),
            ));
        }
        Ok(WriteOptions {
            zstd_level: self.zstd_level,
            max_row_group_rows: self.max_row_group_rows,
            include_turnover: self.include_turnover,
            include_metadata_columns: self.include_metadata_columns,
        })
    }
}

/// Optional healthcheck ping (healthchecks.io and friends).
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NotificationsConfig {
    /// Base URL pinged on start, success and failure.
    pub healthcheck_url: String,
}

/// Logging settings.
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LoggingConfig {
    /// Optional log file; logs are appended with daily rotation.
    pub file: Option<PathBuf>,
    /// Log file verbosity (defaults to `general.log_level`).
    pub file_level: Option<String>,
}

impl Config {
    /// Load a config file, or return defaults when it does not exist.
    pub fn load_or_default(path: Option<&Path>) -> Result<(Config, Option<PathBuf>)> {
        let explicit = path.is_some();
        let path = match path {
            Some(p) => p.to_path_buf(),
            None => {
                let candidate = PathBuf::from(DEFAULT_CONFIG_NAME);
                if !candidate.exists() {
                    return Ok((Config::default(), None));
                }
                candidate
            }
        };
        if !path.exists() {
            if explicit {
                return Err(Error::Config(format!(
                    "config file {} does not exist",
                    path.display()
                )));
            }
            return Ok((Config::default(), None));
        }
        let text = std::fs::read_to_string(&path).map_err(|e| Error::io(&path, e))?;
        let cfg: Config = toml::from_str(&text)?;
        cfg.validate()?;
        Ok((cfg, Some(path)))
    }

    /// Semantic validation (beyond TOML syntax).
    pub fn validate(&self) -> Result<()> {
        if self.general.concurrency == 0 {
            return Err(Error::Config("general.concurrency must be >= 1".into()));
        }
        if self.general.concurrency > 64 {
            return Err(Error::Config(
                "general.concurrency > 64 would only waste sockets; keep it <= 64".into(),
            ));
        }
        if self.collection.timeframes.is_empty() {
            return Err(Error::Config(
                "collection.timeframes must list at least one timeframe".into(),
            ));
        }
        if self.collection.overlap_candles < 0 {
            return Err(Error::Config(
                "collection.overlap_candles must be >= 0".into(),
            ));
        }
        if self.collection.max_empty_windows == 0 {
            return Err(Error::Config(
                "collection.max_empty_windows must be >= 1".into(),
            ));
        }
        self.collection.start_spec()?;
        self.storage.layout_options()?;
        self.storage.write_options()?;
        Ok(())
    }

    /// Build the KuCoin client configuration.
    pub fn kucoin_config(&self) -> KucoinConfig {
        KucoinConfig {
            base_url: self.general.base_url.clone(),
            timeout: std::time::Duration::from_secs(self.general.request_timeout_secs.max(1)),
            retry: RetryPolicy {
                max_retries: self.rate_limit.max_retries,
                base_delay: std::time::Duration::from_millis(
                    self.rate_limit.base_backoff_ms.max(1),
                ),
                max_delay: std::time::Duration::from_secs(self.rate_limit.max_backoff_secs.max(1)),
            },
            pool_capacity: self.rate_limit.weight_limit.max(1.0),
            pool_window: std::time::Duration::from_secs(self.rate_limit.window_secs.max(1)),
            min_interval: std::time::Duration::from_millis(self.rate_limit.min_interval_ms),
        }
    }

    /// Environment overrides applied after loading:
    /// `KCS_DATA_DIR`, `KCS_STATE_DIR`, `KCS_BASE_URL`, `KCS_CONCURRENCY`,
    /// `KCS_LOG_LEVEL`.
    pub fn apply_env_overrides(&mut self) {
        if let Ok(v) = std::env::var("KCS_DATA_DIR") {
            self.general.data_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("KCS_STATE_DIR") {
            self.general.state_dir = PathBuf::from(v);
        }
        if let Ok(v) = std::env::var("KCS_BASE_URL") {
            self.general.base_url = v;
        }
        if let Ok(v) = std::env::var("KCS_CONCURRENCY") {
            if let Ok(n) = v.parse::<usize>() {
                self.general.concurrency = n;
            }
        }
        if let Ok(v) = std::env::var("KCS_LOG_LEVEL") {
            self.general.log_level = v;
        }
    }

    /// Config file contents written by `kcs-klines init`.
    pub fn template() -> String {
        let cfg = Config {
            collection: CollectionConfig {
                symbols: vec!["BTC-USDT".into(), "ETH-USDT".into(), "SOL-USDT".into()],
                timeframes: vec![
                    Timeframe::M1,
                    Timeframe::M5,
                    Timeframe::M15,
                    Timeframe::H1,
                    Timeframe::H4,
                    Timeframe::D1,
                ],
                ..Default::default()
            },
            ..Default::default()
        };
        let body = toml::to_string_pretty(&cfg).expect("config serialises");
        format!(
            "# kcs-klines configuration\n\
             # Full documentation: README.md\n\
             #\n\
             # `start = \"listing\"` walks backwards until KuCoin stops returning\n\
             # candles, i.e. until the true start of the pair's history.\n\
             # An empty `symbols` list means: collect every pair the exchange\n\
             # lists, with no filtering. A scheduled `backfill` picks up newly\n\
             # listed pairs on its own.\n\n{body}"
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_valid() {
        Config::default().validate().unwrap();
    }

    #[test]
    fn parses_a_full_config() {
        let text = r#"
[general]
data_dir = "/srv/kcs/data"
state_dir = "/srv/kcs/state"
concurrency = 8
log_level = "debug"

[rate_limit]
weight_limit = 2000
window_secs = 30
min_interval_ms = 40

[collection]
symbols = ["BTC-USDT", "ETH-USDT"]
timeframes = ["1m", "1h", "1d"]
start = "2019-01-01"
overlap_candles = 3

[storage]
partition = "month"
zstd_level = 5
include_turnover = true

[storage.partition_overrides]
"1h" = "year"

[notifications]
healthcheck_url = "https://hc-ping.com/xyz"

[logging]
file = "/var/log/kcs-klines.log"
"#;
        let cfg: Config = toml::from_str(text).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.general.concurrency, 8);
        assert_eq!(cfg.general.data_dir, PathBuf::from("/srv/kcs/data"));
        assert_eq!(cfg.collection.timeframes.len(), 3);
        assert_eq!(cfg.collection.start_spec().unwrap(), Some(1_546_300_800));

        let layout = cfg.storage.layout_options().unwrap();
        assert_eq!(layout.rule_for(Timeframe::H1), PartitionRule::Year);
        assert_eq!(layout.rule_for(Timeframe::M1), PartitionRule::Month);
        assert_eq!(cfg.storage.write_options().unwrap().zstd_level, 5);
    }

    #[test]
    fn rejects_semantically_broken_configs() {
        let mut cfg = Config::default();
        cfg.general.concurrency = 0;
        assert!(cfg.validate().is_err());

        let mut cfg = Config::default();
        cfg.collection.timeframes.clear();
        assert!(cfg.validate().is_err());

        let mut cfg = Config::default();
        cfg.collection.start = "yesterday".into();
        assert!(cfg.validate().is_err());

        let mut cfg = Config::default();
        cfg.collection.max_empty_windows = 0;
        assert!(cfg.validate().is_err());

        let mut cfg = Config::default();
        cfg.storage.compression = "gzip".into();
        assert!(cfg.validate().is_err());

        let mut cfg = Config::default();
        cfg.storage.zstd_level = 99;
        assert!(cfg.validate().is_err());

        let mut cfg = Config::default();
        cfg.storage
            .partition_overrides
            .insert("9x".into(), PartitionRule::Year);
        assert!(cfg.validate().is_err());
    }

    #[test]
    fn unknown_keys_are_rejected() {
        let err = toml::from_str::<Config>("[general]\nghost = 1\n").unwrap_err();
        assert!(err.to_string().contains("ghost") || err.to_string().contains("unknown"));
    }

    #[test]
    fn env_overrides_win() {
        let mut cfg = Config::default();
        std::env::set_var("KCS_DATA_DIR", "/tmp/kcs-env-test");
        std::env::set_var("KCS_CONCURRENCY", "7");
        cfg.apply_env_overrides();
        std::env::remove_var("KCS_DATA_DIR");
        std::env::remove_var("KCS_CONCURRENCY");
        assert_eq!(cfg.general.data_dir, PathBuf::from("/tmp/kcs-env-test"));
        assert_eq!(cfg.general.concurrency, 7);
    }

    #[test]
    fn template_round_trips() {
        let text = Config::template();
        let cfg: Config = toml::from_str(&text).unwrap();
        cfg.validate().unwrap();
        assert_eq!(cfg.collection.symbols.len(), 3);
        assert_eq!(cfg.collection.timeframes.len(), 6);
    }

    #[test]
    fn kucoin_config_is_built_from_rate_limit_section() {
        let cfg = Config {
            rate_limit: RateLimitConfig {
                weight_limit: 1000.0,
                window_secs: 10,
                min_interval_ms: 25,
                ..Default::default()
            },
            ..Default::default()
        };
        let kc = cfg.kucoin_config();
        assert_eq!(kc.pool_capacity, 1000.0);
        assert_eq!(kc.pool_window, std::time::Duration::from_secs(10));
        assert_eq!(kc.min_interval, std::time::Duration::from_millis(25));
        assert_eq!(kc.base_url, "https://api.kucoin.com");
    }
}
