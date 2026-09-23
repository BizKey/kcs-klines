//! The collection engine: resumable backfill and incremental update.
//!
//! # How a scan works
//!
//! KuCoin hands out candles newest-first and only honours `startAt` together
//! with `endAt`, so history is walked **backwards** one window at a time
//! (a window is at most 1500 bars). While walking, the engine
//!
//! * **jumps over already-covered periods for free** — the coverage of every
//!   Parquet file is read from its footer statistics, so a second run makes a
//!   handful of requests instead of thousands;
//! * **stops at the real start of history**: once a window comes back empty
//!   (and deeper probes confirm nothing older exists) the pair's listing date
//!   has been found and is cached in [`crate::state`];
//! * **flushes complete periods to disk as it goes**, so an interrupted run
//!   keeps everything it already fetched and the next run resumes from there.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::config::Config;
use crate::error::Result;
use crate::kucoin::{Candle, KucoinClient, Timeframe};
use crate::state::StateStore;
use crate::storage::{parquet_store, Period, Store};
use crate::util::{format_ts, now_unix};

/// KuCoin did not exist before this instant (2010-01-01T00:00:00Z); the bound
/// also prevents runaway backward scans.
pub const MIN_TIMESTAMP: i64 = 1_262_304_000;

/// How many invalid candles are logged individually before logging switches to a
/// single summary line.
const MAX_LOGGED_INVALID: u64 = 5;

/// Outcome of collecting one `(symbol, timeframe)` series.
#[derive(Debug, Clone)]
pub struct SeriesReport {
    /// Trading pair.
    pub symbol: String,
    /// Timeframe.
    pub timeframe: Timeframe,
    /// HTTP requests performed.
    pub requests: u64,
    /// Requests that needed a retry.
    pub retries: u64,
    /// Windows fetched (excluding probes).
    pub windows: u64,
    /// Windows that came back empty.
    pub empty_windows: u64,
    /// Deep probes used to confirm the start of history.
    pub probes: u64,
    /// Periods skipped because the local file already covered them.
    pub periods_skipped: u64,
    /// Candles returned by the exchange.
    pub candles_fetched: u64,
    /// Candles rejected by validation.
    pub candles_invalid: u64,
    /// Bars that were not on disk before.
    pub rows_added: u64,
    /// Bars whose values were refreshed.
    pub rows_refreshed: u64,
    /// Net change in row count.
    pub rows_net: i64,
    /// Files rewritten.
    pub files_written: u64,
    /// Files that needed no rewrite.
    pub files_unchanged: u64,
    /// Oldest bar stored after the run.
    pub first_candle: Option<i64>,
    /// Newest bar stored after the run.
    pub last_candle: Option<i64>,
    /// Start of history (configured, cached or discovered).
    pub history_start: Option<i64>,
    /// Wall-clock duration.
    pub elapsed: Duration,
    /// True when nothing was written.
    pub dry_run: bool,
}

impl SeriesReport {
    fn new(symbol: &str, tf: Timeframe, dry_run: bool) -> Self {
        SeriesReport {
            symbol: symbol.to_string(),
            timeframe: tf,
            requests: 0,
            retries: 0,
            windows: 0,
            empty_windows: 0,
            probes: 0,
            periods_skipped: 0,
            candles_fetched: 0,
            candles_invalid: 0,
            rows_added: 0,
            rows_refreshed: 0,
            rows_net: 0,
            files_written: 0,
            files_unchanged: 0,
            first_candle: None,
            last_candle: None,
            history_start: None,
            elapsed: Duration::ZERO,
            dry_run,
        }
    }

    /// One-line summary for logs.
    pub fn summary(&self) -> String {
        let range = match (self.first_candle, self.last_candle) {
            (Some(a), Some(b)) => format!("{} .. {}", format_ts(a), format_ts(b)),
            _ => "no data".to_string(),
        };
        format!(
            "{} {} +{} new / {} refreshed (net {:+}), {} windows ({} empty), {} requests, {} period jumps, {:.1}s, {}{}",
            self.symbol,
            self.timeframe.slug(),
            self.rows_added,
            self.rows_refreshed,
            self.rows_net,
            self.windows,
            self.empty_windows,
            self.requests,
            self.periods_skipped,
            self.elapsed.as_secs_f64(),
            range,
            if self.dry_run { ", DRY RUN" } else { "" }
        )
    }
}

/// Aggregate report of a whole run.
#[derive(Debug, Clone, Default)]
pub struct RunReport {
    /// Per-series results.
    pub series: Vec<SeriesReport>,
    /// Series that failed, with their error message.
    pub failures: Vec<(String, Timeframe, String)>,
    /// Total wall-clock duration.
    pub elapsed: Duration,
}

impl RunReport {
    /// Total bars added across every series.
    pub fn total_rows_added(&self) -> u64 {
        self.series.iter().map(|s| s.rows_added).sum()
    }

    /// Total requests performed.
    pub fn total_requests(&self) -> u64 {
        self.series.iter().map(|s| s.requests).sum()
    }

    /// True when nothing failed. Skipped series are not failures.
    pub fn is_success(&self) -> bool {
        self.failures.is_empty()
    }

    /// Human readable multi-line summary.
    pub fn render(&self) -> String {
        let mut out = String::new();
        for s in &self.series {
            out.push_str("  ");
            out.push_str(&s.summary());
            out.push('\n');
        }
        for (symbol, tf, err) in &self.failures {
            out.push_str(&format!("  {symbol} {} FAILED: {err}\n", tf.slug()));
        }
        out.push_str(&format!(
            "  totals: {} series, {} failures, {} new bars, {} requests, {:.1}s",
            self.series.len(),
            self.failures.len(),
            self.total_rows_added(),
            self.total_requests(),
            self.elapsed.as_secs_f64()
        ));
        out
    }
}

/// Collects series using a shared client and store.
#[derive(Debug, Clone)]
pub struct Collector {
    client: Arc<KucoinClient>,
    store: Store,
    state: StateStore,
    cfg: Arc<Config>,
    dry_run: bool,
    /// Memoised exchange clock (see [`Collector::exchange_now`]).
    exchange_time: Arc<tokio::sync::OnceCell<i64>>,
}

impl Collector {
    /// Build a collector.
    pub fn new(
        client: Arc<KucoinClient>,
        store: Store,
        state: StateStore,
        cfg: Arc<Config>,
    ) -> Self {
        Collector {
            client,
            store,
            state,
            cfg,
            dry_run: false,
            exchange_time: Arc::new(tokio::sync::OnceCell::new()),
        }
    }

    /// Return a collector that fetches and reports but never writes.
    pub fn with_dry_run(mut self, dry_run: bool) -> Self {
        self.dry_run = dry_run;
        self
    }

    /// Shared KuCoin client.
    pub fn client(&self) -> &Arc<KucoinClient> {
        &self.client
    }

    /// Store in use.
    pub fn store(&self) -> &Store {
        &self.store
    }

    /// State store in use.
    pub fn state_store(&self) -> &StateStore {
        &self.state
    }

    /// Exchange time, falling back to the local clock.
    ///
    /// Read once per collector and cached: with `symbols = []` a run covers
    /// thousands of series, and one clock request each would both waste thousands
    /// of calls and walk straight into KuCoin's rate limiter. A clock that is a
    /// few hours stale only moves the newest bar of the run, which the next run
    /// picks up (and the overlap mechanism refreshes).
    pub async fn exchange_now(&self) -> i64 {
        *self
            .exchange_time
            .get_or_init(|| async {
                match self.client.server_time().await {
                    Ok(ts) => ts,
                    Err(e) => {
                        tracing::warn!(error = %e, "cannot read exchange time, using the local clock");
                        now_unix()
                    }
                }
            })
            .await
    }

