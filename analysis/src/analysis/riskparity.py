"""A book of several tokens, weighted unequally and de-risked when volatility rises.

This is the "hold a few names, in different proportions, and move money out of the
volatile ones" idea, written down as something measurable:

* **the universe** is the top `top` symbols by trailing turnover, re-selected on the
  rebalance calendar from data that existed at that moment. `min_history` keeps the
  fresh listings out of it, which matters more than anything else here: on this
  archive, demanding three years of history instead of one cut the commission bill
  from 145% of capital to 17% and the volatility from 111% to 33% (CONCLUSIONS §1.6).
  Selecting by a rule rather than by hand is also what makes the result honest — the
  hand-picked five-major basket in §1.6 cannot say that;
* **the weights** are either equal or proportional to `1/volatility`. Inverse
  volatility is the literal reading of the request: a token whose volatility rises —
  usually because it is falling — gets a smaller target, so the position is cut, and
  a riser is trimmed back to its target rather than left to run. Measured, it pays
  off only on a wild universe (top 10 with one year of history: Sharpe 0.49 → 0.61);
  once `min_history` filters the listings out, equal weights win (0.77 against 0.62)
  and inverse volatility only adds turnover;
* **the risk budget** scales the whole book so that its own realised volatility stays
  near `vol_budget` a year, with the rest in cash. It never levers up, only down. It
  is a dial rather than a promise: volatility is estimated from the trailing window,
  and in crypto the next month is regularly worse than the last — asking for 40% on
  a wild universe delivered 111%, on an established one 33–48%;
* **the frequency** is `rebalance` bars, and it is the dial that matters most
  (CONCLUSIONS §1.1). Weekly to monthly is the plateau; every bar is a loss.

Everything is decided on a bar's close and applied on the next close, never on the
same one. Costs are charged on the traded notional measured against the weights as
they have *drifted*, which is the mistake `portfolio.py` made once (CONCLUSIONS §3).

Usage::

    uv run kcs-riskparity --top 5 --min-history 3y --vol-budget 0.4
    uv run kcs-riskparity --weight invvol --top 10 --min-history 1y
    uv run kcs-riskparity --vol-budget none --json        # the risk dial, switched off
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as st
import sys
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import Costs, data, metrics, report
from .metrics import Performance, pct

DATA_DIR = data.DEFAULT_DATA_DIR
OUT_DIR = data.repo_root() / "analysis" / "out"
DEFAULT_CALENDAR = "BTC-USDT"
WEIGHT_SCHEMES = ("equal", "invvol")
#: A symbol this calm is a stablecoin rather than a position. Inverse volatility
#: would otherwise put the whole book in it: measured on this archive, a top-20
#: inverse-vol book held 91.6% of USDC-USDT and looked wonderfully safe, because
#: it was 92% cash.
MIN_VOLATILITY = 0.10
#: Floor for the single-name cap: twice an equal weight, and never below this.
DEFAULT_MAX_WEIGHT = 0.25


def auto_max_weight(top: int) -> float:
    """Twice an equal weight, never below a quarter.

    A five-name book is capped at 40% a name, which lets inverse volatility lean
    without turning the book into one bet; a two-name book is not capped at all,
    because capping it would leave half the capital in cash by arithmetic rather
    than by decision.
    """
    return max(DEFAULT_MAX_WEIGHT, 2.0 / max(top, 1))
#: How stale a close may be before a symbol counts as gone from the universe.
MAX_AGE_DAYS = 3


@dataclass
class Series:
    """One symbol's closes and turnovers, read once and indexed by time."""

    symbol: str
    times: list[int]
    closes: list[float]
    turnovers: list[float]

    def close_at(self, moment: int) -> float | None:
        """The last close at or before `moment`."""
        index = bisect_right(self.times, moment)
        return self.closes[index - 1] if index else None

    def turnover_between(self, start: int, end: int) -> float:
        """Total turnover of the bars inside `[start, end]`."""
        low, high = bisect_right(self.times, start - 1), bisect_right(self.times, end)
        return sum(self.turnovers[low:high])


def read_series(data_dir: Path | str, symbol: str, timeframe: str) -> Series:
    """Only the `time`, `close` and `turnover` columns — what this module needs."""
    import pyarrow.parquet as pq

    files = data.series_files(data_dir, symbol, timeframe)
    if not files:
        raise FileNotFoundError(f"no parquet files for {symbol} {timeframe} under {data_dir}")
    closes: dict[int, tuple[float, float]] = {}
    for path in files:
        table = pq.read_table(path, columns=["time", "close", "turnover"]).to_pydict()
        for moment, close, turnover in zip(table["time"], table["close"], table["turnover"]):
            closes[moment] = (close, turnover)
    ordered = sorted(closes)
    return Series(
        symbol=symbol,
        times=ordered,
        closes=[closes[m][0] for m in ordered],
        turnovers=[closes[m][1] for m in ordered],
    )


@dataclass
class Aligned:
    """One symbol sampled onto the calendar: a forward-filled price and its turnover."""

    symbol: str
    prices: array
    turnovers: array
    first_index: int | None

    def history_bars(self, index: int) -> int:
        return 0 if self.first_index is None else index - self.first_index


def align(series: Series, calendar: list[int], max_age: int) -> Aligned:
    """Sample a series onto the calendar, forward-filling a missing close."""
    prices = array("d", [math.nan] * len(calendar))
    turnovers = array("d", [0.0] * len(calendar))
    by_time = dict(zip(series.times, zip(series.closes, series.turnovers)))
    last_close = math.nan
    last_moment: int | None = None
    first_index: int | None = None
    for index, moment in enumerate(calendar):
        bar = by_time.get(moment)
        if bar is not None:
            last_close, last_moment = bar[0], moment
            turnovers[index] = bar[1]
        elif last_moment is not None and moment - last_moment > max_age * 86400:
            last_close, last_moment = math.nan, None  # too stale: out of the universe
        prices[index] = last_close
        if first_index is None and not math.isnan(last_close):
            first_index = index
    return Aligned(symbol=series.symbol, prices=prices, turnovers=turnovers, first_index=first_index)


