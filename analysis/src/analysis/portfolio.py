"""Cross-sectional momentum across a universe of symbols.

The single-asset strategies in this toolkit answer "should I hold this?". This
module answers the other question the archive is big enough for: *which* of ~1000
pairs to hold. Every rebalance it ranks the universe by the return over the last
`lookback` bars, buys the top slice in equal weights (optionally shorts the
bottom slice), and holds until the next rebalance.

Why this is the most promising shape for this data: it is the only strategy class
here whose turnover is set by the rebalance schedule rather than by how noisy the
signal is. The hourly studies in this repository showed the same trend rule dying
at 1,268 round trips and thriving at 30; cross-sectional momentum rebalances
monthly by construction, and spreads risk over hundreds of positions instead of
one.

Honest details, because they decide the result:

* weights are equal within a side; `long-only` sums to 1, `long-short` is
  +0.5 / -0.5 so the gross book stays at 1;
* a symbol that has not traded for five bars is treated as delisted: it leaves
  the universe, and if it was held the position is marked at its final print and
  reported as dropped;
* commission is charged on realised turnover, measured against the weights as
  they have *drifted* with returns, not against the previous targets;
* the benchmark is the equal-weight universe, bought once and never rebalanced —
  the honest comparison for a ranking strategy.

Usage::

    uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --top 0.2
    uv run kcs-portfolio --timeframe 1d --lookback 90 --mode long-short --top 0.1 --json out.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import Costs, data, metrics
from .metrics import pct

DEFAULT_CALENDAR = "BTC-USDT"


@dataclass(frozen=True)
class Panel:
    """One symbol sampled on the rebalance calendar.

    `closes[k]` is the last close at or before rebalance date `k`, `None` when the
    symbol had not listed yet; `momentum[k]` is its return over the lookback,
    `None` when either end of that comparison is missing.
    """

    symbol: str
    closes: list[float | None]
    momentum: list[float | None]


@dataclass
class Rebalance:
    """What one rebalance did."""

    index: int
    time: int
    longs: list[str]
    shorts: list[str]
    turnover: float
    candidates: int


@dataclass
class PortfolioResult:
    """The portfolio's curve, its rebalances, and the equal-weight benchmark."""

    label: str
    costs: Costs
    lookback: int
    rebalance: int
    top: float
    mode: str
    rebalances: list[Rebalance]
    equity: list[float]
    performance: metrics.Performance
    benchmark_equity: list[float]
    benchmark: metrics.Performance
    universe: int
    holdings_mean: float
    fees_paid: float
    dropped: int
    warnings: list[str] = field(default_factory=list)

    @property
    def mean_turnover(self) -> float:
        if not self.rebalances:
            return 0.0
        return statistics.fmean(r.turnover for r in self.rebalances)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "lookback": self.lookback,
            "rebalance": self.rebalance,
            "top": self.top,
            "mode": self.mode,
            "universe": self.universe,
            "rebalances": len(self.rebalances),
            "holdings_mean": self.holdings_mean,
            "mean_turnover": self.mean_turnover,
            "fees_paid": self.fees_paid,
            "dropped_marks": self.dropped,
            "performance": self.performance.as_dict(),
            "buy_and_hold_equal_weight": self.benchmark.as_dict(),
            "costs": {"fee_per_side": self.costs.fee_per_side, "slippage_per_side": self.costs.slippage_per_side},
            "last_rebalance": (
                {
                    "time": self.rebalances[-1].time,
                    "longs": self.rebalances[-1].longs,
                    "shorts": self.rebalances[-1].shorts,
                }
                if self.rebalances
                else None
            ),
            "warnings": list(self.warnings),
        }


def close_at(
    times: list[int], closes: list[float], target: int, max_age: int | None = None
) -> float | None:
    """The last close at or before `target`, or `None` when it is too old.

    `max_age` is what makes a delisting visible: without it the last print of a
    symbol that stopped trading would live on forever, keep its frozen momentum
    and stay in the long book. With it, a symbol that has not traded for
    `max_age` seconds is simply out of the universe.
    """
    index = bisect.bisect_right(times, target)
    if index == 0:
        return None
    moment = times[index - 1]
    if max_age is not None and target - moment > max_age:
        return None
    return closes[index - 1]


