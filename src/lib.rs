//! `kcs-klines` — collect historical KuCoin **Spot** OHLCV klines and store them
//! locally as partitioned Parquet.
//!
//! The crate is split into layers that can be used independently:
//!
//! * [`kucoin`] — wire types, a weight-aware rate limiter and a thin REST client;
//! * [`storage`] — path layout, Parquet schema, atomic writes and merges;
//! * [`collector`] — the resumable backfill/update engine;
//! * [`status`] — a fast, read-only inventory of what is already stored;
//! * [`verify`] — integrity checks over what is already on disk.

pub mod cli;
pub mod collector;
pub mod config;
pub mod error;
pub mod kucoin;
pub mod notify;
pub mod status;
pub mod storage;
pub mod util;
pub mod verify;

pub use error::{Error, Result};
pub use kucoin::{KucoinClient, KucoinConfig, Timeframe};