@dataclass
class Rebalance:
    """One decision: what was chosen, at what weight, and what it cost to get there."""

    index: int
    time: int
    filled_index: int
    holdings: list[str]
    weights: dict[str, float]
    turnover: float
    scale: float
    candidates: int
    #: What this rebalance cost, in the same units as the equity curve, so a window
    #: can report the commission it actually paid rather than the whole run's.
    cost: float = 0.0
    benchmark_cost: float = 0.0

    @property
    def invested(self) -> float:
        return sum(self.weights.values())


@dataclass
class RiskParityResult:
    """The book's curve, its rebalances, and the same universe held passively."""

    label: str
    timeframe: str
    top: int
    weight_scheme: str
    vol_budget: float | None
    rebalance: int
    lookback: int
    min_history_bars: int
    costs: Costs
    times: list[int]
    equity: list[float]
    benchmark_equity: list[float]
    rebalances: list[Rebalance]
    performance: Performance
    benchmark: Performance
    universe_size: int
    fees_paid: float
    benchmark_fees: float
    achieved_vol: float
    skipped: list[int] = field(default_factory=list)
    #: One entry per bar: the share of capital at work at that close.
    invested_by_bar: list[float] = field(default_factory=list)
    #: Commission paid on each bar, book and benchmark, in the same units as `equity`.
    cost_by_bar: list[float] = field(default_factory=list)
    benchmark_cost_by_bar: list[float] = field(default_factory=list)
    gated_out: list[int] = field(default_factory=list)
    #: The trailing-return gate, if one was on, and every name it took out.
    trend_bars: int | None = None
    trend_exits: list[tuple[int, str, float]] = field(default_factory=list)
    window_text: str | None = None
    warnings: list[str] = field(default_factory=list)
    #: Holding one price for the whole window — the reference every book has to beat,
    #: and a different animal from `benchmark_equity`, which re-selects and re-equalises
    #: its universe on the same grid as the book.
    buy_hold_equity: list[float] = field(default_factory=list)
    buy_hold_label: str = ""
    buy_hold: Performance | None = None

    @property
    def final_equity(self) -> float:
        return self.performance.final_equity

    @property
    def invested_mean(self) -> float:
        if not self.rebalances:
            return 0.0
        return st.fmean(r.invested for r in self.rebalances)

    @property
    def invested_bar_mean(self) -> float:
        """The average share of capital at work across bars, not across rebalances."""
        return st.fmean(self.invested_by_bar) if self.invested_by_bar else 0.0

    @property
    def flat_bar_share(self) -> float:
        """How often the book held nothing at all — the cost of a trend rule."""
        if not self.invested_by_bar:
            return 0.0
        return sum(1 for value in self.invested_by_bar if value <= 0.0) / len(self.invested_by_bar)

    @property
    def holdings_mean(self) -> float:
        if not self.rebalances:
            return 0.0
        return st.fmean(len(r.holdings) for r in self.rebalances)

    @property
    def turnover_mean(self) -> float:
        if not self.rebalances:
            return 0.0
        return st.fmean(r.turnover for r in self.rebalances)

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "timeframe": self.timeframe,
            "top": self.top,
            "weight_scheme": self.weight_scheme,
            "vol_budget": self.vol_budget,
            "rebalance_bars": self.rebalance,
            "lookback_bars": self.lookback,
            "min_history_bars": self.min_history_bars,
            "trend_bars": self.trend_bars,
            "trend_exits": len(self.trend_exits),
            "universe_size": self.universe_size,
            "window_text": self.window_text,
            "rebalances": len(self.rebalances),
            "holdings_mean": self.holdings_mean,
            "invested_mean": self.invested_mean,
            "invested_bar_mean": self.invested_bar_mean,
            "flat_bar_share": self.flat_bar_share,
            "turnover_mean": self.turnover_mean,
            "fees_paid": self.fees_paid,
            "benchmark_fees_paid": self.benchmark_fees,
            "achieved_vol": self.achieved_vol,
            "performance": self.performance.as_dict(),
            "benchmark_equal_weight": self.benchmark.as_dict(),
            "benchmark_buy_and_hold": (
                {"label": self.buy_hold_label, **self.buy_hold.as_dict()} if self.buy_hold else None
            ),
            "costs": {
                "fee_per_side": self.costs.fee_per_side,
                "slippage_per_side": self.costs.slippage_per_side,
            },
            "last_rebalance": (
                {
                    "time": self.rebalances[-1].time,
                    "holdings": self.rebalances[-1].holdings,
                    "weights": self.rebalances[-1].weights,
                    "invested": self.rebalances[-1].invested,
                }
                if self.rebalances
                else None
            ),
            "warnings": list(self.warnings),
        }


