//! Print the cost of borrowing each asset on KuCoin cross margin.
//!
//! KuCoin quotes borrow interest per currency in the signed endpoint
//! `GET /api/v3/margin/borrowRate?currency=X&isIsolated=false`, which answers with a
//! **daily** and an **hourly** rate plus the borrow limits. The list of currencies that
//! margin accepts is public (`GET /api/v1/margin/config`), so this binary needs a key only
//! for the rates themselves.
//!
//! Credentials come from the environment — `KC-API-KEY`, `KC-API-SECRET`,
//! `KC-API-PASSPHRASE` — and are never read from a file, written anywhere, or logged. The
//! passphrase is sent in its v2 form (HMAC of the passphrase with the secret), which is what
//! `KC-API-KEY-VERSION: 2` means.
//!
//!     cargo run --release --bin borrow-rates
//!     KC_API_BASE=http://127.0.0.1:8080 cargo run --bin borrow-rates   # tests / mocks

use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use clap::Parser;
use serde::Deserialize;

const DEFAULT_BASE: &str = "https://api.kucoin.com";
/// Cost of the borrow-rate endpoint is 20, the limit 3,000 a minute: one request every
/// 150 ms keeps even a hundred currencies well inside it.
const REQUEST_GAP: Duration = Duration::from_millis(150);

#[derive(Parser, Debug)]
#[command(
    name = "borrow-rates",
    about = "Print the current KuCoin cross-margin borrow rate for every borrowable asset",
    long_about = None
)]
struct Opts {
    /// API base URL (mock servers in tests, or a mirror).
    #[arg(long, env = "KC_API_BASE", default_value = DEFAULT_BASE)]
    base: String,
    /// Only show assets whose annualised rate is at least this percent (0 = all).
    #[arg(long, default_value_t = 0.0)]
    min_annual_percent: f64,
}

/// Authentication is a property of the **key**, not of one currency: when it fails, every
/// other request fails the same way, so it must stop the run instead of becoming a warning
/// that ends in "nothing could be read".
#[derive(Debug)]
struct AuthError(String);

impl std::fmt::Display for AuthError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for AuthError {}

#[derive(Debug, Clone)]
struct Credentials {
    key: String,
    secret: String,
    passphrase: String,
}

#[derive(Debug, Clone, PartialEq)]
struct BorrowRate {
    currency: String,
    hourly: f64,
    daily: f64,
    interest_free_limit: f64,
    borrow_limit_total: f64,
}

impl BorrowRate {
    /// Simple annualisation, not compound: KuCoin quotes a day rate and everyone comparing
    /// the cost of a loan to an APR says "day × 365". Hourly compounding is slightly more and
    /// is visible in `--json`-free form through the daily column, so the table stays honest
    /// about what it shows.
    fn annual_percent(&self) -> f64 {
        self.daily * 365.0 * 100.0
    }
}

/// Read the three credentials, reporting **all** missing names rather than the first.
fn credentials_from(lookup: impl Fn(&str) -> Option<String>) -> Result<Credentials> {
    let need = ["KC-API-KEY", "KC-API-SECRET", "KC-API-PASSPHRASE"];
    let mut missing = Vec::new();
    let mut got = Vec::new();
    for name in need {
        match lookup(name).filter(|value| !value.trim().is_empty()) {
            Some(value) => got.push(value),
            None => missing.push(name),
        }
    }
    if !missing.is_empty() {
        bail!(
            "missing credentials: {} — set them in the environment. A shell cannot assign a \
             name containing a hyphen (KC-API-KEY=x is a command, not an assignment), so the \
             underscore aliases KC_API_KEY / KC_API_SECRET / KC_API_PASSPHRASE work too, and \
             `env 'KC-API-KEY=x' borrow-rates` works for the hyphen form. The key needs the \
             Margin (cross) permission; the passphrase is the API passphrase, not your login one.",
            missing.join(", ")
        );
    }
    Ok(Credentials {
        key: got[0].clone(),
        secret: got[1].clone(),
        passphrase: got[2].clone(),
    })
}

