//! Small helpers: time formatting/parsing, backoff, glob matching, hex.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, NaiveDate, TimeZone, Utc};

use crate::error::{Error, Result};

/// Current wall-clock time as unix seconds.
pub fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Highest multiple of `step` that is `<= ts` (floor division, works for negatives).
pub fn align_down(ts: i64, step: i64) -> i64 {
    debug_assert!(step > 0);
    ts.div_euclid(step) * step
}

/// Render a unix timestamp as `YYYY-MM-DD HH:MM:SS UTC`.
pub fn format_ts(ts: i64) -> String {
    match DateTime::from_timestamp(ts, 0) {
        Some(dt) => dt.format("%Y-%m-%d %H:%M:%S UTC").to_string(),
        None => format!("<invalid ts {ts}>"),
    }
}

/// Render a unix timestamp as `YYYY-MM-DD`.
pub fn format_date(ts: i64) -> String {
    match DateTime::from_timestamp(ts, 0) {
        Some(dt) => dt.format("%Y-%m-%d").to_string(),
        None => format!("<invalid ts {ts}>"),
    }
}

/// Unix seconds for `YYYY-MM-DD` (UTC).
pub fn date_to_unix(date: &str) -> Result<i64> {
    let d = NaiveDate::parse_from_str(date, "%Y-%m-%d")
        .map_err(|e| Error::Config(format!("invalid date `{date}`: {e}")))?;
    Ok(d.and_hms_opt(0, 0, 0)
        .expect("00:00:00 is valid")
        .and_utc()
        .timestamp())
}

/// Parse a `start` specification: `listing`, a unix timestamp, `YYYY-MM-DD`,
/// `YYYY-MM-DDTHH:MM:SSZ` or any RFC 3339 string.
///
/// Returns `None` for `listing` (meaning "scan back until the exchange stops
/// answering with candles").
pub fn parse_start_spec(spec: &str) -> Result<Option<i64>> {
    let spec = spec.trim();
    if spec.eq_ignore_ascii_case("listing") || spec.eq_ignore_ascii_case("earliest") {
        return Ok(None);
    }
    if spec.chars().all(|c| c.is_ascii_digit()) {
        let ts: i64 = spec
            .parse()
            .map_err(|e| Error::Config(format!("invalid unix timestamp `{spec}`: {e}")))?;
        return Ok(Some(ts));
    }
    if spec.len() == 10 {
        return Ok(Some(date_to_unix(spec)?));
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(spec) {
        return Ok(Some(dt.timestamp()));
    }
    if let Ok(ndt) = chrono::NaiveDateTime::parse_from_str(spec, "%Y-%m-%d %H:%M:%S") {
        return Ok(Some(Utc.from_utc_datetime(&ndt).timestamp()));
    }
    Err(Error::Config(format!(
        "cannot parse start spec `{spec}`; expected `listing`, unix seconds, `YYYY-MM-DD`, or RFC 3339"
    )))
}

/// Exponential backoff with full jitter, clamped to `max`.
///
/// `attempt` is zero-based: the first retry uses roughly `base`.
pub fn backoff_delay(base: Duration, factor: f64, attempt: u32, max: Duration) -> Duration {
    let exp = factor.powi(attempt.min(16) as i32);
    let raw = base.as_secs_f64() * exp;
    let capped = raw.min(max.as_secs_f64()).max(0.0);
    // Full jitter in [0.5, 1.5) scaled around the capped value keeps retries
    // from synchronising across concurrent workers without a `rand` dependency.
    let noise = pseudo_random_unit_f64();
    let jittered = capped * (0.5 + noise);
    Duration::from_secs_f64(jittered.min(max.as_secs_f64()).max(0.001))
}

/// Cheap xorshift-based uniform `[0, 1)` value seeded from the system clock.
fn pseudo_random_unit_f64() -> f64 {
    use std::cell::Cell;
    thread_local! {
        static STATE: Cell<u64> = const { Cell::new(0) };
    }
    STATE.with(|state| {
        let mut x = state.get();
        if x == 0 {
            x = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_nanos() as u64)
                .unwrap_or(0x2545_F491_4F6C_DD1D)
                | 1;
        }
        // xorshift64*
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        state.set(x);
        (x >> 11) as f64 / (1u64 << 53) as f64
    })
}

/// Lowercase hex encoding.
pub fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

/// Human readable byte size.
pub fn human_bytes(bytes: u64) -> String {
    const UNITS: [&str; 5] = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut value = bytes as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{bytes} B")
    } else {
        format!("{value:.1} {}", UNITS[unit])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn align_down_floors() {
        assert_eq!(align_down(1_700_000_012, 60), 1_699_999_980);
        assert_eq!(align_down(60, 60), 60);
        assert_eq!(align_down(0, 60), 0);
        assert_eq!(align_down(-1, 60), -60);
    }

    #[test]
    fn parses_start_specs() {
        assert_eq!(parse_start_spec("listing").unwrap(), None);
        assert_eq!(parse_start_spec("1500000000").unwrap(), Some(1_500_000_000));
        assert_eq!(parse_start_spec("2021-01-01").unwrap(), Some(1_609_459_200));
        assert_eq!(
            parse_start_spec("2021-01-01T00:00:00Z").unwrap(),
            Some(1_609_459_200)
        );
        assert!(parse_start_spec("nonsense").is_err());
    }

    #[test]
    fn backoff_grows_and_is_capped() {
        let base = Duration::from_millis(500);
        let max = Duration::from_secs(30);
        for attempt in 0..20u32 {
            let d = backoff_delay(base, 2.0, attempt, max);
            assert!(d <= max, "attempt {attempt} produced {d:?} > max");
            assert!(d >= Duration::from_millis(1));
        }
        let first = backoff_delay(base, 2.0, 0, max);
        let fifth = backoff_delay(base, 2.0, 5, max);
        assert!(fifth >= first);
    }

    #[test]
    fn hex_encoding() {
        assert_eq!(to_hex(&[0x00, 0x0f, 0xff]), "000fff");
        assert_eq!(to_hex(&[]), "");
    }
}
