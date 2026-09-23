//! Error type shared by the whole crate.

use std::path::{Path, PathBuf};

/// Crate-wide error type.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// Transport-level failure (connect, timeout, TLS, body decode).
    #[error("HTTP transport error: {0}")]
    Http(#[from] reqwest::Error),

    /// KuCoin answered with a business error code (`code != 200000`).
    #[error("KuCoin API error {code}: {message}")]
    Api {
        /// Business code returned by KuCoin, e.g. `"400100"`.
        code: String,
        /// Human readable message returned by KuCoin.
        message: String,
    },

    /// I/O failure annotated with the path that caused it.
    #[error("I/O error on {path}: {source}")]
    Io {
        /// Offending path.
        path: PathBuf,
        /// Underlying error.
        #[source]
        source: std::io::Error,
    },

    /// I/O failure without a meaningful path.
    #[error("I/O error: {0}")]
    PlainIo(#[from] std::io::Error),

    /// Parquet layer failure.
    #[error("parquet error: {0}")]
    Parquet(#[from] parquet::errors::ParquetError),

    /// Arrow layer failure.
    #[error("arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),

    /// JSON (de)serialisation failure.
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),

    /// Configuration file could not be parsed.
    #[error("TOML error: {0}")]
    Toml(#[from] toml::de::Error),

    /// Configuration is syntactically valid but semantically wrong.
    #[error("invalid configuration: {0}")]
    Config(String),

    /// Data coming from the exchange or from disk violates our invariants.
    #[error("invalid data: {0}")]
    Data(String),

    /// KuCoin kept failing after all retries were exhausted.
    #[error("giving up after {attempts} attempts: {last}")]
    RetriesExhausted {
        /// Number of attempts performed.
        attempts: u32,
        /// Description of the last failure.
        last: String,
    },

    /// Anything else.
    #[error("{0}")]
    Other(String),
}

/// Convenience alias.
pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    /// Attach a path to a bare [`std::io::Error`].
    pub fn io(path: impl AsRef<Path>, source: std::io::Error) -> Self {
        Error::Io {
            path: path.as_ref().to_path_buf(),
            source,
        }
    }

    /// True when the failure is worth retrying at a higher level.
    pub fn is_transient(&self) -> bool {
        match self {
            Error::Http(e) => e.is_timeout() || e.is_connect() || e.is_request(),
            Error::PlainIo(e) => matches!(
                e.kind(),
                std::io::ErrorKind::Interrupted
                    | std::io::ErrorKind::TimedOut
                    | std::io::ErrorKind::WouldBlock
            ),
            _ => false,
        }
    }
}