fn hmac_b64(secret: &str, message: &str) -> String {
    // HMAC accepts keys of any length, so this can only fail on a broken hash impl.
    let mut mac = Hmac::<Sha256>::new_from_slice(secret.as_bytes())
        .expect("HMAC-SHA256 accepts keys of any length");
    mac.update(message.as_bytes());
    STANDARD.encode(mac.finalize().into_bytes())
}

/// `base64(HMAC-SHA256(secret, prehash))`, the KC-API-SIGN of API v2.
fn sign(secret: &str, prehash: &str) -> String {
    hmac_b64(secret, prehash)
}

/// v2 sends the passphrase as `base64(HMAC-SHA256(secret, passphrase))`, not the raw string.
fn passphrase_v2(secret: &str, passphrase: &str) -> String {
    hmac_b64(secret, passphrase)
}

/// A limit, printed without pretending it is money: big caps lose the decimals, small ones keep
/// them, and an asset with no limit gets a dash rather than a misleading zero.
fn limit_text(value: f64) -> String {
    if value <= 0.0 {
        return "-".to_string();
    }
    if value >= 1_000_000.0 {
        format!("{value:.0}")
    } else if value >= 1.0 {
        format!("{value:.2}")
    } else {
        format!("{value:.6}")
    }
}

/// Render the table: header, then rows by annual cost, worst first.
fn render(rows: &mut [BorrowRate], min_annual_percent: f64) -> String {
    rows.sort_by(|a, b| {
        b.annual_percent()
            .partial_cmp(&a.annual_percent())
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.currency.cmp(&b.currency))
    });
    let shown: Vec<&BorrowRate> = rows
        .iter()
        .filter(|row| row.annual_percent() >= min_annual_percent)
        .collect();
    let mut out = format!(
        "{:<10}{:>10}{:>12}{:>16}{:>18}\n",
        "asset", "%/day", "%/year", "free limit", "borrow limit"
    );
    for row in shown {
        out.push_str(&format!(
            "{:<10}{:>10.6}{:>12.2}{:>16}{:>18}\n",
            row.currency,
            row.daily * 100.0,
            row.annual_percent(),
            limit_text(row.interest_free_limit),
            limit_text(row.borrow_limit_total),
        ));
    }
    out
}

#[derive(Deserialize)]
struct ConfigReply {
    code: String,
    data: Option<ConfigData>,
}

#[derive(Deserialize)]
struct ConfigData {
    #[serde(rename = "currencyList")]
    currency_list: Vec<String>,
}

#[derive(Deserialize)]
struct RateReply {
    code: String,
    msg: Option<String>,
    data: Option<RateData>,
}

#[derive(Deserialize)]
struct RateData {
    #[serde(rename = "currentRateHourly")]
    hourly: String,
    #[serde(rename = "currentRateDaily")]
    daily: String,
    #[serde(rename = "interestFreeBorrowLimit", default)]
    interest_free_limit: Option<String>,
    #[serde(rename = "borrowLimitTotal", default)]
    borrow_limit_total: Option<String>,
}

fn parse_num(text: &Option<String>) -> f64 {
    text.as_deref()
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(0.0)
}

async fn currencies(client: &reqwest::Client, base: &str) -> Result<Vec<String>> {
    let url = format!("{base}/api/v1/margin/config");
    let reply: ConfigReply = client
        .get(&url)
        .send()
        .await
        .with_context(|| format!("request to {url} failed"))?
        .error_for_status()
        .with_context(|| format!("{url} returned an error status"))?
        .json()
        .await
        .with_context(|| format!("{url} did not return the expected JSON"))?;
    if reply.code != "200000" {
        bail!("{url} answered code {} instead of 200000", reply.code);
    }
    Ok(reply.data.map(|data| data.currency_list).unwrap_or_default())
}