def restrict(
    result: RiskParityResult,
    *,
    first: int,
    text: str,
    label: str | None = None,
) -> RiskParityResult:
    """The part of a book's life inside `[first, the end]`, rebased to 1.0 there.

    Slicing the *result* rather than the *inputs* is the point, exactly as in
    `engine.restrict`: the universe ranking, the volatility estimates, the age
    filter and the risk budget's trailing volatility all still see the history
    before the window, so a windowed run measures the same book rather than a
    version of it that started cold with no trailing volatility to size itself.

    The book is taken as it stood on the window's first bar — rebasing says "this
    is what you held", not "this is what you would have bought". Commissions are
    the window's own, restated against the capital the window started with.
    """
    inside = [i for i, moment in enumerate(result.times) if moment >= first]
    if len(inside) < 3:
        raise ValueError(
            f"the window holds {len(inside)} bar(s) of {result.timeframe}: "
            "give a longer period"
        )
    if inside[0] == 0:
        return result
    start, base = inside[0], result.equity[inside[0]]
    if base <= 0:
        raise ValueError("the book was worth nothing when the window opened")
    benchmark_base = result.benchmark_equity[inside[0]] or 1.0
    times = [result.times[i] for i in inside]
    equity = [result.equity[i] / base for i in inside]
    benchmark = [result.benchmark_equity[i] / benchmark_base for i in inside]
    # A passive hold has no state to carry in: "what you held" and "what you would have
    # bought" are the same position, so the window pays one entry side and starts at 1.0.
    hold: list[float] = []
    if result.buy_hold_equity:
        hold_base = result.buy_hold_equity[inside[0]]
        if hold_base <= 0:
            raise ValueError("the buy & hold reference was worth nothing when the window opened")
        hold = [(result.buy_hold_equity[i] / hold_base) * (1.0 - result.costs.rate) for i in inside]
    per_year = data.bars_per_year(result.timeframe)
    # A window is its own timeline, so every index the result carries has to be
    # relative to it: markers, rows and the equity column all address the window.
    # (Leaving the full-run indices in place drew every marker outside the chart.)
    rebalances = [
        replace(
            r,
            index=max(r.index - inside[0], 0),
            filled_index=r.filled_index - inside[0],
        )
        for r in result.rebalances
        if r.filled_index >= inside[0]
    ]
    fees = sum(result.cost_by_bar[i] for i in inside) / base
    benchmark_fees = sum(result.benchmark_cost_by_bar[i] for i in inside) / base
    exits = [
        (i - inside[0], symbol, cost / base)
        for i, symbol, cost in result.trend_exits
        if i >= inside[0]
    ]
    invested_by_bar = [result.invested_by_bar[i] for i in inside]
    gated = [i - inside[0] for i in result.gated_out if i >= inside[0]]

    windowed = RiskParityResult(
        label=label or result.label,
        timeframe=result.timeframe,
        top=result.top,
        weight_scheme=result.weight_scheme,
        vol_budget=result.vol_budget,
        rebalance=result.rebalance,
        lookback=result.lookback,
        min_history_bars=result.min_history_bars,
        costs=result.costs,
        times=times,
        equity=equity,
        benchmark_equity=benchmark,
        rebalances=rebalances,
        performance=metrics.performance(equity, per_year, timestamps=times),
        benchmark=metrics.performance(benchmark, per_year, timestamps=times),
        universe_size=result.universe_size,
        fees_paid=fees,
        benchmark_fees=benchmark_fees,
        achieved_vol=metrics.performance(equity, per_year).ann_vol,
        invested_by_bar=invested_by_bar,
        cost_by_bar=[result.cost_by_bar[i] / base for i in inside],
        benchmark_cost_by_bar=[result.benchmark_cost_by_bar[i] / base for i in inside],
        gated_out=gated,
        trend_bars=result.trend_bars,
        trend_exits=exits,
        buy_hold_equity=hold,
        buy_hold_label=result.buy_hold_label,
        buy_hold=metrics.performance(hold, per_year, timestamps=times) if hold else None,
        window_text=text,
        warnings=list(result.warnings),
    )
    # The inherited warnings describe the whole run — "no symbol had enough history
    # in 2017" is noise in a report about last year — so they are rebuilt from the
    # facts inside the window instead of carried over.
    windowed.warnings = [
        w
        for w in result.warnings
        if not w.startswith(("no symbol had enough history", "the budget asked", "the trend rule emptied"))
    ]
    if windowed.gated_out:
        windowed.warnings.append(
            f"the trend rule emptied the book on {len(windowed.gated_out)} rebalance(s) inside the "
            "window: the capital was held in cash on those days"
        )
    inside_skips = [i for i in result.skipped if i >= inside[0]]
    if inside_skips:
        windowed.warnings.append(
            f"no symbol had enough history on {len(inside_skips)} rebalance(s) inside the window: "
            "the book was in cash on those days"
        )
    if result.vol_budget is not None and windowed.performance.ann_vol > result.vol_budget * 1.5:
        windowed.warnings.append(
            f"the budget asked for {result.vol_budget:.0%} a year and the window delivered "
            f"{windowed.performance.ann_vol:.0%}: trailing volatility is a forecast, not a promise"
        )
    windowed.warnings.append(
        "the window starts with the book as it stood: whatever was held on its first "
        "bar is held, and the return before that bar is not part of this window"
    )
    return windowed


def passes_trend(row: Aligned, index: int, trend: int | None) -> bool:
    """Is this name worth holding at `index`, by its own trailing return only?

    `trend` is a lookback in bars: the name stays only while its close is above the
    close that many bars ago — the same signal `Tsmom` reads, and the cheapest rule
    that has survived every test in this repository. A name too young to have a price
    that far back fails: it has no trend to be on yet. `None` turns the rule off.
    """
    if trend is None:
        return True
    if index - trend < 0:
        return False
    now, then = row.prices[index], row.prices[index - trend]
    if math.isnan(now) or math.isnan(then) or then <= 0:
        return False
    return now > then


def trailing_volatility(prices: list[float], bars_per_year: float) -> float:
    """Annualised standard deviation of the bar-to-bar returns inside `prices`.

    Simple returns (`p[i]/p[i-1] - 1`), population stdev (`pstdev`, dividing by N),
    scaled by `sqrt(bars_per_year)`. Same convention as `metrics.ann_vol`, so a
    window quoted here and the reported volatility of the same window agree.
    """
    returns = [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)) if prices[i - 1] > 0]
    if len(returns) < 3:
        return float("nan")
    return st.pstdev(returns) * math.sqrt(bars_per_year)


def select_universe(aligned: dict[str, Aligned], index: int, top: int, lookback: int, min_history: int):
    """The most traded symbols that existed for long enough, by past data only."""
    scored = []
    for symbol, row in aligned.items():
        if row.first_index is None or row.history_bars(index) < min_history:
            continue
        if math.isnan(row.prices[index]):
            continue
        turnover = float(sum(row.turnovers[max(0, index - lookback + 1) : index + 1]))
        scored.append((turnover, symbol))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _, symbol in scored[:top]], len(scored)


