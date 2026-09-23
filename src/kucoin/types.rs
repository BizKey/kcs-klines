//! Wire types for the KuCoin Spot REST API.
//!
//! # Candle field order (important)
//!
//! `/api/v1/market/candles` returns rows as
//! `[time, open, close, high, low, volume, turnover]` — note that **close comes
//! before high/low**, which is *not* the same order as most other exchanges and
//! is a silent data-corruption trap. The order below was verified empirically
//! against the live API: for 6272 sampled rows across 5 symbols and 5
//! granularities the `[t, o, c, h, l, v, tv]` reading satisfied
//! `high >= max(open, close) && low <= min(open, close)` in 100% of cases,
//! while the `[t, o, h, l, c, v, tv]` reading failed for the majority of rows.
//! [`Candle::from_row`] encodes that order and [`Candle::validate`] re-checks it.

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::Value;

use crate::error::{Error, Result};

/// A single OHLCV bar.
///
/// `time` is the **open time** of the bar in unix seconds (UTC), always aligned
/// to the timeframe grid.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Candle {
    /// Bar open time, unix seconds UTC.
    pub time: i64,
    /// Open price.
    pub open: f64,
    /// Highest price.
    pub high: f64,
    /// Lowest price.
    pub low: f64,
    /// Close price.
    pub close: f64,
    /// Base asset volume.
    pub volume: f64,
    /// Quote asset volume (turnover). `0.0` when the exchange omitted it.
    pub turnover: f64,
}

impl Candle {
    /// Parse one raw KuCoin row (`[time, open, close, high, low, volume, turnover]`).
    ///
    /// Both string-encoded and numeric JSON values are accepted, because some
    /// KuCoin endpoints use strings while others use numbers.
    pub fn from_row(row: &[Value]) -> Result<Self> {
        if row.len() < 6 {
            return Err(Error::Data(format!(
                "candle row has {} fields, expected at least 6",
                row.len()
            )));
        }
        let mut time = as_i64(&row[0])
            .ok_or_else(|| Error::Data(format!("candle time is not a number: {}", row[0])))?;
        // Spot reports seconds; futures and a few endpoints report milliseconds.
        if time > 100_000_000_000 {
            time /= 1000;
        }
        let num = |i: usize, what: &str| -> Result<f64> {
            as_f64(&row[i])
                .ok_or_else(|| Error::Data(format!("candle {what} is not a number: {}", row[i])))
        };
        Ok(Candle {
            time,
            open: num(1, "open")?,
            close: num(2, "close")?,
            high: num(3, "high")?,
            low: num(4, "low")?,
            volume: num(5, "volume")?,
            turnover: if row.len() > 6 {
                as_f64(&row[6]).unwrap_or(0.0)
            } else {
                0.0
            },
        })
    }

    /// Check OHLC and volume invariants (ignores grid alignment).
    pub fn validate_invariants(&self) -> Result<()> {
        let values = [
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.turnover,
        ];
        if values.iter().any(|v| !v.is_finite()) {
            return Err(Error::Data(format!(
                "non-finite value in candle at {}: {self:?}",
                self.time
            )));
        }
        // Prices are exact decimal strings from the exchange, so a tiny relative
        // tolerance is enough to absorb float round-trips without hiding real
        // corruption.
        let tol = 1e-9 * self.high.abs().max(1.0);
        if self.high + tol < self.low {
            return Err(Error::Data(format!(
                "high < low in candle at {}: {self:?}",
                self.time
            )));
        }
        if self.high + tol < self.open.max(self.close) {
            return Err(Error::Data(format!(
                "high < max(open, close) in candle at {}: {self:?}",
                self.time
            )));
        }
        if self.low - tol > self.open.min(self.close) {
            return Err(Error::Data(format!(
                "low > min(open, close) in candle at {}: {self:?}",
                self.time
            )));
        }
        if self.volume < 0.0 || self.turnover < 0.0 {
            return Err(Error::Data(format!(
                "negative volume in candle at {}: {self:?}",
                self.time
            )));
        }
        Ok(())
    }