    /// Resolve the symbol list: CLI overrides, then the config list, then every
    /// pair listed by `/api/v2/symbols` (no filtering of any kind).
    pub async fn resolve_symbols(&self, overrides: &[String]) -> Result<Vec<String>> {
        let mut symbols: Vec<String> = if !overrides.is_empty() {
            overrides.to_vec()
        } else if !self.cfg.collection.symbols.is_empty() {
            self.cfg.collection.symbols.clone()
        } else {
            let all = self.client.symbols().await?;
            tracing::info!(count = all.len(), "discovered symbols from /api/v2/symbols");
            all.into_iter().map(|s| s.symbol).collect()
        };
        symbols.sort();
        symbols.dedup();
        Ok(symbols)
    }

    /// Collect one series.
    /// Collect one series, resuming where the previous run stopped.
    ///
    /// # Completeness rule
    ///
    /// A bar is written only once **its own period has finished** — for `1mon` that
    /// is a calendar month, for `1h` an hour, for `1m` a minute. `Timeframe::align`
    /// is exactly that boundary, so the rule is the same for every timeframe: the
    /// scan stops at the start of the current bar, and a still-forming bar is never
    /// stored.
    ///
    /// The archive therefore always ends on a *complete* bar, which is what makes
    /// resuming trivial: the next run continues right after the last stored bar, and
    /// everything already covered costs no request at all.
    pub async fn collect_series(&self, symbol: &str, tf: Timeframe) -> Result<SeriesReport> {
        let started = Instant::now();
        let limit = tf.max_candles() as i64;
        let overlap = self.cfg.collection.overlap_candles.max(0);
        let max_empty = self.cfg.collection.max_empty_windows.max(1);

        let mut report = SeriesReport::new(symbol, tf, self.dry_run);
        let mut covered = self.store.coverage_by_period(symbol, tf)?;
        let mut state = self.state.load(symbol, tf)?;

        let now = self.exchange_now().await;
        // Because `endAt` is exclusive, an upper bound of `X` means "every bar
        // strictly older than X", and `tf.align(now)` is the start of the bar that
        // is still forming — so this bound keeps only finished bars.
        let end_exclusive = tf.align(now).max(MIN_TIMESTAMP);
        let newest_stored = covered.values().map(|(_, hi)| *hi).max();
        match newest_stored {
            Some(last) => tracing::debug!(
                symbol,
                timeframe = tf.slug(),
                last_stored = %format_ts(last),
                end_exclusive = %format_ts(end_exclusive),
                "resuming right after the last stored bar"
            ),
            None => tracing::debug!(
                symbol,
                timeframe = tf.slug(),
                end_exclusive = %format_ts(end_exclusive),
                "no local data yet, starting from scratch"
            ),
        }
        if end_exclusive <= MIN_TIMESTAMP {
            report.elapsed = started.elapsed();
            return Ok(report);
        }

        // `retries` is a run-wide counter (the client is shared by every worker),
        // so it is reported as a delta rather than as a per-series figure.
        let retries_before = self.client.retry_count();

        // Lower bound of the scan, inclusive: no bar older than this is needed.
        let configured = self.cfg.collection.start_spec()?;
        let mut discovery = false;
        let mut scan_lo: Option<i64> = match (configured, state.history_start) {
            (Some(ts), _) => Some(tf.align(ts).max(MIN_TIMESTAMP)),
            (None, Some(ts)) => Some(tf.align(ts).max(MIN_TIMESTAMP)),
            (None, None) => {
                discovery = true;
                None
            }
        };
        // The cache is only a claim about the exchange, and the files can disprove
        // it: bars sitting *older* than the cached floor mean the claim is simply
        // wrong. In that case drop it and probe again, so a stale cache can never
        // hide data that exists upstream.
        let oldest_on_disk = covered.values().map(|(lo, _)| *lo).min();
        if let (Some(cached), Some(on_disk)) = (scan_lo, oldest_on_disk) {
            if on_disk < cached {
                tracing::debug!(
                    symbol,
                    timeframe = tf.slug(),
                    cached_floor = %format_ts(cached),
                    oldest_on_disk = %format_ts(on_disk),
                    "the cached history floor is stale, probing again"
                );
                scan_lo = configured.map(|ts| tf.align(ts).max(MIN_TIMESTAMP));
                discovery = scan_lo.is_none();
            }
        }

        // Find where this pair's history actually begins. Walking **daily**
        // candles (1500 days per request, so ~4 calls for a decade) gives a far
        // better floor than guessing from empty intraday windows, and it lets the
        // main scan run strictly linearly down to that floor — so a long
        // delisting gap can never hide older data from us.
        if discovery {
            match self.discover_history_start(symbol, &mut report).await {
                Ok(Some(first)) => {
                    // Align the daily floor to the target timeframe's grid, which
                    // for the calendar month means the 1st of that month.
                    let floor = tf.align(first).max(MIN_TIMESTAMP);
                    tracing::info!(
                        symbol,
                        timeframe = tf.slug(),
                        history_start = %format_ts(floor),
                        "history floor discovered from daily candles"
                    );
                    scan_lo = Some(floor);
                    discovery = false;
                }
                Ok(None) => tracing::warn!(
                    symbol,
                    "no daily history available; falling back to empty-window probing"
                ),
                Err(e) => tracing::warn!(
                    symbol,
                    error = %e,
                    "daily history probe failed; falling back to empty-window probing"
                ),
            }
        }

        tracing::debug!(
            symbol,
            timeframe = tf.slug(),
            end_exclusive = %format_ts(end_exclusive),
            scan_lo = scan_lo.map(format_ts).unwrap_or_else(|| "<discover>".into()),
            covered_periods = covered.len(),
            "starting series scan"
        );

        let mut buffers: BTreeMap<Period, Vec<Candle>> = BTreeMap::new();
        // `cursor` is always the *exclusive* upper bound of the next window.
        let mut cursor = end_exclusive;
        let mut empties: u32 = 0;

        loop {
            if cursor <= MIN_TIMESTAMP {
                break;
            }
            if let Some(lo) = scan_lo {
                if cursor <= lo {
                    break;
                }
            }

            // The newest bar this window can deliver, and the period it belongs to.
            let newest_wanted = tf.shift(cursor, -1);
            let period = self.store.period_of(newest_wanted, tf);
            // A period starts on a calendar boundary (Jan 1), which is not
            // necessarily a bar label — a weekly bar, for instance, is labelled
            // with a Thursday. Floor it so the window bounds stay on the grid.
            let needed_lo =
                tf.align(scan_lo.map_or_else(|| period.start(), |lo| lo.max(period.start())));

            // Free jump: the file already holds everything this period still needs,
            // so not a single request is spent on it.
            let mut resume_from: Option<i64> = None;
            if let Some((c_lo, c_hi)) = covered.get(&period).copied() {
                if c_lo <= needed_lo && c_hi >= newest_wanted {
                    self.flush_ready(
                        symbol,
                        tf,
                        &mut buffers,
                        needed_lo,
                        &mut covered,
                        &mut report,
                    )?;
                    report.periods_skipped += 1;
                    cursor = needed_lo;
                    empties = 0;
                    continue;
                }
                if c_lo <= needed_lo {
                    // The head of this period is on disk and only the tail is
                    // missing, so continue right after the last stored bar instead of
                    // re-fetching the whole period; `overlap` re-reads the last stored
                    // bars so a late exchange correction is still picked up.
                    resume_from = Some(tf.shift(c_hi, -(overlap - 1).max(0)).max(needed_lo));
                } else if c_hi >= newest_wanted && cursor > c_lo {
                    // The opposite case: the tail is on disk but the head is not — a
                    // pair that only started trading mid-period, or a hole right after
                    // a period boundary. Jump straight down to the missing head instead
                    // of re-walking the covered middle, otherwise a handful of missing
                    // bars would cost an entire period on every run.
                    report.periods_skipped += 1;
                    cursor = c_lo;
                    empties = 0;
                    continue;
                }
            }

            let window_start = resume_from.unwrap_or_else(|| tf.shift(cursor, -limit));
            let from = window_start.max(needed_lo).max(MIN_TIMESTAMP);
            if from >= cursor {
                break;
            }
            tracing::debug!(
                symbol,
                timeframe = tf.slug(),
                from = %format_ts(from),
                to = %format_ts(cursor),
                slots = tf.slots_between(from, cursor),
                resumed = resume_from.is_some(),
                "fetching window"
            );
            let candles = self.client.candles_window(symbol, tf, from, cursor).await?;
            report.windows += 1;
            report.requests += 1;

            if candles.is_empty() {
                report.empty_windows += 1;
                if discovery {
                    empties += 1;
                    if empties >= max_empty {
                        // Confirm that nothing older than the cursor exists: a long
                        // delisting gap must not silently truncate history.
                        match self
                            .probe_deeper_data(symbol, tf, cursor, limit, &mut report)
                            .await?
                        {
                            Some((found_at, window_top)) => {
                                tracing::info!(
                                    symbol,
                                    timeframe = tf.slug(),
                                    found_at = %format_ts(found_at),
                                    "empty stretch above older data, resuming the scan"
                                );
                                empties = 0;
                                // Resume from the top of the probe window, so nothing
                                // between the cursor and the probe can be skipped.
                                cursor = cursor.min(window_top);
                                continue;
                            }
                            None => {
                                // Every slot between the oldest bar we hold and the
                                // empty boundary was covered by a requested window,
                                // so caching the oldest *known* bar is both safe and
                                // far tighter than caching the boundary: later runs
                                // jump the whole empty band instead of re-walking it.
                                let floor = report.first_candle.unwrap_or(cursor);
                                tracing::info!(
                                    symbol,
                                    timeframe = tf.slug(),
                                    history_start = %format_ts(floor),
                                    "reached the start of history"
                                );
                                scan_lo = Some(floor);
                                break;
                            }
                        }
                    }
                }
                cursor = from;
                continue;
            }

            empties = 0;
            let newest = candles.iter().map(|c| c.time).max().expect("non-empty");
            let oldest = candles.iter().map(|c| c.time).min().expect("non-empty");
            report.candles_fetched += candles.len() as u64;

            for candle in candles {
                if let Err(e) = candle.validate(tf) {
                    report.candles_invalid += 1;
                    if self.cfg.collection.strict_validation {
                        return Err(e);
                    }
                    // Keep it anyway. KuCoin really does serve bars such as
                    // 2017-11-29 07:00 BTC-USDT 1h with `high < low`, and dropping
                    // them leaves holes that do not exist upstream: the archive has
                    // to mirror the exchange, and `verify` reports such rows
                    // separately as invalid rather than as missing.
                    if report.candles_invalid <= MAX_LOGGED_INVALID {
                        tracing::warn!(
                            symbol,
                            timeframe = tf.slug(),
                            error = %e,
                            "keeping a candle that violates OHLC invariants"
                        );
                    } else if report.candles_invalid == MAX_LOGGED_INVALID + 1 {
                        tracing::warn!(
                            symbol,
                            timeframe = tf.slug(),
                            "further invalid candles in this series are not logged individually"
                        );
                    }
                }
                let p = self.store.period_of(candle.time, tf);
                buffers.entry(p).or_default().push(candle);
            }
            report.first_candle = Some(report.first_candle.map_or(oldest, |v: i64| v.min(oldest)));
            report.last_candle = Some(report.last_candle.map_or(newest, |v: i64| v.max(newest)));

            // Step exactly one window down: adjacent windows tile the range, so
            // progression never depends on what the exchange returned and no bar
            // can fall between two windows.
            cursor = from;
            self.flush_ready(symbol, tf, &mut buffers, cursor, &mut covered, &mut report)?;
        }

        // Everything still buffered belongs to the newest periods.
        self.flush_all(symbol, tf, &mut buffers, &mut covered, &mut report)?;

        if !self.dry_run {
            let (first, _, _) = self.coverage_summary(&covered);
            // Where history *really* starts is where the data starts: a pair may be
            // listed on the 19th while its first monthly bar only exists from the 1st
            // of the next month. Caching the actual first bar stops every later run
            // from re-probing the empty band in front of it.
            if let Some(lo) = first.or(scan_lo) {
                state.history_start = Some(lo);
            }
            if let Err(e) = self.state.save(symbol, tf, &state) {
                tracing::warn!(symbol, timeframe = tf.slug(), error = %e, "cannot persist series state");
            }
        }

        let (first, last, _) = self.coverage_summary(&covered);
        report.first_candle = first.or(report.first_candle);
        report.last_candle = last.or(report.last_candle);
        report.history_start = report.first_candle.or(scan_lo);
        report.retries = self.client.retry_count() - retries_before;
        report.elapsed = started.elapsed();
        Ok(report)
    }

