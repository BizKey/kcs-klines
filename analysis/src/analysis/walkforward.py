"""Walk-forward validation: choose parameters on the past, judge them on the future.

A parameter sweep over one stretch of history answers "which setting looked best
in hindsight" — the question that overfits. This module asks the honest one: pick
the best parameter on a *train* window, then measure how that choice did on the
*unseen* window that follows it, and repeat by rolling forward.

The output is one stitched out-of-sample curve — the sequence of test windows,
each traded with the parameter chosen before it started — plus what the same
spans would have given to buy & hold, the share of profitable test windows, and
how stable the choice was (a parameter that wins in every window is a different
kind of finding from one that wins once).

What no backtest can fix is that the *last* split is still a single sample, and
that trying many parameter grids until one of them looks good here is itself
selection. The table is meant to be read with that in mind: what matters is
whether the out-of-sample result is positive, comparable to buy & hold, and
stable across windows — not whether it is the biggest number in the report.

Usage::

    uv run kcs-walkforward --strategy sma --grid window=50,100,200 \\
        --train 3000 --test 1000 --timeframe 1h

    uv run kcs-walkforward --strategy tsmom --grid lookback=168,720 --grid rebalance=168,720 \\
        --train 4000 --test 1000 --metric cagr
"""

from __future__ import annotations

import argparse
import itertools
import math
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import Costs, data, engine, metrics
from .data import Bar, iso
from .metrics import pct
from .strategies import available, get_strategy, parameters

#: Which train statistic to choose parameters by.
METRICS = ("sharpe", "cagr", "total_return")


@dataclass(frozen=True)
class Split:
    """One train/test pair, in bar indices: `[train_start, train_end)` then `[train_end, test_end)`."""

    index: int
    train_start: int
    train_end: int
    test_end: int


@dataclass
class SplitOutcome:
    """What one split produced: the choice, its training score, and its out-of-sample result."""

    split: Split
    params: dict
    train_score: float
    train: metrics.Performance
    test: metrics.Performance
    test_equity: list[float]


@dataclass
class WalkForwardResult:
    """The whole walk-forward run, including the stitched out-of-sample curve."""

    label: str
    strategy: str
    metric: str
    candidates: int
    outcomes: list[SplitOutcome]
    oos_equity: list[float]
    oos: metrics.Performance
    benchmark_equity: list[float]
    benchmark: metrics.Performance
    costs: Costs
    warnings: list[str] = field(default_factory=list)

    @property
    def parameter_counts(self) -> dict[str, int]:
        """How often each parameter set was chosen, most frequent first."""
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            key = ", ".join(f"{k}={v}" for k, v in sorted(outcome.params.items()))
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    @property
    def winning_share(self) -> float:
        """Share of test windows that made money."""
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.test.total_return > 0) / len(self.outcomes)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "strategy": self.strategy,
            "metric": self.metric,
            "candidates": self.candidates,
            "splits": len(self.outcomes),
            "winning_share": self.winning_share,
            "parameter_counts": self.parameter_counts,
            "out_of_sample": self.oos.as_dict(),
            "buy_and_hold": self.benchmark.as_dict(),
            "costs": {"fee_per_side": self.costs.fee_per_side, "slippage_per_side": self.costs.slippage_per_side},
            "warnings": list(self.warnings),
        }


def split_bounds(n_bars: int, train: int, test: int, step: int | None = None) -> list[Split]:
    """Roll a train/test window pair forward through a series of `n_bars` bars.

    `step` defaults to `test`, which tiles the series with non-overlapping test
    windows. A larger step leaves gaps (fewer, more independent samples); a
    smaller one overlaps the tests, which correlates them.
    """
    if train < 2 or test < 2:
        raise ValueError("train and test must each be at least 2 bars")
    step = test if step is None else step
    if step < 1:
        raise ValueError("step must be at least 1 bar")

    bounds: list[Split] = []
    train_end = train
    while train_end + test <= n_bars:
        bounds.append(
            Split(
                index=len(bounds) + 1,
                train_start=train_end - train,
                train_end=train_end,
                test_end=train_end + test,
            )
        )
        train_end += step
    return bounds


def expand_grid(grid: dict[str, list[object]]) -> list[dict]:
    """Cartesian product of a parameter grid, in a deterministic order."""
    if not grid:
        raise ValueError("the grid is empty: pass at least one parameter to search")
    keys = sorted(grid)
    for key in keys:
        if not grid[key]:
            raise ValueError(f"the grid has no values for {key!r}")
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def slice_equity(equity: list[float], start: int, end: int) -> list[float]:
    """The equity of `[start, end)`, normalised so its first bar is 1.0.

    Measuring a window from its own first bar — rather than from the bar before
    it — is what lets stitched windows be multiplied together without counting a
    bar twice: the move into `start` already belongs to the previous window.
    """
    base = equity[start]
    return [value / base for value in equity[start:end]]


