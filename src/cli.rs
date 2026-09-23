//! Command line interface.
//!
//! ```text
//! kcs-klines backfill --symbol BTC-USDT --timeframe 1m   # collect / resume
//! kcs-klines status                                      # what is on disk
//! kcs-klines verify --timeframe 1m                       # look for holes
//! ```

use std::path::PathBuf;

use clap::{ArgAction, Args, Parser, Subcommand};

use crate::kucoin::Timeframe;

/// Collect KuCoin Spot klines into partitioned Parquet files.
#[derive(Debug, Parser)]
#[command(
    name = "kcs-klines",
    version,
    about = "Historical KuCoin Spot OHLCV klines collector (Parquet + Zstd)",
    long_about = "Collect historical KuCoin Spot OHLCV klines, store them locally as \
                  partitioned Parquet files (Zstd).\n\n\
                  Everything is resumable: an interrupted backfill keeps what it already \
                  fetched, and a repeated run only asks the exchange for what is missing.",
    after_help = "Examples:\n  \
                  kcs-klines backfill --symbol BTC-USDT --timeframe 1m --timeframe 1h\n  \
                  kcs-klines backfill --jobs 8    # what the monthly cron job calls\n  \
                  kcs-klines status               # what is on disk right now\n  \
                  kcs-klines verify --timeframe 1m --json report.json\n"
)]
pub struct Cli {
    /// Configuration file (default: ./kcs-klines.toml when it exists).
    #[arg(
        short,
        long,
        global = true,
        env = "KCS_KLINES_CONFIG",
        value_name = "PATH"
    )]
    pub config: Option<PathBuf>,

    /// Increase log verbosity (repeatable).
    #[arg(short, long, global = true, action = ArgAction::Count)]
    pub verbose: u8,

    /// Decrease log verbosity (repeatable).
    #[arg(short, long, global = true, action = ArgAction::Count)]
    pub quiet: u8,

    /// Command to run.
    #[command(subcommand)]
    pub command: Command,
}

impl Cli {
    /// Effective log level for the console.
    pub fn log_level(&self, configured: &str) -> String {
        let base = match configured.to_ascii_lowercase().as_str() {
            "trace" => 4,
            "debug" => 3,
            "info" => 2,
            "warn" => 1,
            _ => 0,
        };
        let level = base + self.verbose as i32 - self.quiet as i32;
        match level.clamp(0, 4) {
            0 => "error",
            1 => "warn",
            2 => "info",
            3 => "debug",
            _ => "trace",
        }
        .to_string()
    }
}

/// Commands.
#[derive(Debug, Subcommand)]
pub enum Command {
    /// Download the full history of every selected series.
    Backfill(CollectArgs),

    /// Show what is already stored on disk (no network access).
    Status(StatusArgs),

    /// Check the integrity of what is already on disk.
    Verify(VerifyArgs),

    /// List the symbols that would be collected.
    Symbols(SymbolsArgs),

    /// Write an example configuration file.
    Init(InitArgs),
}

/// Selection and behaviour of the collection command.
#[derive(Debug, Args, Clone, Default)]
pub struct CollectArgs {
    /// Symbols to collect (repeatable, comma separated). Defaults to the config,
    /// or to every symbol matching the discovery filters.
    #[arg(
        long = "symbol",
        short = 's',
        value_delimiter = ',',
        value_name = "SYMBOL"
    )]
    pub symbols: Vec<String>,

    /// Timeframes to collect (repeatable, comma separated). Defaults to the config.
    #[arg(
        long = "timeframe",
        short = 't',
        value_delimiter = ',',
        value_name = "TF"
    )]
    pub timeframes: Vec<String>,

    /// History start: `listing`, unix seconds, `YYYY-MM-DD` or RFC 3339.
    #[arg(long, value_name = "SPEC")]
    pub start: Option<String>,

    /// Fetch and report, but write nothing.
    #[arg(long)]
    pub dry_run: bool,

    /// Maximum number of series collected in parallel.
    #[arg(long, value_name = "N")]
    pub jobs: Option<usize>,
}

impl CollectArgs {
    /// Parse the `--timeframe` values.
    pub fn parsed_timeframes(&self) -> Result<Vec<Timeframe>, String> {
        let mut out = Vec::new();
        for raw in &self.timeframes {
            let tf: Timeframe = raw.parse().map_err(|e| format!("{e}"))?;
            if !out.contains(&tf) {
                out.push(tf);
            }
        }
        Ok(out)
    }
}

/// Options for viewing the local inventory.
#[derive(Debug, Args, Clone, Default)]
pub struct StatusArgs {
    /// Restrict to these symbols.
    #[arg(long = "symbol", short = 's', value_delimiter = ',')]
    pub symbols: Vec<String>,

    /// Restrict to these timeframes.
    #[arg(long = "timeframe", short = 't', value_delimiter = ',')]
    pub timeframes: Vec<String>,

    /// Print the inventory as JSON.
    #[arg(long)]
    pub json: bool,

    /// Print the newest N bars of each selected series.
    #[arg(long, value_name = "N", conflicts_with = "head")]
    pub tail: Option<usize>,