def cap_weights(weights: dict[str, float], max_weight: float) -> dict[str, float]:
    """No single name above `max_weight`; the excess goes to the rest.

    Without this, inverse volatility concentrates: a calm name gets a big weight and
    the book stops being a book. Whatever cannot be placed — a one-name universe
    capped at a quarter — simply stays in cash.
    """
    if max_weight >= 1.0 or not weights:
        return weights
    weights = dict(weights)
    for _ in range(10):
        over = [symbol for symbol, weight in weights.items() if weight > max_weight + 1e-12]
        if not over:
            break
        excess = sum(weights[symbol] - max_weight for symbol in over)
        for symbol in over:
            weights[symbol] = max_weight
        room = {s: w for s, w in weights.items() if w < max_weight - 1e-12}
        total_room = sum(room.values())
        if total_room <= 0:
            break
        for symbol, weight in room.items():
            weights[symbol] = weight + excess * weight / total_room
    return weights


def weights_for(
    universe: list[str],
    aligned: dict[str, Aligned],
    index: int,
    scheme: str,
    vol_window: int,
    bars_per_year: float,
    max_weight: float = DEFAULT_MAX_WEIGHT,
) -> dict[str, float]:
    """Equal weights, or proportional to `1/volatility`; capped and summing to 1."""
    if not universe:
        return {}
    if scheme == "equal":
        return cap_weights({symbol: 1.0 / len(universe) for symbol in universe}, max_weight)
    raw: dict[str, float] = {}
    for symbol in universe:
        window = [
            float(value)
            for value in aligned[symbol].prices[max(0, index - vol_window) : index + 1]
            if not math.isnan(value)
        ]
        vol = trailing_volatility(window, bars_per_year)
        if vol == vol and vol >= MIN_VOLATILITY:
            raw[symbol] = 1.0 / vol
    if not raw:
        return {}
    total = sum(raw.values())
    return cap_weights({symbol: amount / total for symbol, amount in raw.items()}, max_weight)


def _buy_and_hold_curve(
    aligned: dict[str, Aligned],
    calendar: list[int],
    symbol: str | None,
    costs: Costs,
) -> tuple[list[float], str]:
    """One price held from the first bar, one commission side paid on the way in.

    The curve is `price[i] / price[0]`, less the entry fee, marked at the same closes
    the book is marked at — so it is comparable line for line. A symbol listed after
    the calendar starts sits in cash (1.0) until its first price, which is what
    holding it would actually have done. No exit fee: like the book, an open position
    is not liquidated on the last bar.
    """
    if symbol is None:
        return [], ""
    row = aligned.get(symbol)
    if row is None:
        raise ValueError(f"--buy-hold {symbol}: no series for it on the calendar")
    prices = row.prices
    first = next((i for i, price in enumerate(prices) if not math.isnan(price)), None)
    if first is None:
        raise ValueError(f"--buy-hold {symbol}: it has no price at all")
    curve: list[float] = [1.0] * len(calendar)
    base = float(prices[first])
    last = base
    for index in range(first, len(calendar)):
        price = float(prices[index])
        if not math.isnan(price):
            last = price
        curve[index] = (last / base) * (1.0 - costs.rate)
    return curve, symbol