def pick_best(scored: list[tuple[dict, metrics.Performance]], metric: str) -> int:
    """Index of the best candidate by `metric`; ties go to the first."""
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {', '.join(METRICS)}, got {metric!r}")
    return max(range(len(scored)), key=lambda i: getattr(scored[i][1], metric))


def run_walkforward(
    bars: list[Bar],
    strategy_name: str,
    grid: dict[str, list[object]],
    timeframe: str,
    *,
    train: int,
    test: int,
    step: int | None = None,
    metric: str = "sharpe",
    costs: Costs | None = None,
    label: str | None = None,
) -> WalkForwardResult:
    """Roll a parameter choice forward and stitch the out-of-sample windows."""
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {', '.join(METRICS)}, got {metric!r}")
    costs = costs or Costs()
    candidates = expand_grid(grid)
    bounds = split_bounds(len(bars), train, test, step)
    if not bounds:
        raise ValueError(
            f"{len(bars)} bars is not enough for one split: need train + test = {train + test}"
        )

    per_year = data.bars_per_year(timeframe)
    outcomes: list[SplitOutcome] = []
    oos_curve: list[float] = [1.0]
    benchmark_curve: list[float] = [1.0]
    oos_running = 1.0
    benchmark_running = 1.0
    warnings: list[str] = []

    for split in bounds:
        scored: list[tuple[dict, metrics.Performance]] = []
        for params in candidates:
            strategy = get_strategy(strategy_name, **params)
            history = bars[: split.train_end]
            run = engine.run_backtest(
                history, strategy.targets(history), timeframe, costs, label=strategy.slug, strict=True
            )
            segment = slice_equity(run.equity, split.train_start, split.train_end)
            scored.append((params, metrics.performance(segment, per_year)))

        best = pick_best(scored, metric)
        params, train_performance = scored[best]

        strategy = get_strategy(strategy_name, **params)
        history = bars[: split.test_end]
        run = engine.run_backtest(
            history, strategy.targets(history), timeframe, costs, label=strategy.slug, strict=True
        )
        test_segment = slice_equity(run.equity, split.train_end, split.test_end)
        test_performance = metrics.performance(test_segment, per_year)

        # Paste the window onto the stitched curve at the level reached so far,
        # then advance that level by the window's own result.
        oos_curve.extend(value * oos_running for value in test_segment)
        oos_running *= test_segment[-1]

        # Buy & hold over the same window, paying one entry and one exit per span.
        start_close = bars[split.train_end - 1].close
        benchmark_segment = [bar.close / start_close for bar in bars[split.train_end : split.test_end]]
        benchmark_segment = [value * (1.0 - costs.rate) ** 2 for value in benchmark_segment]
        benchmark_curve.extend(value * benchmark_running for value in benchmark_segment)
        benchmark_running *= benchmark_segment[-1]

        outcomes.append(
            SplitOutcome(
                split=split,
                params=params,
                train_score=getattr(train_performance, metric),
                train=train_performance,
                test=test_performance,
                test_equity=test_segment,
            )
        )

    oos = metrics.performance(oos_curve, per_year)
    benchmark = metrics.performance(benchmark_curve, per_year)
    result = WalkForwardResult(
        label=label or f"{strategy_name} walk-forward",
        strategy=strategy_name,
        metric=metric,
        candidates=len(candidates),
        outcomes=outcomes,
        oos_equity=oos_curve,
        oos=oos,
        benchmark_equity=benchmark_curve,
        benchmark=benchmark,
        costs=costs,
    )
    if len(outcomes) < 5:
        result.warnings.append(
            f"only {len(outcomes)} split(s): too few to say anything about stability"
        )
    if len(set(result.parameter_counts)) == len(outcomes) and len(outcomes) > 2:
        result.warnings.append(
            "a different parameter won every window: the choice is not stable"
        )
    return result