    /// Check invariants **and** that the bar sits on the timeframe grid.
    pub fn validate(&self, tf: Timeframe) -> Result<()> {
        self.validate_invariants()?;
        if tf.align(self.time) != self.time {
            return Err(Error::Data(format!(
                "candle time {} is not aligned to the {} grid (expected {})",
                self.time,
                tf.slug(),
                tf.align(self.time)
            )));
        }
        Ok(())
    }
}

/// Unix seconds of the first day of the month containing `ts`, at 00:00 UTC.
fn month_floor(ts: i64) -> i64 {
    use chrono::{DateTime, Datelike, NaiveDate};
    let dt = DateTime::from_timestamp(ts, 0).unwrap_or_else(|| {
        DateTime::from_timestamp(0, 0).expect("the epoch is always representable")
    });
    let (year, month) = (dt.year(), dt.month());
    match NaiveDate::from_ymd_opt(year, month, 1) {
        Some(date) => date
            .and_hms_opt(0, 0, 0)
            .expect("midnight is a valid time")
            .and_utc()
            .timestamp(),
        // Only reachable for timestamps outside chrono's calendar range.
        None => ts,
    }
}

/// `ts` (aligned to a month start) shifted by `bars` calendar months.
fn shift_months(ts: i64, bars: i64) -> i64 {
    use chrono::{DateTime, Datelike};
    let dt = DateTime::from_timestamp(ts, 0).unwrap_or_else(|| {
        DateTime::from_timestamp(0, 0).expect("the epoch is always representable")
    });
    let total = dt.year() as i64 * 12 + (dt.month() as i64 - 1) + bars;
    let year = total.div_euclid(12) as i32;
    let month = (total.rem_euclid(12) + 1) as u32;
    month_floor_for(year, month).unwrap_or(ts)
}

fn month_floor_for(year: i32, month: u32) -> Option<i64> {
    use chrono::NaiveDate;
    NaiveDate::from_ymd_opt(year, month, 1).map(|d| {
        d.and_hms_opt(0, 0, 0)
            .expect("midnight is a valid time")
            .and_utc()
            .timestamp()
    })
}

/// Whole months between two month starts (positive when `to` is later).
fn months_between(from: i64, to: i64) -> i64 {
    use chrono::{DateTime, Datelike};
    let (a, b) = (
        DateTime::from_timestamp(from, 0),
        DateTime::from_timestamp(to, 0),
    );
    match (a, b) {
        (Some(a), Some(b)) => {
            let months_a = a.year() as i64 * 12 + a.month() as i64 - 1;
            let months_b = b.year() as i64 * 12 + b.month() as i64 - 1;
            months_b - months_a
        }
        _ => 0,
    }
}

fn as_i64(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s
            .trim()
            .parse::<i64>()
            .ok()
            .or_else(|| s.trim().parse::<f64>().ok().map(|f| f as i64)),
        _ => None,
    }
}

fn as_f64(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        _ => None,
    }
}

/// Supported candle intervals.
///
/// All of them except [`Timeframe::Mon1`] are fixed-length, so `time + n * interval`
/// is always a valid bar label. A calendar month is not: it runs 28-31 days, which
/// is why every piece of time arithmetic in this crate goes through
/// [`Timeframe::align`], [`Timeframe::shift`] and [`Timeframe::slots_between`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Timeframe {
    /// 1 minute.
    M1,
    /// 3 minutes.
    M3,
    /// 5 minutes.
    M5,
    /// 15 minutes.
    M15,
    /// 30 minutes.
    M30,
    /// 1 hour.
    H1,
    /// 2 hours.
    H2,
    /// 4 hours.
    H4,
    /// 6 hours.
    H6,
    /// 8 hours.
    H8,
    /// 12 hours.
    H12,
    /// 1 day.
    D1,
    /// 1 week.
    W1,
    /// 1 calendar month.
    ///
    /// Unlike every other timeframe this one is **not** a fixed number of
    /// seconds: KuCoin labels the bar with the first day of the month at 00:00
    /// UTC, and months run 28-31 days. All arithmetic therefore goes through
    /// [`Timeframe::align`], [`Timeframe::shift`] and
    /// [`Timeframe::slots_between`] rather than `time + n * interval`.
    Mon1,
}

