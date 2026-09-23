//! `kcs-klines` binary: argument parsing, logging and the command implementations.

use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{Context, Result};
use clap::Parser;
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use tracing_subscriber::filter::EnvFilter;
use tracing_subscriber::prelude::*;

use kcs_klines::cli::{Cli, CollectArgs, Command, InitArgs, StatusArgs, SymbolsArgs, VerifyArgs};
use kcs_klines::collector::{Collector, RunReport, SeriesReport};
use kcs_klines::config::{Config, LoggingConfig};
use kcs_klines::kucoin::{KucoinClient, Timeframe};
use kcs_klines::notify;
use kcs_klines::status;
use kcs_klines::storage::Store;
use kcs_klines::util::format_ts;
use kcs_klines::verify::{self, VerifyOptions, VerifyReport};
use kcs_klines::Error;

/// Exit code used for fatal problems (bad config, unreachable exchange, …).
const EXIT_FATAL: u8 = 2;
/// Exit code used when the run completed but something failed.
const EXIT_PARTIAL: u8 = 1;

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli).await {
        Ok(code) => code,
        Err(e) => {
            // `{:#}` prints the whole context chain, which is what makes a
            // "cannot write /data/...: No space left on device" readable.
            eprintln!("error: {e:#}");
            ExitCode::from(EXIT_FATAL)
        }
    }
}

/// Shared runtime state.
struct App {
    cfg: Arc<Config>,
    client: Arc<KucoinClient>,
    store: Store,
}

impl App {
    fn new(cfg: Config) -> Result<Self> {
        let client = Arc::new(
            KucoinClient::new(cfg.kucoin_config()).context("cannot build the KuCoin client")?,
        );
        let store = Store::new(
            cfg.general.data_dir.clone(),
            cfg.storage.layout_options()?,
            cfg.storage.write_options()?,
        );
        Ok(App {
            cfg: Arc::new(cfg),
            client,
            store,
        })
    }
}

async fn run(cli: Cli) -> Result<ExitCode> {
    let (mut cfg, config_path) = Config::load_or_default(cli.config.as_deref())?;
    cfg.apply_env_overrides();

    let console_level = cli.log_level(&cfg.general.log_level);
    let explicit_verbosity = cli.verbose > 0 || cli.quiet > 0;
    let _log_guard = init_logging(&console_level, &cfg.logging, explicit_verbosity);
    if explicit_verbosity && std::env::var_os("RUST_LOG").is_some() {
        tracing::warn!(
            "RUST_LOG is set, but -v/-q take precedence; unset it if you want RUST_LOG to rule"
        );
    }

    match &config_path {
        Some(path) => tracing::debug!(config = %path.display(), "loaded configuration"),
        None => tracing::debug!("no configuration file found, using built-in defaults"),
    }

    let healthcheck = cfg.notifications.healthcheck_url.clone();
    let started = Instant::now();
    if !healthcheck.is_empty() {
        notify::ping_quietly(&healthcheck, "/start").await;
    }

    let result = dispatch(cli, cfg).await;

    match &result {
        Ok(code) if *code == ExitCode::SUCCESS => {
            if !healthcheck.is_empty() {
                notify::ping_quietly(&healthcheck, "").await;
            }
            tracing::info!(elapsed = ?started.elapsed(), "done");
        }
        _ => {
            if !healthcheck.is_empty() {
                notify::ping_quietly(&healthcheck, "/fail").await;
            }
        }
    }
    result
}

async fn dispatch(cli: Cli, cfg: Config) -> Result<ExitCode> {
    match cli.command {
        Command::Backfill(args) => collect_command(cfg, args).await,
        Command::Status(args) => status_command(cfg, args).await,
        Command::Verify(args) => verify_command(cfg, args).await,
        Command::Symbols(args) => symbols_command(cfg, args).await,
        Command::Init(args) => init_command(args).await,
    }
}

// ── collect ───────────────────────────────────────────────────────────────────