def render(result: WalkForwardResult, bars: list[Bar]) -> str:
    """The console report: per-split table, stability, stitched out-of-sample."""
    lines: list[str] = []
    add = lines.append
    add("=" * 100)
    add(f"{result.label} — {result.strategy}, chosen by train {result.metric}, {result.costs}")
    add("=" * 100)
    add(f"candidates  : {result.candidates} parameter set(s) per split")
    add(
        f"splits      : {len(result.outcomes)}  "
        f"({result.winning_share:.0%} of test windows made money)"
    )
    add("")
    add(
        f"{'#':>3}  {'train':<28}{'test':<28}{'chosen':<30}{'train ' + result.metric:>14}"
        f"{'test ret':>11}{'test DD':>10}"
    )
    add("-" * 100)
    for outcome in result.outcomes:
        split = outcome.split
        chosen = ", ".join(f"{key}={value}" for key, value in sorted(outcome.params.items()))
        add(
            f"{split.index:>3}  "
            f"{iso(bars[split.train_start].time) + ' .. ' + iso(bars[split.train_end - 1].time):<28}"
            f"{iso(bars[split.train_end].time) + ' .. ' + iso(bars[split.test_end - 1].time):<28}"
            f"{chosen:<30}{outcome.train_score:>14.2f}"
            f"{pct(outcome.test.total_return):>11}{pct(outcome.test.max_dd):>10}"
        )
    add("")
    add("parameter stability (how often each set won):")
    for name, count in result.parameter_counts.items():
        add(f"  {count:>3} x  {name}")
    add("")
    add(f"{'metric':<26}{'out-of-sample':>18}{'buy & hold':>18}")
    add("-" * 62)

    def row(label: str, left: str, right: str) -> None:
        add(f"{label:<26}{left:>18}{right:>18}")

    row("total return", pct(result.oos.total_return), pct(result.benchmark.total_return))
    row("CAGR", pct(result.oos.cagr), pct(result.benchmark.cagr))
    row("annualised vol", pct(result.oos.ann_vol), pct(result.benchmark.ann_vol))
    row("Sharpe (rf=0)", f"{result.oos.sharpe:.2f}", f"{result.benchmark.sharpe:.2f}")
    row("max drawdown", pct(result.oos.max_dd), pct(result.benchmark.max_dd))
    row("years traded", f"{result.oos.years:.2f}", f"{result.benchmark.years:.2f}")
    for warning in result.warnings:
        add("")
        add(f"WARNING     : {warning}")
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------


def parse_grids(raw: list[str], strategy: str) -> dict[str, list[object]]:
    """`--grid window=50,100` (repeatable) into a parameter grid."""
    accepted = parameters(strategy)
    grid: dict[str, list[object]] = {}
    for item in raw:
        name, separator, values = item.partition("=")
        name = name.strip()
        if not separator:
            raise SystemExit(f"--grid needs NAME=V1,V2,…, got {item!r}")
        if name not in accepted:
            raise SystemExit(
                f"strategy {strategy!r} has no parameter {name!r}; it accepts: {', '.join(accepted)}"
            )
        parsed: list[object] = []
        for value in values.split(","):
            value = value.strip()
            if not value:
                continue
            parsed.append(coerce(value))
        if not parsed:
            raise SystemExit(f"--grid {item!r} has no values")
        grid[name] = parsed
    return grid


def coerce(value: str) -> object:
    """`"50"` -> 50, `"2.5"` -> 2.5, `"true"` -> True, anything else stays a string."""
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcs-walkforward",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--strategy", default="sma", choices=available())
    parser.add_argument(
        "--grid",
        action="append",
        default=[],
        metavar="NAME=V1,V2,…",
        help="parameter grid, repeatable: --grid window=50,100 --grid mode=long-only",
    )
    parser.add_argument("--train", type=int, default=3000, help="train window, in bars")
    parser.add_argument("--test", type=int, default=1000, help="test window, in bars")
    parser.add_argument("--step", type=int, default=None, help="roll step in bars (default: one test window)")
    parser.add_argument("--metric", default="sharpe", choices=METRICS, help="what to choose parameters by")
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument("--data-dir", type=Path, default=data.DEFAULT_DATA_DIR)
    parser.add_argument("--json", type=Path, default=None, help="write the summary as JSON here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.grid:
        raise SystemExit("--grid is required, e.g. --grid window=50,100,200")

    grid = parse_grids(args.grid, args.strategy)
    candidates = math.prod(len(values) for values in grid.values())
    if candidates > 400:
        raise SystemExit(
            f"the grid has {candidates} combinations; that is {candidates} backtests per split. "
            "Narrow it — and remember every extra combination is another chance to overfit."
        )

    bars = data.load_series(args.data_dir, args.symbol, args.timeframe)
    result = run_walkforward(
        bars,
        args.strategy,
        grid,
        args.timeframe,
        train=args.train,
        test=args.test,
        step=args.step,
        metric=args.metric,
        costs=Costs(fee_per_side=args.fee, slippage_per_side=args.slippage),
        label=f"{args.symbol} {args.timeframe} {args.strategy}",
    )
    print(f"loaded {len(bars):,} bars from {args.data_dir / args.symbol / args.timeframe}")
    print(render(result, bars))

    if args.json:
        import json

        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result.as_dict(), indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