def run_riskparity(
    aligned: dict[str, Aligned],
    calendar: list[int],
    *,
    timeframe: str,
    top: int = 10,
    lookback: int = 30,
    rebalance: int = 30,
    weight_scheme: str = "equal",
    vol_budget: float | None = 0.4,
    vol_window: int = 30,
    min_history: int = 365,
    max_weight: float | None = None,
    costs: Costs | None = None,
    label: str = "risk parity",
    buy_hold: str | None = None,
    trend: int | None = None,
) -> RiskParityResult:
    """Run the book over the calendar. Decide on a close, act on the next one."""
    if weight_scheme not in WEIGHT_SCHEMES:
        raise ValueError(f"weight scheme must be one of {', '.join(WEIGHT_SCHEMES)}")
    if top < 1:
        raise ValueError("top must be at least 1 symbol")
    if lookback < 2 or vol_window < 3 or rebalance < 1 or min_history < 0:
        raise ValueError("lookback needs 2+ bars, vol_window 3+, rebalance 1+, min_history 0+")
    if trend is not None and trend < 1:
        raise ValueError("trend needs 1+ bars, or None to switch the rule off")
    if len(calendar) < 3:
        raise ValueError("the calendar holds fewer than three bars")
    costs = costs or Costs()
    bars_per_year = data.bars_per_year(timeframe)
    max_weight = auto_max_weight(top) if max_weight is None else max_weight

    hold_curve, hold_label = _buy_and_hold_curve(aligned, calendar, buy_hold, costs)
    weights: dict[str, float] = {}
    passive: dict[str, float] = {}          # the same names, equal weight, no budget
    pending: dict[str, float] | None = None
    pending_passive: dict[str, float] | None = None
    pending_info: tuple[int, list[str], float, int] | None = None
    equity = [1.0]
    benchmark = [1.0]
    fees_paid = 0.0
    benchmark_fees = 0.0
    rebalances: list[Rebalance] = []
    warnings: list[str] = []
    skipped: list[int] = []
    #: A name that loses its trend is sold on the next close, whatever the grid says;
    #: it can only come back at the next rebalance, which keeps entries on the same slow
    #: clock as the rest of the book. Exits are the one decision this module makes
    #: between rebalances.
    pending_exits: list[str] | None = None
    trend_exits: list[tuple[int, str, float]] = []
    gated_out: list[int] = []
    #: What the benchmark paid, per bar, including the rebalances the book skipped.
    benchmark_events: list[tuple[int, float]] = []
    #: The share of capital actually at work at the close of every bar. The rebalance
    #: table alone cannot answer "how much of the time was this book in cash" once a
    #: trend rule can empty it between rebalances.
    invested_by_bar: list[float] = [0.0]

    for index in range(1, len(calendar)):
        # 1. mark the book at this close; the weights drift on their own
        cash = 1.0 - sum(weights.values())
        grown: dict[str, float] = {}
        for symbol, share in weights.items():
            now, was = aligned[symbol].prices[index], aligned[symbol].prices[index - 1]
            if math.isnan(now) or math.isnan(was) or was <= 0:
                now, was = 1.0, 1.0  # gone from the feed: it rides its last print
            grown[symbol] = share * (now / was)
        total = cash + sum(grown.values())
        equity.append(equity[-1] * total)
        if total > 0:
            weights = {symbol: value / total for symbol, value in grown.items()}

        # the passive alternative holds the same names at equal weight and full size
        passive_grown: dict[str, float] = {}
        for symbol, share in passive.items():
            now, was = aligned[symbol].prices[index], aligned[symbol].prices[index - 1]
            if math.isnan(now) or math.isnan(was) or was <= 0:
                now, was = 1.0, 1.0
            passive_grown[symbol] = share * (now / was)
        passive_total = (1.0 - sum(passive.values())) + sum(passive_grown.values())
        benchmark.append(benchmark[-1] * passive_total)
        if passive_total > 0:
            passive = {symbol: value / passive_total for symbol, value in passive_grown.items()}

        # 2a. a name that lost its trend yesterday is sold at this close, before the
        # rebalance that would have re-weighted it: its share goes to cash, not to the
        # survivors, until the grid next speaks.
        if pending_exits:
            dropped = {s: weights.get(s, 0.0) for s in pending_exits}
            turnover = sum(dropped.values())
            cost = equity[-1] * turnover * costs.rate
            fees_paid += cost
            equity[-1] = max(equity[-1] - cost, 0.0)
            for symbol, share in dropped.items():
                weights.pop(symbol, None)
                if share > 0:
                    trend_exits.append((index, symbol, cost))
            if turnover > 0:
                total = 1.0 - sum(weights.values()) + sum(weights.values())
                if total > 0:
                    weights = {s: v / total for s, v in weights.items()}
            pending_exits = None

        # 2b. yesterday's decision takes effect at this close, never at its own. The book
        # and the benchmark are filled separately: a trend rule that empties the book says
        # nothing about when the passive comparison re-equalises.
        if pending is not None or pending_passive is not None:
            turnover = cost = 0.0
            if pending is not None:
                turnover = sum(
                    abs(pending.get(s, 0.0) - weights.get(s, 0.0))
                    for s in set(pending) | set(weights)
                )
                cost = equity[-1] * turnover * costs.rate
                fees_paid += cost
                equity[-1] = max(equity[-1] - cost, 0.0)
                weights = pending
            benchmark_cost = 0.0
            if pending_passive is not None:
                benchmark_turnover = sum(
                    abs(pending_passive.get(s, 0.0) - passive.get(s, 0.0))
                    for s in set(pending_passive) | set(passive)
                )
                benchmark_cost = benchmark[-1] * benchmark_turnover * costs.rate
                benchmark_fees += benchmark_cost
                benchmark[-1] = max(benchmark[-1] - benchmark_cost, 0.0)
                passive = pending_passive
                benchmark_events.append((index, benchmark_cost))
            if pending is not None:
                decision_index, holdings, scale, candidates = pending_info  # type: ignore[misc]
                rebalances.append(
                    Rebalance(
                        index=decision_index,
                        time=calendar[decision_index],
                        filled_index=index,
                        holdings=holdings,
                        weights=dict(pending),
                        turnover=turnover,
                        scale=scale,
                        candidates=candidates,
                        cost=cost,
                        benchmark_cost=benchmark_cost,
                    )
                )
            pending, pending_info, pending_passive = None, None, None

        # 3. a new decision, from the close of this bar only
        if index % rebalance:
            # between rebalances the only thing that acts is the trend rule
            if trend is not None:
                losing = [s for s in weights if not passes_trend(aligned[s], index, trend)]
                if losing:
                    pending_exits = losing
            invested_by_bar.append(sum(weights.values()))
            continue
        universe, candidates = select_universe(aligned, index, top, lookback, min_history)
        book_universe = universe
        if trend is not None:
            book_universe = [s for s in universe if passes_trend(aligned[s], index, trend)]
            if not book_universe:
                gated_out.append(index)
        targets = weights_for(
            book_universe, aligned, index, weight_scheme, vol_window, bars_per_year, max_weight
        )
        # The benchmark never passes through the trend gate, and it re-equalises on the
        # grid whether or not the book had anything to buy: gating it too would fold the
        # rule being measured into the thing it is measured against.
        pending_passive = weights_for(
            universe, aligned, index, "equal", vol_window, bars_per_year, max_weight
        )
        if not targets:
            skipped.append(index)
            invested_by_bar.append(sum(weights.values()))  # the old book is still held
            continue
        scale = 1.0
        if vol_budget is not None and len(equity) > vol_window:
            window = [
                equity[k] / equity[k - 1] - 1.0
                for k in range(len(equity) - vol_window, len(equity))
                if equity[k - 1] > 0
            ]
            # the same estimator the toolkit reports: simple returns, pstdev, sqrt(bars/year)
            realised = st.pstdev(window) * math.sqrt(bars_per_year) if len(window) > 3 else 0.0
            if realised > 0:
                scale = min(1.0, vol_budget / realised)  # de-risk only, never lever up
        pending = {symbol: weight * scale for symbol, weight in targets.items()}
        pending_info = (index, sorted(pending), scale, candidates)
        invested_by_bar.append(sum(weights.values()))

    if len(invested_by_bar) != len(calendar):  # the curve and the exposure must line up
        raise AssertionError(
            f"recorded {len(invested_by_bar)} exposures for {len(calendar)} bars"
        )

    # Commission per bar for the book and the benchmark. Rebalance records alone cannot
    # account for a window once exits pay costs between them.
    cost_by_bar = [0.0] * len(calendar)
    for event in rebalances:
        cost_by_bar[event.filled_index] += event.cost
    for bar, _, paid in trend_exits:
        cost_by_bar[bar] += paid
    benchmark_cost_by_bar = [0.0] * len(calendar)
    for bar, paid in benchmark_events:
        benchmark_cost_by_bar[bar] += paid

    curve = [value / equity[0] for value in equity]
    passive = [value / benchmark[0] for value in benchmark]
    performance = metrics.performance(curve, bars_per_year, timestamps=calendar)
    result = RiskParityResult(
        label=label,
        timeframe=timeframe,
        top=top,
        weight_scheme=weight_scheme,
        vol_budget=vol_budget,
        rebalance=rebalance,
        lookback=lookback,
        min_history_bars=min_history,
        costs=costs,
        times=calendar,
        equity=curve,
        benchmark_equity=passive,
        rebalances=rebalances,
        performance=performance,
        benchmark=metrics.performance(passive, bars_per_year, timestamps=calendar),
        universe_size=len(aligned),
        fees_paid=fees_paid,
        benchmark_fees=benchmark_fees,
        achieved_vol=performance.ann_vol,
        skipped=skipped,
        invested_by_bar=invested_by_bar,
        cost_by_bar=cost_by_bar,
        benchmark_cost_by_bar=benchmark_cost_by_bar,
        gated_out=gated_out,
        warnings=warnings,
        trend_bars=trend,
        trend_exits=trend_exits,
        buy_hold_equity=hold_curve,
        buy_hold_label=hold_label,
        buy_hold=(
            metrics.performance(hold_curve, bars_per_year, timestamps=calendar)
            if hold_curve
            else None
        ),
    )
    if gated_out:
        warnings.append(
            f"the trend rule emptied the book on {len(gated_out)} rebalance(s): "
            "everything chosen was below its own level a lookback ago, so the capital was held in cash"
        )
    if skipped:
        warnings.append(
            f"no symbol had enough history on {len(skipped)} rebalance(s), "
            f"{data.iso(calendar[skipped[0]])} .. {data.iso(calendar[skipped[-1]])}: "
            "the book was in cash until the universe filled up"
        )
    if len(aligned) < top:
        result.warnings.append(
            f"only {len(aligned)} symbols were read, fewer than the {top} asked for"
        )
    if vol_budget is not None and performance.ann_vol > vol_budget * 1.5:
        result.warnings.append(
            f"the budget asked for {vol_budget:.0%} a year and the book delivered "
            f"{performance.ann_vol:.0%}: trailing volatility is a forecast, not a promise"
        )
    return result


