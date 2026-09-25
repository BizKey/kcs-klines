"""A basket: one strategy, several symbols, one combined equity curve.

`kcs-backtest` answers "what would this rule have done on this series?" and
`kcs-portfolio` answers "which of ~1000 symbols should I hold?". This module
answers the question in between, and the one most people actually start with:
*I want to trade these five names with this rule — what does the account look
like?*

The model is the simplest one that stays honest:

* every leg runs its own strategy through the same engine as everything else, so
  it obeys the same timing rule and pays its own commission;
* the timeline is the set of bars **every** leg has — the intersection, not a
  forward-filled calendar — so no leg is ever marked on a bar it did not trade;
* the basket starts on the first of those bars and every leg is rebased to 1.0
  there: whatever a leg earned before the window belongs to no basket, and the
  window is exactly what all the legs have in common;
* the weights are fixed at `1/N` and are **not** rebalanced between legs, so the
  combined curve is exactly `1 + Σ w·(ratio − 1)` and no cross-leg turnover is
  charged. Rebalancing legs against each other is a real decision with a real
  cost, and it is not smuggled in here;
* the benchmark is the same names bought once, in equal parts, at the **second**
  bar of the window and held to the last close — the honest alternative to trading
  the basket at all. The window's first bar is where the newest leg's history
  starts, and nothing can be traded there (`targets[0]` never fills), so a
  benchmark entered at its open would be credited with a move the strategy is not
  allowed to take; `engine.buy_and_hold` follows the same rule.

Usage::

    uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT
    uv run kcs-basket --symbols BTC-USDT,ETH-USDT --strategy sma --param window=200
    uv run kcs-basket --symbols BTC-USDT,ETH-USDT --timeframe 1d --no-artifacts

Artifacts land in `analysis/out/` (gitignored): the combined curve as CSV, the
same curve as an SVG chart against the benchmark, and the summary as JSON.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import Costs, data, engine, metrics, report
from .data import Bar, iso
from .metrics import Performance, cagr, pct
from .run_backtest import parse_params
from .strategies import available, get_strategy

DATA_DIR = data.DEFAULT_DATA_DIR
OUT_DIR = data.repo_root() / "analysis" / "out"


@dataclass
class Leg:
    """One symbol's strategy over the basket's window, rebased to 1.0 at its start.

    `fees` is restated the same way: commission paid inside the window as a
    fraction of the capital the leg had when the window opened. `positions` is the
    exposure the leg held on each bar of the shared timeline — kept so the chart
    can mark when the basket moved money in or out.
    """

    symbol: str
    weight: float
    equity: list[float]
    positions: list[float]
    performance: Performance
    trades: int
    exposure: float
    fees: float
    open_at_end: bool = False

    @property
    def contribution(self) -> float:
        """What this leg added to the basket's total return (the weights sum to 1)."""
        return self.weight * self.performance.total_return

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "weight": self.weight,
            "total_return": self.performance.total_return,
            "cagr": self.performance.cagr,
            "ann_vol": self.performance.ann_vol,
            "sharpe": self.performance.sharpe,
            "max_dd": self.performance.max_dd,
            "trades": self.trades,
            "exposure": self.exposure,
            "fees_paid": self.fees,
            "contribution": self.contribution,
            "open_at_end": self.open_at_end,
        }


