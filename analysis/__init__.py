"""A small toolkit for testing trading strategies on locally collected klines.

The data comes from the Rust collector in the repository root
(`kcs-klines backfill`), which writes KuCoin Spot OHLCV as Parquet under
`data/kucoin/spot/<SYMBOL>/<TIMEFRAME>/`. This package reads that data, runs a
strategy over it under an explicit execution model, and reports what happened.

Layers, lowest first:

* `analysis.data`       — load and audit a series (continuity, OHLC sanity);
* `analysis.metrics`    — indicators and performance statistics;
* `analysis.engine`     — execution, costs, trades, equity curve, invariants;
* `analysis.strategies` — pluggable strategies (see `analysis.strategies.base`);
* `analysis.report`     — console report plus CSV/JSON/SVG artifacts;
* `analysis.journal`    — an append-only, re-checkable record of runs (import
  it explicitly: `from analysis import journal`; keeping it out of this module's
  imports is what lets `python -m analysis.journal` run without a runpy warning).

Quick start::

    from analysis import Costs, data, engine
    from analysis.strategies import get_strategy

    bars = data.load_series("data/kucoin/spot", "BTC-USDT", "1h")
    strategy = get_strategy("sma", window=200)
    result = engine.run_backtest(bars, strategy.targets(bars), "1h", Costs(fee_per_side=0.001))
    print(result.label, result.performance.cagr, result.bookkeeping_error)

Or from the command line::

    .venv/bin/python -m analysis.run_backtest --symbol BTC-USDT --timeframe 1h
"""

from __future__ import annotations

from .engine import BacktestResult, Benchmark, Costs, Trade, buy_and_hold, run_backtest
from .metrics import Performance, performance, pct, sma
from .strategies import Strategy, available, get_strategy

__all__ = [
    "BacktestResult",
    "Benchmark",
    "Costs",
    "Performance",
    "Strategy",
    "Trade",
    "available",
    "fingerprint",
    "buy_and_hold",
    "get_strategy",
    "pct",
    "performance",
    "run_backtest",
    "sma",
]