def render(result: RiskParityResult) -> str:
    """Console report: what was selected, what it cost, and how it compares."""
    lines: list[str] = []
    add = lines.append
    add("=" * 96)
    add(f"{result.label} — {result.timeframe}, {result.costs}")
    add("=" * 96)
    add(
        f"window      : {data.iso(result.times[0])} .. {data.iso(result.times[-1])} UTC "
        f"({result.performance.years:.2f} years, {len(result.times):,} bars)"
    )
    budget = "off" if result.vol_budget is None else f"{result.vol_budget:.0%} a year"
    add(
        f"selection   : top {result.top} of {result.universe_size} symbols by {result.lookback}-bar "
        f"turnover, at least {result.min_history_bars} bars of history, re-selected every "
        f"{result.rebalance} bars"
    )
    add(f"weights     : {result.weight_scheme}, risk budget {budget}")
    if result.trend_bars is not None:
        add(
            f"trend rule  : a name is held only while its close is above its close "
            f"{result.trend_bars} bar(s) earlier; a name that loses its trend is sold on the "
            f"next close and can only return at a rebalance — {len(result.trend_exits)} exits"
        )
    add("execution   : decided on a close, applied on the next close; costs on drifted weights")
    add("")
    add(
        f"rebalances  : {len(result.rebalances)}, ~{result.holdings_mean:.1f} holdings, "
        f"{result.invested_mean:.0%} of capital invested on average, "
        f"turnover {result.turnover_mean:.2f} of the book each time"
    )
    add(f"fees paid   : {pct(result.fees_paid)} of starting capital")
    if result.trend_bars is not None:
        add(
            f"at work     : {result.invested_bar_mean:.0%} of capital on average across bars, "
            f"nothing held at all on {result.flat_bar_share:.0%} of them"
        )
    if result.rebalances:
        last = result.rebalances[-1]
        held = sorted(last.weights.items(), key=lambda item: -item[1])
        shown = ", ".join(f"{symbol} {weight:.1%}" for symbol, weight in held[:8])
        add(f"last book   : {data.iso(last.time)} UTC — {shown}{'…' if len(held) > 8 else ''}")
        add(f"              {last.invested:.0%} invested, scale {last.scale:.2f}")
    if result.trend_exits:
        recent = result.trend_exits[-1]
        add(
            f"last exit   : {data.iso(result.times[recent[0]])} UTC — {recent[1]} sold when it "
            f"lost its trend"
        )
    add("")
    hold_column = f"buy & hold {result.buy_hold_label}" if result.buy_hold else None
    add(f"{'metric':<26}{'portfolio':>18}{'equal-weight hold':>20}"
        + (f"{hold_column:>22}" if hold_column else ""))
    add("-" * (64 + (22 if hold_column else 0)))

    def row(label: str, left: str, right: str, third: str = "") -> None:
        add(f"{label:<26}{left:>18}{right:>20}" + (f"{third:>22}" if hold_column else ""))

    hold = result.buy_hold
    row("total return", pct(result.performance.total_return), pct(result.benchmark.total_return),
        pct(hold.total_return) if hold else "")
    row("CAGR", pct(result.performance.cagr), pct(result.benchmark.cagr),
        pct(hold.cagr) if hold else "")
    row("annualised vol", pct(result.performance.ann_vol), pct(result.benchmark.ann_vol),
        pct(hold.ann_vol) if hold else "")
    row("Sharpe (rf=0)", f"{result.performance.sharpe:.2f}", f"{result.benchmark.sharpe:.2f}",
        f"{hold.sharpe:.2f}" if hold else "")
    row("max drawdown", pct(result.performance.max_dd), pct(result.benchmark.max_dd),
        pct(hold.max_dd) if hold else "")
    row("years", f"{result.performance.years:.2f}", f"{result.benchmark.years:.2f}",
        f"{hold.years:.2f}" if hold else "")
    add("")
    row(
        "final equity",
        f"{result.performance.final_equity:.2f}x",
        f"{result.benchmark.final_equity:.2f}x",
        f"{hold.final_equity:.2f}x" if hold else "",
    )
    add(
        "note        : the benchmark holds the same selection at equal weight and full size, "
        f"paying the same commission ({pct(result.benchmark_fees)} of starting capital)"
    )
    if hold:
        add(
            f"note        : buy & hold {result.buy_hold_label} holds one price from the first bar, "
            f"paying one commission side ({pct(result.costs.fee_per_side + result.costs.slippage_per_side)} "
            "on the way in) and no exit"
        )
    for warning in result.warnings:
        add(f"WARNING     : {warning}")
    return "\n".join(lines)


