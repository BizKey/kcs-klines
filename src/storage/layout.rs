//! On-disk layout: `data/kucoin/spot/{SYMBOL}/{TF}/{YEAR}/{YEAR-MONTH}.parquet`.
//!
//! Partition granularity is derived from the timeframe:
//!
//! | timeframe      | partition | example path                                    |
//! |----------------|-----------|-------------------------------------------------|
//! | `1m` … `30m`   | month     | `data/kucoin/spot/BTC-USDT/1m/2017/2017-09.parquet` |
//! | `1h` … `1d`    | year      | `data/kucoin/spot/BTC-USDT/1h/2017.parquet`      |
//! | `1w`, `1mon`   | single    | `data/kucoin/spot/BTC-USDT/1w/all.parquet`       |
//!
//! Monthly files keep Parquet row groups small, make rewrites cheap and let
//! DuckDB/Polars read a whole tree with
//! `read_parquet('data/kucoin/spot/BTC-USDT/1m/**/*.parquet')`.

use std::fmt;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Datelike, NaiveDate};

use crate::kucoin::Timeframe;

/// How a timeframe is split into files.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PartitionRule {
    /// Pick automatically from the timeframe length.
    Auto,
    /// One file per calendar month (UTC).
    Month,
    /// One file per calendar year (UTC).
    Year,
    /// One file for the whole history.
    Single,
}

impl PartitionRule {
    /// Resolve [`PartitionRule::Auto`] into a concrete rule.
    pub fn resolve(self, tf: Timeframe) -> PartitionRule {
        match self {
            PartitionRule::Auto => {
                // Bar *length*, so `approx_seconds` is enough: a month counts as
                // 30 days and lands in the single-file bucket, which is where a
                // ~110-bar series belongs anyway.
                if tf.approx_seconds() <= Timeframe::M30.approx_seconds() {
                    PartitionRule::Month
                } else if tf.approx_seconds() <= Timeframe::D1.approx_seconds() {
                    PartitionRule::Year
                } else {
                    PartitionRule::Single
                }
            }
            other => other,
        }
    }
}

impl fmt::Display for PartitionRule {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            PartitionRule::Auto => "auto",
            PartitionRule::Month => "month",
            PartitionRule::Year => "year",
            PartitionRule::Single => "single",
        })
    }
}

impl std::str::FromStr for PartitionRule {
    type Err = crate::error::Error;

    fn from_str(s: &str) -> crate::error::Result<Self> {
        Ok(match s.trim().to_ascii_lowercase().as_str() {
            "auto" => PartitionRule::Auto,
            "month" | "monthly" => PartitionRule::Month,
            "year" | "yearly" => PartitionRule::Year,
            "single" | "none" | "all" => PartitionRule::Single,
            other => {
                return Err(crate::error::Error::Config(format!(
                    "unknown partition rule `{other}`; expected auto, month, year or single"
                )))
            }
        })
    }
}

impl serde::Serialize for PartitionRule {
    fn serialize<S: serde::Serializer>(
        &self,
        serializer: S,
    ) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.to_string())
    }
}

impl<'de> serde::Deserialize<'de> for PartitionRule {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> std::result::Result<Self, D::Error> {
        let raw = String::deserialize(deserializer)?;
        raw.parse().map_err(serde::de::Error::custom)
    }
}

/// Far future boundary used by [`Period::Single`].
const FAR_FUTURE: i64 = 4_102_444_800; // 2100-01-01T00:00:00Z

/// A single partition: a month, a year, or the whole history.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Period {
    /// Calendar month (UTC).
    Month {
        /// Year.
        year: i32,
        /// Month, 1-12.
        month: u32,
    },
    /// Calendar year (UTC).
    Year {
        /// Year.
        year: i32,
    },
    /// Everything in one file.
    Single,
}

impl Period {
    /// Period that contains `ts` under `rule` (which must be resolved).
    pub fn containing(ts: i64, rule: PartitionRule) -> Period {
        let dt = DateTime::from_timestamp(ts, 0)
            .unwrap_or_else(|| DateTime::from_timestamp(0, 0).expect("epoch is always valid"));
        match rule {
            // Callers normally pass an already-resolved rule; `Auto` degrades to
            // month partitioning, which is the finest and therefore safest split.
            PartitionRule::Auto | PartitionRule::Month => Period::Month {
                year: dt.year(),
                month: dt.month(),
            },
            PartitionRule::Year => Period::Year { year: dt.year() },
            PartitionRule::Single => Period::Single,
        }
    }

    /// Inclusive start of the period, unix seconds UTC.
    pub fn start(&self) -> i64 {
        match *self {
            Period::Month { year, month } => month_start_unix(year, month),
            Period::Year { year } => month_start_unix(year, 1),
            Period::Single => 0,
        }
    }