    fn coverage_summary(
        &self,
        covered: &BTreeMap<Period, (i64, i64)>,
    ) -> (Option<i64>, Option<i64>, u64) {
        let first = covered.values().map(|(lo, _)| *lo).min();
        let last = covered.values().map(|(_, hi)| *hi).max();
        (first, last, covered.len() as u64)
    }

    /// Flush every buffered period that lies strictly below `cursor`.
    ///
    /// `cursor` is an exclusive upper bound, so a period is finished once no bar
    /// at or below it can still belong to that period.
    fn flush_ready(
        &self,
        symbol: &str,
        tf: Timeframe,
        buffers: &mut BTreeMap<Period, Vec<Candle>>,
        cursor: i64,
        covered: &mut BTreeMap<Period, (i64, i64)>,
        report: &mut SeriesReport,
    ) -> Result<()> {
        let ready: Vec<Period> = buffers
            .keys()
            .filter(|p| cursor <= p.start())
            .copied()
            .collect();
        for period in ready {
            if let Some(candles) = buffers.remove(&period) {
                self.flush_one(symbol, tf, period, candles, covered, report)?;
            }
        }
        Ok(())
    }

    /// Flush everything that is still buffered.
    fn flush_all(
        &self,
        symbol: &str,
        tf: Timeframe,
        buffers: &mut BTreeMap<Period, Vec<Candle>>,
        covered: &mut BTreeMap<Period, (i64, i64)>,
        report: &mut SeriesReport,
    ) -> Result<()> {
        let periods: Vec<Period> = buffers.keys().copied().collect();
        for period in periods {
            if let Some(candles) = buffers.remove(&period) {
                self.flush_one(symbol, tf, period, candles, covered, report)?;
            }
        }
        Ok(())
    }

