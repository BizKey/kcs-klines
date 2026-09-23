//! Client-side, weight-based rate limiting for the KuCoin "Public" rate pool.
//!
//! KuCoin documents two things that matter here:
//!
//! * every endpoint consumes a *weight* from a shared pool (VIP0 public pool:
//!   `2000` weight per `30s`);
//! * `/api/v1/market/candles` weighs `3`.
//!
//! Rather than sleeping a fixed amount between calls, this limiter models the
//! pool as a continuously refilling token bucket and additionally honours the
//! `gw-ratelimit-remaining` / `gw-ratelimit-reset` response headers, so a busy
//! run slows down *before* it gets a `429`.

use std::time::Duration;

use tokio::sync::Mutex;
use tokio::time::Instant;

#[derive(Debug)]
struct State {
    tokens: f64,
    last_refill: Instant,
    next_allowed: Instant,
}

/// Token-bucket limiter shared by every worker of a run.
#[derive(Debug)]
pub struct RateLimiter {
    state: Mutex<State>,
    capacity: f64,
    window: Duration,
    min_interval: Duration,
    refill_per_sec: f64,
}

impl RateLimiter {
    /// `capacity` weight is refilled over `window`; consecutive requests are
    /// spaced by at least `min_interval`.
    pub fn new(capacity: f64, window: Duration, min_interval: Duration) -> Self {
        let capacity = capacity.max(1.0);
        let window = if window.is_zero() {
            Duration::from_secs(30)
        } else {
            window
        };
        let now = Instant::now();
        RateLimiter {
            state: Mutex::new(State {
                tokens: capacity,
                last_refill: now,
                next_allowed: now,
            }),
            capacity,
            window,
            min_interval,
            refill_per_sec: capacity / window.as_secs_f64(),
        }
    }

    /// Total weight per window.
    pub fn capacity(&self) -> f64 {
        self.capacity
    }

    /// Window over which the weight refills.
    pub fn window(&self) -> Duration {
        self.window
    }

    fn refill(state: &mut State, refill_per_sec: f64, capacity: f64, now: Instant) {
        let elapsed = now
            .saturating_duration_since(state.last_refill)
            .as_secs_f64();
        if elapsed > 0.0 {
            state.tokens = (state.tokens + elapsed * refill_per_sec).min(capacity);
            state.last_refill = now;
        }
    }

    /// Wait until `weight` may be spent, then spend it.
    pub async fn acquire(&self, weight: f64) {
        let weight = weight.clamp(0.0, self.capacity);
        loop {
            let wait = {
                let mut st = self.state.lock().await;
                let now = Instant::now();
                Self::refill(&mut st, self.refill_per_sec, self.capacity, now);
                if st.tokens >= weight && now >= st.next_allowed {
                    st.tokens -= weight;
                    let base = st.next_allowed.max(now);
                    st.next_allowed = base + self.min_interval;
                    return;
                }
                let mut wait = Duration::from_secs_f64(
                    ((weight - st.tokens).max(0.0) / self.refill_per_sec).max(0.001),
                );
                if st.next_allowed > now {
                    wait = wait.max(st.next_allowed - now);
                }
                wait
            };
            tokio::time::sleep(wait).await;
        }
    }

    /// React to a `429` (or an explicit `Retry-After`) by blocking every worker.
    pub async fn penalize(&self, delay: Duration) {
        let mut st = self.state.lock().await;
        let now = Instant::now();
        Self::refill(&mut st, self.refill_per_sec, self.capacity, now);
        st.tokens = 0.0;
        st.next_allowed = now + delay;
    }

    /// Feed back `gw-ratelimit-remaining` (weight) and `gw-ratelimit-reset` (ms).
    ///
    /// When the pool is nearly exhausted we proactively wait for the reset
    /// instead of walking into a `429`.
    pub async fn note_headers(&self, remaining: Option<f64>, reset: Option<Duration>) {
        let Some(remaining) = remaining else { return };
        let threshold = (self.capacity * 0.10).max(5.0);
        if remaining > threshold {
            return;
        }
        let mut st = self.state.lock().await;
        let now = Instant::now();
        Self::refill(&mut st, self.refill_per_sec, self.capacity, now);
        st.tokens = st.tokens.min(remaining.max(0.0));
        let pause = reset.unwrap_or_else(|| self.window / 10);
        if st.next_allowed < now + pause {
            st.next_allowed = now + pause;
        }
    }

    /// Weight currently available (test/diagnostic helper).
    pub async fn available(&self) -> f64 {
        let mut st = self.state.lock().await;
        let now = Instant::now();
        Self::refill(&mut st, self.refill_per_sec, self.capacity, now);
        st.tokens
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn spends_budget_then_waits_for_refill() {
        // 6 weight per second, no minimum spacing: 6 requests are free,
        // the 7th has to wait ~1s for a single token.
        let limiter = RateLimiter::new(6.0, Duration::from_secs(1), Duration::ZERO);
        let t0 = Instant::now();
        for _ in 0..6 {
            limiter.acquire(1.0).await;
        }
        assert!(
            t0.elapsed() < Duration::from_millis(1),
            "first 6 must be free"
        );

        limiter.acquire(1.0).await;
        let waited = t0.elapsed();
        assert!(
            waited >= Duration::from_millis(150),
            "7th acquire should wait for a refill, waited {waited:?}"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn respects_min_interval() {
        let limiter = RateLimiter::new(100.0, Duration::from_secs(1), Duration::from_millis(100));
        let t0 = Instant::now();
        limiter.acquire(3.0).await;
        limiter.acquire(3.0).await;
        assert!(t0.elapsed() >= Duration::from_millis(100));
    }

    #[tokio::test(start_paused = true)]
    async fn penalize_blocks_everyone() {
        let limiter = RateLimiter::new(2000.0, Duration::from_secs(30), Duration::ZERO);
        limiter.penalize(Duration::from_secs(5)).await;
        let t0 = Instant::now();
        limiter.acquire(3.0).await;
        assert!(t0.elapsed() >= Duration::from_secs(4));
    }

    #[tokio::test(start_paused = true)]
    async fn low_remaining_header_pauses_before_429() {
        let limiter = RateLimiter::new(2000.0, Duration::from_secs(30), Duration::ZERO);
        limiter
            .note_headers(Some(4.0), Some(Duration::from_secs(2)))
            .await;
        let t0 = Instant::now();
        limiter.acquire(3.0).await;
        assert!(t0.elapsed() >= Duration::from_secs(1));
    }

    #[tokio::test(start_paused = true)]
    async fn healthy_remaining_header_is_ignored() {
        let limiter = RateLimiter::new(2000.0, Duration::from_secs(30), Duration::ZERO);
        limiter
            .note_headers(Some(1900.0), Some(Duration::from_secs(30)))
            .await;
        let t0 = Instant::now();
        limiter.acquire(3.0).await;
        assert!(t0.elapsed() < Duration::from_millis(50));
    }

    #[tokio::test(start_paused = true)]
    async fn weight_refills_over_time() {
        let limiter = RateLimiter::new(30.0, Duration::from_secs(30), Duration::ZERO);
        limiter.acquire(30.0).await;
        assert!(limiter.available().await < 1.0);
        tokio::time::sleep(Duration::from_secs(15)).await;
        let half = limiter.available().await;
        assert!((10.0..=20.0).contains(&half), "expected ~15, got {half}");
    }
}