async fn borrow_rate(
    client: &reqwest::Client,
    base: &str,
    creds: &Credentials,
    currency: &str,
) -> Result<BorrowRate> {
    let path = format!("/api/v3/margin/borrowRate?currency={currency}&isIsolated=false");
    let mut attempt = 0;
    loop {
        attempt += 1;
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .context("system clock is before the epoch")?
            .as_millis()
            .to_string();
        let signature = sign(&creds.secret, &format!("{timestamp}GET{path}"));
        let reply = client
            .get(format!("{base}{path}"))
            .header("KC-API-KEY", &creds.key)
            .header("KC-API-SIGN", signature)
            .header("KC-API-TIMESTAMP", &timestamp)
            .header("KC-API-PASSPHRASE", passphrase_v2(&creds.secret, &creds.passphrase))
            .header("KC-API-KEY-VERSION", "2")
            .send()
            .await;
        let reply = match reply {
            Ok(reply) => reply,
            Err(error) => return Err(anyhow!("{currency}: request to {base}{path} failed: {error}")),
        };
        let status = reply.status();
        let body: RateReply = reply
            .json()
            .await
            .with_context(|| format!("{currency}: response was not the expected JSON"))?;
        if status == reqwest::StatusCode::TOO_MANY_REQUESTS && attempt <= 3 {
            tokio::time::sleep(Duration::from_millis(500 * attempt as u64)).await;
            continue;
        }
        return match (body.code.as_str(), body.data) {
            ("200000", Some(data)) => Ok(BorrowRate {
                currency: currency.to_string(),
                hourly: data.hourly.parse().context("hourly rate is not a number")?,
                daily: data.daily.parse().context("daily rate is not a number")?,
                interest_free_limit: parse_num(&data.interest_free_limit),
                borrow_limit_total: parse_num(&data.borrow_limit_total),
            }),
            // KuCoin answers the whole authentication family with 400001-400007; all of
            // them mean "this key cannot be used", and all of them are fatal for the run.
            (code @ ("400001" | "400002" | "400003" | "400004" | "400005" | "400006" | "400007"), _) => {
                Err(anyhow::Error::new(AuthError(format!(
                    "{currency}: authentication failed ({code}: {}) — check \
                     KC-API-KEY / KC-API-SECRET / KC-API-PASSPHRASE (KC-API-KEY-VERSION 2) and that \
                     the key has the Margin (cross) permission; a key minted for a different \
                     KuCoin site also fails here",
                    body.msg.as_deref().unwrap_or("no message")
                ))))
            }
            (code, _) => bail!("{currency}: code {code} {}", body.msg.unwrap_or_default()),
        };
    }
}

/// The whole run, separated from `main` so the tests can drive it against a mock server.
async fn collect(opts: &Opts, creds: &Credentials) -> Result<String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
        .context("could not build the HTTP client")?;
    let names = currencies(&client, &opts.base).await?;
    if names.is_empty() {
        bail!(
            "the exchange returned an empty margin currency list — nothing to borrow right now \
             (or the endpoint changed)"
        );
    }
    let mut rows = Vec::with_capacity(names.len());
    let mut failed = 0usize;
    for name in &names {
        match borrow_rate(&client, &opts.base, creds, name).await {
            Ok(row) => rows.push(row),
            Err(error) => {
                if let Some(auth) = error.downcast_ref::<AuthError>() {
                    return Err(anyhow!("{auth}"));
                }
                failed += 1;
                eprintln!("warning: {error}");
            }
        }
        tokio::time::sleep(REQUEST_GAP).await;
    }
    if rows.is_empty() {
        bail!("no borrow rates could be read ({failed} assets failed)");
    }
    Ok(render(&mut rows, opts.min_annual_percent))
}