def rebalance_dates(times: list[int], count: int) -> list[int]:
    """Every `count`-th bar on an absolute grid, snapped to the calendar's bars.

    The grid is anchored to the epoch rather than to the start of the data, so the
    rebalance dates do not move when the archive gains history.
    """
    if count < 1:
        raise ValueError("rebalance must be at least 1 bar")
    if len(times) < 2:
        return []
    step = int(statistics.median([b - a for a, b in zip(times, times[1:])]))
    period = count * step
    dates: list[int] = []
    grid = -(-times[0] // period) * period
    for moment in times:
        if moment >= grid:
            dates.append(moment)
            grid += period
    return dates


def build_panel(
    times: list[int],
    closes: list[float],
    symbol: str,
    dates: list[int],
    lookback_seconds: int,
    max_age: int | None = None,
) -> Panel:
    """Sample one symbol's closes and lookback returns onto the rebalance calendar."""
    sampled = [close_at(times, closes, date, max_age) for date in dates]
    momentum: list[float | None] = []
    for date, value in zip(dates, sampled):
        past = close_at(times, closes, date - lookback_seconds, max_age)
        momentum.append(None if value is None or past in (None, 0) else value / past - 1.0)
    return Panel(symbol=symbol, closes=sampled, momentum=momentum)


def load_calendar(
    data_dir: Path | str,
    timeframe: str,
    *,
    calendar: str = DEFAULT_CALENDAR,
) -> list[int]:
    """Rebalance calendar for a timeframe, taken from a reference symbol.

    Using one liquid symbol as the calendar keeps the memory cost independent of
    the universe size: every other symbol is only ever asked for its close on
    those dates.
    """
    times, _ = read_closes(data_dir, calendar, timeframe)
    if len(times) < 2:
        raise ValueError(f"{calendar} {timeframe} has too few bars to be a calendar")
    return times


def read_closes(data_dir: Path | str, symbol: str, timeframe: str) -> tuple[list[int], list[float]]:
    """Only the `time` and `close` columns of a series — the fast path for panels."""
    import pyarrow.parquet as pq

    files = data.series_files(data_dir, symbol, timeframe)
    if not files:
        raise FileNotFoundError(f"no parquet files for {symbol} {timeframe} under {data_dir}")
    pairs: dict[int, float] = {}
    for path in files:
        table = pq.read_table(path, columns=["time", "close"])
        columns = table.to_pydict()
        for moment, close in zip(columns["time"], columns["close"]):
            pairs[moment] = close
    ordered = sorted(pairs)
    return ordered, [pairs[moment] for moment in ordered]


def select(momentum: dict[str, float], top: float, mode: str) -> tuple[list[str], list[str]]:
    """The strongest and weakest slices of a momentum cross-section.

    `top` is a fraction of the ranked universe when below 1, or an absolute count
    when at least 1. Ties are broken by symbol name so a run is reproducible.
    """
    if not momentum:
        return [], []
    ranked = sorted(momentum.items(), key=lambda item: (-item[1], item[0]))
    size = max(1, round(len(ranked) * top)) if top < 1 else int(top)
    size = min(size, len(ranked))
    longs = [symbol for symbol, _ in ranked[:size]]
    shorts: list[str] = []
    if mode == "long-short":
        # Weakest first, mirroring the long side's strongest first.
        shorts = [symbol for symbol, _ in reversed(ranked[-size:])]
        shorts = [symbol for symbol in shorts if symbol not in longs]
    return longs, shorts


def run_portfolio(
    panels: list[Panel],
    dates: list[int],
    *,
    lookback: int,
    rebalance: int,
    top: float = 0.2,
    mode: str = "long-only",
    costs: Costs | None = None,
    label: str = "cross-sectional momentum",
    bars_per_year: float = 365.0,
) -> PortfolioResult:
    """Rank, hold, rebalance, and charge for the turnover."""
    if mode not in ("long-only", "long-short"):
        raise ValueError("mode must be 'long-only' or 'long-short'")
    if top <= 0:
        raise ValueError("top must be positive")
    costs = costs or Costs()
    if len(dates) < 3 or not panels:
        raise ValueError("need at least a few rebalance dates and one symbol with data")
    # The curve has one point per rebalance, so that is the period to annualise by.
    per_year = bars_per_year / rebalance

    warnings: list[str] = []
    by_symbol = {panel.symbol: panel for panel in panels}
    weights: dict[str, float] = {}
    equity: list[float] = [1.0]
    rebalances: list[Rebalance] = []
    fees_paid = 0.0
    dropped = 0

    wiped_out: int | None = None
    for k in range(len(dates) - 1):
        momentum = {
            panel.symbol: value
            for panel in panels
            if (value := panel.momentum[k]) is not None and panel.closes[k] is not None
        }
        longs, shorts = select(momentum, top, mode)
        if not longs and not shorts:
            equity.append(equity[-1])
            rebalances.append(Rebalance(k, dates[k], [], [], 0.0, 0))
            continue

        # Mark the book to the next rebalance before rebalancing it. The return
        # is `sum(weight * (ratio - 1))`, not `sum(weight * ratio)`: the two agree
        # only when the weights sum to 1, and a long/short book sums to zero with
        # the balance sitting in cash.
        growth = 1.0
        drifted: dict[str, float] = {}
        for symbol, weight in weights.items():
            panel = by_symbol[symbol]
            start, end = panel.closes[k], panel.closes[k + 1]
            if start is None:
                continue
            if end is None:  # no bar since: the position sits at its last print
                dropped += 1
                end = start
            ratio = end / start
            growth += weight * (ratio - 1.0)
            drifted[symbol] = weight * ratio
        value_after = equity[-1] * growth
        # Normalise the drifted book back to the capital it now represents, so
        # the weights sum to it again (1 for a long-only book, 0 for long/short)
        # and the turnover below measures a trade rather than a price move.
        # Dividing twice here charged commission for a position that was merely
        # held: a single holding that tripled paid 61% turnover per rebalance.
        if growth > 0 and drifted:
            drifted = {symbol: amount / growth for symbol, amount in drifted.items()}

        side = 0.5 if mode == "long-short" and shorts else 1.0
        targets: dict[str, float] = {}
        for symbol in longs:
            targets[symbol] = targets.get(symbol, 0.0) + side / len(longs)
        for symbol in shorts:
            targets[symbol] = targets.get(symbol, 0.0) - side / len(shorts)

        turnover = sum(
            abs(targets.get(symbol, 0.0) - drifted.get(symbol, 0.0))
            for symbol in set(targets) | set(drifted)
        )
        cost = value_after * turnover * costs.rate
        fees_paid += cost
        value_after -= cost

        weights = targets
        rebalances.append(Rebalance(k, dates[k], longs, shorts, turnover, len(momentum)))
        if value_after <= 0:
            # A geared book can lose more than everything in one period: a short
            # leg on a symbol that multiplied. The portfolio is gone, so the rest
            # of the curve is zero rather than negative.
            equity.append(0.0)
            wiped_out = dates[k + 1]
            equity.extend([0.0] * (len(dates) - len(equity)))
            break
        equity.append(value_after)

    # Benchmarks and statistics need one point per rebalance, starting at 1.
    base = equity[0]
    curve = [value / base for value in equity]
    benchmark = equal_weight_benchmark(panels, dates, costs)
    result = PortfolioResult(
        label=label,
        costs=costs,
        lookback=lookback,
        rebalance=rebalance,
        top=top,
        mode=mode,
        rebalances=rebalances,
        equity=curve,
        performance=metrics.performance(curve, per_year),
        benchmark_equity=benchmark,
        benchmark=metrics.performance(benchmark, per_year),
        universe=len(panels),
        holdings_mean=statistics.fmean(
            [len(r.longs) + len(r.shorts) for r in rebalances] or [0.0]
        ),
        fees_paid=fees_paid,
        dropped=dropped,
        warnings=warnings,
    )
    if wiped_out is not None:
        result.warnings.append(
            f"portfolio wiped out at {data.iso(wiped_out)} UTC: the book lost more than "
            "100% in one holding period, so the rest of the curve is flat at zero"
        )
    if result.dropped:
        result.warnings.append(
            f"{result.dropped} holding(s) had no bar at the next rebalance and were marked at "
            "their last print (delisting or a data gap)"
        )
    if len(panels) < 20:
        result.warnings.append(
            f"only {len(panels)} symbols: a cross-section needs breadth to mean anything"
        )
    return result


def equal_weight_benchmark(panels: list[Panel], dates: list[int], costs: Costs) -> list[float]:
    """Equal weight the whole universe: buy each symbol when it first appears, never rebalance.

    Weighting is fixed at `1 / universe`, so a symbol that lists halfway through
    the sample is bought with its share at that point and the share sits in cash
    until then. That is the honest passive alternative to a ranking strategy: the
    same universe, the same dates, no selection and no rebalancing.
    """
    if not panels:
        return [1.0] * len(dates)
    weight = 1.0 / len(panels)
    entries: list[int | None] = []
    for panel in panels:
        entries.append(next((k for k, close in enumerate(panel.closes) if close is not None), None))

    curve: list[float] = []
    for k in range(len(dates)):
        total = 0.0
        for panel, entry in zip(panels, entries):
            if entry is None or k < entry:
                total += weight  # not listed yet: that share is in cash
                continue
            latest = panel.closes[k]
            if latest is None:  # gone since: mark at the last print it had
                latest = next(
                    (panel.closes[j] for j in range(k - 1, entry - 1, -1) if panel.closes[j] is not None),
                    panel.closes[entry],
                )
            total += weight * (latest / panel.closes[entry]) * (1.0 - costs.rate) ** 2
        curve.append(total)
    return curve


def render(result: PortfolioResult, dates: list[int]) -> str:
    """Console report: what was held, what it cost, and how it compares."""
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    add(f"{result.label} — {result.mode}, {result.costs}")
    add("=" * 78)
    add(
        f"universe    : {result.universe} symbols, {len(result.rebalances)} rebalances, "
        f"~{result.holdings_mean:.0f} positions each"
    )
    add(
        f"settings    : lookback {result.lookback} bars, rebalance every {result.rebalance} bars, "
        f"top {result.top:g}"
    )
    add(
        f"turnover    : {result.mean_turnover:.2f} per rebalance, fees paid {pct(result.fees_paid)} "
        "of starting capital"
    )
    if result.rebalances:
        last = result.rebalances[-1]
        add("")
        add(f"last rebalance {data.iso(last.time)} UTC — {last.candidates} symbols ranked")
        add(f"  long : {', '.join(last.longs[:12])}{'…' if len(last.longs) > 12 else ''}")
        if last.shorts:
            add(f"  short: {', '.join(last.shorts[:12])}{'…' if len(last.shorts) > 12 else ''}")
    add("")
    add(f"{'metric':<26}{'portfolio':>18}{'equal weight':>18}")
    add("-" * 62)

    def row(label: str, left: str, right: str) -> None:
        add(f"{label:<26}{left:>18}{right:>18}")

    row("total return", pct(result.performance.total_return), pct(result.benchmark.total_return))
    row("CAGR", pct(result.performance.cagr), pct(result.benchmark.cagr))
    row("annualised vol", pct(result.performance.ann_vol), pct(result.benchmark.ann_vol))
    row("Sharpe (rf=0)", f"{result.performance.sharpe:.2f}", f"{result.benchmark.sharpe:.2f}")
    row("max drawdown", pct(result.performance.max_dd), pct(result.benchmark.max_dd))
    row("years", f"{result.performance.years:.2f}", f"{result.benchmark.years:.2f}")
    add("")
    add(
        "note        : the curve is marked at rebalances, so drawdowns inside a "
        "holding period are not visible"
    )
    for warning in result.warnings:
        add(f"WARNING     : {warning}")
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcs-portfolio",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--lookback", type=int, default=30, help="ranking window, in bars")
    parser.add_argument("--rebalance", type=int, default=30, help="bars between rebalances")
    parser.add_argument("--top", type=float, default=0.2, help="fraction (<1) or count (>=1) per side")
    parser.add_argument("--mode", default="long-only", choices=("long-only", "long-short"))
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument("--min-bars", type=int, default=0, help="skip symbols shorter than this")
    parser.add_argument("--limit", type=int, default=None, help="use only the first N symbols")
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR, help="symbol whose bars define the schedule")
    parser.add_argument("--data-dir", type=Path, default=data.DEFAULT_DATA_DIR)
    parser.add_argument("--json", type=Path, default=None)
    return parser.parse_args(argv)


