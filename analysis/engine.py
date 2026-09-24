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
    "buy_and_hold",
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

        # Close the outgoing leg. When the exposure only flips sign, one of the
        # two sides paid belongs to the incoming leg, so the exit keeps one side.
        if open_trade is not None:
            exit_equity = after_costs if pos == 0 else after_costs / (1.0 - rate)
            open_trade.exit_index = i
            open_trade.exit_time = bars[i].time
            open_trade.exit_price = bars[i].open
            open_trade.equity_at_exit = exit_equity
            open_trade.bars_held = i - open_trade.entry_index
            open_trade.gross_return = (bars[i].open / open_trade.entry_price) ** open_trade.direction - 1.0
            open_trade.net_return = exit_equity / open_trade.equity_at_entry - 1.0
            trades.append(open_trade)
            open_trade = None

        if pos != 0:
            # A trade is measured from just *before* its own entry commission to
            # just *after* its exit commission, so a round trip carries both
            # sides of the cost and compounding all trades reproduces the equity
            # curve. On a sign flip, the first of the two sides paid belongs to
            # the outgoing leg, so the new basis is one side lower.
            entry_basis = before_costs if prev == 0 else before_costs * (1.0 - rate)
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
    """Passive benchmark: buy the first bar's open, sell the last bar's close.

    It pays the same two sides of commission as a strategy round trip, so the
    comparison is like for like. The risk statistics (volatility, Sharpe,
    drawdown) come from the price path itself, because two one-off commissions
    are a drag on the outcome rather than per-bar risk; the headline return is
    net of both sides.
    """
    if not bars:
        raise ValueError("no bars to benchmark")
    costs = costs or Costs()
    equity = [b.close / bars[0].open for b in bars]
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
