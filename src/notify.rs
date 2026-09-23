//! Optional liveness pings (healthchecks.io and compatible services).
//!
//! A monthly job that silently stops running is worse than one that fails
//! loudly, so the collector can ping an external watchdog on start, success and
//! failure. Pings never fail the run — they are logged and ignored.

use std::time::Duration;

use crate::error::Result;

/// Ping `base` with an optional suffix (`/start`, `/fail`).
///
/// The empty suffix means "success" for healthchecks.io.
pub async fn ping(base: &str, suffix: &str) -> Result<()> {
    let base = base.trim();
    if base.is_empty() {
        return Ok(());
    }
    let url = format!("{}{}", base.trim_end_matches('/'), suffix);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .user_agent(concat!("kcs-klines/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let response = client.get(&url).send().await?;
    tracing::debug!(url = %url, status = response.status().as_u16(), "healthcheck ping sent");
    Ok(())
}

/// Ping and downgrade any failure to a warning.
pub async fn ping_quietly(base: &str, suffix: &str) {
    if let Err(e) = ping(base, suffix).await {
        tracing::warn!(error = %e, "healthcheck ping failed");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[tokio::test]
    async fn pings_the_expected_paths() {
        let server = MockServer::start().await;
        for p in ["/start", "/fail", "/done"] {
            Mock::given(method("GET"))
                .and(path(p))
                .respond_with(ResponseTemplate::new(200))
                .mount(&server)
                .await;
        }
        ping(&server.uri(), "/start").await.unwrap();
        ping(&server.uri(), "/fail").await.unwrap();
        ping(&server.uri(), "/done").await.unwrap();
    }

    #[tokio::test]
    async fn empty_url_is_a_noop() {
        ping("", "").await.unwrap();
        ping("   ", "/start").await.unwrap();
    }

    #[tokio::test]
    async fn failures_are_swallowed_by_the_quiet_variant() {
        // Nothing is listening on port 1.
        ping_quietly("http://127.0.0.1:1/hc", "/start").await;
    }
}