async fn collect_command(cfg: Config, args: CollectArgs) -> Result<ExitCode> {
    let mut cfg = cfg;
    if let Some(start) = &args.start {
        cfg.collection.start = start.clone();
    }
    if !args.timeframes.is_empty() {
        cfg.collection.timeframes = args.parsed_timeframes().map_err(|e| anyhow::anyhow!(e))?;
    }
    if let Some(jobs) = args.jobs {
        cfg.general.concurrency = jobs.max(1);
    }
    cfg.validate()?;

    let app = App::new(cfg)?;
    let collector = Collector::new(app.client.clone(), app.store.clone(), app.cfg.clone())
        .with_dry_run(args.dry_run);

    let symbols = collector
        .resolve_symbols(&args.symbols)
        .await
        .context("cannot resolve the symbol list")?;
    if symbols.is_empty() {
        anyhow::bail!("no symbols selected; pass --symbol or list them under collection.symbols");
    }
    let timeframes = app.cfg.collection.timeframes.clone();
    tracing::info!(
        symbols = symbols.len(),
        timeframes = ?timeframes.iter().map(|t| t.slug()).collect::<Vec<_>>(),
        concurrency = app.cfg.general.concurrency,
        data_dir = %app.store.data_dir().display(),
        "collecting {} series",
        symbols.len() * timeframes.len()
    );

    let report = run_series(
        &collector,
        &symbols,
        &timeframes,
        app.cfg.general.concurrency,
    )
    .await;
    print!("{}", report.render());
    tracing::info!(
        new_bars = report.total_rows_added(),
        requests = report.total_requests(),
        elapsed = ?report.elapsed,
        "collection finished"
    );

    if report.is_success() {
        Ok(ExitCode::SUCCESS)
    } else {
        Ok(ExitCode::from(EXIT_PARTIAL))
    }
}

/// Run every `(symbol, timeframe)` job, at most `concurrency` at a time.
async fn run_series(
    collector: &Collector,
    symbols: &[String],
    timeframes: &[Timeframe],
    concurrency: usize,
) -> RunReport {
    let started = Instant::now();
    let mut report = RunReport::default();
    let semaphore = Arc::new(Semaphore::new(concurrency.max(1)));
    let mut set: JoinSet<(String, Timeframe, Result<SeriesReport, Error>)> = JoinSet::new();

    'spawn: for symbol in symbols {
        for tf in timeframes {
            let symbol = symbol.clone();
            let tf = *tf;
            let collector = collector.clone();
            let permit = match semaphore.clone().acquire_owned().await {
                Ok(permit) => permit,
                Err(_) => break 'spawn,
            };
            set.spawn(async move {
                let _permit = permit;
                tracing::debug!(symbol = %symbol, timeframe = tf.slug(), "series start");
                let result = collector.collect_series(&symbol, tf).await;
                (symbol, tf, result)
            });
        }
    }

    loop {
        tokio::select! {
            joined = set.join_next() => {
                match joined {
                    Some(Ok((symbol, tf, result))) => match result {
                        Ok(series) => {
                            tracing::info!("{}", series.summary());
                            report.series.push(series);
                        }
                        Err(e) => {
                            tracing::error!(symbol = %symbol, timeframe = tf.slug(), error = %e, "series failed");
                            report.failures.push((symbol, tf, e.to_string()));
                        }
                    },
                    Some(Err(join_error)) => {
                        if join_error.is_panic() {
                            tracing::error!(error = %join_error, "a collector worker panicked");
                            report.failures.push((
                                "<worker>".into(),
                                Timeframe::M1,
                                join_error.to_string(),
                            ));
                        }
                    }
                    None => break,
                }
            }
            _ = tokio::signal::ctrl_c() => {
                tracing::warn!("interrupted; aborting the series still in flight (files stay consistent)");
                set.abort_all();
                break;
            }
        }
    }
    set.shutdown().await;
    report.elapsed = started.elapsed();
    report
}

// ── status ────────────────────────────────────────────────────────────────────