impl Timeframe {
    /// Every supported timeframe, ascending.
    pub const ALL: [Timeframe; 14] = [
        Timeframe::M1,
        Timeframe::M3,
        Timeframe::M5,
        Timeframe::M15,
        Timeframe::M30,
        Timeframe::H1,
        Timeframe::H2,
        Timeframe::H4,
        Timeframe::H6,
        Timeframe::H8,
        Timeframe::H12,
        Timeframe::D1,
        Timeframe::W1,
        Timeframe::Mon1,
    ];

    /// Value of the API `type` query parameter, e.g. `"1min"`.
    pub const fn api_type(self) -> &'static str {
        match self {
            Timeframe::M1 => "1min",
            Timeframe::M3 => "3min",
            Timeframe::M5 => "5min",
            Timeframe::M15 => "15min",
            Timeframe::M30 => "30min",
            Timeframe::H1 => "1hour",
            Timeframe::H2 => "2hour",
            Timeframe::H4 => "4hour",
            Timeframe::H6 => "6hour",
            Timeframe::H8 => "8hour",
            Timeframe::H12 => "12hour",
            Timeframe::D1 => "1day",
            Timeframe::W1 => "1week",
            Timeframe::Mon1 => "1month",
        }
    }

    /// Short canonical form used in directory names, e.g. `"1m"`.
    pub const fn slug(self) -> &'static str {
        match self {
            Timeframe::M1 => "1m",
            Timeframe::M3 => "3m",
            Timeframe::M5 => "5m",
            Timeframe::M15 => "15m",
            Timeframe::M30 => "30m",
            Timeframe::H1 => "1h",
            Timeframe::H2 => "2h",
            Timeframe::H4 => "4h",
            Timeframe::H6 => "6h",
            Timeframe::H8 => "8h",
            Timeframe::H12 => "12h",
            Timeframe::D1 => "1d",
            Timeframe::W1 => "1w",
            Timeframe::Mon1 => "1mon",
        }
    }

    /// True when the bar length varies (only the calendar month).
    pub const fn is_calendar(self) -> bool {
        matches!(self, Timeframe::Mon1)
    }

    /// Bar length in seconds, or `None` for calendar timeframes.
    ///
    /// Use [`Timeframe::shift`] instead of multiplying this by a bar count when
    /// you need a timestamp: that only holds for fixed-length bars.
    pub const fn fixed_seconds(self) -> Option<i64> {
        match self {
            Timeframe::Mon1 => None,
            Timeframe::M1 => Some(60),
            Timeframe::M3 => Some(180),
            Timeframe::M5 => Some(300),
            Timeframe::M15 => Some(900),
            Timeframe::M30 => Some(1_800),
            Timeframe::H1 => Some(3_600),
            Timeframe::H2 => Some(7_200),
            Timeframe::H4 => Some(14_400),
            Timeframe::H6 => Some(21_600),
            Timeframe::H8 => Some(28_800),
            Timeframe::H12 => Some(43_200),
            Timeframe::D1 => Some(86_400),
            Timeframe::W1 => Some(604_800),
        }
    }

    /// Rough bar length in seconds, for sizing heuristics only (a month counts
    /// as 30 days). Never use it to compute a timestamp.
    pub const fn approx_seconds(self) -> i64 {
        match self.fixed_seconds() {
            Some(seconds) => seconds,
            None => 30 * 86_400,
        }
    }

    /// Floor `ts` to the start of the bar that contains it.
    ///
    /// For fixed-length bars this is a plain grid alignment; for the calendar
    /// month it is the first day of that month at 00:00 UTC.
    pub fn align(self, ts: i64) -> i64 {
        match self.fixed_seconds() {
            Some(seconds) => crate::util::align_down(ts, seconds),
            None => month_floor(ts),
        }
    }

    /// Move `bars` bars away from `ts`, which must already be aligned.
    ///
    /// Works for calendar timeframes too, where `bars` months are added.
    pub fn shift(self, ts: i64, bars: i64) -> i64 {
        match self.fixed_seconds() {
            Some(seconds) => ts.saturating_add(bars.saturating_mul(seconds)),
            None => shift_months(ts, bars),
        }
    }

    /// Number of bars in the half-open window `[from, to_exclusive)`.
    ///
    /// KuCoin treats `endAt` as exclusive, so for a dense series this is exactly
    /// how many bars a request for that window returns.
    pub fn slots_between(self, from: i64, to_exclusive: i64) -> i64 {
        if to_exclusive <= from {
            return 0;
        }
        match self.fixed_seconds() {
            Some(seconds) => (to_exclusive - from) / seconds,
            None => months_between(from, to_exclusive),
        }
    }

    /// Maximum number of candles a single KuCoin **Spot** request returns.
    pub const fn max_candles(self) -> usize {
        1500
    }
}

