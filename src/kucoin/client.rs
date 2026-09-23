//! Thin, purpose-built KuCoin **Spot** REST client.
//!
//! Deliberately hand-rolled instead of using a general exchange crate, because
//! the ingestion logic depends on three details that generic wrappers get wrong:
//!
//! 1. `/api/v1/market/candles` returns **100** candles when only one of
//!    `startAt`/`endAt` is given, and up to **1500** only when *both* are
//!    present. [`KucoinClient::candles_window`] therefore always sends both and
//!    refuses oversized windows instead of silently losing data.
//! 2. The response is ordered **newest first** and rows carry
//!    `[time, open, close, high, low, volume, turnover]`.
//! 3. Public market data needs **no API credentials**, so nothing here touches
//!    keys or signing — which keeps the collector read-only.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use reqwest::header::HeaderMap;
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::error::{Error, Result};
use crate::kucoin::rate_limit::RateLimiter;
use crate::kucoin::types::{Candle, Envelope, SymbolInfo, Timeframe};
use crate::util::backoff_delay;

/// Documented weight of `/api/v1/market/candles`.
pub const CANDLES_WEIGHT: f64 = 3.0;
/// Documented weight of `/api/v2/symbols`.
pub const SYMBOLS_WEIGHT: f64 = 10.0;
/// Documented weight of `/api/v1/timestamp`.
pub const TIMESTAMP_WEIGHT: f64 = 1.0;

/// Business codes KuCoin uses to signal rate limiting.
const RATE_LIMIT_CODES: [&str; 4] = ["429000", "429001", "429002", "400002"];

/// Retry behaviour for transient failures.
#[derive(Debug, Clone)]
pub struct RetryPolicy {
    /// Maximum number of *retries* (so up to `max_retries + 1` attempts).
    pub max_retries: u32,
    /// Base delay for the first retry.
    pub base_delay: Duration,
    /// Upper bound for any single backoff delay.
    pub max_delay: Duration,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        RetryPolicy {
            max_retries: 6,
            base_delay: Duration::from_millis(500),
            max_delay: Duration::from_secs(30),
        }
    }
}

/// Everything the client needs to talk to KuCoin.
#[derive(Debug, Clone)]
pub struct KucoinConfig {
    /// REST base URL, e.g. `https://api.kucoin.com`.
    pub base_url: String,
    /// Per-request timeout.
    pub timeout: Duration,
    /// Retry policy.
    pub retry: RetryPolicy,
    /// Public pool capacity (weight per `pool_window`).
    pub pool_capacity: f64,
    /// Public pool window.
    pub pool_window: Duration,
    /// Minimum spacing between two requests.
    pub min_interval: Duration,
}

impl Default for KucoinConfig {
    fn default() -> Self {
        KucoinConfig {
            base_url: "https://api.kucoin.com".to_string(),
            timeout: Duration::from_secs(30),
            retry: RetryPolicy::default(),
            pool_capacity: 2000.0,
            pool_window: Duration::from_secs(30),
            min_interval: Duration::from_millis(50),
        }
    }
}

/// KuCoin Spot market-data client (public endpoints only).
#[derive(Debug, Clone)]
pub struct KucoinClient {
    http: reqwest::Client,
    cfg: Arc<KucoinConfig>,
    limiter: Arc<RateLimiter>,
    requests: Arc<AtomicU64>,
    retries: Arc<AtomicU64>,
}

impl KucoinClient {
    /// Build a client plus its shared rate limiter.
    pub fn new(cfg: KucoinConfig) -> Result<Self> {
        let http = reqwest::Client::builder()
            .timeout(cfg.timeout)
            .connect_timeout(Duration::from_secs(10))
            .user_agent(concat!("kcs-klines/", env!("CARGO_PKG_VERSION")))
            .build()?;
        let limiter = Arc::new(RateLimiter::new(
            cfg.pool_capacity,
            cfg.pool_window,
            cfg.min_interval,
        ));
        Ok(KucoinClient {
            http,
            cfg: Arc::new(cfg),
            limiter,
            requests: Arc::new(AtomicU64::new(0)),
            retries: Arc::new(AtomicU64::new(0)),
        })
    }