/// Show what is on disk. Never touches the network.
async fn status_command(cfg: Config, args: StatusArgs) -> Result<ExitCode> {
    let app = App::new(cfg)?;
    let timeframes = args.parsed_timeframes().map_err(|e| anyhow::anyhow!(e))?;
    let report = status::inventory(&app.store, &args.symbols, &timeframes)?;

    if let Some(n) = args.tail.or(args.head) {
        let from_head = args.head.is_some();
        if report.series.is_empty() {
            println!("nothing stored yet");
            return Ok(ExitCode::SUCCESS);
        }
        for series in &report.series {
            println!(
                "{} {} — {} bars, {} .. {}",
                series.symbol,
                series.timeframe.slug(),
                series.bars,
                series.first.map(format_ts).unwrap_or_else(|| "-".into()),
                series.last.map(format_ts).unwrap_or_else(|| "-".into())
            );
            let bars = if from_head {
                status::head(&app.store, &series.symbol, series.timeframe, n)?
            } else {
                status::tail(&app.store, &series.symbol, series.timeframe, n)?
            };
            println!(
                "  {:<20} {:>14} {:>14} {:>14} {:>14} {:>16}",
                "time", "open", "high", "low", "close", "volume"
            );
            for bar in bars {
                println!(
                    "  {:<20} {:>14} {:>14} {:>14} {:>14} {:>16}",
                    format_ts(bar.time).replace(" UTC", ""),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume
                );
            }
        }
        return Ok(ExitCode::SUCCESS);
    }

    if args.json {
        println!("{}", serde_json::to_string_pretty(&report.series)?);
    } else {
        print!("{}", report.render());
        println!();
    }
    Ok(ExitCode::SUCCESS)
}

// ── verify ────────────────────────────────────────────────────────────────────

async fn verify_command(cfg: Config, args: VerifyArgs) -> Result<ExitCode> {
    let app = App::new(cfg)?;
    let opts = VerifyOptions {
        max_gaps: args.max_gaps,
        min_gap_bars: args.min_gap_bars,
    };

    let mut series = if args.symbols.is_empty() {
        verify::discover_series(&app.store)?
    } else {
        let mut out = Vec::new();
        for symbol in &args.symbols {
            for tf in app.store.stored_timeframes(symbol)? {
                out.push((symbol.clone(), tf));
            }
        }
        out
    };
    let wanted = args.parsed_timeframes().map_err(|e| anyhow::anyhow!(e))?;
    if !wanted.is_empty() {
        series.retain(|(_, tf)| wanted.contains(tf));
    }
    if series.is_empty() {
        println!(
            "nothing to verify: no data found under {}",
            app.store.data_dir().display()
        );
        return Ok(ExitCode::SUCCESS);
    }

    let mut reports: Vec<VerifyReport> = Vec::with_capacity(series.len());
    let mut unclean = 0usize;
    for (symbol, tf) in &series {
        let report = verify::verify_series(&app.store, symbol, *tf, opts)?;
        if !report.is_clean() {
            unclean += 1;
        }
        println!("{}", report.render());
        reports.push(report);
    }

    if let Some(path) = &args.json {
        let body = serde_json::to_string_pretty(&reports)?;
        std::fs::write(path, body).with_context(|| format!("cannot write {}", path.display()))?;
        println!("wrote {} report(s) to {}", reports.len(), path.display());
    }

    let total_rows: u64 = reports.iter().map(|r| r.rows).sum();
    let total_gaps: usize = reports.iter().map(|r| r.gaps.len()).sum();
    println!(
        "checked {} series, {} rows, {} series with anomalies, {} gaps",
        reports.len(),
        total_rows,
        unclean,
        total_gaps
    );

    if args.strict && unclean > 0 {
        return Ok(ExitCode::from(EXIT_PARTIAL));
    }
    Ok(ExitCode::SUCCESS)
}

// ── symbols ───────────────────────────────────────────────────────────────────

async fn symbols_command(cfg: Config, args: SymbolsArgs) -> Result<ExitCode> {
    let app = App::new(cfg)?;
    let collector = Collector::new(app.client.clone(), app.store.clone(), app.cfg.clone());
    let symbols = collector.resolve_symbols(&[]).await?;
    if args.json {
        println!("{}", serde_json::to_string_pretty(&symbols)?);
    } else {
        for symbol in &symbols {
            println!("{symbol}");
        }
        eprintln!("{} symbols", symbols.len());
    }
    Ok(ExitCode::SUCCESS)
}

