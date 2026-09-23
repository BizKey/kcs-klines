//! Contract tests for the KuCoin REST client.
//!
//! These lock down the behaviours the whole collector depends on:
//!
//! * `/api/v1/market/candles` answers with **100** bars when only one of
//!   `startAt`/`endAt` is present, and with up to **1500** only when both are
//!   sent;
//! * `startAt` is **inclusive** and `endAt` is **exclusive** — measured against
//!   the live API and documented on `KucoinClient::candles_window`, and the
//!   reason the client takes a half-open window.
//!
//! A regression in either silently truncates history.
//!
//! Every fixture below uses timestamps that sit on the 1-minute grid: the client
//! refuses windows whose bounds are not bar labels, because that is the property
//! the collector's window tiling depends on.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use kcs_klines::kucoin::{KucoinClient, KucoinConfig, RetryPolicy, Timeframe};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, ResponseTemplate};

fn client(server: &MockServer, retry: RetryPolicy) -> KucoinClient {
    KucoinClient::new(KucoinConfig {
        base_url: server.uri(),
        timeout: Duration::from_secs(5),
        retry,
        pool_capacity: 2000.0,
        pool_window: Duration::from_secs(30),
        min_interval: Duration::ZERO,
    })
    .expect("client builds")
}

fn fast_retry() -> RetryPolicy {
    RetryPolicy {
        max_retries: 3,
        base_delay: Duration::from_millis(5),
        max_delay: Duration::from_millis(50),
    }
}

fn row(time: i64, price: f64) -> serde_json::Value {
    serde_json::json!([
        time.to_string(),
        price.to_string(),
        price.to_string(),
        (price + 1.0).to_string(),
        (price - 1.0).to_string(),
        "1.0",
        "10.0"
    ])
}

fn query_of(request: &Request) -> HashMap<String, String> {
    request
        .url
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect()
}

#[tokio::test]
async fn always_sends_both_start_and_end() {
    // The whole reason for this test: with only one bound KuCoin silently
    // returns 100 bars instead of 1500.
    let server = MockServer::start().await;
    let seen: Arc<std::sync::Mutex<Vec<HashMap<String, String>>>> =
        Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink = seen.clone();

    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(move |req: &Request| {
            sink.lock().unwrap().push(query_of(req));
            ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "code": "200000",
                "data": [row(1_599_999_960, 10.0)]
            }))
        })
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_910_080, 1_599_998_400)
        .await
        .unwrap();

    let calls = seen.lock().unwrap().clone();
    assert_eq!(calls.len(), 1);
    let q = &calls[0];
    assert_eq!(q.get("startAt").map(String::as_str), Some("1599910080"));
    assert_eq!(q.get("endAt").map(String::as_str), Some("1599998400"));
    assert_eq!(q.get("type").map(String::as_str), Some("1min"));
    assert_eq!(q.get("symbol").map(String::as_str), Some("BTC-USDT"));
}

#[tokio::test]
async fn candles_come_back_sorted_and_deduplicated() {
    let server = MockServer::start().await;
    // KuCoin answers newest first and may repeat a timestamp at the edge.
    let data = vec![
        row(1_600_000_080, 12.0),
        row(1_600_000_020, 11.0),
        row(1_600_000_020, 11.5),
        row(1_599_999_960, 10.0),
    ];
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"code": "200000", "data": data})),
        )
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let candles = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_140)
        .await
        .unwrap();

    assert_eq!(candles.len(), 3, "duplicate timestamps must collapse");
    assert_eq!(
        candles.iter().map(|c| c.time).collect::<Vec<_>>(),
        vec![1_599_999_960, 1_600_000_020, 1_600_000_080]
    );
    // The first row KuCoin listed for a duplicated timestamp wins, which keeps
    // the outcome independent of sort internals.
    assert_eq!(candles[1].close, 11.0);
}

#[tokio::test]
async fn oversized_windows_are_rejected_instead_of_truncated() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"code": "200000", "data": []})),
        )
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    // 1501 one-minute bars: one bar too many, so KuCoin would drop the oldest
    // and leave a hole in the middle of the requested range.
    let err = client
        .candles_window(
            "BTC-USDT",
            Timeframe::M1,
            1_599_999_960,
            1_599_999_960 + 1501 * 60,
        )
        .await
        .expect_err("must refuse");
    assert!(err.to_string().contains("1500"), "{err}");

    // Exactly 1500 slots is fine.
    client
        .candles_window(
            "BTC-USDT",
            Timeframe::M1,
            1_599_999_960,
            1_599_999_960 + 1500 * 60,
        )
        .await
        .expect("exactly the limit is allowed");

    // Windows whose bounds are not bar labels are refused as well: that signals an
    // off-by-one bug in the caller, which is exactly the failure mode that used to
    // lose one bar per window boundary.
    assert!(
        client
            .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_990, 1_600_000_050)
            .await
            .is_err(),
        "an unaligned `from` must be refused"
    );
    assert!(
        client
            .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_050)
            .await
            .is_err(),
        "a span that is not a whole number of bars must be refused"
    );
}