    /// Print the oldest N bars of each selected series.
    #[arg(long, value_name = "N")]
    pub head: Option<usize>,
}

impl StatusArgs {
    /// Parse the `--timeframe` values.
    pub fn parsed_timeframes(&self) -> Result<Vec<Timeframe>, String> {
        let mut out = Vec::new();
        for raw in &self.timeframes {
            let tf: Timeframe = raw.parse().map_err(|e| format!("{e}"))?;
            if !out.contains(&tf) {
                out.push(tf);
            }
        }
        Ok(out)
    }
}

/// Verification options.
#[derive(Debug, Args, Clone, Default)]
pub struct VerifyArgs {
    /// Restrict to these symbols.
    #[arg(long = "symbol", short = 's', value_delimiter = ',')]
    pub symbols: Vec<String>,

    /// Restrict to these timeframes.
    #[arg(long = "timeframe", short = 't', value_delimiter = ',')]
    pub timeframes: Vec<String>,

    /// Write the reports to a JSON file.
    #[arg(long, value_name = "PATH")]
    pub json: Option<PathBuf>,

    /// Report at most this many gaps per series.
    #[arg(long, default_value_t = 20)]
    pub max_gaps: usize,

    /// Only list gaps wider than this many bars.
    #[arg(long, default_value_t = 2)]
    pub min_gap_bars: i64,

    /// Exit with status 1 when any series has gaps or invalid rows.
    #[arg(long)]
    pub strict: bool,
}

impl VerifyArgs {
    /// Parse the `--timeframe` values.
    pub fn parsed_timeframes(&self) -> Result<Vec<Timeframe>, String> {
        let mut out = Vec::new();
        for raw in &self.timeframes {
            let tf: Timeframe = raw.parse().map_err(|e| format!("{e}"))?;
            if !out.contains(&tf) {
                out.push(tf);
            }
        }
        Ok(out)
    }
}

/// Options for listing the symbols that would be collected.
#[derive(Debug, Args, Clone, Default)]
pub struct SymbolsArgs {
    /// Print raw JSON instead of a plain list.
    #[arg(long)]
    pub json: bool,
}

/// `init` options.
#[derive(Debug, Args, Clone, Default)]
pub struct InitArgs {
    /// Where to write the configuration file.
    #[arg(long, value_name = "PATH", default_value = "kcs-klines.toml")]
    pub output: PathBuf,

    /// Overwrite an existing file.
    #[arg(long)]
    pub force: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn cli_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn parses_backfill_with_repeated_flags() {
        let cli = Cli::try_parse_from([
            "kcs-klines",
            "backfill",
            "--symbol",
            "BTC-USDT",
            "--symbol",
            "ETH-USDT",
            "--timeframe",
            "1m",
            "--timeframe",
            "1h",
            "--start",
            "2020-01-01",
            "--jobs",
            "2",
        ])
        .unwrap();
        match cli.command {
            Command::Backfill(args) => {
                assert_eq!(args.symbols, vec!["BTC-USDT", "ETH-USDT"]);
                assert_eq!(args.parsed_timeframes().unwrap().len(), 2);
                assert_eq!(args.start.as_deref(), Some("2020-01-01"));
                assert_eq!(args.jobs, Some(2));
                assert!(!args.dry_run);
            }
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn parses_comma_separated_symbols() {
        let cli =
            Cli::try_parse_from(["kcs-klines", "backfill", "-s", "BTC-USDT,ETH-USDT"]).unwrap();
        match cli.command {
            Command::Backfill(args) => assert_eq!(args.symbols.len(), 2),
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn rejects_unknown_timeframe() {
        let cli = Cli::try_parse_from(["kcs-klines", "backfill", "-t", "7m"]).unwrap();
        match cli.command {
            Command::Backfill(args) => assert!(args.parsed_timeframes().is_err()),
            other => panic!("unexpected command: {other:?}"),
        }
    }

    #[test]
    fn verbosity_flags_adjust_the_level() {
        let quiet = Cli::try_parse_from(["kcs-klines", "-q", "symbols"]).unwrap();
        assert_eq!(quiet.log_level("info"), "warn");
        let verbose = Cli::try_parse_from(["kcs-klines", "-vv", "symbols"]).unwrap();
        assert_eq!(verbose.log_level("info"), "trace");
        let louder = Cli::try_parse_from(["kcs-klines", "-v", "symbols"]).unwrap();
        assert_eq!(louder.log_level("error"), "warn");
        let default = Cli::try_parse_from(["kcs-klines", "symbols"]).unwrap();
        assert_eq!(default.log_level("info"), "info");
    }

    #[test]
    fn verify_defaults_are_sane() {
        let cli = Cli::try_parse_from(["kcs-klines", "verify"]).unwrap();
        match cli.command {
            Command::Verify(args) => {
                assert_eq!(args.max_gaps, 20);
                assert_eq!(args.min_gap_bars, 2);
                assert!(!args.strict);
            }
            other => panic!("unexpected command: {other:?}"),
        }
    }
}