    /// Exclusive end of the period, unix seconds UTC.
    pub fn end_exclusive(&self) -> i64 {
        match *self {
            Period::Month { year, month } => {
                let (ny, nm) = if month == 12 {
                    (year + 1, 1)
                } else {
                    (year, month + 1)
                };
                month_start_unix(ny, nm)
            }
            Period::Year { year } => month_start_unix(year + 1, 1),
            Period::Single => FAR_FUTURE,
        }
    }

    /// Previous period, or `None` when there is nothing before.
    pub fn prev(&self) -> Option<Period> {
        match *self {
            Period::Month { year, month } => Some(if month == 1 {
                Period::Month {
                    year: year - 1,
                    month: 12,
                }
            } else {
                Period::Month {
                    year,
                    month: month - 1,
                }
            }),
            Period::Year { year } => Some(Period::Year { year: year - 1 }),
            Period::Single => None,
        }
    }

    /// Next period, or `None` for [`Period::Single`].
    pub fn next(&self) -> Option<Period> {
        match *self {
            Period::Month { year, month } => Some(if month == 12 {
                Period::Month {
                    year: year + 1,
                    month: 1,
                }
            } else {
                Period::Month {
                    year,
                    month: month + 1,
                }
            }),
            Period::Year { year } => Some(Period::Year { year: year + 1 }),
            Period::Single => None,
        }
    }

    /// File stem, e.g. `2017-09`, `2017` or `all`.
    pub fn slug(&self) -> String {
        match *self {
            Period::Month { year, month } => format!("{year:04}-{month:02}"),
            Period::Year { year } => format!("{year:04}"),
            Period::Single => "all".to_string(),
        }
    }

    /// Optional intermediate directory, e.g. `2017` for a month period.
    pub fn dir_component(&self) -> Option<String> {
        match *self {
            Period::Month { year, .. } => Some(format!("{year:04}")),
            Period::Year { .. } | Period::Single => None,
        }
    }
}

impl fmt::Display for Period {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.slug())
    }
}

/// Unix seconds of `YYYY-MM-01T00:00:00Z`.
fn month_start_unix(year: i32, month: u32) -> i64 {
    match NaiveDate::from_ymd_opt(year, month, 1) {
        Some(d) => d
            .and_hms_opt(0, 0, 0)
            .expect("midnight is a valid time")
            .and_utc()
            .timestamp(),
        None => {
            // Out-of-range years cannot occur from real timestamps; clamp to be safe.
            if year < 1970 {
                0
            } else {
                FAR_FUTURE
            }
        }
    }
}

/// Layout configuration (exchange/market prefixes and partitioning).
#[derive(Debug, Clone)]
pub struct LayoutOptions {
    /// Exchange directory name, default `kucoin`.
    pub exchange: String,
    /// Market directory name, default `spot`.
    pub market: String,
    /// Global partition rule.
    pub partition: PartitionRule,
    /// Per-timeframe overrides.
    pub partition_overrides: Vec<(Timeframe, PartitionRule)>,
}

impl Default for LayoutOptions {
    fn default() -> Self {
        LayoutOptions {
            exchange: "kucoin".to_string(),
            market: "spot".to_string(),
            partition: PartitionRule::Auto,
            partition_overrides: Vec::new(),
        }
    }
}

impl LayoutOptions {
    /// Effective partition rule for a timeframe.
    pub fn rule_for(&self, tf: Timeframe) -> PartitionRule {
        self.partition_overrides
            .iter()
            .find(|(t, _)| *t == tf)
            .map(|(_, r)| *r)
            .unwrap_or(self.partition)
            .resolve(tf)
    }

    /// Path of a partition file relative to the data directory.
    pub fn relative_path(&self, symbol: &str, tf: Timeframe, period: Period) -> PathBuf {
        let mut p = PathBuf::from(&self.exchange);
        p.push(&self.market);
        p.push(symbol);
        p.push(tf.slug());
        if let Some(dir) = period.dir_component() {
            p.push(dir);
        }
        p.push(format!("{}.parquet", period.slug()));
        p
    }

    /// Absolute path of a partition file.
    pub fn path(&self, data_dir: &Path, symbol: &str, tf: Timeframe, period: Period) -> PathBuf {
        data_dir.join(self.relative_path(symbol, tf, period))
    }