/// Read a credential under its documented name, or its underscore alias.
///
/// Bash cannot assign `KC-API-KEY=x` — a name with a hyphen is not an identifier, so that
/// line tries to run a command called `KC-API-KEY`. The alias is not a convenience: without it
/// the documented names are only settable through `env`, which is easy to get wrong.
fn env_credential(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .or_else(|| std::env::var(name.replace('-', "_")).ok())
}

#[tokio::main]
async fn main() -> Result<()> {
    let opts = Opts::parse();
    // A missing key is a usage error, not a runtime failure: say what to set and stop
    // before anything is sent, with the exit code scripts expect for "you ran it wrong".
    let creds = match credentials_from(env_credential) {
        Ok(creds) => creds,
        Err(error) => {
            eprintln!("error: {error}");
            std::process::exit(2);
        }
    };
    let table = collect(&opts, &creds).await?;
    print!("{table}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{header_exists, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[test]
    fn the_signature_matches_an_independently_computed_hmac() {
        // Computed with Python's hmac/base64 against the same inputs.
        let prehash = "1700000000000GET/api/v3/margin/borrowRate?currency=BTC&isIsolated=false";
        assert_eq!(sign("test-secret", prehash), "ANJA5laCC+fYm7ZjPH9T7iM88M8Sh89Iaw2mvAL2Gqk=");
    }

    #[test]
    fn the_v2_passphrase_is_a_hmac_not_the_raw_string() {
        assert_eq!(
            passphrase_v2("test-secret", "test-passphrase"),
            "UbgWiL7WdjQOVBl1OLuMgUbTl9VlKFsjFbLedtCDPrY="
        );
    }

    #[test]
    fn missing_credentials_are_all_named_and_nothing_is_sent() {
        let error = credentials_from(|_| None).unwrap_err().to_string();
        for name in ["KC-API-KEY", "KC-API-SECRET", "KC-API-PASSPHRASE"] {
            assert!(error.contains(name), "error must name {name}: {error}");
        }
        assert!(error.contains("Margin"), "error must say which permission is needed");
    }

    #[test]
    fn blank_credentials_count_as_missing() {
        let error = credentials_from(|name| {
            Some(if name == "KC-API-KEY" { "  ".to_string() } else { "value".to_string() })
        })
        .unwrap_err()
        .to_string();
        assert!(error.contains("KC-API-KEY"));
        assert!(!error.contains("KC-API-SECRET"));
    }

    #[test]
    fn rows_are_sorted_by_annual_cost_and_annualised_simply() {
        let mut rows = vec![
            BorrowRate { currency: "CHEAP".into(), hourly: 1e-6, daily: 0.00001, interest_free_limit: 0.0, borrow_limit_total: 100.0 },
            BorrowRate { currency: "PRICEY".into(), hourly: 1e-4, daily: 0.001, interest_free_limit: 0.0, borrow_limit_total: 10.0 },
            BorrowRate { currency: "MID".into(), hourly: 1e-5, daily: 0.0001, interest_free_limit: 1.0, borrow_limit_total: 50.0 },
        ];
        let table = render(&mut rows, 0.0);
        let pricey = table.find("PRICEY").expect("PRICEY in the table");
        let mid = table.find("MID").expect("MID in the table");
        let cheap = table.find("CHEAP").expect("CHEAP in the table");
        assert!(pricey < mid && mid < cheap, "table not sorted by annual cost:\n{table}");
        // 0.001/day is 36.5%/year, and the column says so.
        let line = table.lines().find(|line| line.contains("PRICEY")).unwrap();
        assert!(line.contains("36.50"), "annual column wrong: {line}");
    }

    #[test]
    fn the_minimum_filter_drops_cheap_assets() {
        let mut rows = vec![
            BorrowRate { currency: "CHEAP".into(), hourly: 0.0, daily: 0.0, interest_free_limit: 0.0, borrow_limit_total: 1.0 },
            BorrowRate { currency: "PRICEY".into(), hourly: 0.0, daily: 0.001, interest_free_limit: 0.0, borrow_limit_total: 1.0 },
        ];
        let table = render(&mut rows, 1.0);
        assert!(table.contains("PRICEY"));
        assert!(!table.contains("CHEAP"));
    }

    #[tokio::test]
    async fn the_run_lists_public_currencies_and_asks_the_signed_endpoint_for_each() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/margin/config"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "code": "200000",
                "data": { "currencyList": ["BTC", "ETH"] }
            })))
            .mount(&server)
            .await;
        for (currency, daily) in [("BTC", "0.00008466"), ("ETH", "0.0002")] {
            Mock::given(method("GET"))
                .and(path("/api/v3/margin/borrowRate"))
                .and(query_param("currency", currency))
                .and(query_param("isIsolated", "false"))
                .and(header_exists("KC-API-KEY"))
                .and(header_exists("KC-API-SIGN"))
                .and(header_exists("KC-API-TIMESTAMP"))
                .and(header_exists("KC-API-PASSPHRASE"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "code": "200000",
                    "data": {
                        "currentRateHourly": "0.00000353",
                        "currentRateDaily": daily,
                        "borrowLimitTotal": "600.00000000",
                        "interestFreeBorrowLimit": "0.60000000"
                    }
                })))
                .mount(&server)
                .await;
        }
        let opts = Opts { base: server.uri(), min_annual_percent: 0.0 };
        let creds = Credentials { key: "k".into(), secret: "s".into(), passphrase: "p".into() };
        let table = collect(&opts, &creds).await.expect("run against the mock");
        // ETH at 0.02%/day is dearer than BTC at 0.0085%/day, so it comes first.
        let eth = table.find("ETH").expect("ETH row");
        let btc = table.find("BTC").expect("BTC row");
        assert!(eth < btc, "rows not sorted by cost:\n{table}");
        assert!(table.contains("7.30"), "ETH annual should be 7.30%:\n{table}");
    }

    /// The live run once answered every currency with 400003 ("API key does not exist"), which
    /// this program reported as 65 warnings and then "no borrow rates could be read" — the one
    /// thing the user cannot act on. The whole 400001-400007 family means "this key is
    /// unusable", so any of them must stop the run with a permission hint.
    #[tokio::test]
    async fn any_authentication_code_stops_the_run_with_a_permission_hint() {
        for code in ["400001", "400002", "400003", "400004", "400005", "400006", "400007"] {
            let server = MockServer::start().await;
            Mock::given(method("GET"))
                .and(path("/api/v1/margin/config"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "code": "200000", "data": { "currencyList": ["BTC", "ETH"] }
                })))
                .mount(&server)
                .await;
            Mock::given(method("GET"))
                .and(path("/api/v3/margin/borrowRate"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "code": code, "msg": "The API key does not exist or site mismatch."
                })))
                .mount(&server)
                .await;
            let opts = Opts { base: server.uri(), min_annual_percent: 0.0 };
            let creds = Credentials { key: "k".into(), secret: "s".into(), passphrase: "p".into() };
            let error = collect(&opts, &creds).await.unwrap_err().to_string();
            assert!(error.contains("authentication failed"), "{code} got: {error}");
            assert!(error.contains(code), "{code} must be named: {error}");
            assert!(error.contains("Margin"), "{code} must point at the permission: {error}");
            // It must stop at the first answer, not warn once per currency.
            assert!(!error.contains("could be read"), "{code} fell through to the generic error");
        }
    }

    #[tokio::test]
    async fn an_empty_currency_list_is_said_out_loud() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/api/v1/margin/config"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "code": "200000", "data": { "currencyList": [] }
            })))
            .mount(&server)
            .await;
        let opts = Opts { base: server.uri(), min_annual_percent: 0.0 };
        let creds = Credentials { key: "k".into(), secret: "s".into(), passphrase: "p".into() };
        let error = collect(&opts, &creds).await.unwrap_err().to_string();
        assert!(error.contains("empty"), "got: {error}");
    }
}
