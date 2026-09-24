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
* `analysis.journal`    — an append-only, re-checkable record of runs.

`journal` and `run_backtest` are deliberately *not* imported here: keeping them
out of this module's imports is what lets `python -m analysis.journal` and
`python -m analysis.run_backtest` run without a `runpy` re-import warning.

Quick start::

    from analysis import Costs, data, engine
    from analysis.strategies import get_strategy

    bars = data.load_series(data.DEFAULT_DATA_DIR, "BTC-USDT", "1h")
    strategy = get_strategy("sma", window=200)
    result = engine.run_backtest(bars, strategy.targets(bars), "1h", Costs(fee_per_side=0.001))
    print(result.label, result.performance.cagr, result.bookkeeping_error)

Or from the command line, with `uv` doing the environment work::

    uv run kcs-backtest --strategy sma --param window=200
    uv run kcs-journal report
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
    "buy_and_hold",
    "get_strategy",
    "pct",
    "performance",
    "run_backtest",
    "sma",
]
