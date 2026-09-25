"""The execution model: turning a strategy's target exposure into an equity curve.

The one rule everything follows, chosen so a backtest cannot peek at the future:

    the target is read from the **close** of bar `t`;
    the position is changed at the **open of bar `t+1`**;
    equity is marked at bar opens, so the exposure held over
    `(open[t], open[t+1])` is `target[t-1]`.

Two invariants are checked on every run:

* every change of exposure pays commission on the notional traded, so a round
  trip costs two sides and a flip costs two sides as well;
* compounding every trade's `net_return` reproduces the equity curve exactly
  (`BacktestResult.bookkeeping_error`). That identity is what catches a
  look-ahead bug: apply a bar's move to the position decided on that same bar's
  close and the curve explodes while the trade list stops matching it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace

from .data import Bar, bars_per_year as bars_per_year_of, iso
from .metrics import Performance, cagr, performance

__all__ = [
    "Costs",
    "Trade",
    "BacktestResult",
    "Benchmark",
    "run_backtest",
    "restrict",
    "buy_and_hold",
    "buy_and_hold_bars",
    "fees_in_window",
    "close_fill_final_equity",
    "bookkeeping_warning",
]


@dataclass(frozen=True, slots=True)
class Costs:
    """Trading costs, charged on the notional of every position change."""

    fee_per_side: float = 0.001
    slippage_per_side: float = 0.0

    def __post_init__(self) -> None:
        if self.fee_per_side < 0 or self.slippage_per_side < 0:
            raise ValueError("costs cannot be negative")
        if self.rate >= 1:
            raise ValueError("total cost per side must stay below 100%")

    @property
    def rate(self) -> float:
        return self.fee_per_side + self.slippage_per_side

    def __str__(self) -> str:
        parts = [f"fee {self.fee_per_side:.3%}/side"]
        if self.slippage_per_side:
            parts.append(f"slippage {self.slippage_per_side:.3%}/side")
        return ", ".join(parts)


@dataclass
class Trade:
    """One leg: from opening a non-zero exposure to closing it."""

    direction: int
    entry_index: int
    entry_time: int
    entry_price: float
    equity_at_entry: float
    exit_index: int | None = None
    exit_time: int | None = None
    exit_price: float | None = None
    equity_at_exit: float | None = None
    bars_held: int | None = None
    gross_return: float | None = None
    net_return: float | None = None
    open_at_end: bool = False

    @property
    def is_closed(self) -> bool:
        return not self.open_at_end

    @property
    def side(self) -> str:
        return "long" if self.direction > 0 else "short"


@dataclass
class Benchmark:
    """Passive alternative over the same window: buy the first open, hold to the end."""

    equity: list[float]
    performance: Performance
    gross_equity: float
    exposure: float = 1.0

    @property
    def label(self) -> str:
        return "buy & hold"


@dataclass
class BacktestResult:
    """Everything a run produced: curve, book, and summary statistics."""

    label: str
    targets: list[float]
    positions: list[float]
    equity: list[float]
    trades: list[Trade]
    costs: Costs
    fees_paid: float
    performance: Performance
    gross_equity: float
    benchmark: Benchmark
    close_fill_equity: float
    warnings: list[str] = field(default_factory=list)

    # -- derived, trade-level views -------------------------------------------

    @property
    def closed_trades(self) -> list[Trade]:
        return [t for t in self.trades if t.is_closed]

    @property
    def exposure(self) -> float:
        """Average absolute exposure actually held, in `[0, 1]`."""
        return sum(abs(p) for p in self.positions) / len(self.positions)

    @property
    def win_rate(self) -> float:
        closed = self.closed_trades
        if not closed:
            return 0.0
        return sum(1 for t in closed if (t.net_return or 0.0) > 0) / len(closed)

    @property
    def avg_trade(self) -> float:
        return _mean([t.net_return or 0.0 for t in self.closed_trades])

    @property
    def median_trade(self) -> float:
        return _median([t.net_return or 0.0 for t in self.closed_trades])

    @property
    def geo_trade(self) -> float:
        """Mean compounded return per trade — the rate the equity curve actually grew at."""
        returns = [t.net_return for t in self.closed_trades if t.net_return is not None and t.net_return > -1]
        if not returns:
            return 0.0
        return math.exp(sum(math.log1p(r) for r in returns) / len(returns)) - 1.0

    @property
    def avg_bars_held(self) -> float:
        return _mean([float(t.bars_held or 0) for t in self.closed_trades])

    @property
    def best_trade(self) -> float:
        closed = [t.net_return or 0.0 for t in self.closed_trades]
        return max(closed, default=0.0)

    @property
    def worst_trade(self) -> float:
        closed = [t.net_return or 0.0 for t in self.closed_trades]
        return min(closed, default=0.0)

    @property
    def open_position(self) -> bool:
        return self.positions[-1] != 0

    @property
    def final_equity(self) -> float:
        return self.performance.final_equity

    @property
    def bookkeeping(self) -> float:
        """Product of `(1 + net_return)` over every trade, including the open one."""
        product = 1.0
        for trade in self.trades:
            product *= 1.0 + (trade.net_return or 0.0)
        return product

    @property
    def bookkeeping_error(self) -> float:
        """Relative gap between the trade book and the equity curve; ~1e-14 is float noise."""
        if self.final_equity <= 0:
            return 0.0
        return abs(self.bookkeeping / self.final_equity - 1.0)

    def as_dict(self, *, include_curves: bool = False) -> dict:
        """JSON-friendly summary. Curves are omitted unless asked for."""
        data = {
            "label": self.label,
            "costs": {"fee_per_side": self.costs.fee_per_side, "slippage_per_side": self.costs.slippage_per_side},
            "fees_paid": self.fees_paid,
            "gross_equity": self.gross_equity,
            "close_fill_equity": self.close_fill_equity,
            "exposure": self.exposure,
            "closed_trades": len(self.closed_trades),
            "win_rate": self.win_rate,
            "avg_trade": self.avg_trade,
            "median_trade": self.median_trade,
            "geo_trade": self.geo_trade,
            "avg_bars_held": self.avg_bars_held,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "open_position": self.open_position,
            "bookkeeping": self.bookkeeping,
            "bookkeeping_error": self.bookkeeping_error,
            "warnings": list(self.warnings),
            "strategy": self.performance.as_dict(),
            "buy_and_hold": {
                "final_equity": self.benchmark.performance.final_equity,
                "gross_equity": self.benchmark.gross_equity,
                "total_return": self.benchmark.performance.total_return,
                "cagr": self.benchmark.performance.cagr,
                "ann_vol": self.benchmark.performance.ann_vol,
                "sharpe": self.benchmark.performance.sharpe,
                "max_dd": self.benchmark.performance.max_dd,
            },
        }
        if include_curves:
            data["equity"] = self.equity
            data["positions"] = self.positions
            data["targets"] = self.targets
            data["buy_hold_equity"] = self.benchmark.equity
        return data


def run_backtest(
    bars: list[Bar],
    targets: list[float],
    timeframe: str,
    costs: Costs | None = None,
    *,
    label: str = "strategy",
    strict: bool = False,
) -> BacktestResult:
    """Run one strategy over one series.

    `targets[t]` is the exposure the strategy wants *after* seeing the close of
    bar `t` (0 flat, 1 fully long, -1 fully short, fractions allowed); it takes
    effect at the open of bar `t+1`. `targets[0]` therefore never trades.

    Raises `ValueError` on a length mismatch or an exposure outside `[-1, 1]`.
    """
    if not bars:
        raise ValueError("no bars to run on")
    if len(targets) != len(bars):
        raise ValueError(f"got {len(targets)} targets for {len(bars)} bars")
    for t in targets:
        if not -1.0 <= t <= 1.0:
            raise ValueError(f"exposure {t} outside [-1, 1]")

    costs = costs or Costs()
    rate = costs.rate
    n = len(bars)

    equity = [1.0] * n
    positions = [0.0] * n
    trades: list[Trade] = []
    open_trade: Trade | None = None
    fees_paid = 0.0

    wiped_out_at: int | None = None

    for i in range(1, n):
        if equity[i - 1] <= 0:
            # The account was wiped out earlier: there is nothing left to trade.
            equity[i] = 0.0
            positions[i] = 0.0
            continue

        prev = positions[i - 1]
        pos = float(targets[i - 1])
        # The move into open[i] belongs to `prev`: the exposure already held
        # when bar i-1 opened. The decision `pos` is only credited from open[i].
        before_costs = equity[i - 1] * (1.0 + prev * (bars[i].open / bars[i - 1].open - 1.0))
        turn = abs(pos - prev)

        if turn:
            cost = rate * turn * before_costs
            fees_paid += cost
        after_costs = before_costs * (1.0 - rate) ** turn
        equity[i] = after_costs
        positions[i] = pos

        if after_costs <= 0:
            # A geared exposure can lose everything in one bar (a full short when
            # the price doubles, say). The run stops here at zero equity.
            wiped_out_at = i
            if open_trade is not None:
                open_trade.exit_index = i
                open_trade.exit_time = bars[i].time
                open_trade.exit_price = bars[i].open
                open_trade.equity_at_exit = 0.0
                open_trade.bars_held = i - open_trade.entry_index
                open_trade.gross_return = (bars[i].open / open_trade.entry_price) ** open_trade.direction - 1.0
                open_trade.net_return = -1.0
                trades.append(open_trade)
                open_trade = None
            for j in range(i + 1, n):
                equity[j] = 0.0
                positions[j] = 0.0
            break

        if pos == prev:
            continue

        # A trade is a holding period: it ends when the exposure reaches zero or
        # turns around, not when a strategy merely resizes it. Without that,
        # volatility targeting — which adjusts the size on most bars — would book
        # thousands of one-bar "trades" that are really one position.
        if open_trade is not None and (pos == 0 or (pos > 0) != (prev > 0)):
            # Close the outgoing leg at a real point on the equity curve: the
            # level just after the commission for this change has been paid. On a
            # sign flip that level is also where the incoming leg starts, so the
            # two books meet exactly and compounding the trades still reproduces
            # the curve.
            open_trade.exit_index = i
            open_trade.exit_time = bars[i].time
            open_trade.exit_price = bars[i].open
            open_trade.equity_at_exit = after_costs
            open_trade.bars_held = i - open_trade.entry_index
            open_trade.gross_return = (bars[i].open / open_trade.entry_price) ** open_trade.direction - 1.0
            open_trade.net_return = after_costs / open_trade.equity_at_entry - 1.0
            trades.append(open_trade)
            open_trade = None

        if pos != 0 and open_trade is None:
            # A trade runs from just *before* its own entry commission to just
            # *after* its exit commission, so a round trip carries both sides of
            # the cost and `prod(1 + net_return)` equals the equity curve — for
            # integer sides and for fractional exposure alike. Anything that
            # happens inside the trade (a volatility target resizing it, say) is
            # simply part of the path between those two points.
            entry_basis = before_costs if prev == 0 else after_costs
            open_trade = Trade(
                direction=1 if pos > 0 else -1,
                entry_index=i,
                entry_time=bars[i].time,
                entry_price=bars[i].open,
                equity_at_entry=entry_basis,
            )

    # Mark an open position to market at the last close (its exit is not paid).
    last = bars[-1]
    mark_to_market = equity[-1] * (1.0 + positions[-1] * (last.close / last.open - 1.0))
    if open_trade is not None:
        open_trade.exit_index = n - 1
        open_trade.exit_time = last.time
        open_trade.exit_price = last.close
        open_trade.equity_at_exit = mark_to_market
        open_trade.bars_held = n - 1 - open_trade.entry_index
        open_trade.gross_return = (last.close / open_trade.entry_price) ** open_trade.direction - 1.0
        open_trade.net_return = mark_to_market / open_trade.equity_at_entry - 1.0
        open_trade.open_at_end = True
        trades.append(open_trade)
    # Same signal path with zero costs, to show what trading alone cost.
    gross_equity = 1.0
    for i in range(1, n):
        held = positions[i - 1]
        gross_equity *= 1.0 + held * (bars[i].open / bars[i - 1].open - 1.0)
    gross_equity *= 1.0 + positions[-1] * (last.close / last.open - 1.0)

    timestamps = [b.time for b in bars]
    perf = performance(equity, bars_per_year_of(timeframe), final_equity=mark_to_market, timestamps=timestamps)

    result = BacktestResult(
        label=label,
        targets=list(targets),
        positions=positions,
        equity=equity,
        trades=trades,
        costs=costs,
        fees_paid=fees_paid,
        performance=perf,
        gross_equity=gross_equity,
        benchmark=buy_and_hold(bars, costs, timeframe),
        close_fill_equity=close_fill_final_equity(bars, targets, costs),
    )

    if wiped_out_at is not None:
        result.warnings.append(
            f"account wiped out at {iso(bars[wiped_out_at].time)} UTC: equity reached zero, "
            "so the remaining bars are not traded"
        )

    warning = bookkeeping_warning(result)
    if warning:
        if strict:
            raise AssertionError(warning)
        result.warnings.append(warning)
    return result


def bookkeeping_warning(result: BacktestResult) -> str | None:
    """`None` when compounding the trades reproduces the equity curve.

    The two are computed independently — one from the price path, one from the
    trade list — so a mismatch means the execution model drifted from what the
    trade book claims. Tolerance is float noise (relative), not a budget.
    """
    if result.final_equity <= 0 or result.bookkeeping_error <= 1e-9:
        return None
    return (
        f"trade book and equity curve disagree by {result.bookkeeping_error:.3e} "
        f"({result.bookkeeping:.6f}x vs {result.final_equity:.6f}x)"
    )


def buy_and_hold(bars: list[Bar], costs: Costs | None = None, timeframe: str = "1d") -> Benchmark:
    """Passive benchmark over a whole series: see `buy_and_hold_bars`."""
    return buy_and_hold_bars(bars, timeframe, costs)


def buy_and_hold_bars(
    bars: list[Bar], timeframe: str = "1d", costs: Costs | None = None
) -> Benchmark:
    """Passive benchmark: buy at the first open *any* strategy could have used.

    The entry is the **second** bar's open, not the first's. Every strategy in
    this toolkit reads its signal from a bar's close and is filled at the next
    bar's open, so `open[1]` is the earliest price a strategy can trade at — and
    the benchmark has to start from the same place, or the comparison flatters
    it. On a listing bar the gap is not a rounding detail: PYTH-USDT's first
    hourly bar ran from 0.06 to 0.319 (`close/open` 5.3x), so a benchmark entered
    at `open[0]` was handed a move nobody could have traded and reported +13.5%
    for a series that fell 78.8% from the first price actually available.

    The curve keeps one point per bar so it lines up with the strategy's: bar 0
    is cash at 1.0, the position is bought at `open[1]` and sold at the last
    close, paying one commission side at each end. The risk statistics
    (volatility, Sharpe, drawdown) come from the price path itself, because two
    one-off commissions are a drag on the outcome rather than per-bar risk; the
    headline return is net of both sides.

    Callers that measure a *window* — `kcs-basket` on every leg, `restrict` on a
    backtest — pass that window's bars, so the same rule decides the same way
    there: the benchmark enters one bar into the window, never at its first bar.
    """
    if not bars:
        raise ValueError("no bars to benchmark")
    costs = costs or Costs()
    base = bars[1].open if len(bars) > 1 else bars[0].open
    equity = [1.0] + [bar.close / base for bar in bars[1:]]
    perf = performance(
        equity,
        bars_per_year_of(timeframe),
        timestamps=[b.time for b in bars],
    )
    net_final = equity[-1] * (1.0 - costs.rate) ** 2  # one entry, one exit
    perf = replace(
        perf,
        final_equity=net_final,
        total_return=net_final - 1.0,
        cagr=cagr(net_final, perf.years),
    )
    return Benchmark(equity=equity, performance=perf, gross_equity=equity[-1])


def fees_in_window(run: BacktestResult, rate: float, start_index: int) -> float:
    """Commission the engine charged from `start_index` on, in units of equity 1.0.

    The engine only reports the total for the run, and a run may be longer than
    the window being reported; this recovers the part inside the window from the
    same identity the engine uses — `equity[i]` is `before_costs` times
    `(1 − rate)^turnover` — which `test_engine.py` checks against the reported
    total when the window covers the whole series.
    """
    if rate <= 0:
        return 0.0
    paid = 0.0
    for i in range(max(start_index, 1), len(run.equity)):
        if run.equity[i] <= 0:  # wiped out here: the engine stops charging
            continue
        turn = abs(run.positions[i] - run.positions[i - 1])
        if turn:
            paid += rate * turn * run.equity[i] / (1.0 - rate) ** turn
    return paid


def restrict(
    result: BacktestResult,
    bars: list[Bar],
    timeframe: str,
    *,
    first: int,
    label: str | None = None,
) -> BacktestResult:
    """The part of a run inside `[first, the end]`, rebased to 1.0 at its start.

    Slicing the *result* rather than the *input* is the whole point: the strategy
    still saw every bar before the window, so its signals are the ones it really
    had — truncating the series instead would leave the indicators cold and
    invent a different history. Nothing after `first` is dropped from what came
    before it, and nothing after the last bar was ever read.

    Everything a reader looks at is then window-relative, in the same way the
    basket is: the curve starts at 1.0, the benchmark enters at the window's
    second open (`buy_and_hold_bars` on the window), fees are restated against
    the capital the window started with, and a position that was already open
    when the window began is carried in as a partial trade so that compounding
    the trade book still reproduces the curve exactly.
    """
    inside = [i for i, bar in enumerate(bars) if bar.time >= first]
    if len(inside) < 3:
        raise ValueError(
            f"the window starting {iso(bars[inside[0]].time) if inside else 'after the end'} "
            f"holds {len(inside)} bar(s): that is not enough to measure anything"
        )
    start, end = inside[0], inside[-1]
    base = result.equity[start]
    if base <= 0:
        raise ValueError("the account was already wiped out when the window opened")
    if start == 0:
        return result  # the window is the whole run: nothing to rebase

    window = [bars[i] for i in inside]
    times = [bar.time for bar in window]
    per_year = bars_per_year_of(timeframe)

    equity = [result.equity[i] / base for i in inside]
    equity[-1] = result.performance.final_equity / base  # the open position, marked to market
    positions = [result.positions[i] for i in inside]
    targets = [result.targets[i] for i in inside]

    trades: list[Trade] = []
    for trade in result.trades:
        if trade.entry_index >= start:
            trades.append(trade)
            continue
        if trade.exit_index is None or trade.exit_index < start:
            continue
        trades.append(_carried_trade(trade, bars, start, base))

    gross = 1.0
    for k in range(1, len(inside)):
        gross *= 1.0 + positions[k - 1] * (bars[inside[k]].open / bars[inside[k - 1]].open - 1.0)
    gross *= 1.0 + positions[-1] * (window[-1].close / window[-1].open - 1.0)

    perf = performance(equity, per_year, final_equity=equity[-1], timestamps=times)
    restricted = BacktestResult(
        label=label or result.label,
        targets=targets,
        positions=positions,
        equity=equity,
        trades=trades,
        costs=result.costs,
        fees_paid=fees_in_window(result, result.costs.rate, start) / base,
        performance=perf,
        gross_equity=gross,
        benchmark=buy_and_hold_bars(window, timeframe, result.costs),
        close_fill_equity=close_fill_final_equity(window, targets, result.costs),
        warnings=list(result.warnings),
    )
    if any(t.entry_index == start and t.equity_at_entry == 1.0 and t.bars_held for t in trades):
        restricted.warnings.append(
            "a position was already open when the window began: it is shown as a trade from "
            "the window's first bar, and its earlier history is not part of this window"
        )
    warning = bookkeeping_warning(restricted)
    if warning:
        restricted.warnings.append(warning)
    return restricted


def _carried_trade(trade: Trade, bars: list[Bar], start: int, base: float) -> Trade:
    """A position held when the window opened, as a trade that starts with it."""
    exit_price = trade.exit_price if trade.exit_price is not None else bars[-1].close
    gross = (exit_price / bars[start].open) ** trade.direction - 1.0
    return Trade(
        direction=trade.direction,
        entry_index=start,
        entry_time=bars[start].time,
        entry_price=bars[start].open,
        equity_at_entry=1.0,
        exit_index=trade.exit_index,
        exit_time=trade.exit_time,
        exit_price=exit_price,
        equity_at_exit=(trade.equity_at_exit or 0.0) / base,
        bars_held=(trade.exit_index - start) if trade.exit_index is not None else None,
        gross_return=gross,
        net_return=((trade.equity_at_exit or 0.0) / base) - 1.0,
        open_at_end=trade.open_at_end,
    )


def close_fill_final_equity(bars: list[Bar], targets: list[float], costs: Costs | None = None) -> float:
    """Same signals, but filled at the signal bar's own close.

    The usual backtest convention, kept only as a sensitivity check: it assumes
    you can always trade at the closing tick. The gap to `run_backtest`'s
    next-open fill is what acting one bar later costs.
    """
    if len(targets) != len(bars):
        raise ValueError(f"got {len(targets)} targets for {len(bars)} bars")
    rate = (costs or Costs()).rate
    equity = 1.0
    prev = 0.0
    for i in range(1, len(bars)):
        pos = float(targets[i - 1])
        equity *= 1.0 + pos * (bars[i].close / bars[i - 1].close - 1.0)
        if pos != prev:
            equity *= (1.0 - rate) ** abs(pos - prev)
        prev = pos
    return equity


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0