def artifact_stem(result: RiskParityResult) -> str:
    """A name that says which book this was, so two runs cannot overwrite each other."""
    scheme = "eq" if result.weight_scheme == "equal" else "iv"
    budget = "off" if result.vol_budget is None else f"b{result.vol_budget * 100:.0f}"
    window = f"_last{result.window_text}" if result.window_text else ""
    trend = f"_tr{result.trend_bars}" if result.trend_bars is not None else ""
    return f"riskparity_{scheme}{result.top}-{budget}{trend}{window}_{result.timeframe}"


def write_equity(path: Path, result: RiskParityResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    by_time = {r.time: r for r in result.rebalances}
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        header = ["time_utc", "portfolio", "equal_weight_hold"]
        if result.buy_hold_equity:
            header.append(f"buy_and_hold_{result.buy_hold_label}")
        header += ["rebalanced", "invested"]
        writer.writerow(header)
        for index, moment in enumerate(result.times):
            fill = next((r for r in result.rebalances if r.filled_index == index), None)
            writer.writerow(
                [
                    data.iso(moment),
                    f"{result.equity[index]:.8f}",
                    f"{result.benchmark_equity[index]:.8f}",
                    *(
                        [f"{result.buy_hold_equity[index]:.8f}"]
                        if result.buy_hold_equity
                        else []
                    ),
                    data.iso(by_time[fill.time].time) if fill and fill.time in by_time else "",
                    # every bar, not only the rebalance rows: a trend rule changes the
                    # exposure in between, and the column is where a reader looks for it
                    f"{result.invested_by_bar[index]:.6f}",
                ]
            )


def write_chart(path: Path, result: RiskParityResult) -> None:
    """The book against the passive alternative, with every rebalance marked."""
    portfolio_label = f"portfolio ({result.weight_scheme})"
    benchmark_label = "equal weight hold"
    # A rebalance is marked green when it puts more of the book to work, red when it
    # takes risk off, and grey when it left the size alone — not "into the market" /
    # "out of it", since the book is almost never all-in or all-cash. A reshuffle at
    # the same size is not "more invested", so it gets its own colour. The half-point
    # band keeps float dust from choosing the colour.
    markers = []
    previous = 0.0
    for rebalance in result.rebalances:
        change = rebalance.invested - previous
        colour = (
            report.MARKER_ENTRY
            if change > 0.005
            else report.MARKER_EXIT
            if change < -0.005
            else report.MARKER_SAME
        )
        markers.append(
            (
                rebalance.filled_index,
                colour,
                f"{data.iso(rebalance.time)} UTC — selected {len(rebalance.holdings)} names, "
                f"{previous:.0%} → {rebalance.invested:.0%} invested, "
                f"turnover {rebalance.turnover:.2f}",
            )
        )
        previous = rebalance.invested
    # Losing a trend is the other event this book has: the name is sold between
    # rebalances, so it needs its own colour rather than the rebalance vocabulary.
    for index, symbol, _ in result.trend_exits:
        markers.append(
            (
                index,
                report.MARKER_FLIP,
                f"{data.iso(result.times[index])} UTC — {symbol} lost its trend "
                f"({result.trend_bars} bars) and was sold to cash",
            )
        )
    markers.sort(key=lambda item: item[0])
    curves = {portfolio_label: result.equity, benchmark_label: result.benchmark_equity}
    colors = {portfolio_label: "#1a73e8", benchmark_label: "#9aa0a6"}
    levels = {
        portfolio_label: result.performance.final_equity,
        benchmark_label: result.benchmark.final_equity,
    }
    if result.buy_hold_equity:
        hold_label = f"buy & hold {result.buy_hold_label}"
        curves[hold_label] = result.buy_hold_equity
        colors[hold_label] = "#f9ab00"
        if result.buy_hold:
            levels[hold_label] = result.buy_hold.final_equity
    report.write_curves(
        path,
        result.times,
        curves,
        f"top {result.top} by turnover, {result.weight_scheme}, "
        f"{'no risk budget' if result.vol_budget is None else f'{result.vol_budget:.0%} vol budget'} "
        f"({result.costs})",
        colors=colors,
        markers=markers,
        marker_words={
            report.MARKER_ENTRY: ("more invested", "green"),
            report.MARKER_EXIT: ("less invested", "red"),
            report.MARKER_SAME: ("size unchanged", "grey"),
            report.MARKER_FLIP: ("trend exit", "purple"),
        },
        marker_title="rebalances and trend exits" if result.trend_exits else "rebalances",
        levels=levels,
    )


def write_metrics(path: Path, result: RiskParityResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2, default=str))


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcs-riskparity",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--timeframe", default="1d", help="bar size (default: %(default)s)")
    parser.add_argument("--top", type=int, default=10, help="how many symbols to hold at most")
    parser.add_argument(
        "--min-history",
        default="1y",
        help="ignore symbols younger than this, e.g. 3y, 180d (default: %(default)s)",
    )
    parser.add_argument("--lookback", default="30d", help="turnover window (default: %(default)s)")
    parser.add_argument("--rebalance", default="30d", help="how often to re-select (default: %(default)s)")
    parser.add_argument(
        "--weight", default="equal", choices=WEIGHT_SCHEMES, help="weighting scheme (default: %(default)s)"
    )
    parser.add_argument(
        "--vol-budget",
        default="0.4",
        help="annualised volatility target, or `none` to hold the book undiluted",
    )
    parser.add_argument("--vol-window", default="30d", help="volatility window (default: %(default)s)")
    parser.add_argument(
        "--trend",
        default="none",
        help="hold a name only while its close is above its close this long ago "
        "(e.g. 30d), or `none` to stay always invested (default: %(default)s)",
    )
    parser.add_argument(
        "--buy-hold",
        default=None,
        help="symbol to plot as buy & hold, or `none` (default: the --calendar symbol)",
    )
    parser.add_argument(
        "--max-weight",
        type=float,
        default=None,
        help="cap on any single holding (default: twice an equal weight, at least 25%%)",
    )
    parser.add_argument(
        "--last",
        default=None,
        metavar="PERIOD",
        help="report only the last stretch, e.g. 1y, 6mon, 90d (the book still sees all the "
             "history before it)",
    )
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR, help="symbol whose bars define the schedule")
    parser.add_argument("--fee", type=float, default=0.001, help="commission per side (default: %(default)s)")
    parser.add_argument("--slippage", type=float, default=0.0, help="extra cost per side (default: %(default)s)")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--no-artifacts", action="store_true", help="print only, write nothing")
    parser.add_argument("--json", action="store_true", help="also write the summary as JSON")
    return parser.parse_args(argv)