    /// Base URL in use.
    pub fn base_url(&self) -> &str {
        &self.cfg.base_url
    }

    /// Number of HTTP requests actually sent (retries included).
    pub fn request_count(&self) -> u64 {
        self.requests.load(Ordering::Relaxed)
    }

    /// Number of retried requests.
    pub fn retry_count(&self) -> u64 {
        self.retries.load(Ordering::Relaxed)
    }

    /// The shared rate limiter (used by tests and diagnostics).
    pub fn limiter(&self) -> &Arc<RateLimiter> {
        &self.limiter
    }

    /// Exchange server time in unix seconds.
    pub async fn server_time(&self) -> Result<i64> {
        let env: Envelope<i64> = self
            .get_envelope("/api/v1/timestamp", &[], TIMESTAMP_WEIGHT)
            .await?;
        let ms = env
            .data
            .ok_or_else(|| Error::Data("/api/v1/timestamp returned no data".into()))?;
        Ok(if ms > 100_000_000_000 { ms / 1000 } else { ms })
    }

    /// All tradable symbols with their metadata.
    pub async fn symbols(&self) -> Result<Vec<SymbolInfo>> {
        let env: Envelope<Vec<SymbolInfo>> = self
            .get_envelope("/api/v2/symbols", &[], SYMBOLS_WEIGHT)
            .await?;
        env.data
            .ok_or_else(|| Error::Data("/api/v2/symbols returned no data".into()))
    }

    /// Fetch one window of candles covering `[from, to_exclusive)`.
    ///
    /// # KuCoin range semantics (measured, not assumed)
    ///
    /// For `/api/v1/market/candles` **`startAt` is inclusive and `endAt` is
    /// exclusive**: requesting `[a, a + N * interval)` returns exactly `N` bars,
    /// `a … a + (N-1) * interval`. Measured against the live API on
    /// 2021-01-29 (BTC-USDT, `1hour`, a stretch where every hourly bar exists):
    ///
    /// | slots requested | rows returned |
    /// |-----------------|---------------|
    /// | 1               | 1             |
    /// | 4               | 4             |
    /// | 1000            | 999           |
    /// | 1500            | 1499          |
    /// | 1600            | 1500 (the **oldest** bars are dropped) |
    ///
    /// Treating `endAt` as inclusive loses exactly one bar per window — 33 holes
    /// over a 34-window backfill — so this function takes an exclusive bound, and
    /// the collector walks adjacent windows that tile the range with no holes.
    ///
    /// Returns candles sorted ascending, all inside `[from, to_exclusive)`.
    pub async fn candles_window(
        &self,
        symbol: &str,
        tf: Timeframe,
        from: i64,
        to_exclusive: i64,
    ) -> Result<Vec<Candle>> {
        if to_exclusive <= from {
            return Ok(Vec::new());
        }
        // Both bounds must sit on the bar grid. For fixed-length timeframes that
        // also means the span is a whole number of bars; for the calendar month it
        // means both are month starts.
        if tf.align(from) != from || tf.align(to_exclusive) != to_exclusive {
            return Err(Error::Data(format!(
                "window {from}..{to_exclusive} is not aligned to the {} grid",
                tf.slug()
            )));
        }
        let slots = tf.slots_between(from, to_exclusive);
        if slots > tf.max_candles() as i64 {
            return Err(Error::Data(format!(
                "window {from}..{to_exclusive} covers {slots} {} bars, but KuCoin Spot returns at most {} per request; \
                 split the window (the API silently truncates to the newest bars, leaving a hole)",
                tf.slug(),
                tf.max_candles()
            )));
        }

        let params = [
            ("type", tf.api_type().to_string()),
            ("symbol", symbol.to_string()),
            ("startAt", from.to_string()),
            ("endAt", to_exclusive.to_string()),
        ];
        debug_assert_eq!(params.len(), 4, "startAt and endAt are both mandatory");

        let env: Envelope<Vec<Vec<Value>>> = self
            .get_envelope("/api/v1/market/candles", &params, CANDLES_WEIGHT)
            .await?;
        let rows = env.data.unwrap_or_default();

        let mut candles = Vec::with_capacity(rows.len());
        for row in &rows {
            match Candle::from_row(row) {
                Ok(c) => candles.push(c),
                Err(e) => tracing::warn!(
                    symbol,
                    timeframe = tf.slug(),
                    error = %e,
                    "skipping malformed candle row"
                ),
            }
        }
        // A stable sort keeps the result deterministic when the exchange repeats
        // a timestamp, and the first row it listed wins.
        candles.sort_by_key(|c| c.time);
        candles.dedup_by_key(|c| c.time);
        // Defensive: never let a stray bar outside the half-open window break the
        // tiling assumption the collector relies on.
        candles.retain(|c| c.time >= from && c.time < to_exclusive);
        Ok(candles)
    }