@dataclass
class BasketResult:
    """The combined curve, its legs, and the same names held passively."""

    label: str
    symbols: list[str]
    timeframe: str
    strategy: str
    params: dict
    costs: Costs
    times: list[int]
    equity: list[float]
    benchmark_equity: list[float]
    legs: list[Leg]
    performance: Performance
    benchmark: Performance
    fees_paid: float
    dropped: int
    window_text: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return self.performance.final_equity

    @property
    def leg_shares(self) -> dict[str, float]:
        """Where the money actually ended up, as a share of the basket's equity.

        The weights are set to `1/N` once and never rebalanced, so a leg that
        performed better owns a bigger slice by the end: equal weights at the
        start are not equal weights later, and the report says both.
        """
        final = self.performance.final_equity
        if final <= 0:
            return {leg.symbol: 0.0 for leg in self.legs}
        return {leg.symbol: leg.weight * leg.equity[-1] / final for leg in self.legs}

    @property
    def capital_in_market(self) -> float:
        """Average share of the basket's capital in the assets rather than in cash."""
        return sum(leg.weight * leg.exposure for leg in self.legs)

    def as_dict(self, *, include_curves: bool = False) -> dict:
        payload = {
            "label": self.label,
            "symbols": list(self.symbols),
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "params": dict(self.params),
            "costs": {
                "fee_per_side": self.costs.fee_per_side,
                "slippage_per_side": self.costs.slippage_per_side,
            },
            "window": {
                "first": self.times[0],
                "last": self.times[-1],
                "bars": len(self.times),
                "leg_bars_dropped": self.dropped,
            },
            "legs": [leg.as_dict() for leg in self.legs],
            "leg_shares": self.leg_shares,
            "capital_in_market": self.capital_in_market,
            "basket": self.performance.as_dict(),
            "benchmark_equal_weight": self.benchmark.as_dict(),
            "fees_paid": self.fees_paid,
            "warnings": list(self.warnings),
        }
        if include_curves:
            payload["equity"] = self.equity
            payload["benchmark_equity"] = self.benchmark_equity
            payload["leg_equity"] = {leg.symbol: leg.equity for leg in self.legs}
        return payload


def parse_symbols(raw: str) -> list[str]:
    """`"BTC-USDT, eth-usdt"` -> `["BTC-USDT", "ETH-USDT"]`, order preserved."""
    symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if not symbols:
        raise SystemExit("--symbols needs at least one symbol, e.g. --symbols BTC-USDT,ETH-USDT")
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if duplicates:
        raise SystemExit(f"--symbols lists {', '.join(duplicates)} twice; one leg per symbol is kept")
    return symbols


def common_window(series: dict[str, list[Bar]]) -> tuple[int, int]:
    """The stretch of time every leg covers, as `(first, last)`.

    A leg that listed late starts the whole basket late. The alternative — holding
    its share in cash until it appears — is a different experiment, and one that
    flatters whichever leg happens to be new.
    """
    spans = [(bars[0].time, bars[-1].time) for bars in series.values()]
    return max(first for first, _ in spans), min(last for _, last in spans)


def shared_timeline(series: dict[str, list[Bar]], first: int, last: int) -> list[int]:
    """The bars *every* leg has inside `[first, last]`, ascending.

    The intersection rather than a calendar: a bar one leg is missing is a bar on
    which the basket cannot be marked, and forward-filling a stale price through
    an exchange-wide outage would invent a return that nobody could have earned.
    """
    stamps = [{bar.time for bar in bars if first <= bar.time <= last} for bars in series.values()]
    return sorted(set.intersection(*stamps))