    /// Merge one buffered period into its Parquet file.
    fn flush_one(
        &self,
        symbol: &str,
        tf: Timeframe,
        period: Period,
        candles: Vec<Candle>,
        covered: &mut BTreeMap<Period, (i64, i64)>,
        report: &mut SeriesReport,
    ) -> Result<()> {
        if candles.is_empty() {
            return Ok(());
        }
        let buffered_min = candles.iter().map(|c| c.time).min().expect("non-empty");
        let buffered_max = candles.iter().map(|c| c.time).max().expect("non-empty");

        if self.dry_run {
            report.files_written += 1;
            let newly = match covered.get(&period).copied() {
                Some((lo, hi)) => candles
                    .iter()
                    .filter(|c| c.time < lo || c.time > hi)
                    .count() as u64,
                None => candles.len() as u64,
            };
            report.rows_added += newly;
            report.rows_refreshed += candles.len() as u64 - newly;
            covered
                .entry(period)
                .and_modify(|(lo, hi)| {
                    *lo = (*lo).min(buffered_min);
                    *hi = (*hi).max(buffered_max);
                })
                .or_insert((buffered_min, buffered_max));
            return Ok(());
        }

        let (path, stats) = self.store.merge(symbol, tf, period, &candles)?;
        report.rows_added += stats.rows_added;
        report.rows_refreshed += stats.rows_refreshed;
        report.rows_net += stats.net_new();
        if stats.changed {
            report.files_written += 1;
            tracing::debug!(
                symbol,
                timeframe = tf.slug(),
                period = %period,
                rows = stats.rows_after,
                added = stats.rows_added,
                path = %path.display(),
                "wrote partition"
            );
        } else {
            report.files_unchanged += 1;
        }

        // Re-read the real coverage from the file footer so later period jumps
        // are based on what is actually on disk.
        if let Some((lo, hi)) = parquet_store::time_range(&path)? {
            covered.insert(period, (lo, hi));
        } else {
            covered
                .entry(period)
                .and_modify(|(lo, hi)| {
                    *lo = (*lo).min(buffered_min);
                    *hi = (*hi).max(buffered_max);
                })
                .or_insert((buffered_min, buffered_max));
        }
        Ok(())
    }

    /// Walk **daily** candles backwards to find the very first bar of a pair.
    ///
    /// One 1-day window carries 1500 bars, so a decade of history costs three or
    /// four requests, and the answer is exact to the day instead of being guessed
    /// from empty intraday windows. Returns `None` when the pair has no daily
    /// history at all (the caller then falls back to probing).
    async fn discover_history_start(
        &self,
        symbol: &str,
        report: &mut SeriesReport,
    ) -> Result<Option<i64>> {
        let probe_tf = Timeframe::D1;
        let limit = probe_tf.max_candles() as i64;
        let now = self.exchange_now().await;
        // Exclusive upper bound: daily bars strictly before the current day.
        let mut cursor = probe_tf.align(now).max(MIN_TIMESTAMP);
        let mut oldest: Option<i64> = None;

        // 12 windows is ~49 years: a hard stop that can never loop forever.
        for _ in 0..12 {
            if cursor <= MIN_TIMESTAMP {
                break;
            }
            let from = probe_tf.shift(cursor, -limit).max(MIN_TIMESTAMP);
            let candles = self
                .client
                .candles_window(symbol, probe_tf, from, cursor)
                .await?;
            report.requests += 1;
            match candles.first() {
                Some(first) => {
                    oldest = Some(oldest.map_or(first.time, |o: i64| o.min(first.time)));
                    // Next window ends exactly where this one started: adjacent
                    // half-open windows leave no room for a hole.
                    cursor = first.time.max(from);
                }
                None => break,
            }
        }
        Ok(oldest)
    }

    /// Probe exponentially further back for any candle older than `below`.
    ///
    /// Read-only: it never advances the scan cursor, it only answers "is there
    /// anything at all down there?", which is what separates a genuine listing
    /// date from a long delisting gap. Returns the oldest candle found together
    /// with the exclusive top of the window it was found in, so the caller can
    /// resume the linear scan from there without skipping anything.
    async fn probe_deeper_data(
        &self,
        symbol: &str,
        tf: Timeframe,
        below_exclusive: i64,
        limit: i64,
        report: &mut SeriesReport,
    ) -> Result<Option<(i64, i64)>> {
        // Distances are counted in bars, so this works for the calendar month too.
        let mut gap_bars = limit.saturating_mul(8);
        for _ in 0..10 {
            let to = tf.shift(below_exclusive, -gap_bars);
            if to <= MIN_TIMESTAMP {
                return Ok(None);
            }
            let from = tf.shift(to, -limit).max(MIN_TIMESTAMP);
            tracing::debug!(
                symbol,
                timeframe = tf.slug(),
                from = %format_ts(from),
                to = %format_ts(to),
                "probing deeper for older data"
            );
            let probe = self.client.candles_window(symbol, tf, from, to).await?;
            report.requests += 1;
            report.probes += 1;
            if let Some(first) = probe.first() {
                return Ok(Some((first.time, to)));
            }
            gap_bars = gap_bars.saturating_mul(8);
        }
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::{LayoutOptions, WriteOptions};
    use std::collections::HashMap;
    use wiremock::matchers::{method, path as path_matcher};
    use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

    /// A stand-in for the KuCoin Spot candle endpoint.
    ///
    /// It keeps a canonical series of hourly bars and serves **any** requested
    /// window the way the real API does: aggregated to the requested timeframe,
    /// filtered to the **half-open** range `[startAt, endAt)` — KuCoin treats
    /// `endAt` as exclusive, which was measured against the live API, see
    /// [`KucoinClient::candles_window`] — newest first, truncated to 1500 bars.
    /// Tests therefore exercise the real pagination logic instead of mocking
    /// hand-computed window boundaries.
    struct FakeExchange {
        /// Canonical hourly bars, ascending.
        hourly: Vec<(i64, f64)>,
        /// When false, `1day` requests answer with an empty list (used to
        /// exercise the probing fallback).
        serve_daily: bool,
        /// Drop the first N monthly bars before answering. KuCoin serves no bar for
        /// a month in which a pair only traded for part of the time, which is how a
        /// series can start later than the date the daily probe finds.
        skip_first_months: usize,
    }

    impl FakeExchange {
        fn new(hourly: Vec<(i64, f64)>) -> Self {
            let mut hourly = hourly;
            hourly.sort_by_key(|(t, _)| *t);
            FakeExchange {
                hourly,
                serve_daily: true,
                skip_first_months: 0,
            }
        }

        /// Build `count` hourly bars starting at `start`.
        fn series(start: i64, count: i64, price: f64) -> Vec<(i64, f64)> {
            Self::series_step(start, count, 3600, price)
        }

        /// Build `count` bars `step` seconds apart.
        fn series_step(start: i64, count: i64, step: i64, price: f64) -> Vec<(i64, f64)> {
            (0..count).map(|i| (start + i * step, price)).collect()
        }

        /// Bars aggregated to `tf` (open = first, close = last of the bucket).
        fn bars_for(&self, tf: Timeframe) -> Vec<(i64, f64)> {
            let mut out: Vec<(i64, f64)> = Vec::new();
            for (t, price) in &self.hourly {
                // `align` handles the calendar month as well as fixed intervals.
                let bucket = tf.align(*t);
                match out.last_mut() {
                    Some((bt, p)) if *bt == bucket => *p = *price,
                    _ => out.push((bucket, *price)),
                }
            }
            out
        }
    }

    impl Respond for FakeExchange {
        fn respond(&self, request: &Request) -> ResponseTemplate {
            let params: HashMap<String, String> = request
                .url
                .query_pairs()
                .map(|(k, v)| (k.into_owned(), v.into_owned()))
                .collect();
            let tf: Timeframe = params
                .get("type")
                .and_then(|t| t.parse().ok())
                .unwrap_or(Timeframe::H1);
            let start: i64 = params
                .get("startAt")
                .and_then(|v| v.parse().ok())
                .unwrap_or(0);
            let end: i64 = params
                .get("endAt")
                .and_then(|v| v.parse().ok())
                .unwrap_or(i64::MAX);

            if tf == Timeframe::D1 && !self.serve_daily {
                return ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"code": "200000", "data": []}));
            }