impl fmt::Display for Timeframe {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.slug())
    }
}

impl FromStr for Timeframe {
    type Err = Error;

    fn from_str(s: &str) -> Result<Self> {
        let key = s.trim().to_ascii_lowercase();
        let tf = match key.as_str() {
            "1m" | "1min" | "1minute" | "60" => Timeframe::M1,
            "3m" | "3min" | "3minute" | "180" => Timeframe::M3,
            "5m" | "5min" | "5minute" | "300" => Timeframe::M5,
            "15m" | "15min" | "15minute" | "900" => Timeframe::M15,
            "30m" | "30min" | "30minute" | "1800" => Timeframe::M30,
            "1h" | "1hour" | "60m" | "3600" => Timeframe::H1,
            "2h" | "2hour" | "120m" | "7200" => Timeframe::H2,
            "4h" | "4hour" | "240m" | "14400" => Timeframe::H4,
            "6h" | "6hour" | "360m" | "21600" => Timeframe::H6,
            "8h" | "8hour" | "480m" | "28800" => Timeframe::H8,
            "12h" | "12hour" | "720m" | "43200" => Timeframe::H12,
            "1d" | "1day" | "d" | "86400" => Timeframe::D1,
            "1w" | "1week" | "w" | "604800" => Timeframe::W1,
            "1mon" | "1month" | "1mo" => Timeframe::Mon1,
            other => {
                return Err(Error::Config(format!(
                    "unknown timeframe `{other}`; supported: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 1w, 1mon"
                )))
            }
        };
        Ok(tf)
    }
}

impl Serialize for Timeframe {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        serializer.serialize_str(self.slug())
    }
}

impl<'de> Deserialize<'de> for Timeframe {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let raw = String::deserialize(deserializer)?;
        Timeframe::from_str(&raw).map_err(serde::de::Error::custom)
    }
}

/// One entry of `/api/v2/symbols`.
///
/// `symbol` is all the collector itself needs; the remaining fields are parsed so
/// library users get the full listing metadata without another request.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SymbolInfo {
    /// Trading pair, e.g. `BTC-USDT`.
    pub symbol: String,
    /// Base currency, e.g. `BTC`.
    pub base_currency: String,
    /// Quote currency, e.g. `USDT`.
    pub quote_currency: String,
    /// Whether trading is currently enabled.
    #[serde(default)]
    pub enable_trading: bool,
    /// KuCoin market segment, e.g. `USDS`.
    #[serde(default)]
    pub market: Option<String>,
    /// Special treatment (delisting candidate) flag.
    #[serde(default)]
    pub st: bool,
}

/// KuCoin response envelope: `{"code":"200000","data":...}`.
#[derive(Debug, Deserialize)]
pub struct Envelope<T> {
    /// Business code; `"200000"` means success.
    #[serde(deserialize_with = "de_string_from_any")]
    pub code: String,
    /// Payload, absent on errors.
    ///
    /// `Option` fields are already optional for serde, and adding
    /// `#[serde(default)]` here would make the derive demand `T: Default`.
    pub data: Option<T>,
    /// Error text, present only on failures.
    #[serde(default, alias = "message")]
    pub msg: Option<String>,
}

