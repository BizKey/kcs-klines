//! KuCoin Spot REST access: wire types, rate limiting and the HTTP client.

pub mod client;
pub mod rate_limit;
pub mod types;

pub use client::{KucoinClient, KucoinConfig, RetryPolicy, CANDLES_WEIGHT};
pub use rate_limit::RateLimiter;
pub use types::{Candle, Envelope, SymbolInfo, Timeframe};