def run_basket(
    series: dict[str, list[Bar]],
    strategy_name: str,
    params: dict,
    timeframe: str,
    *,
    costs: Costs | None = None,
    label: str | None = None,
    strict: bool = False,
    window: int | None = None,
    window_text: str | None = None,
) -> BasketResult:
    """Run one strategy on every leg and combine the curves at fixed weights.

    `window` (seconds) shortens the reported stretch to the last that much of the
    data, on top of what the legs have in common. The legs are still run over
    their whole history, so their signals are the ones they really had: only the
    reported window moves.
    """
    if not series:
        raise ValueError("a basket needs at least one symbol")
    costs = costs or Costs()
    symbols = list(series)

    first, last = common_window(series)
    if window:
        first = max(first, last - window)
    if first > last:
        spans = ", ".join(
            f"{symbol} {iso(bars[0].time)}..{iso(bars[-1].time)}" for symbol, bars in series.items()
        )
        raise ValueError(f"the legs share no window at all: {spans}")
    timeline = shared_timeline(series, first, last)
    if len(timeline) < 3:
        raise ValueError(
            f"the legs share only {len(timeline)} bar(s) inside {iso(first)} .. {iso(last)}: "
            "that is not enough to measure anything"
        )
    dropped = (
        sum(1 for bars in series.values() for bar in bars if first <= bar.time <= last)
        - len(timeline) * len(series)
    )

    per_year = data.bars_per_year(timeframe)
    weight = 1.0 / len(symbols)
    warnings: list[str] = []
    legs: list[Leg] = []
    bench_curves: list[list[float]] = []
    fees_paid = 0.0

    for symbol, bars in series.items():
        # History before the window is what warms the indicators up; nothing after
        # its end is read at all, so no leg can see past the window.
        windowed = [bar for bar in bars if bar.time <= last]
        index = {bar.time: i for i, bar in enumerate(windowed)}
        start_index = index[timeline[0]]
        strategy = get_strategy(strategy_name, **params)
        run = engine.run_backtest(
            windowed,
            strategy.targets(windowed),
            timeframe,
            costs,
            label=symbol,
            strict=strict,
        )

        slice_ = [index[moment] for moment in timeline]
        curve = [run.equity[i] for i in slice_]
        base = curve[0]
        if base <= 0:
            # The account was already gone when the window opened: this leg
            # contributes a flat zero rather than a division by zero.
            rebased = [0.0] * len(curve)
            warnings.append(f"{symbol}: the account was wiped out before the window opened")
        else:
            rebased = [value / base for value in curve]
            # Mirror the engine: the last point carries the close of the last bar,
            # which is what `final_equity` reports for a single series.
            rebased[-1] = run.performance.final_equity / base

        # Counted the way `kcs-backtest` counts them — closed trades only, and only
        # those entered inside the window — so the two commands agree on one leg,
        # and the leg still open at the end is reported as a warning instead.
        window_trades = [t for t in run.trades if t.entry_index >= start_index]
        closed = [t for t in window_trades if t.is_closed]
        open_at_end = bool(window_trades and window_trades[-1].open_at_end)
        # The engine reports fees against the capital the run started with; the
        # basket starts later, so they are restated against the capital the leg
        # actually had when the window opened — otherwise a leg that was already
        # up before the window looks like it paid more than it did.
        fee = engine.fees_in_window(run, costs.rate, start_index) / base if base > 0 else 0.0
        legs.append(
            Leg(
                symbol=symbol,
                weight=weight,
                equity=rebased,
                positions=[run.positions[i] for i in slice_],
                performance=metrics.performance(rebased, per_year, timestamps=timeline),
                trades=len(closed),
                exposure=sum(abs(run.positions[i]) for i in slice_) / len(slice_),
                fees=fee,
                open_at_end=open_at_end,
            )
        )
        fees_paid += weight * fee
        # The benchmark buys once at the *second* bar of the window and holds —
        # the same rule as `engine.buy_and_hold`, on the window instead of on the
        # whole series, which is what keeps a listing bar out of it.
        leg_bars = [windowed[i] for i in slice_]
        bench_curves.append(engine.buy_and_hold_bars(leg_bars, timeframe, costs).equity)
        if legs[-1].open_at_end:
            warnings.append(f"{symbol}: the leg is still in the market at the end of the window")

    # Fixed weights: the basket's return is the weighted sum of the legs' returns,
    # which is `1 + Σ w·(ratio − 1)` — the same identity the portfolio uses.
    equity = [
        1.0 + sum(leg.weight * (leg.equity[k] - 1.0) for leg in legs) for k in range(len(timeline))
    ]
    benchmark_curve = [
        sum(leg.weight * curve[k] for leg, curve in zip(legs, bench_curves))
        for k in range(len(timeline))
    ]
    benchmark = metrics.performance(benchmark_curve, per_year, timestamps=timeline)
    net_final = benchmark_curve[-1] * (1.0 - costs.rate) ** 2  # one entry, one exit
    benchmark = replace(
        benchmark,
        final_equity=net_final,
        total_return=net_final - 1.0,
        cagr=cagr(net_final, benchmark.years),
    )

    result = BasketResult(
        label=label or f"{len(symbols)}-symbol basket",
        symbols=symbols,
        timeframe=timeframe,
        strategy=strategy_name,
        params=dict(params),
        costs=costs,
        times=timeline,
        equity=equity,
        benchmark_equity=benchmark_curve,
        legs=legs,
        performance=metrics.performance(equity, per_year, timestamps=timeline),
        benchmark=benchmark,
        fees_paid=fees_paid,
        dropped=dropped,
        window_text=window_text if window else None,
        warnings=warnings,
    )
    if len(symbols) == 1:
        result.warnings.append("a basket of one leg is just that leg: kcs-backtest says it in more detail")
    return result