    /// GET a KuCoin endpoint and unwrap the response envelope, retrying
    /// transient failures with exponential backoff.
    async fn get_envelope<T: DeserializeOwned>(
        &self,
        path: &str,
        params: &[(&str, String)],
        weight: f64,
    ) -> Result<Envelope<T>> {
        let url = format!("{}{}", self.cfg.base_url, path);
        let max_attempts = self.cfg.retry.max_retries + 1;
        let mut last_error: Option<Error> = None;

        for attempt in 0..max_attempts {
            let is_last = attempt + 1 == max_attempts;
            if attempt > 0 {
                self.retries.fetch_add(1, Ordering::Relaxed);
            }

            self.limiter.acquire(weight).await;
            self.requests.fetch_add(1, Ordering::Relaxed);

            let mut request = self.http.get(&url);
            if !params.is_empty() {
                request = request.query(params);
            }

            let response = match request.send().await {
                Ok(r) => r,
                Err(e) => {
                    // Connection reset mid-body is a request error, not a
                    // transport error, and is worth retrying.
                    let transient =
                        e.is_timeout() || e.is_connect() || e.is_request() || e.is_body();
                    if transient && !is_last {
                        let delay = backoff_delay(
                            self.cfg.retry.base_delay,
                            2.0,
                            attempt,
                            self.cfg.retry.max_delay,
                        );
                        tracing::warn!(
                            path,
                            attempt = attempt + 1,
                            delay_ms = delay.as_millis() as u64,
                            error = %e,
                            "request failed, retrying"
                        );
                        last_error = Some(Error::Http(e));
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(Error::Http(e));
                }
            };

            let status = response.status();
            let retry_after = parse_retry_after(response.headers());
            let reset = parse_millis_header(response.headers(), "gw-ratelimit-reset");
            let remaining = parse_f64_header(response.headers(), "gw-ratelimit-remaining");
            self.limiter.note_headers(remaining, reset).await;

            if status.as_u16() == 429 {
                let delay = retry_after.unwrap_or_else(|| {
                    backoff_delay(
                        self.cfg.retry.base_delay,
                        2.0,
                        attempt,
                        self.cfg.retry.max_delay,
                    )
                });
                // Block every worker, not just this one: the pool is shared.
                self.limiter.penalize(delay).await;
                tracing::warn!(
                    path,
                    attempt = attempt + 1,
                    delay_ms = delay.as_millis() as u64,
                    "HTTP 429 from KuCoin, backing off"
                );
                last_error = Some(Error::Other(format!("HTTP 429 on {path}")));
                if is_last {
                    break;
                }
                continue;
            }

            if status.is_server_error() {
                let delay = backoff_delay(
                    self.cfg.retry.base_delay,
                    2.0,
                    attempt,
                    self.cfg.retry.max_delay,
                );
                tracing::warn!(
                    path,
                    status = status.as_u16(),
                    attempt = attempt + 1,
                    "server error from KuCoin, retrying"
                );
                last_error = Some(Error::Other(format!("HTTP {status} on {path}")));
                if is_last {
                    break;
                }
                tokio::time::sleep(delay).await;
                continue;
            }

            let body = response.text().await?;
            if !status.is_success() && status.as_u16() != 200 {
                return Err(Error::Other(format!(
                    "unexpected HTTP {status} on {path}: {}",
                    truncate(&body, 300)
                )));
            }

            let envelope: Envelope<T> = match serde_json::from_str(&body) {
                Ok(env) => env,
                Err(e) => {
                    return Err(Error::Data(format!(
                        "cannot parse KuCoin response from {path}: {e}; body={}",
                        truncate(&body, 300)
                    )))
                }
            };

            if envelope.is_ok() {
                return Ok(envelope);
            }

            let msg = envelope.msg.clone().unwrap_or_default();
            if RATE_LIMIT_CODES.contains(&envelope.code.as_str()) {
                let delay = retry_after.unwrap_or_else(|| {
                    backoff_delay(
                        self.cfg.retry.base_delay,
                        2.0,
                        attempt,
                        self.cfg.retry.max_delay,
                    )
                });
                self.limiter.penalize(delay).await;
                tracing::warn!(
                    path,
                    code = %envelope.code,
                    attempt = attempt + 1,
                    "KuCoin rate-limit code, backing off"
                );
                last_error = Some(Error::Api {
                    code: envelope.code,
                    message: msg,
                });
                if is_last {
                    break;
                }
                continue;
            }

            // Business errors are terminal: retrying will not help.
            return Err(Error::Api {
                code: envelope.code,
                message: msg,
            });
        }

        Err(Error::RetriesExhausted {
            attempts: max_attempts,
            last: last_error
                .map(|e| e.to_string())
                .unwrap_or_else(|| "unknown failure".to_string()),
        })
    }
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        let mut end = max;
        while end > 0 && !s.is_char_boundary(end) {
            end -= 1;
        }
        format!("{}…", &s[..end])
    }
}