def universes(data_dir: Path | str, timeframe: str, *, limit: int | None = None) -> list[str]:
    """Every symbol that has a series of this timeframe, alphabetically."""
    pairs = sorted(symbol for symbol, tf in data.available_series(data_dir) if tf == timeframe)
    return pairs[:limit] if limit else pairs


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = universes(args.data_dir, args.timeframe, limit=args.limit)
    if len(symbols) < 2:
        raise SystemExit(f"not enough series for {args.timeframe} under {args.data_dir}")

    calendar_times = load_calendar(args.data_dir, args.timeframe, calendar=args.calendar)
    dates = rebalance_dates(calendar_times, args.rebalance)
    if len(dates) < 3:
        raise SystemExit(
            f"{args.rebalance}-bar rebalancing leaves {len(dates)} dates; use a shorter interval"
        )
    step = int(statistics.median([b - a for a, b in zip(calendar_times, calendar_times[1:])]))
    lookback_seconds = args.lookback * step
    # A symbol that has not printed for five bars is treated as delisted.
    max_age = 5 * step

    panels: list[Panel] = []
    for symbol in symbols:
        try:
            times, closes = read_closes(args.data_dir, symbol, args.timeframe)
        except FileNotFoundError:
            continue
        if len(times) < args.min_bars:
            continue
        panel = build_panel(times, closes, symbol, dates, lookback_seconds, max_age)
        if any(value is not None for value in panel.momentum):
            panels.append(panel)
    print(f"loaded {len(panels)} symbols on {len(dates)} rebalance dates ({args.timeframe})")

    result = run_portfolio(
        panels,
        dates,
        lookback=args.lookback,
        rebalance=args.rebalance,
        top=args.top,
        mode=args.mode,
        costs=Costs(fee_per_side=args.fee, slippage_per_side=args.slippage),
        label=f"{args.timeframe} cross-sectional momentum",
        bars_per_year=data.bars_per_year(args.timeframe),
    )
    print(render(result, dates))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result.as_dict(), indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
