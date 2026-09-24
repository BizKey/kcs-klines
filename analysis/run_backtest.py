#!/usr/bin/env python3
"""Run one strategy over one stored kline series and report the result.

Examples::

    # the default: SMA 200 on BTC-USDT hourly, 0.1% per side
    .venv/bin/python -m analysis.run_backtest

    # another symbol, another timeframe, an SMA window sweep
    .venv/bin/python -m analysis.run_backtest --symbol ETH-USDT --timeframe 4h --sweep 50,100,200

    # what the same signals would do without commission
    .venv/bin/python -m analysis.run_backtest --fee 0

    # any registered strategy, with its own parameters
    .venv/bin/python -m analysis.run_backtest --list
    .venv/bin/python -m analysis.run_backtest --strategy sma-ls --param window=100

    # everything in one JSON, including the equity curve
    .venv/bin/python -m analysis.run_backtest --json --json-curves

Artifacts land in `analysis/out/` (gitignored): a trade list, the equity curve,
an SVG chart and the metrics. Data is read-only and nothing touches the network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python analysis/run_backtest.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis import Costs, data, engine, journal, report  # noqa: E402
from analysis.metrics import pct  # noqa: E402
from analysis.strategies import (  # noqa: E402
    Strategy,
    available,
    describe_registry,
    get_strategy,
    parameters,
    sweep_parameter,
)

DATA_DIR = Path("data/kucoin/spot")
OUT_DIR = Path("analysis/out")
FEE_GRID = (0.0, 0.0002, 0.0005, 0.001, 0.002)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbol", default="BTC-USDT", help="series to load (default: %(default)s)")
    parser.add_argument("--timeframe", default="1h", help="bar size (default: %(default)s)")
    parser.add_argument(
        "--strategy",
        default="sma",
        choices=available(),
        help="registered strategy (default: %(default)s; see also --list)",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="strategy parameter, repeatable (e.g. --param window=100); see --list",
    )
    parser.add_argument("--fee", type=float, default=0.001, help="commission per side (default: %(default)s)")
    parser.add_argument("--slippage", type=float, default=0.0, help="extra cost per side (default: %(default)s)")
    parser.add_argument(
        "--sweep",
        default=None,
        metavar="[NAME=]V1,V2,…",
        help="run one parameter over several values, e.g. --sweep 50,100 or --sweep window=50,100",
    )
    parser.add_argument("--no-fee-grid", action="store_true", help="skip the commission sensitivity table")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="root of the parquet archive")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--no-artifacts", action="store_true", help="print only, write nothing")
    parser.add_argument("--json", action="store_true", help="also write the metrics as JSON")
    parser.add_argument("--json-curves", action="store_true", help="include the equity curve in that JSON")
    parser.add_argument("--list", action="store_true", help="list strategies and stored series, then exit")
    parser.add_argument(
        "--journal",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="record this run in the trade journal (default dir: ./journal, kept in the repo)",
    )
    parser.add_argument("--note", default="", help="a human note for the journal entry")
    parser.add_argument(
        "--keep-trades",
        action="store_true",
        help="also copy the per-trade table into the journal (journal/trades/<run_id>.csv)",
    )
    return parser.parse_args(argv)


def coerce(value: str) -> object:
    """`"50"` -> 50, `"2.5"` -> 2.5, `"true"` -> True, anything else stays a string."""
    lowered = value.strip().lower()
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


def parse_params(raw: list[str], strategy: str) -> dict[str, object]:
    """Turn repeated `--param NAME=VALUE` into keyword arguments for the strategy."""
    accepted = parameters(strategy)
    params: dict[str, object] = {}
    for item in raw:
        name, separator, value = item.partition("=")
        name = name.strip()
        if not separator:
            raise SystemExit(f"--param needs NAME=VALUE, got {item!r}")
        if name not in accepted:
            expected = ", ".join(f"{key} (default {value!r})" for key, value in accepted.items())
            raise SystemExit(f"strategy {strategy!r} has no parameter {name!r}; it accepts: {expected}")
        params[name] = coerce(value)
    return params


def strategy_for(name: str, params: dict[str, object], **overrides: object) -> Strategy:
    """Build the strategy from the already-validated parameters plus overrides."""
    return get_strategy(name, **{**params, **overrides})


def parse_sweep(raw: str, strategy: str) -> tuple[str, list[object]]:
    """`--sweep 50,100` and `--sweep window=50,100` both return (name, values)."""
    name, separator, values = raw.partition("=")
    if not separator:
        name, values = sweep_parameter(strategy) or "", raw
    if not name:
        raise SystemExit(
            f"strategy {strategy!r} does not declare a default sweep parameter; "
            "name one explicitly, e.g. --sweep window=50,100"
        )
    accepted = parameters(strategy)
    if name not in accepted:
        raise SystemExit(f"strategy {strategy!r} has no parameter {name!r}; it accepts: {', '.join(accepted)}")
    parsed = [coerce(value) for value in values.split(",") if value.strip()]
    if not parsed:
        raise SystemExit(f"--sweep {raw!r} has no values")
    return name, parsed


def run(bars, strategy: Strategy, timeframe: str, costs: Costs):
    label = f"{strategy.slug}"
    return engine.run_backtest(bars, strategy.targets(bars), timeframe, costs, label=label)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list:
        print("strategies :")
        for line in describe_registry():
            print(line)
        print()
        series = data.available_series(args.data_dir)
        print(f"series     : {len(series)} on disk under {args.data_dir}")
        for symbol, timeframe in series[:15]:
            print(f"  {symbol:<14}{timeframe}")
        if len(series) > 15:
            print(f"  … and {len(series) - 15} more")
        return 0

    # Validate the flags before doing any work: a typo should not cost a data load.
    params = parse_params(args.param, args.strategy)
    sweep = parse_sweep(args.sweep, args.strategy) if args.sweep else None

    bars = data.load_series(args.data_dir, args.symbol, args.timeframe)
    quality = data.data_quality(bars, args.timeframe)
    costs = Costs(fee_per_side=args.fee, slippage_per_side=args.slippage)
    strategy = strategy_for(args.strategy, params)

    result = run(bars, strategy, args.timeframe, costs)
    print(f"loaded {quality.bars:,} bars from {len(data.series_files(args.data_dir, args.symbol, args.timeframe))} files")
    print(report.render(args.symbol, args.timeframe, quality, result))
    print()
    print(f"strategy    : {strategy.describe()}")

    _print_recent_trades(result)

    if sweep:
        _print_sweep(args, bars, costs, params, sweep)
    if not args.no_fee_grid:
        _print_fee_grid(args, bars, strategy)

    if args.no_artifacts:
        if args.journal is not None:
            print("note: --journal needs the trade table; rerun without --no-artifacts", file=sys.stderr)
            return 2
        return 0

    stem = f"{strategy.slug}_{args.symbol}_{args.timeframe}"
    trades_path = args.out_dir / f"{stem}_trades.csv"
    equity_path = args.out_dir / f"{stem}_equity.csv"
    chart_path = args.out_dir / f"{stem}_equity.svg"
    report.write_trades(trades_path, result)
    report.write_equity(equity_path, bars, result)
    report.write_chart(
        chart_path,
        bars,
        result,
        f"{args.symbol} {args.timeframe} — {strategy.slug} ({result.costs})",
    )
    print()
    for path in (trades_path, equity_path, chart_path):
        print(f"wrote {path}")

    if args.json:
        json_path = args.out_dir / f"{stem}_metrics.json"
        report.write_metrics(json_path, result, quality, args.symbol, args.timeframe)
        print(f"wrote {json_path}")

    if args.journal is not None:
        _record(args, result, quality, bars, params, trades_path)
    return 0


def _record(args, result, quality, bars, params: dict, trades_path: Path) -> None:
    """Append the run to the journal, optionally with its per-trade table."""
    directory = journal.journal_dir_for(args.journal)
    entry = journal.entry_for(
        result,
        quality,
        bars,
        args.strategy,
        params,
        args.symbol,
        args.timeframe,
        note=args.note,
    )
    if args.keep_trades:
        target = journal.trades_path(directory, entry.run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(trades_path.read_bytes())
        entry.trades_file = target.name
    path = journal.append(entry, directory)
    print(f"journalized {entry.run_id} -> {path}")
    print(f"            verify later with: python -m analysis.journal verify --id {entry.run_id}")


def _print_recent_trades(result: engine.BacktestResult, count: int = 10) -> None:
    closed = result.closed_trades
    if not closed:
        return
    print()
    print(f"last {min(count, len(closed))} closed trades:")
    print(f"  {'entry':<17}{'exit':<17}{'bars':>7}{'gross':>11}{'net':>11}")
    for trade in closed[-count:]:
        print(
            f"  {data.iso(trade.entry_time):<17}{data.iso(trade.exit_time or 0):<17}"
            f"{trade.bars_held:>7}{pct(trade.gross_return or 0.0):>11}{pct(trade.net_return or 0.0):>11}"
        )


def _print_sweep(
    args: argparse.Namespace,
    bars,
    costs: Costs,
    params: dict[str, object],
    sweep: tuple[str, list[object]],
) -> None:
    name, values = sweep
    print()
    print(f"parameter sweep over {name} (net, same fee and execution):")
    print(f"  {name:>8}{'trades':>9}{'total':>12}{'CAGR':>10}{'maxDD':>10}{'Sharpe':>9}{'exposure':>10}")
    for value in values:
        attempt = run(bars, strategy_for(args.strategy, params, **{name: value}), args.timeframe, costs)
        perf = attempt.performance
        print(
            f"  {str(value):>8}{len(attempt.closed_trades):>9}{pct(perf.total_return):>12}"
            f"{pct(perf.cagr):>10}{pct(perf.max_dd):>10}{perf.sharpe:>9.2f}{pct(attempt.exposure):>10}"
        )
    bench = run(bars, strategy_for(args.strategy, params), args.timeframe, costs).benchmark.performance
    print(
        f"  {'B&H':>8}{1:>9}{pct(bench.total_return):>12}{pct(bench.cagr):>10}"
        f"{pct(bench.max_dd):>10}{bench.sharpe:>9.2f}{'100.00%':>10}"
    )


def _print_fee_grid(args: argparse.Namespace, bars, strategy: Strategy) -> None:
    print()
    print("commission sensitivity (net, same signals):")
    print(f"  {'fee/side':>9}{'trades':>9}{'final equity':>16}{'total':>14}{'CAGR':>10}")
    for fee in FEE_GRID:
        sweep = run(bars, strategy, args.timeframe, Costs(fee_per_side=fee, slippage_per_side=args.slippage))
        perf = sweep.performance
        print(
            f"  {fee:>9.2%}{len(sweep.closed_trades):>9}{perf.final_equity:>15.4f}x"
            f"{pct(perf.total_return):>14}{pct(perf.cagr):>10}"
        )
    bench = run(bars, strategy, args.timeframe, Costs(fee_per_side=args.fee)).benchmark.performance
    print(
        f"  {'B&H':>9}{1:>9}{bench.final_equity:>15.4f}x"
        f"{pct(bench.total_return):>14}{pct(bench.cagr):>10}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
