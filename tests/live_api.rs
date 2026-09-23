//! Tests that talk to the real KuCoin API.
//!
//! They are `#[ignore]`d by default so `cargo test` stays hermetic and fast:
//!
//! ```text
//! cargo test --test live_api -- --ignored --nocapture
//! ```
//!
//! They exist to catch the failures a mock cannot: changed field order, changed
//! response shape, tightened rate limits, or a spot endpoint that starts
//! honouring only one of `startAt`/`endAt`.

use std::time::Duration;

use kcs_klines::kucoin::{KucoinClient, KucoinConfig, Timeframe};

fn live_client() -> KucoinClient {
    KucoinClient::new(KucoinConfig {
        base_url: std::env::var("KCS_BASE_URL")
            .unwrap_or_else(|_| "https://api.kucoin.com".to_string()),
        timeout: Duration::from_secs(30),
        ..Default::default()
    })
    .expect("client builds")
}

#[tokio::test]
#[ignore = "hits the real KuCoin API"]
async fn server_time_is_recent() {
    let client = live_client();
    let now = client.server_time().await.expect("server time");
    let local = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    assert!(
        (now - local).abs() < 120,
        "exchange time {now} is far from local time {local}"
    );
}

#[tokio::test]
#[ignore = "hits the real KuCoin API"]
async fn a_full_window_returns_the_documented_maximum() {
    let client = live_client();
    let now = client.server_time().await.expect("server time");
    let tf = Timeframe::M1;
    // `endAt` is exclusive, so a full window is `1500` slots wide.
    let end_exclusive = tf.align(now);
    let start = tf.shift(end_exclusive, -(tf.max_candles() as i64));
    let candles = client
        .candles_window("BTC-USDT", tf, start, end_exclusive)
        .await
        .expect("candles");

    assert_eq!(
        candles.len(),
        tf.max_candles(),
        "a dense 1500-slot window must come back full; sending both startAt and \\
         endAt is what unlocks it, and `endAt` is exclusive"
    );
    // Sorted ascending, aligned, and OHLC-consistent.
    for pair in candles.windows(2) {
        assert!(pair[0].time < pair[1].time, "timestamps must be ascending");
    }
    for candle in &candles {
        candle.validate(tf).expect("valid candle");
    }
}

#[tokio::test]
#[ignore = "hits the real KuCoin API"]
async fn deep_history_is_reachable_by_paging_backwards() {
    let client = live_client();
    // 2017-10-19, the first day BTC-USDT traded on KuCoin.
    let ancient = 1_508_371_200i64;
    let tf = Timeframe::D1;
    let candles = client
        .candles_window("BTC-USDT", tf, ancient, ancient + 11 * 86_400)
        .await
        .expect("candles");
    assert!(
        !candles.is_empty(),
        "expected daily candles around the listing date"
    );
    assert!(candles[0].time >= ancient);
    assert!(candles[0].time < ancient + 30 * 86_400);
}

#[tokio::test]
#[ignore = "hits the real KuCoin API"]
async fn weekly_candles_sit_on_the_epoch_grid() {
    // `Candle::validate` enforces alignment, and the collector relies on it, so
    // weekly bars really must be multiples of 604800 seconds.
    let client = live_client();
    let now = client.server_time().await.expect("server time");
    let tf = Timeframe::W1;
    let end_exclusive = tf.shift(tf.align(now), 1);
    let start = tf.shift(end_exclusive, -50);
    let candles = client
        .candles_window("BTC-USDT", tf, start, end_exclusive)
        .await
        .expect("candles");
    assert!(!candles.is_empty(), "expected weekly candles");
    for candle in &candles {
        candle.validate(tf).expect("weekly candle must validate");
    }
}

#[tokio::test]
#[ignore = "hits the real KuCoin API"]
async fn monthly_candles_sit_on_the_calendar_grid() {
    // Monthly bars are labelled with the 1st of the month at 00:00 UTC and are
    // 28-31 days long, so `validate` uses calendar alignment, not `time % n`.
    let client = live_client();
    let now = client.server_time().await.expect("server time");
    let tf = Timeframe::Mon1;
    let end_exclusive = tf.shift(tf.align(now), 1);
    let start = tf.shift(end_exclusive, -60);
    let candles = client
        .candles_window("BTC-USDT", tf, start, end_exclusive)
        .await
        .expect("candles");

    assert_eq!(
        candles.len(),
        60,
        "a 60-slot monthly window must come back full"
    );
    for candle in &candles {
        candle.validate(tf).expect("monthly candle must validate");
    }
    for pair in candles.windows(2) {
        assert_eq!(
            pair[1].time,
            tf.shift(pair[0].time, 1),
            "consecutive month labels must be exactly one month apart"
        );
    }
}

#[tokio::test]
#[ignore = "hits the real KuCoin API"]
async fn symbol_listing_works() {
    let client = live_client();
    let symbols = client.symbols().await.expect("symbols");
    assert!(symbols.len() > 100, "only {} symbols", symbols.len());
    assert!(symbols.iter().any(|s| s.symbol == "BTC-USDT"));
}