    /// Directory holding every partition of one (symbol, timeframe).
    pub fn series_dir(&self, data_dir: &Path, symbol: &str, tf: Timeframe) -> PathBuf {
        data_dir
            .join(&self.exchange)
            .join(&self.market)
            .join(symbol)
            .join(tf.slug())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::date_to_unix;

    fn ts(date: &str) -> i64 {
        date_to_unix(date).unwrap()
    }

    #[test]
    fn auto_rule_matches_timeframe_length() {
        assert_eq!(
            PartitionRule::Auto.resolve(Timeframe::M1),
            PartitionRule::Month
        );
        assert_eq!(
            PartitionRule::Auto.resolve(Timeframe::M30),
            PartitionRule::Month
        );
        assert_eq!(
            PartitionRule::Auto.resolve(Timeframe::H1),
            PartitionRule::Year
        );
        assert_eq!(
            PartitionRule::Auto.resolve(Timeframe::D1),
            PartitionRule::Year
        );
        assert_eq!(
            PartitionRule::Auto.resolve(Timeframe::W1),
            PartitionRule::Single
        );
    }

    #[test]
    fn month_period_bounds() {
        let p = Period::Month {
            year: 2024,
            month: 2,
        };
        assert_eq!(p.start(), ts("2024-02-01"));
        assert_eq!(p.end_exclusive(), ts("2024-03-01"));
        assert_eq!(p.slug(), "2024-02");
        assert_eq!(p.dir_component().as_deref(), Some("2024"));
    }

    #[test]
    fn december_rolls_over_to_next_year() {
        let p = Period::Month {
            year: 2023,
            month: 12,
        };
        assert_eq!(
            p.next(),
            Some(Period::Month {
                year: 2024,
                month: 1
            })
        );
        assert_eq!(
            p.prev(),
            Some(Period::Month {
                year: 2023,
                month: 11
            })
        );
        assert_eq!(p.end_exclusive(), ts("2024-01-01"));
        let jan = Period::Month {
            year: 2024,
            month: 1,
        };
        assert_eq!(jan.prev(), Some(p));
    }

    #[test]
    fn year_period_has_no_subdirectory() {
        let p = Period::Year { year: 2021 };
        assert_eq!(p.slug(), "2021");
        assert_eq!(p.dir_component(), None);
        assert_eq!(p.start(), ts("2021-01-01"));
        assert_eq!(p.end_exclusive(), ts("2022-01-01"));
    }

    #[test]
    fn containing_picks_the_right_period() {
        let rule = PartitionRule::Month;
        let p = Period::containing(ts("2024-03-15") + 3600, rule);
        assert_eq!(
            p,
            Period::Month {
                year: 2024,
                month: 3
            }
        );
        let rule = PartitionRule::Year;
        assert_eq!(
            Period::containing(ts("2024-03-15"), rule),
            Period::Year { year: 2024 }
        );
        assert_eq!(
            Period::containing(ts("2024-03-15"), PartitionRule::Single),
            Period::Single
        );
    }

    #[test]
    fn layout_paths_follow_the_spec() {
        let opts = LayoutOptions::default();
        let data = Path::new("/data");
        assert_eq!(
            opts.path(
                data,
                "BTC-USDT",
                Timeframe::M1,
                Period::Month {
                    year: 2017,
                    month: 9
                }
            ),
            PathBuf::from("/data/kucoin/spot/BTC-USDT/1m/2017/2017-09.parquet")
        );
        assert_eq!(
            opts.path(data, "BTC-USDT", Timeframe::H1, Period::Year { year: 2026 }),
            PathBuf::from("/data/kucoin/spot/BTC-USDT/1h/2026.parquet")
        );
        assert_eq!(
            opts.path(data, "BTC-USDT", Timeframe::W1, Period::Single),
            PathBuf::from("/data/kucoin/spot/BTC-USDT/1w/all.parquet")
        );
    }

    #[test]
    fn per_timeframe_overrides_win() {
        let opts = LayoutOptions {
            partition_overrides: vec![(Timeframe::H1, PartitionRule::Month)],
            ..Default::default()
        };
        assert_eq!(opts.rule_for(Timeframe::H1), PartitionRule::Month);
        assert_eq!(opts.rule_for(Timeframe::M1), PartitionRule::Month);
        assert_eq!(opts.rule_for(Timeframe::W1), PartitionRule::Single);
    }

    #[test]
    fn partition_rule_parsing() {
        assert_eq!(
            "auto".parse::<PartitionRule>().unwrap(),
            PartitionRule::Auto
        );
        assert_eq!(
            "MONTHLY".parse::<PartitionRule>().unwrap(),
            PartitionRule::Month
        );
        assert_eq!(
            "none".parse::<PartitionRule>().unwrap(),
            PartitionRule::Single
        );
        assert!("weekly".parse::<PartitionRule>().is_err());
    }
}