#[tokio::test]
async fn retries_on_429_then_succeeds() {
    let server = MockServer::start().await;
    let attempts = Arc::new(AtomicUsize::new(0));
    let counter = attempts.clone();

    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(move |_: &Request| {
            let n = counter.fetch_add(1, Ordering::SeqCst);
            if n == 0 {
                ResponseTemplate::new(429)
                    .insert_header("retry-after", "0")
                    .set_body_string("too many requests")
            } else {
                ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "code": "200000",
                    "data": [row(1_599_999_960, 10.0)]
                }))
            }
        })
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let candles = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_020)
        .await
        .unwrap();
    assert_eq!(candles.len(), 1);
    assert_eq!(attempts.load(Ordering::SeqCst), 2, "one retry was expected");
    assert_eq!(client.retry_count(), 1);
}

#[tokio::test]
async fn gives_up_after_the_retry_budget() {
    let server = MockServer::start().await;
    let attempts = Arc::new(AtomicUsize::new(0));
    let counter = attempts.clone();
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(move |_: &Request| {
            counter.fetch_add(1, Ordering::SeqCst);
            ResponseTemplate::new(503).set_body_string("upstream unavailable")
        })
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let err = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_020)
        .await
        .expect_err("must fail");
    assert!(
        matches!(err, kcs_klines::Error::RetriesExhausted { .. }),
        "unexpected error: {err:?}"
    );
    // max_retries = 3 means four attempts in total.
    assert_eq!(attempts.load(Ordering::SeqCst), 4);
}

#[tokio::test]
async fn retries_on_rate_limit_business_codes() {
    let server = MockServer::start().await;
    let attempts = Arc::new(AtomicUsize::new(0));
    let counter = attempts.clone();
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(move |_: &Request| {
            let n = counter.fetch_add(1, Ordering::SeqCst);
            if n == 0 {
                // KuCoin sometimes signals throttling inside the envelope.
                ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "code": "429000",
                    "msg": "Too many requests"
                }))
            } else {
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"code": "200000", "data": []}))
            }
        })
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let candles = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_020)
        .await
        .unwrap();
    assert!(candles.is_empty());
    assert_eq!(attempts.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn business_errors_are_not_retried() {
    let server = MockServer::start().await;
    let attempts = Arc::new(AtomicUsize::new(0));
    let counter = attempts.clone();
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(move |_: &Request| {
            counter.fetch_add(1, Ordering::SeqCst);
            ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "code": "400100",
                "msg": "Invalid parameter"
            }))
        })
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let err = client
        .candles_window("NOPE-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_020)
        .await
        .expect_err("must fail");
    match err {
        kcs_klines::Error::Api { code, message } => {
            assert_eq!(code, "400100");
            assert_eq!(message, "Invalid parameter");
        }
        other => panic!("unexpected error: {other:?}"),
    }
    assert_eq!(
        attempts.load(Ordering::SeqCst),
        1,
        "a permanent error must not be retried"
    );
}

#[tokio::test]
async fn malformed_rows_are_skipped_not_fatal() {
    let server = MockServer::start().await;
    let data = vec![
        serde_json::json!(["not-a-time", "1", "1", "1", "1", "1", "1"]),
        row(1_599_999_960, 10.0),
    ];
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"code": "200000", "data": data})),
        )
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let candles = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_020)
        .await
        .unwrap();
    assert_eq!(candles.len(), 1);
}

#[tokio::test]
async fn null_data_is_treated_as_an_empty_window() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/market/candles"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"code": "200000", "data": null})),
        )
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let candles = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_599_999_960, 1_600_000_020)
        .await
        .unwrap();
    assert!(candles.is_empty());
}

#[tokio::test]
async fn inverted_ranges_short_circuit() {
    let server = MockServer::start().await;
    // No mock is mounted: any request would fail the test loudly.
    let client = client(&server, fast_retry());
    let candles = client
        .candles_window("BTC-USDT", Timeframe::M1, 1_600_000_060, 1_599_999_960)
        .await
        .unwrap();
    assert!(candles.is_empty());
    assert_eq!(
        client.request_count(),
        0,
        "no request is needed for an empty range"
    );
}

#[tokio::test]
async fn server_time_is_normalised_to_seconds() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v1/timestamp"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"code": "200000", "data": 1_599_999_960_123i64})),
        )
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    assert_eq!(client.server_time().await.unwrap(), 1_599_999_960);
}

#[tokio::test]
async fn symbols_are_parsed_from_the_envelope() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/api/v2/symbols"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "code": "200000",
            "data": [{
                "symbol": "BTC-USDT",
                "baseCurrency": "BTC",
                "quoteCurrency": "USDT",
                "enableTrading": true,
                "market": "USDS",
                "st": false
            }]
        })))
        .mount(&server)
        .await;

    let client = client(&server, fast_retry());
    let symbols = client.symbols().await.unwrap();
    assert_eq!(symbols.len(), 1);
    assert_eq!(symbols[0].symbol, "BTC-USDT");
    assert_eq!(symbols[0].quote_currency, "USDT");
    assert!(symbols[0].enable_trading);
}