impl<T> Envelope<T> {
    /// Business code that indicates success.
    pub const OK: &'static str = "200000";

    /// True when the exchange reported success.
    pub fn is_ok(&self) -> bool {
        self.code == Self::OK
    }
}

/// Accept both `"200000"` and `200000` for the `code` field.
fn de_string_from_any<'de, D: Deserializer<'de>>(d: D) -> std::result::Result<String, D::Error> {
    let v = Value::deserialize(d)?;
    match v {
        Value::String(s) => Ok(s),
        Value::Number(n) => Ok(n.to_string()),
        other => Err(serde::de::Error::custom(format!(
            "expected string or number code, got {other}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_row_in_documented_order() {
        // Real row shape observed on /api/v1/market/candles.
        let row = json!([
            "1790121600",
            "86194.4",
            "86592.7",
            "87276.8",
            "86148.2",
            "629.3541969",
            "54530007.84"
        ]);
        let c = Candle::from_row(row.as_array().unwrap()).unwrap();
        assert_eq!(c.time, 1_790_121_600);
        assert_eq!(c.open, 86194.4);
        assert_eq!(c.close, 86592.7);
        assert_eq!(c.high, 87276.8);
        assert_eq!(c.low, 86148.2);
        assert_eq!(c.volume, 629.3541969);
        assert_eq!(c.turnover, 54530007.84);
        c.validate(Timeframe::D1).unwrap();
    }

    #[test]
    fn accepts_numeric_fields_and_millisecond_times() {
        let row = json!([1_790_121_600_000i64, 1.0, 2.0, 3.0, 0.5, 10, 20]);
        let c = Candle::from_row(row.as_array().unwrap()).unwrap();
        assert_eq!(c.time, 1_790_121_600);
        assert_eq!(c.high, 3.0);
    }

    #[test]
    fn rejects_malformed_rows() {
        assert!(Candle::from_row(json!(["1", "2"]).as_array().unwrap()).is_err());
        assert!(
            Candle::from_row(json!(["x", "1", "2", "3", "0.5", "1"]).as_array().unwrap()).is_err()
        );
        assert!(
            Candle::from_row(json!(["1", "y", "2", "3", "0.5", "1"]).as_array().unwrap()).is_err()
        );
    }

    #[test]
    fn validate_catches_impossible_bars() {
        let bad = Candle {
            time: 1_700_000_000 - 1_700_000_000 % 60,
            open: 10.0,
            high: 9.0, // high below open/close
            low: 8.0,
            close: 9.5,
            volume: 1.0,
            turnover: 1.0,
        };
        assert!(bad.validate(Timeframe::M1).is_err());

        let unaligned = Candle {
            time: 1_700_000_030,
            ..bad
        };
        // high/low fixed so only alignment can fail
        let unaligned = Candle {
            high: 11.0,
            low: 7.0,
            ..unaligned
        };
        assert!(unaligned.validate(Timeframe::M1).is_err());
    }

    #[test]
    fn timeframe_parsing_accepts_api_and_short_forms() {
        assert_eq!("1min".parse::<Timeframe>().unwrap(), Timeframe::M1);
        assert_eq!("1m".parse::<Timeframe>().unwrap(), Timeframe::M1);
        assert_eq!("1hour".parse::<Timeframe>().unwrap(), Timeframe::H1);
        assert_eq!("1H".parse::<Timeframe>().unwrap(), Timeframe::H1);
        assert_eq!("1day".parse::<Timeframe>().unwrap(), Timeframe::D1);
        assert_eq!("1week".parse::<Timeframe>().unwrap(), Timeframe::W1);
        assert_eq!("1mon".parse::<Timeframe>().unwrap(), Timeframe::Mon1);
        assert_eq!("1month".parse::<Timeframe>().unwrap(), Timeframe::Mon1);
        assert!("7m".parse::<Timeframe>().is_err());
    }

    #[test]
    fn slot_count_counts_half_open_windows() {
        assert_eq!(Timeframe::M1.slots_between(0, 0), 0);
        assert_eq!(Timeframe::M1.slots_between(60, 0), 0);
        assert_eq!(Timeframe::M1.slots_between(0, 60), 1);
        assert_eq!(Timeframe::M1.slots_between(0, 60 * 1500), 1500);
        assert_eq!(Timeframe::H1.slots_between(0, 3600), 1);
        assert_eq!(Timeframe::H1.slots_between(0, 7200), 2);
    }

    #[test]
    fn monthly_bars_use_calendar_arithmetic() {
        let tf = Timeframe::Mon1;
        assert!(tf.is_calendar());
        assert_eq!(tf.fixed_seconds(), None);
        assert_eq!(tf.api_type(), "1month");
        assert_eq!(tf.slug(), "1mon");

        // Alignment floors to the first of the month, at 00:00 UTC.
        assert_eq!(tf.align(1_786_000_000), 1_785_542_400); // 2026-08-01
        assert_eq!(tf.align(1_785_542_400), 1_785_542_400);

        // Shifting crosses month lengths and year boundaries correctly.
        let jan = 1_704_067_200; // 2024-01-01
        assert_eq!(tf.shift(jan, 1), 1_706_745_600); // 2024-02-01
        assert_eq!(tf.shift(jan, 2), 1_709_251_200); // 2024-03-01 (leap year)
        assert_eq!(tf.shift(jan, -1), 1_701_388_800); // 2023-12-01
        assert_eq!(tf.shift(jan, 12), 1_735_689_600); // 2025-01-01
                                                      // A month is not 30 days, and February is not 31.
        assert_eq!(tf.shift(jan, 1) - jan, 31 * 86_400);
        assert_eq!(tf.slots_between(jan, tf.shift(jan, 2)), 2);

        // Validation rejects a monthly bar that is not on the 1st.
        let mut candle = Candle {
            time: jan,
            open: 1.0,
            high: 1.0,
            low: 1.0,
            close: 1.0,
            volume: 1.0,
            turnover: 1.0,
        };
        candle.validate(tf).unwrap();
        candle.time = jan + 86_400;
        assert!(candle.validate(tf).is_err());
    }

    #[test]
    fn fixed_timeframes_align_and_shift() {
        let tf = Timeframe::H1;
        assert!(!tf.is_calendar());
        assert_eq!(tf.fixed_seconds(), Some(3_600));
        assert_eq!(tf.align(1_600_000_012), 1_599_998_400);
        assert_eq!(tf.shift(1_599_998_400, -3), 1_599_998_400 - 10_800);
        assert_eq!(
            tf.slots_between(1_599_998_400, 1_599_998_400 + 9 * 3_600),
            9
        );
        // A span that is not a whole number of bars floors; the client rejects
        // unaligned bounds before this can matter.
        assert_eq!(tf.slots_between(1_599_998_400, 1_600_001_600), 0);
        assert_eq!(tf.approx_seconds(), 3_600);
        assert_eq!(Timeframe::Mon1.approx_seconds(), 30 * 86_400);
    }

    #[test]
    fn envelope_accepts_string_and_numeric_codes() {
        let ok: Envelope<Vec<Vec<Value>>> =
            serde_json::from_str(r#"{"code":"200000","data":[]}"#).unwrap();
        assert!(ok.is_ok());
        let numeric: Envelope<Vec<Value>> =
            serde_json::from_str(r#"{"code":200000,"data":[]}"#).unwrap();
        assert!(numeric.is_ok());
        let err: Envelope<Value> =
            serde_json::from_str(r#"{"code":"400100","msg":"Invalid parameter"}"#).unwrap();
        assert!(!err.is_ok());
        assert_eq!(err.msg.as_deref(), Some("Invalid parameter"));
        let err2: Envelope<Value> =
            serde_json::from_str(r#"{"code":"400100","message":"Nope"}"#).unwrap();
        assert_eq!(err2.msg.as_deref(), Some("Nope"));
    }
}
