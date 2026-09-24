# analysis — testing trading strategies on the collected klines

A small, dependency-light toolkit that answers one question over and over:
**what would this strategy have done on this data, once you pay for trading?**

```bash
uv sync                          # create/refresh the environment from uv.lock
uv run kcs-backtest              # SMA 200 on BTC-USDT 1h, 0.1% per side
uv run kcs-backtest --symbol ETH-USDT --timeframe 4h --sweep 50,100,200
uv run kcs-backtest --fee 0      # same signals, no commission
uv run kcs-backtest --list       # strategies, their parameters, stored series
uv run kcs-backtest --strategy sma-ls --param window=100
uv run kcs-backtest --journal --note "why I ran this"
uv run kcs-journal verify        # re-check what was recorded
uv run pytest                    # 147 tests
```

`analysis` is a [uv](https://docs.astral.sh/uv/) workspace member: the root
project collects data, this one tests strategies against it. `uv run` uses the
environment described by `pyproject.toml` + `uv.lock` — nothing to activate — and
`uv run python -m analysis.run_backtest` is an exact equivalent of
`uv run kcs-backtest`.

Strategy parameters are generic: `--param NAME=VALUE` (repeatable) and
`--sweep [NAME=]V1,V2,…`. Nothing in the CLI knows about any particular
strategy, so a new one needs no plumbing — `--list` shows what each accepts.

Everything is offline and read-only: the data comes from the Rust collector in
the repository root (`kcs-klines backfill`), which writes KuCoin Spot OHLCV as
Parquet under `data/kucoin/spot/<SYMBOL>/<TIMEFRAME>/`.

---

## Layout

`analysis` is a uv workspace member with a `src` layout, so the importable
package is `analysis/src/analysis/` and its tests travel with it:

```
analysis/
├── pyproject.toml          # this package: pyarrow, dev pytest, the two console scripts
├── README.md
├── out/                    # artifacts (gitignored)
└── src/analysis/
    ├── data.py  metrics.py  engine.py  report.py  journal.py  run_backtest.py
    ├── strategies/         # one module per strategy + the registry
    └── tests/              # pytest suite, inside the package on purpose
```

| file | what lives there |
|---|---|
| `data.py` | load one series from Parquet; audit continuity, OHLC sanity, timeframe arithmetic (including calendar months) |
| `metrics.py` | `sma`, `max_drawdown`, `performance` (vol, Sharpe, CAGR), `pct` |
| `engine.py` | the execution model: costs, positions, trades, equity curve, invariants, buy & hold benchmark |
| `strategies/base.py` | the `Strategy` interface — one method, `targets(bars)` |
| `strategies/sma_trend.py` | `SmaTrend` (long-only) and `SmaTrendLongShort` |
| `strategies/sma_reversion.py` | `SmaReversion` — buy below the average, sell above (contrarian) |
| `strategies/breakout.py` | `DonchianBreakout` — a worked example of adding one |
| `strategies/registry.py` | registry machinery: `register`, `parameters`, `sweep_parameter` |
| `strategies/__init__.py` | the registry: `get_strategy("sma", window=200)`, parameter introspection |
| `report.py` | console report plus CSV / JSON / SVG writers |
| `journal.py` | the trade journal: record a run, re-verify it later (see `journal/README.md`) |
| `run_backtest.py` | the CLI that ties it together |
| `out/` | artifacts (gitignored) |
| `tests/` | pytest suite: engine contracts, registry-wide strategy checks, CLI end-to-end on a temp archive, and a regression test against the real archive |

Tests sit inside the package because they exercise internals directly
(`from ..engine import …`) and belong to the same import tree; a test module is
never imported by the package itself.

### Running it with uv

```bash
uv sync                     # create/refresh .venv from pyproject.toml + uv.lock
uv run kcs-backtest …       # console script declared in analysis/pyproject.toml
uv run kcs-journal …        # the other one
uv run pytest               # tests (config lives in the root pyproject.toml)
uv add --package analysis some-package          # a new runtime dependency
uv add --package analysis --dev some-dev-tool   # a new dev dependency
```

`uv run` re-syncs the environment first, so a fresh clone needs nothing but
`uv sync` (or just the first `uv run`). Without uv, the equivalent is
`python -m analysis.run_backtest` inside an environment that has `pyarrow`
installed — the package itself is pip-installable from `analysis/`.

---

## The one rule that keeps a backtest honest

```
targets[t]   is decided on the CLOSE of bar t
the position changes at the OPEN of bar t+1
equity       is marked at bar opens
```

So the exposure held over `(open[t], open[t+1])` is `targets[t-1]`, and a
strategy may only look at `bars[0..t]` when producing `targets[t]` — never
later. Getting this wrong is not a rounding error: applying a bar's move to the
position decided on *that same bar's close* produced a **1.2 billion x** equity
curve on BTC-USDT 1h where the honest number is **1.89x**. That episode is why
the engine carries two checks:

* **the trade book must reproduce the curve** — `result.bookkeeping_error` is the
  relative gap between the independently computed equity path and the product of
  all trade returns. Healthy runs sit at ~1e-14; a look-ahead bug pushes it to
  order 1 and is surfaced as a warning (`--strict` turns it into an exception);
* **commissions are charged on every change of exposure**, on the full notional,
  from the equity at that moment. A round trip pays two sides, a long→short flip
  pays two as well. `result.gross_equity` is the same signal path with zero
  costs, so the difference is exactly what trading cost.

Each trade's `net_return` spans from just before its own entry commission to
just after its exit commission, which is what makes the book and the curve add
up: `prod(1 + net_return) == final_equity`.

A more optimistic fill convention (trading at the signal bar's own close) is
computable for comparison via `engine.close_fill_final_equity` — on BTC-USDT 1h
it gives 0.78x against 1.89x, which is the cost of acting one bar later.

---

## Adding a strategy

Three things are required — `targets()`, `slug` and `describe()` — plus a
registration. Nothing else: the CLI, the report, the artifacts and the contract
tests pick the strategy up from the registry.

```python
# analysis/src/analysis/strategies/breakout.py — this exact file ships with the toolkit
from ..data import Bar
from .base import Strategy
from .registry import register          # NOT `from . import register`: that is a cycle


class DonchianBreakout(Strategy):
    name = "breakout"
    sweep_param = "lookback"        # what `--sweep 10,20` sweeps by default

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    @property
    def slug(self) -> str:
        return f"donchian{self.lookback}"

    @property
    def warmup(self) -> int:
        return self.lookback        # bars needed before a signal can exist

    @property
    def params(self) -> dict:
        return {"lookback": self.lookback}

    def targets(self, bars: list[Bar]) -> list[float]:
        out: list[float] = []
        for i, bar in enumerate(bars):
            if i < self.lookback:
                out.append(0.0)                       # warm-up: no signal yet
            else:
                window = [b.high for b in bars[i - self.lookback:i]]   # excludes bar i
                out.append(1.0 if bar.close > max(window) else 0.0)
        return out

    def describe(self) -> str:
        return f"long on a new {self.lookback}-bar high, flat otherwise"


@register("breakout")
def _breakout(lookback: int = 20, **_: object) -> Strategy:
    """Long on a new N-bar high, flat otherwise."""
    return DonchianBreakout(lookback=lookback)
```

Then add one line to `analysis/src/analysis/strategies/__init__.py` so the package imports
your module (or import it yourself before calling the CLI):

```python
from .breakout import DonchianBreakout
```

```bash
uv run kcs-backtest --list
#   breakout(lookback=20)   [--sweep lookback]
#       Long on a new N-bar high, flat otherwise.
uv run kcs-backtest --strategy breakout --param lookback=30
uv run kcs-backtest --strategy breakout --sweep 10,20,40
```

`analysis/src/analysis/strategies/breakout.py` is itself that example, already registered and
already covered by the tests — copy it as a starting point.

Two rules keep this from breaking:

* import the decorator from `.registry`, never from the package root
  (`from . import register` deadlocks the import of the package — a test guards
  this);
* keep the module free of cycle-prone imports at load time.

The factory's named parameters *are* the CLI parameters: they come from
`inspect.signature`, so `--param` knows exactly what to accept and rejects a
typo with the list of valid names — before loading any data.

### What you get for free, and what to check

| free | why |
|---|---|
| execution, costs, equity curve, trades, drawdown, Sharpe | `engine.py` owns all of it |
| report line, CSV/JSON/SVG artifacts, `--json` | `report.py` |
| `--param`, `--sweep`, `--list`, unknown-name errors | the registry |
| interface + no-look-ahead tests | the registry is looped over by `src/analysis/tests/test_strategies.py` |
| the invariant "trade book = equity curve" | checked on every run, reported as `bookkeeping_error` |

Two things a strategy must get right on its own:

* **look-ahead.** `targets[t]` may only use `bars[0..t]`. The test
  `test_targets_never_peek_at_later_bars` rewrites one bar at a time and asserts
  that no *earlier* target moved — it runs over every registered strategy, so a
  new one is covered without writing a test.
* **warm-up.** Return `0.0` until the indicator has enough history; the engine
  fills `targets[0]` with whatever you return first, and that bar never trades.

Exposure may be fractional or negative (`-1` shorts); the engine validates it
into `[-1, 1]`. A geared strategy can wipe the account out, in which case the run
stops at zero equity and says so in `result.warnings`.

## The trade journal

A backtest is only worth something if it can be re-checked. `--journal` appends
a record of the run to `journal/` in the repository — not to the gitignored
`analysis/out/` — holding the strategy name and its parameters, the costs, the
exact data window with a SHA-256 digest of its OHLCV values, the full metric set
and your own note:

```bash
uv run kcs-backtest --strategy sma --param window=200 \
    --journal --note "reference run before touching the engine"

uv run kcs-journal report        # table of runs, per-strategy roll-up
uv run kcs-journal verify        # re-run everything and compare
uv run kcs-journal show --id 20260924T191341Z-sma200
```

`verify` reloads the archive, **slices it back to the recorded window**, rebuilds
the strategy from the entry alone, re-runs the engine with the recorded costs and
compares every numeric field at a relative tolerance of 1e-9 — and, when the run
kept its trade table, checks that table row by row as well. It exits non-zero on
failure, so it can sit in CI. The status distinguishes the failure modes:

| status | meaning |
|---|---|
| `verified` | data digest and every metric reproduce |
| `data-changed` | metrics reproduce, but the stored bars are not those recorded |
| `mismatch` | same inputs, different numbers — the strategy, the engine or the entry changed |
| `unknown-strategy` | the registry no longer has this strategy name |

That window slicing is not a detail. The archive is alive: while this README was
being written the collector appended a bar and the reference series grew from
78,264 to 78,265 bars. An entry recorded before that still verifies, because the
run pinned the bars it actually evaluated rather than "whatever is on disk".

Entries are append-only (`runs.jsonl`, one JSON object per line) so a re-run
never rewrites history: run the same strategy again next month and `report` shows
both entries side by side, which is how you watch statistics move as the archive
grows. `--keep-trades` additionally copies the per-trade table into
`journal/trades/<run_id>.csv`.

Format details are in [`journal/README.md`](../journal/README.md).

---

## Using it as a library

```python
from analysis import Costs, data, engine
from analysis.strategies import get_strategy

bars = data.load_series("data/kucoin/spot", "BTC-USDT", "1h")
strategy = get_strategy("sma", window=200)
costs = Costs(fee_per_side=0.001, slippage_per_side=0.0002)

result = engine.run_backtest(bars, strategy.targets(bars), "1h", costs, label=strategy.slug)

print(result.performance.cagr, result.bookkeeping_error, len(result.closed_trades))
print(result.benchmark.performance.final_equity)   # buy & hold, same window, same costs
print(result.as_dict())                            # JSON-friendly summary
```

Exposure may be fractional or negative (`-1` shorts), which is why the engine
validates it into `[-1, 1]`; it may also wipe the account out, in which case the
run stops at zero equity and says so in `result.warnings`.

---

## What the artifacts contain

For `--symbol BTC-USDT --timeframe 1h --strategy sma --param window=200`, `out/` gets:

* `sma200_BTC-USDT_1h_trades.csv` — one row per leg: side, entry/exit time and
  price, bars held, gross and net return, and the equity level at each end;
* `sma200_BTC-USDT_1h_equity.csv` — per bar: close, strategy equity, buy & hold
  equity, position held;
* `sma200_BTC-USDT_1h_equity.svg` — both curves on a log scale (hand-written
  SVG, no plotting dependency);
* `sma200_BTC-USDT_1h_metrics.json` — the summary, the data audit, and the
  benchmark if `--json` is given (`--json-curves` adds the full curves).

---

## Notes on the data it reads

* A series is one symbol at one timeframe; the loader de-duplicates overlapping
  partitions by timestamp and sorts ascending.
* `data_quality()` reports gaps and missing bars using the timeframe's own
  calendar — a missing `1mon` bar counts as one missing month, not thirty.
* Bars that violate OHLC are counted, never dropped: KuCoin really serves them,
  and the archive stores them verbatim. As of the current BTC-USDT 1h series
  that is 62 gaps / 398 missing bars and 1 impossible bar over 78,264 bars
  covering 2017-10-04 to 2026-09-24.
* `1mon` has no fixed interval (28-31 day bars), so `interval_seconds()` rejects
  it while `bars_per_year()` and `next_label()` handle it as a calendar step.
* The archive ends at the last collected bar, so the final position is marked to
  market rather than closed — the report says when that is the case.