// ── init ──────────────────────────────────────────────────────────────────────

async fn init_command(args: InitArgs) -> Result<ExitCode> {
    if args.output.exists() && !args.force {
        anyhow::bail!(
            "{} already exists; pass --force to overwrite it",
            args.output.display()
        );
    }
    let template = Config::template();
    std::fs::write(&args.output, &template)
        .with_context(|| format!("cannot write {}", args.output.display()))?;

    // Create the directory the config points at, so the first run has nowhere to
    // stumble.
    let parsed: Config = toml::from_str(&template)?;
    let data_dir = &parsed.general.data_dir;
    std::fs::create_dir_all(data_dir)
        .with_context(|| format!("cannot create {}", data_dir.display()))?;
    println!("wrote {}", args.output.display());
    println!("data directory: {}", parsed.general.data_dir.display());
    println!("next: kcs-klines backfill --timeframe 1h");
    Ok(ExitCode::SUCCESS)
}

// ── logging ───────────────────────────────────────────────────────────────────

/// Install the tracing subscriber.
///
/// The returned guard owns the non-blocking file writer and must stay alive for
/// as long as logging is wanted.
fn init_logging(
    console_level: &str,
    logging: &LoggingConfig,
    explicit_verbosity: bool,
) -> Option<tracing_appender::non_blocking::WorkerGuard> {
    let console_filter = console_filter(console_level, explicit_verbosity);
    let console_layer = tracing_subscriber::fmt::layer()
        .with_target(false)
        .with_writer(std::io::stderr);

    let Some(path) = &logging.file else {
        tracing_subscriber::registry()
            .with(console_filter)
            .with(console_layer)
            .init();
        return None;
    };

    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    let directory = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let file_name = path
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "kcs-klines.log".to_string());
    let appender = tracing_appender::rolling::daily(directory, file_name);
    let (writer, guard) = tracing_appender::non_blocking(appender);
    let file_level = logging
        .file_level
        .clone()
        .unwrap_or_else(|| console_level.to_string());
    let file_layer = tracing_subscriber::fmt::layer()
        .with_ansi(false)
        .with_target(true)
        .with_writer(writer)
        .with_filter(EnvFilter::new(file_level));

    tracing_subscriber::registry()
        .with(console_filter)
        .with(console_layer)
        .with(file_layer)
        .init();
    Some(guard)
}

/// Build the console filter.
///
/// `-v`/`-q` are explicit, so they beat `RUST_LOG`; without them the environment
/// variable is honoured, which is what shells, containers and unit files expect.
/// (A stray `RUST_LOG=INFO` silently disabling `-vv` is a confusing way to spend
/// an afternoon.)
fn console_filter(level: &str, explicit_verbosity: bool) -> EnvFilter {
    if !explicit_verbosity {
        if let Ok(filter) = EnvFilter::try_from_default_env() {
            return filter;
        }
    }
    // Keep our own crate at the requested level and quieten noisy dependencies.
    EnvFilter::new(format!("{level},kcs_klines={level},reqwest=warn"))
}

#[cfg(test)]
mod logging_tests {
    use super::*;

    #[test]
    fn explicit_verbosity_beats_the_environment() {
        std::env::set_var("RUST_LOG", "error");
        // Without flags the environment variable rules.
        let from_env = console_filter("debug", false);
        assert!(
            from_env.to_string().contains("error") || from_env.to_string().is_empty(),
            "expected the RUST_LOG filter, got {from_env}"
        );
        // With `-v`/`-q` the requested level wins regardless.
        let from_flags = console_filter("debug", true);
        assert!(
            from_flags.to_string().contains("debug"),
            "expected the flag level, got {from_flags}"
        );
        std::env::remove_var("RUST_LOG");

        // With nothing set, the flags' level is used either way.
        assert!(console_filter("trace", false).to_string().contains("trace"));
    }
}