fn header_str<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a str> {
    headers.get(name).and_then(|v| v.to_str().ok())
}

fn parse_f64_header(headers: &HeaderMap, name: &str) -> Option<f64> {
    header_str(headers, name)?.trim().parse::<f64>().ok()
}

fn parse_millis_header(headers: &HeaderMap, name: &str) -> Option<Duration> {
    let ms = header_str(headers, name)?.trim().parse::<f64>().ok()?;
    if ms <= 0.0 {
        return None;
    }
    Some(Duration::from_secs_f64(ms / 1000.0))
}

/// `Retry-After` in seconds (the delta-seconds form; HTTP-date is not used by KuCoin).
fn parse_retry_after(headers: &HeaderMap) -> Option<Duration> {
    let secs = header_str(headers, "retry-after")?
        .trim()
        .parse::<f64>()
        .ok()?;
    if secs <= 0.0 {
        return None;
    }
    Some(Duration::from_secs_f64(secs))
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::header::{HeaderMap, HeaderValue};

    #[test]
    fn parses_numeric_headers() {
        let mut h = HeaderMap::new();
        h.insert("gw-ratelimit-remaining", HeaderValue::from_static("1969"));
        h.insert("gw-ratelimit-reset", HeaderValue::from_static("7185"));
        h.insert("retry-after", HeaderValue::from_static("2"));
        assert_eq!(parse_f64_header(&h, "gw-ratelimit-remaining"), Some(1969.0));
        assert_eq!(
            parse_millis_header(&h, "gw-ratelimit-reset"),
            Some(Duration::from_millis(7185))
        );
        assert_eq!(parse_retry_after(&h), Some(Duration::from_secs(2)));
        assert_eq!(parse_f64_header(&h, "missing"), None);
        assert_eq!(parse_retry_after(&HeaderMap::new()), None);
    }

    #[test]
    fn truncate_is_char_safe() {
        assert_eq!(truncate("hello", 10), "hello");
        let s = "привет";
        let t = truncate(s, 5);
        assert!(t.ends_with('…'));
        assert!(t.len() <= 8);
    }
}