def render(result: BasketResult) -> str:
    """Console report: the window, what each leg did, and the combined result."""
    lines: list[str] = []
    add = lines.append
    add("=" * 100)
    add(f"{result.label} — {', '.join(result.symbols)} ({result.timeframe})")
    add("=" * 100)
    add(
        f"window      : {iso(result.times[0])} .. {iso(result.times[-1])} UTC "
        f"({result.performance.years:.2f} years, {len(result.times):,} bars every leg shares)"
    )
    if result.dropped:
        add(
            f"alignment   : {result.dropped:,} leg-bar(s) inside the window were dropped because "
            "not every leg had them"
        )
    add(
        f"legs        : {len(result.symbols)}, fixed weight {1.0 / len(result.symbols):.2%} each, "
        "never rebalanced against each other"
    )
    add(f"strategy    : {_strategy_line(result)}")
    add(f"costs       : {result.costs}")
    add(
        f"benchmark   : the same {len(result.symbols)} name(s) bought once, in equal parts, at the "
        "window's second open and held"
    )
    add("execution   : signal on close of t, filled at open of t+1, equity marked at opens")
    add("")
    add(
        f"{'leg':<14}{'weight':>8}{'share':>8}{'total':>10}{'CAGR':>9}{'maxDD':>9}{'Sharpe':>8}"
        f"{'trades':>7}{'fees':>8}{'contribution':>13}"
    )
    add("-" * 94)
    shares = result.leg_shares
    for leg in result.legs:
        perf = leg.performance
        add(
            f"{leg.symbol:<14}{leg.weight:>8.2%}{shares[leg.symbol]:>8.2%}{pct(perf.total_return):>10}"
            f"{pct(perf.cagr):>9}{pct(perf.max_dd):>9}{perf.sharpe:>8.2f}{leg.trades:>7}"
            f"{pct(leg.fees):>8}{pct(leg.contribution):>13}"
        )
    best = max(result.legs, key=lambda leg: leg.contribution)
    worst = min(result.legs, key=lambda leg: leg.contribution)
    low_share, high_share = min(shares.values()), max(shares.values())
    add("")
    add(
        f"allocation  : {1.0 / len(result.symbols):.2%} of the capital per leg at the start, "
        "never rebalanced — so the slices drift with performance"
    )
    add(f"              weight = the fixed target, share = where the money actually ended up")
    if len(result.symbols) > 1:
        add(
            f"              by the end each leg owned {low_share:.2%} .. {high_share:.2%} "
            f"of the account ({', '.join(f'{s} {shares[s]:.1%}' for s in (best.symbol, worst.symbol))})"
        )
    exposure_note = (
        f"per-leg exposure {min(leg.exposure for leg in result.legs):.0%} .. "
        f"{max(leg.exposure for leg in result.legs):.0%}"
        if len(result.symbols) > 1
        else f"the leg was in the market {result.legs[0].exposure:.0%} of the time"
    )
    add(
        f"in market   : {result.capital_in_market:.2%} of the capital was in the assets on average, "
        f"the rest in cash ({exposure_note})"
    )
    add(
        f"best leg    : {best.symbol} {pct(best.contribution)}      "
        f"worst leg: {worst.symbol} {pct(worst.contribution)}"
    )
    add("")
    add(f"{'metric':<26}{'basket':>18}{'equal weight B&H':>20}")
    add("-" * 64)

    def row(label: str, left: str, right: str) -> None:
        add(f"{label:<26}{left:>18}{right:>20}")

    row("total return (net)", pct(result.performance.total_return), pct(result.benchmark.total_return))
    row("CAGR", pct(result.performance.cagr), pct(result.benchmark.cagr))
    row("annualised vol", pct(result.performance.ann_vol), pct(result.benchmark.ann_vol))
    row("Sharpe (rf=0)", f"{result.performance.sharpe:.2f}", f"{result.benchmark.sharpe:.2f}")
    row("max drawdown", pct(result.performance.max_dd), pct(result.benchmark.max_dd))
    row("years", f"{result.performance.years:.2f}", f"{result.benchmark.years:.2f}")
    row("fees paid (of capital)", pct(result.fees_paid), f"{pct(2 * result.costs.rate)} once")
    add("")
    row(
        "final equity",
        f"{result.performance.final_equity:.4f}x",
        f"{result.benchmark.final_equity:.4f}x",
    )
    add(f"drawdown span basket   : {iso(result.performance.max_dd_start)} .. {iso(result.performance.max_dd_end)} UTC")
    add(f"drawdown span B&H      : {iso(result.benchmark.max_dd_start)} .. {iso(result.benchmark.max_dd_end)} UTC")
    add(
        "note        : fixed weights, so the basket curve is the weighted sum of the legs' own "
        "curves; each leg's fees are already inside its curve"
    )
    for warning in result.warnings:
        add(f"WARNING     : {warning}")
    return "\n".join(lines)