def bars_for(spec: str, timeframe: str, *, allow_none: bool = False) -> int | None:
    """`--lookback 30d` -> 30 bars on daily data; `none` -> None when allowed."""
    if spec.strip().lower() in ("none", "off", ""):
        if allow_none:
            return None
        raise SystemExit(f"{spec!r} is not a duration")
    try:
        seconds = data.parse_duration(spec)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if seconds == 0:
        return 0  # `0d` means "no filter", which is what the age flag wants
    step = 30 * 86400 if timeframe in data.CALENDAR_TIMEFRAMES else data.interval_seconds(timeframe)
    bars = round(seconds / step)
    if bars < 1:
        raise SystemExit(
            f"{spec} is {seconds:,} seconds, shorter than one {timeframe} bar ({step:,} s): "
            "`m` is a minute and `mon` a month, so half a year is `6mon`"
        )
    return bars


def budget_for(spec: str) -> float | None:
    if spec.strip().lower() in ("none", "off", ""):
        return None
    try:
        value = float(spec)
    except ValueError:
        raise SystemExit(f"--vol-budget needs a number like 0.4 or `none`, got {spec!r}") from None
    if value <= 0:
        raise SystemExit("--vol-budget must be positive, or `none`")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    frames: dict[str, int] = {}
    for name in ("lookback", "rebalance", "vol_window"):
        frames[name] = bars_for(getattr(args, name), args.timeframe)  # type: ignore[assignment]
        if frames[name] < 1:  # only the age filter may be zero, meaning "no filter"
            raise SystemExit(f"--{name.replace('_', '-')} must cover at least one {args.timeframe} bar")
    min_history = bars_for(args.min_history, args.timeframe)
    trend = bars_for(args.trend, args.timeframe, allow_none=True)
    vol_budget = budget_for(args.vol_budget)

    try:
        calendar_series = read_series(args.data_dir, args.calendar, args.timeframe)
    except FileNotFoundError as exc:
        raise SystemExit(f"{args.calendar}: {exc}") from None
    calendar = calendar_series.times
    symbols = sorted(symbol for symbol, tf in data.available_series(args.data_dir) if tf == args.timeframe)
    print(f"reading {len(symbols)} {args.timeframe} series ({len(calendar):,} bars on the calendar)…")

    aligned = {args.calendar: align(calendar_series, calendar, MAX_AGE_DAYS)}
    for position, symbol in enumerate(symbols, 1):
        if symbol == args.calendar:
            continue
        try:
            aligned[symbol] = align(read_series(args.data_dir, symbol, args.timeframe), calendar, MAX_AGE_DAYS)
        except (FileNotFoundError, ValueError):
            continue
        if position % 200 == 0:
            print(f"  … {position} series read")

    try:
        result = run_riskparity(
            aligned,
            calendar,
            timeframe=args.timeframe,
            top=args.top,
            lookback=frames["lookback"],
            rebalance=frames["rebalance"],
            weight_scheme=args.weight,
            vol_budget=vol_budget,
            vol_window=frames["vol_window"],
            min_history=min_history,
            max_weight=args.max_weight,
            costs=Costs(fee_per_side=args.fee, slippage_per_side=args.slippage),
            label=f"top {args.top} by turnover",
            buy_hold=(
                None
                if args.buy_hold and args.buy_hold.strip().lower() in ("none", "off", "")
                else args.buy_hold or args.calendar
            ),
            trend=trend,
        )
    except ValueError as exc:  # a rejection meant for a human, not a traceback
        raise SystemExit(str(exc)) from None
    if args.last:
        try:
            seconds = data.parse_duration(args.last)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        first = calendar[-1] - seconds
        try:
            result = restrict(
                result,
                first=first,
                text=args.last,
                label=f"top {args.top} by turnover (last {args.last})",
            )
        except ValueError as exc:
            sys.stdout.flush()  # so the complaint lands after the progress, not before it
            raise SystemExit(
                f"{exc}\n`--last {args.last}` is {seconds:,} seconds; `m` is a minute and "
                "`mon` a month, so half a year is `--last 6mon`"
            ) from None

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
    if args.json:
        json_path = args.out_dir / f"{stem}_metrics.json"
        write_metrics(json_path, result)
        print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