            let mut bars = self.bars_for(tf);
            if tf == Timeframe::Mon1 && self.skip_first_months > 0 {
                bars.drain(..self.skip_first_months.min(bars.len()));
            }
            let mut rows: Vec<serde_json::Value> = bars
                .into_iter()
                // Half-open, exactly like KuCoin: `endAt` itself is not returned.
                .filter(|(t, _)| *t >= start && *t < end)
                .map(|(t, p)| row(t, p))
                .collect();
            // KuCoin returns the newest bars first and silently caps the count.
            rows.reverse();
            rows.truncate(tf.max_candles());
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"code": "200000", "data": rows}))
        }
    }

    /// One KuCoin candle row: `[time, open, close, high, low, volume, turnover]`.
    fn row(time: i64, price: f64) -> serde_json::Value {
        serde_json::json!([
            time.to_string(),
            price.to_string(),
            price.to_string(),
            (price * 1.001).to_string(),
            (price * 0.999).to_string(),
            "10.0",
            "1000.0"
        ])
    }

    struct Harness {
        server: MockServer,
        collector: Collector,
        cfg: Arc<Config>,
        _data: tempfile::TempDir,
        _state: tempfile::TempDir,
    }

    async fn harness(cfg_mut: impl FnOnce(&mut Config)) -> Harness {
        let server = MockServer::start().await;
        let data = tempfile::tempdir().unwrap();
        let state_dir = tempfile::tempdir().unwrap();
        let mut cfg = Config::default();
        cfg.general.base_url = server.uri();
        cfg.rate_limit.min_interval_ms = 0;
        cfg.collection.overlap_candles = 1;
        cfg.collection.max_empty_windows = 2;
        cfg_mut(&mut cfg);
        let cfg = Arc::new(cfg);
        let client = Arc::new(KucoinClient::new(cfg.kucoin_config()).unwrap());
        let store = Store::new(
            data.path(),
            LayoutOptions::default(),
            WriteOptions::default(),
        );
        let state = StateStore::new(state_dir.path());
        let collector = Collector::new(client, store, state, cfg.clone());
        Harness {
            server,
            collector,
            cfg,
            _data: data,
            _state: state_dir,
        }
    }

    /// A timestamp written as a date, for fixtures that read better that way.
    fn at(day: &str) -> i64 {
        crate::util::date_to_unix(day).unwrap()
    }

    /// The exchange clock is set just after the last bar a test expects to be
    /// stored: only finished bars are written.
    fn just_after(bar: i64, tf: Timeframe) -> i64 {
        tf.shift(bar, 1)
    }

    async fn mount_time(server: &MockServer, ts: i64) {
        Mock::given(method("GET"))
            .and(path_matcher("/api/v1/timestamp"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"code": "200000", "data": ts * 1000})),
            )
            .mount(server)
            .await;
    }

    async fn mount_exchange(server: &MockServer, exchange: FakeExchange) {
        Mock::given(method("GET"))
            .and(path_matcher("/api/v1/market/candles"))
            .respond_with(exchange)
            .mount(server)
            .await;
    }

    /// 2020-01-01T00:00:00Z — day aligned, so daily discovery and hourly data
    /// agree on the very first bar.
    const DAY0: i64 = 1_577_836_800;

    #[tokio::test]
    async fn windows_tile_the_range_without_holes() {
        // Regression test for KuCoin's exclusive `endAt`. With an inclusive cursor
        // the collector lost exactly one bar per window — 33 holes over a
        // 34-window live backfill — which only an independent continuity check
        // (DuckDB over the stored Parquet) revealed.
        let h = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string(); // DAY0
        })
        .await;
        let bars = FakeExchange::series(DAY0, 4000, 100.0);
        // "Now" is the hour right after the last bar, so all 4000 are finished.
        mount_time(&h.server, just_after(DAY0 + 3999 * 3600, Timeframe::H1)).await;
        mount_exchange(&h.server, FakeExchange::new(bars)).await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        // 4000 bars means three windows: 1500 + 1500 + 1000.
        assert_eq!(report.rows_added, 4000, "{report:?}");
        assert_eq!(report.windows, 3, "{report:?}");

        let files = h
            .collector
            .store()
            .scan_series("BTC-USDT", Timeframe::H1)
            .unwrap();
        let mut times: Vec<i64> = Vec::new();
        for file in &files {
            times.extend(
                parquet_store::read_candles(&file.path)
                    .unwrap()
                    .into_iter()
                    .map(|c| c.time),
            );
        }
        times.sort_unstable();
        assert_eq!(times.len(), 4000);
        assert_eq!(times[0], DAY0);
        assert_eq!(*times.last().unwrap(), DAY0 + 3999 * 3600);

        let holes: Vec<(i64, i64)> = times
            .windows(2)
            .filter(|w| w[1] - w[0] != 3600)
            .map(|w| (w[0], w[1]))
            .collect();
        assert!(
            holes.is_empty(),
            "{} holes between windows, first: {:?}",
            holes.len(),
            holes.first()
        );
    }

    #[tokio::test]
    async fn backfill_walks_back_to_the_listing_date() {
        let h = harness(|_| {}).await;
        let bars = FakeExchange::series(DAY0, 3000, 100.0);
        // `now` is the open time of the next (still forming) bar, i.e. the
        // exclusive upper bound of the complete bars.
        let last_bar = DAY0 + 2999 * 3600; // 2020-05-13 23:00
        mount_time(&h.server, just_after(last_bar, Timeframe::H1)).await;
        mount_exchange(&h.server, FakeExchange::new(bars)).await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();

        assert_eq!(report.candles_fetched, 3000, "{report:?}");
        assert_eq!(report.rows_added, 3000);
        assert_eq!(report.first_candle, Some(DAY0));
        assert_eq!(report.last_candle, Some(last_bar));
        // The daily probe found the real floor, so no heuristic probing was needed.
        assert_eq!(report.probes, 0, "{report:?}");
        assert_eq!(report.history_start, Some(DAY0));

        // Everything landed in one yearly file (H1 partitions by year).
        let files = h
            .collector
            .store()
            .scan_series("BTC-USDT", Timeframe::H1)
            .unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].period.slug(), "2020");
        assert_eq!(files[0].rows, 3000);

        // A second run only looks at what is missing after the last stored bar:
        // one small window, nothing new, older periods jumped over.
        let second = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(second.rows_added, 0, "{second:?}");
        assert!(second.requests <= 1, "{second:?}");
        assert!(second.periods_skipped >= 1, "{second:?}");
    }

    #[tokio::test]
    async fn monthly_backfill_steps_by_calendar_months() {
        // The fake exchange buckets its hourly bars per calendar month, so this
        // exercises the whole scan with a *variable* bar length: 28-31 days.
        let h = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string();
        })
        .await;
        let tf = Timeframe::Mon1;
        // Just over 14 months of hourly bars, from 2020-01-01.
        let bars = FakeExchange::series(DAY0, 24 * 430, 100.0);
        // `now` is 2021-03-01, so the last finished monthly bar is 2021-02.
        // For `1mon` the period *is* the month, so "now" is inside March and the
        // last finished month is February.
        let end_exclusive = at("2021-03-01");
        mount_time(&h.server, end_exclusive).await;
        mount_exchange(&h.server, FakeExchange::new(bars)).await;

        let report = h.collector.collect_series("BTC-USDT", tf).await.unwrap();
        assert_eq!(report.rows_added, 14, "{report:?}");
        assert_eq!(report.first_candle, Some(DAY0));
        assert_eq!(
            report.last_candle,
            Some(crate::util::date_to_unix("2021-02-01").unwrap())
        );

        // Monthly partitions land in a single file.
        let files = h.collector.store().scan_series("BTC-USDT", tf).unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].period.slug(), "all");

        let stored: Vec<Candle> = parquet_store::read_candles(&files[0].path).unwrap();
        assert_eq!(stored.len(), 14);
        for candle in &stored {
            assert_eq!(
                tf.align(candle.time),
                candle.time,
                "labels are month starts"
            );
        }
        for pair in stored.windows(2) {
            // One month apart, which is *not* a constant number of seconds.
            assert_eq!(pair[1].time, tf.shift(pair[0].time, 1), "{pair:?}");
        }
        let steps: std::collections::BTreeSet<i64> = stored
            .windows(2)
            .map(|w| (w[1].time - w[0].time) / 86_400)
            .collect();
        assert!(
            steps.len() > 1,
            "month lengths must vary in this range, got {steps:?}"
        );

        // And the same series verifies cleanly.
        let verify = crate::verify::verify_series(
            h.collector.store(),
            "BTC-USDT",
            tf,
            crate::verify::VerifyOptions::default(),
        )
        .unwrap();
        assert!(verify.is_clean(), "{verify:?}");
        assert_eq!(verify.expected_bars, 14);

        // A repeat run must not re-fetch a single bar.
        let second = h.collector.collect_series("BTC-USDT", tf).await.unwrap();
        assert_eq!(second.requests, 0, "{second:?}");
        assert_eq!(second.rows_added, 0);
    }

    #[tokio::test]
    async fn sync_resumes_right_after_the_last_stored_bar() {
        // January is on disk, the exchange has January/February/March and "now" is
        // 15 March 12:00. The run must fetch February and the finished part of
        // March, and must not touch January again.
        let h = harness(|_| {}).await;
        let january: Vec<Candle> = (0..744).map(|i| candle(DAY0 + i * 3600, 1.0)).collect();
        let period = h.collector.store().period_of(DAY0, Timeframe::H1);
        h.collector
            .store()
            .merge("BTC-USDT", Timeframe::H1, period, &january)
            .unwrap();

        mount_time(&h.server, at("2020-03-15") + 12 * 3600).await;
        mount_exchange(
            &h.server,
            FakeExchange::new(FakeExchange::series(DAY0, 24 * 90, 2.0)),
        )
        .await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        // February 2020 is a leap month (696 hours) plus 1-14 March and the first
        // twelve finished hours of 15 March; the bar still forming (15 March 12:00)
        // is not stored.
        assert_eq!(report.rows_added, 696 + 14 * 24 + 12, "{report:?}");
        assert_eq!(report.first_candle, Some(DAY0));
        assert_eq!(report.last_candle, Some(at("2020-03-15") + 11 * 3600));
        assert_eq!(
            report.windows, 1,
            "one window covers the missing tail: {report:?}"
        );

        let files = h
            .collector
            .store()
            .scan_series("BTC-USDT", Timeframe::H1)
            .unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].rows, 744 + 696 + 14 * 24 + 12);
    }

    #[tokio::test]
    async fn only_finished_bars_are_stored() {
        // For every timeframe the rule is the same: a bar is written once its own
        // period has finished, so the still-forming bar at "now" is never stored.
        let h = harness(|_| {}).await;
        let feb1 = at("2020-02-01");
        // The exchange has bars well past "now", so the clamp is what decides.
        mount_time(&h.server, at("2020-02-20") + 12 * 3600 + 1800).await;
        mount_exchange(
            &h.server,
            FakeExchange::new(FakeExchange::series(feb1, 24 * 19 + 14, 1.0)),
        )
        .await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        // 19 full days plus the finished hours of 20 February (00:00 .. 11:00).
        assert_eq!(report.rows_added, 19 * 24 + 12, "{report:?}");
        assert_eq!(report.last_candle, Some(at("2020-02-20") + 11 * 3600));
        assert!(
            report.last_candle.unwrap() < at("2020-02-20") + 12 * 3600,
            "the forming bar must not be stored"
        );
        let files = h
            .collector
            .store()
            .scan_series("BTC-USDT", Timeframe::H1)
            .unwrap();
        assert_eq!(files[0].max, at("2020-02-20") + 11 * 3600);
    }

    #[tokio::test]
    async fn a_missing_head_costs_one_window_not_a_whole_period() {
        // June 2018 of BTC-USDT really starts on the 4th, so the head of the June
        // period is missing while the tail is on disk. Refilling it must not re-walk
        // the covered middle.
        let h = harness(|cfg| {
            cfg.collection.start = "2020-06-01".to_string();
            cfg.collection.timeframes = vec![Timeframe::M5];
        })
        .await;
        let step = 300i64;
        let jun1 = at("2020-06-01");
        let jun4 = at("2020-06-04");
        let jul1 = at("2020-07-01");
        // Everything from 4 June on is already stored, so only the head is missing.
        let stored = FakeExchange::series_step(jun4, 27 * 24 * 12, step, 1.0);
        let period = h.collector.store().period_of(jun4, Timeframe::M5);
        let existing: Vec<Candle> = stored.iter().map(|(t, p)| candle(*t, *p)).collect();
        h.collector
            .store()
            .merge("BTC-USDT", Timeframe::M5, period, &existing)
            .unwrap();

        // "Now" is exactly 1 July, so the run starts at the June/July boundary.
        mount_time(&h.server, jul1).await;
        mount_exchange(
            &h.server,
            FakeExchange::new(FakeExchange::series_step(jun1, 30 * 24 * 12, step, 2.0)),
        )
        .await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::M5)
            .await
            .unwrap();
        // Only 1-3 June were missing: three days of 5m bars.
        assert_eq!(report.rows_added, 3 * 24 * 12, "{report:?}");
        assert_eq!(
            report.windows, 1,
            "the covered middle must be skipped, not re-fetched: {report:?}"
        );
        assert_eq!(report.first_candle, Some(jun1));
    }

    #[tokio::test]
    async fn the_cached_floor_never_hides_data() {
        // The cached floor may only make the scan look deeper. If the files hold
        // bars older than it, the earlier value wins — a stale cache must never be
        // able to hide data that is sitting on disk.
        let h = harness(|_| {}).await;
        let jan = at("2020-01-01");
        let march = at("2020-03-01");
        let period = h.collector.store().period_of(march, Timeframe::H1);
        let stored: Vec<Candle> = (0..24).map(|i| candle(march + i * 3600, 1.0)).collect();
        h.collector
            .store()
            .merge("BTC-USDT", Timeframe::H1, period, &stored)
            .unwrap();
        // The cache claims "nothing before June", which is later than the data.
        h.collector
            .state_store()
            .save(
                "BTC-USDT",
                Timeframe::H1,
                &crate::state::SeriesState {
                    history_start: Some(at("2020-06-01")),
                },
            )
            .unwrap();

        // The exchange has hourly bars from January and "now" is 2 March.
        mount_time(&h.server, at("2020-03-02")).await;
        mount_exchange(
            &h.server,
            FakeExchange::new(FakeExchange::series(jan, 24 * 70, 1.0)),
        )
        .await;

        let later = Collector::new(
            h.collector.client().clone(),
            h.collector.store().clone(),
            h.collector.state_store().clone(),
            h.cfg.clone(),
        );
        let report = later
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(
            report.first_candle,
            Some(jan),
            "the stale cache must not stop the scan above the data: {report:?}"
        );
    }

    #[tokio::test]
    async fn the_floor_snaps_to_the_first_stored_bar() {
        // A pair can be listed on the 19th while its first monthly bar only exists
        // from the 1st of the next month. Caching the listing date as the floor would
        // make every later run re-probe an empty month.
        let h = harness(|_| {}).await;
        let nov1 = at("2020-11-01");
        // Hourly bars from the 15th: the daily probe finds 1 October as the floor,
        // while the exchange serves no monthly bar before November.
        let hourly = FakeExchange::series_step(at("2020-10-15"), 90 * 24, 3600, 1.0);
        let exchange = FakeExchange {
            skip_first_months: 1,
            ..FakeExchange::new(hourly)
        };
        mount_time(&h.server, at("2021-02-15")).await;
        mount_exchange(&h.server, exchange).await;

        let first = h
            .collector
            .collect_series("BTC-USDT", Timeframe::Mon1)
            .await
            .unwrap();
        assert_eq!(first.first_candle, Some(nov1), "{first:?}");
        assert_eq!(
            first.history_start,
            Some(nov1),
            "the floor must move to the first bar that exists: {first:?}"
        );

        // The next run therefore has nothing to probe at all.
        let later = Collector::new(
            h.collector.client().clone(),
            h.collector.store().clone(),
            h.collector.state_store().clone(),
            h.cfg.clone(),
        );
        let second = later
            .collect_series("BTC-USDT", Timeframe::Mon1)
            .await
            .unwrap();
        assert_eq!(second.requests, 0, "{second:?}");
    }

    #[tokio::test]
    async fn catching_up_with_a_new_bar_costs_one_window() {
        // The interesting case for a monthly-partitioned timeframe: the tail of the
        // newest period is missing by a couple of bars. Only that tail may be
        // fetched — not the whole period.
        let h = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string();
            cfg.collection.timeframes = vec![Timeframe::M5];
        })
        .await;
        let step = 300i64;
        let bars = FakeExchange::series_step(DAY0, 24 * 12 * 2, step, 1.0); // two days
        let first_now = DAY0 + 24 * 12 * 2 * step;
        let second_now = first_now + 2 * step; // two more bars have finished

        // The clock answers `first_now` on the first run and `second_now` afterwards.
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counter = calls.clone();
        Mock::given(method("GET"))
            .and(path_matcher("/api/v1/timestamp"))
            .respond_with(move |_: &Request| {
                let n = counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                let ts = if n == 0 { first_now } else { second_now };
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"code": "200000", "data": ts * 1000}))
            })
            .mount(&h.server)
            .await;

        // The exchange always holds a few bars more than "now".
        let mut available = bars.clone();
        available.extend(FakeExchange::series_step(first_now, 4, step, 1.0));
        mount_exchange(&h.server, FakeExchange::new(available)).await;

        let first = h
            .collector
            .collect_series("BTC-USDT", Timeframe::M5)
            .await
            .unwrap();
        assert_eq!(first.rows_added, 24 * 12 * 2, "{first:?}");
        assert_eq!(first.windows, 1, "{first:?}");

        // A brand new collector, so the memoised exchange clock is read again.
        let later = Collector::new(
            h.collector.client().clone(),
            h.collector.store().clone(),
            h.collector.state_store().clone(),
            h.cfg.clone(),
        );
        let second = later
            .collect_series("BTC-USDT", Timeframe::M5)
            .await
            .unwrap();
        assert_eq!(second.rows_added, 2, "only the two new bars: {second:?}");
        assert_eq!(
            second.windows, 1,
            "the missing tail must cost a single window: {second:?}"
        );
        assert_eq!(second.candles_fetched, 3, "{second:?}");
    }

    #[tokio::test]
    async fn a_series_up_to_the_current_bar_costs_nothing_to_re_run() {
        // Data that reaches the last finished bar is fully covered, so a repeat run
        // spends no request at all — at any timeframe.
        let h = harness(|_| {}).await;
        let start = at("2020-02-01");
        let now = at("2020-03-15") + 12 * 3600;
        let count = (now - 3600 - start) / 3600 + 1; // up to 11:00 of 15 March
        mount_time(&h.server, now).await;
        mount_exchange(
            &h.server,
            FakeExchange::new(FakeExchange::series(start, count, 1.0)),
        )
        .await;

        let first = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(first.rows_added, count as u64, "{first:?}");

        let second = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(second.requests, 0, "{second:?}");
        assert_eq!(second.rows_added, 0);
        assert!(second.periods_skipped >= 1, "{second:?}");
    }

    #[tokio::test]
    async fn the_exchange_clock_is_read_once_per_collector() {
        // `symbols = []` means thousands of series; a clock request each would
        // waste calls and walk straight into the rate limiter.
        let h = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string();
        })
        .await;
        mount_time(&h.server, just_after(DAY0 + 199 * 3600, Timeframe::H1)).await;
        mount_exchange(
            &h.server,
            FakeExchange::new(FakeExchange::series(DAY0, 200, 1.0)),
        )
        .await;

        for symbol in ["BTC-USDT", "ETH-USDT", "SOL-USDT"] {
            h.collector
                .collect_series(symbol, Timeframe::H1)
                .await
                .unwrap();
        }
        let requests = h.server.received_requests().await.unwrap_or_default();
        let clock_calls = requests
            .iter()
            .filter(|r| r.url.path() == "/api/v1/timestamp")
            .count();
        assert_eq!(clock_calls, 1, "the clock must be cached across series");
    }

    #[tokio::test]
    async fn configured_start_stops_the_scan() {
        let start = crate::util::date_to_unix("2020-01-03").unwrap();
        let h = harness(move |cfg| {
            cfg.collection.start = "2020-01-03".to_string();
        })
        .await;
        let bars = FakeExchange::series(DAY0, 100, 3.0);
        mount_time(&h.server, just_after(DAY0 + 99 * 3600, Timeframe::H1)).await;
        mount_exchange(&h.server, FakeExchange::new(bars)).await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(report.history_start, Some(start));
        assert_eq!(report.first_candle, Some(start));
        // 2020-01-03T00:00 .. 2020-01-05T03:00 is 52 hourly bars.
        assert_eq!(report.rows_added, 52, "{report:?}");
        assert_eq!(report.probes, 0, "an explicit start needs no deep probing");
    }

    #[tokio::test]
    async fn long_gap_does_not_truncate_history() {
        // Two clusters of data separated by a long dead stretch. The daily probe
        // finds the true floor, so the linear scan walks straight through the gap.
        let h = harness(|_| {}).await;
        // 1500 hourly bars, then a dead stretch at least twice as wide as a full
        // 1500-bar window — which guarantees that a request comes back empty
        // whatever the window alignment — then 1500 more bars.
        let second = DAY0 + 5500 * 3600;
        let mut bars = FakeExchange::series(DAY0, 1500, 10.0);
        bars.extend(FakeExchange::series(second, 1500, 1.0));
        mount_time(&h.server, just_after(second + 1499 * 3600, Timeframe::H1)).await;
        mount_exchange(&h.server, FakeExchange::new(bars)).await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(report.candles_fetched, 3000, "{report:?}");
        assert_eq!(
            report.first_candle,
            Some(DAY0),
            "older data must survive the gap"
        );
        assert!(report.empty_windows >= 1, "the gap should be visible");
    }

    #[tokio::test]
    async fn fallback_probe_terminates_without_daily_data() {
        // No daily history: the collector falls back to empty-window probing and
        // must still terminate cleanly.
        let h = harness(|cfg| {
            cfg.collection.max_empty_windows = 2;
        })
        .await;
        let bars = FakeExchange::series(DAY0, 3000, 10.0);
        mount_time(&h.server, just_after(DAY0 + 2999 * 3600, Timeframe::H1)).await;
        mount_exchange(
            &h.server,
            FakeExchange {
                serve_daily: false,
                ..FakeExchange::new(bars)
            },
        )
        .await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(report.candles_fetched, 3000, "{report:?}");
        assert!(report.probes >= 1, "the fallback must confirm emptiness");
        assert_eq!(report.first_candle, Some(DAY0));
    }

    #[tokio::test]
    async fn dry_run_writes_nothing() {
        let h = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string();
        })
        .await;
        let collector = h.collector.clone().with_dry_run(true);
        let bars = FakeExchange::series(DAY0, 10, 3.0);
        mount_time(&h.server, just_after(DAY0 + 9 * 3600, Timeframe::H1)).await;
        mount_exchange(&h.server, FakeExchange::new(bars)).await;

        let report = collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert!(report.dry_run);
        assert_eq!(report.rows_added, 10);
        assert!(collector
            .store()
            .scan_series("BTC-USDT", Timeframe::H1)
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn invalid_candles_are_kept_and_counted() {
        let h = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string();
        })
        .await;
        let end = just_after(DAY0 + 3600, Timeframe::H1); // the second bar is finished
        mount_time(&h.server, end).await;
        // An impossible bar: high below open and close.
        let bad = serde_json::json!([
            (DAY0 + 3600).to_string(),
            "10.0",
            "9.5",
            "9.0",
            "8.0",
            "1.0",
            "1.0"
        ]);
        let good = row(DAY0, 3.0);
        Mock::given(method("GET"))
            .and(path_matcher("/api/v1/market/candles"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "code": "200000",
                "data": [bad, good]
            })))
            .mount(&h.server)
            .await;

        let report = h
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .unwrap();
        assert_eq!(report.candles_invalid, 1);
        // Both bars are stored: dropping the impossible one would leave a hole
        // that does not exist upstream. `verify` flags it as an invalid row.
        assert_eq!(report.rows_added, 2, "{report:?}");

        let files = h
            .collector
            .store()
            .scan_series("BTC-USDT", Timeframe::H1)
            .unwrap();
        let stored: Vec<Candle> = files
            .iter()
            .flat_map(|f| parquet_store::read_candles(&f.path).unwrap())
            .collect();
        assert_eq!(stored.len(), 2);
        let odd = stored
            .iter()
            .find(|c| c.time == DAY0 + 3600)
            .expect("the odd bar is kept");
        // The exact values the exchange reported survive, invariant violation and all.
        assert_eq!(
            (odd.open, odd.high, odd.low, odd.close),
            (10.0, 9.0, 8.0, 9.5)
        );
        assert!(odd.high < odd.open.max(odd.close));

        // In strict mode the same row aborts the run.
        let strict = harness(|cfg| {
            cfg.collection.start = "2020-01-01".to_string();
            cfg.collection.strict_validation = true;
        })
        .await;
        mount_time(&strict.server, end).await;
        Mock::given(method("GET"))
            .and(path_matcher("/api/v1/market/candles"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "code": "200000",
                "data": [bad, good]
            })))
            .mount(&strict.server)
            .await;
        assert!(strict
            .collector
            .collect_series("BTC-USDT", Timeframe::H1)
            .await
            .is_err());
    }

    #[tokio::test]
    async fn resolve_symbols_takes_everything_or_an_explicit_list() {
        // Discovery has no filters: every quoted currency, every leveraged token
        // and every delisted pair is included, exactly as the exchange lists them.
        let h = harness(|cfg| {
            cfg.collection.symbols = vec!["CONFIG-USDT".into()];
        })
        .await;
        let symbols_json = serde_json::json!({
            "code": "200000",
            "data": [
                {"symbol":"BTC-USDT","baseCurrency":"BTC","quoteCurrency":"USDT","enableTrading":true},
                {"symbol":"BTC3L-USDT","baseCurrency":"BTC3L","quoteCurrency":"USDT","enableTrading":true},
                {"symbol":"ETH-BTC","baseCurrency":"ETH","quoteCurrency":"BTC","enableTrading":true},
                {"symbol":"XRP-USDC","baseCurrency":"XRP","quoteCurrency":"USDC","enableTrading":true},
                {"symbol":"OLD-USDT","baseCurrency":"OLD","quoteCurrency":"USDT","enableTrading":false}
            ]
        });
        Mock::given(method("GET"))
            .and(path_matcher("/api/v2/symbols"))
            .respond_with(ResponseTemplate::new(200).set_body_json(symbols_json.clone()))
            .mount(&h.server)
            .await;

        // A configured list wins over discovery, and the API is not even queried.
        let configured = h.collector.resolve_symbols(&[]).await.unwrap();
        assert_eq!(configured, vec!["CONFIG-USDT".to_string()]);

        let unfiltered = harness(|cfg| {
            cfg.collection.symbols.clear();
        })
        .await;
        Mock::given(method("GET"))
            .and(path_matcher("/api/v2/symbols"))
            .respond_with(ResponseTemplate::new(200).set_body_json(symbols_json))
            .mount(&unfiltered.server)
            .await;

        let discovered = unfiltered.collector.resolve_symbols(&[]).await.unwrap();
        assert_eq!(
            discovered,
            vec![
                "BTC-USDT".to_string(),
                "BTC3L-USDT".to_string(),
                "ETH-BTC".to_string(),
                "OLD-USDT".to_string(),
                "XRP-USDC".to_string(),
            ],
            "discovery must not filter anything out"
        );

        // CLI overrides beat both, and are de-duplicated.
        let overrides = unfiltered
            .collector
            .resolve_symbols(&["SOL-USDT".to_string(), "SOL-USDT".to_string()])
            .await
            .unwrap();
        assert_eq!(overrides, vec!["SOL-USDT".to_string()]);
    }

    #[test]
    fn report_summary_is_readable() {
        let mut r = SeriesReport::new("BTC-USDT", Timeframe::M1, false);
        r.rows_added = 5;
        r.first_candle = Some(1_600_000_000);
        r.last_candle = Some(1_600_003_600);
        let text = r.summary();
        assert!(text.contains("BTC-USDT 1m +5 new"), "{text}");
        assert!(text.contains("+5 new"));
        assert!(text.contains("2020-09-13"));
    }

    fn candle(time: i64, price: f64) -> Candle {
        Candle {
            time,
            open: price,
            high: price,
            low: price,
            close: price,
            volume: 1.0,
            turnover: price,
        }
    }
}