def _strategy_line(result: BasketResult) -> str:
    """The strategy's own description, rebuilt from the parameters it was given."""
    return get_strategy(result.strategy, **result.params).describe()


def artifact_stem(result: BasketResult) -> str:
    """Filename stem that identifies *this* basket: strategy, legs and timeframe.

    The legs are in the name because they are the thing that changed: without
    them `basket_tsmom720-168_1h_equity.csv` from a five-symbol run and from a
    one-symbol run are the same file, and the second silently overwrites the
    first. Sorted, so re-running the same basket in another order is the same
    file; a digest takes over once the list stops fitting in a filename.
    """
    slug = get_strategy(result.strategy, **result.params).slug
    joined = "+".join(sorted(result.symbols))
    tag = joined if len(joined) <= 40 else f"{len(result.symbols)}legs-{_digest(joined)}"
    window = f"_last{result.window_text}" if result.window_text else ""
    return f"basket_{slug}_{tag}{window}_{result.timeframe}"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def write_equity(path: Path, result: BasketResult) -> None:
    """One row per shared bar: the basket, the benchmark, and every leg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["time_utc", "basket", "benchmark_equal_weight"]
            + [f"leg_{leg.symbol}" for leg in result.legs]
        )
        for k, moment in enumerate(result.times):
            writer.writerow(
                [iso(moment), f"{result.equity[k]:.8f}", f"{result.benchmark_equity[k]:.8f}"]
                + [f"{leg.equity[k]:.8f}" for leg in result.legs]
            )


def rebalance_markers(result: BasketResult) -> list[report.Marker]:
    """A marker on every bar where any leg moved money in or out.

    Green when the capital in the market went up on that bar, red when it went
    down, and the tooltip names the legs that changed — which is what "a swap"
    means for a basket: one bar, several legs, one rebalance.
    """
    if not result.legs:
        return []
    invested = [
        sum(leg.weight * abs(leg.positions[k]) for leg in result.legs)
        for k in range(len(result.times))
    ]
    markers: list[report.Marker] = []
    for k in range(1, len(result.times)):
        if abs(invested[k] - invested[k - 1]) < 1e-12:
            continue
        moved = [
            leg.symbol
            for leg in result.legs
            if abs(leg.positions[k] - leg.positions[k - 1]) > 1e-12
        ]
        if not moved:
            continue
        colour = report.MARKER_ENTRY if invested[k] > invested[k - 1] else report.MARKER_EXIT
        action = "in" if invested[k] > invested[k - 1] else "out"
        markers.append(
            (
                k,
                colour,
                f"{iso(result.times[k])} UTC — {', '.join(moved)} {action}; "
                f"capital in the market {invested[k - 1]:.0%} → {invested[k]:.0%}",
            )
        )
    return markers


def write_chart(path: Path, result: BasketResult) -> None:
    """The combined curve against the same names held passively, swaps marked."""
    basket_label = f"basket ({result.strategy})"
    benchmark_label = "equal weight buy & hold"
    report.write_curves(
        path,
        result.times,
        {basket_label: result.equity, benchmark_label: result.benchmark_equity},
        f"{len(result.symbols)} symbols, {result.timeframe} — {_strategy_line(result)} ({result.costs})",
        colors={basket_label: "#1a73e8", benchmark_label: "#9aa0a6"},
        markers=rebalance_markers(result),
        levels={
            basket_label: result.performance.final_equity,
            benchmark_label: result.benchmark.final_equity,
        },
    )


def write_metrics(path: Path, result: BasketResult, *, include_curves: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(include_curves=include_curves), indent=2, default=str))


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcs-basket",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--symbols",
        required=True,
        help="comma-separated legs, e.g. BTC-USDT,ETH-USDT,SOL-USDT",
    )
    parser.add_argument("--timeframe", default="1h", help="bar size (default: %(default)s)")
    parser.add_argument(
        "--strategy",
        default="tsmom",
        choices=available(),
        help="registered strategy, run on every leg (default: %(default)s)",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="strategy parameter, repeatable (e.g. --param lookback=720); see kcs-backtest --list",
    )
    parser.add_argument(
        "--last",
        default=None,
        metavar="PERIOD",
        help="report only the last stretch, e.g. 1y, 6mon, 30d (the legs still see all the "
             "history before it)",
    )
    parser.add_argument("--fee", type=float, default=0.001, help="commission per side (default: %(default)s)")
    parser.add_argument("--slippage", type=float, default=0.0, help="extra cost per side (default: %(default)s)")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="root of the parquet archive")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--no-artifacts", action="store_true", help="print only, write nothing")
    parser.add_argument("--json", action="store_true", help="also write the summary as JSON")
    parser.add_argument("--json-curves", action="store_true", help="include every curve in that JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = parse_symbols(args.symbols)
    params = parse_params(args.param, args.strategy)

    series: dict[str, list[Bar]] = {}
    for symbol in symbols:
        try:
            series[symbol] = data.load_series(args.data_dir, symbol, args.timeframe)
        except FileNotFoundError as exc:
            raise SystemExit(
                f"{symbol}: {exc}\n(`kcs-backtest --list` shows the stored series)"
            ) from None

    window = None
    if args.last:
        try:
            window = data.parse_duration(args.last)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    try:
        result = run_basket(
            series,
            args.strategy,
            params,
            args.timeframe,
            costs=Costs(fee_per_side=args.fee, slippage_per_side=args.slippage),
            label=f"{args.timeframe} basket" + (f" (last {args.last})" if args.last else ""),
            window=window,
            window_text=args.last,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    print(render(result))
    if args.no_artifacts:
        return 0

    stem = artifact_stem(result)
    equity_path = args.out_dir / f"{stem}_equity.csv"
    chart_path = args.out_dir / f"{stem}_equity.svg"
    write_equity(equity_path, result)
    write_chart(chart_path, result)
    print()
    for path in (equity_path, chart_path):
        print(f"wrote {path}")
    if args.json or args.json_curves:
        json_path = args.out_dir / f"{stem}_metrics.json"
        write_metrics(json_path, result, include_curves=args.json_curves)
        print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
