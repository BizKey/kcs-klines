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
import math
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import Costs, data, metrics, report
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
    #: Health readings per rebalance date, all computed from bars up to that date:
    #: how far below its own running peak the price sits (`below_peak`, a fraction),
    #: how far above its own long moving average it is (`trend`), the median quote
    #: turnover over the gate window (`turnover`) and the annualised volatility over
    #: the same window (`volatility`). `None` where the history does not exist yet.
    #: An empty mapping keeps a hand-built panel valid.
    gates: dict[str, list[float | None]] = field(default_factory=dict)
    #: The rebalance grid this panel was sampled on, shared by reference between panels.
    #: `run_portfolio` checks it, because a panel and a date list that disagree line up
    #: positionally and produce a plausible-looking curve from the wrong bars.
    grid: list[int] | None = None


@dataclass
class Rebalance:
    """What one rebalance did."""

    index: int
    time: int
    longs: list[str]
    shorts: list[str]
    turnover: float
    candidates: int


@dataclass(frozen=True)
class VolTarget:
    """Book-level sizing: hold `target` annualised volatility, up to `cap` of the capital.

    The reading comes from the *unscaled* book — the same names at full size — over the last
    `window` rebalance periods, so the estimate never sees the future and never feeds on its
    own scaling. Measuring the account instead would divide by a volatility that already
    contains the multiplier and turn the dial into a feedback loop.
    """

    target: float = 0.0        # 0 = off
    window: int = 12           # rebalance periods, not bars
    cap: float = 1.0           # never gear up beyond this
    floor: float = 0.0

    def __post_init__(self) -> None:
        if self.target < 0:
            raise ValueError("vol target is a magnitude and cannot be negative")
        if self.window < 2:
            raise ValueError("vol window must be at least 2 rebalance periods")
        if not 0.0 <= self.floor <= self.cap <= 1.0:
            raise ValueError(
                f"need 0 <= floor <= cap <= 1 (got floor={self.floor}, cap={self.cap})"
            )

    @property
    def active(self) -> bool:
        return self.target > 0

    @property
    def min_observations(self) -> int:
        return min(5, self.window)

    def describe(self) -> str:
        return (
            f"{self.target:.0%} annualised over {self.window} rebalances "
            f"(cap {self.cap:g}, floor {self.floor:g})"
        )


@dataclass(frozen=True)
class GateSpec:
    """Which health conditions a symbol must pass to be a candidate.

    Every reading is taken from bars at or before the rebalance date, so a gate can only
    use the past. A zero/None threshold switches that gate off; `active` says whether any
    of them is on.
    """

    trend_bars: int = 0              # price must be above its own mean of this many bars
    trend_threshold: float = 0.0     # ... by at least this much (0 = merely above)
    max_below_peak: float = 0.0      # drop names this far below their own running peak
    min_turnover: float = 0.0        # median quote turnover per bar, gate window
    min_volatility: float = 0.0      # annualised volatility floor (drops dead pairs)

    @property
    def active(self) -> bool:
        return bool(
            self.trend_bars or self.max_below_peak or self.min_turnover or self.min_volatility
        )

    def describe(self) -> str:
        parts = []
        if self.trend_bars:
            parts.append(f"trend {self.trend_bars} bars >= {self.trend_threshold:+.1%}")
        if self.max_below_peak:
            parts.append(f"peak within -{self.max_below_peak:.0%}")
        if self.min_turnover:
            parts.append(f"turnover >= {self.min_turnover:,.0f}/bar")
        if self.min_volatility:
            parts.append(f"volatility >= {self.min_volatility:.0%}")
        return ", ".join(parts) if parts else "none"

    def names(self) -> list[str]:
        out = []
        if self.trend_bars:
            out.append("trend")
        if self.max_below_peak:
            out.append("below_peak")
        if self.min_turnover:
            out.append("turnover")
        if self.min_volatility:
            out.append("volatility")
        return out


def gate_ok(panel: "Panel", k: int, gates: "GateSpec | None") -> bool:
    """Does this symbol pass every switched-on gate at rebalance `k`?

    A gate whose reading does not exist yet (a young series, a short window) counts as
    failed: the point is to refuse to buy what cannot be checked.
    """
    if gates is None or not gates.active:
        return True

    def value(name: str) -> float | None:
        series = panel.gates.get(name)
        if not series or k >= len(series):
            return None
        return series[k]

    if gates.trend_bars:
        trend = value("trend")
        if trend is None or trend < gates.trend_threshold:
            return False
    if gates.max_below_peak:
        below = value("below_peak")
        if below is None or below < -gates.max_below_peak:
            return False
    if gates.min_turnover:
        turn = value("turnover")
        if turn is None or turn < gates.min_turnover:
            return False
    if gates.min_volatility:
        vol = value("volatility")
        if vol is None or vol < gates.min_volatility:
            return False
    return True


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
    #: How the slice is chosen: `rank` takes the top of the cross-section, `sign`
    #: takes everything clearing `threshold` — a filter, so its holdings and its
    #: cash position move with the market.
    select_mode: str = "rank"
    threshold: float = 0.0
    flat_rebalances: int = 0
    #: The health gates the run applied and how many candidates they removed per
    #: rebalance on average (`None` = no gates).
    gates: "GateSpec | None" = None
    gated_mean: float = 0.0
    #: The volatility target and the multiplier it applied at each rebalance.
    vol_target: "VolTarget | None" = None
    vol_scales: list[float] = field(default_factory=list)
    #: The quote filter the run used (`None` = every pair on disk) and how many pairs it
    #: skipped, so the report can say what the universe actually was.
    quotes: tuple[str, ...] | None = ("USDT",)
    skipped_pairs: int = 0
    warnings: list[str] = field(default_factory=list)

    #: Filled by the CLI so the report and the JSON can list the pairs without the
    #: caller re-passing the panels; empty when the result was built by hand.
    pairs: list[dict] = field(default_factory=list)

    @property
    def mean_scale(self) -> float:
        """The average exposure multiplier the volatility target applied (1.0 = off)."""
        return statistics.fmean(self.vol_scales) if self.vol_scales else 1.0

    def pair_stats(self) -> list[dict]:
        """Per-pair rows, busiest first (empty unless `pairs` was filled in)."""
        return self.pairs

    @property
    def cash_share(self) -> float:
        """Share of rebalances the portfolio spent in cash with nothing selected."""
        if not self.rebalances:
            return 0.0
        return self.flat_rebalances / len(self.rebalances)

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
            "select": self.select_mode,
            "threshold": self.threshold,
            "cash_share": self.cash_share,
            "gates": self.gates.describe() if self.gates else "none",
            "gated_per_rebalance": self.gated_mean,
            "vol_target": self.vol_target.describe() if self.vol_target else "off",
            "mean_exposure_multiplier": self.mean_scale,
            "quotes": list(self.quotes) if self.quotes else "any",
            "skipped_pairs": self.skipped_pairs,
            "pairs_traded": sum(1 for row in self.pairs if row["held"]),
            "pairs_never_bought": sum(1 for row in self.pairs if not row["held"]),
            "pairs_busiest": self.pairs[:10],
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
    *,
    gate_seconds: int | None = None,
    trend_bars: int | None = None,
    turnover: list[float] | None = None,
) -> Panel:
    """Sample one symbol's closes, lookback returns and health gates onto the calendar.

    Every reading is taken from bars at or before the rebalance date, so no gate can see
    the future: `below_peak` compares the close with the highest close so far, `trend`
    with the mean of the previous `trend_bars` closes, and `turnover`/`volatility` with
    the `gate_seconds` window ending at that date.
    """
    sampled = [close_at(times, closes, date, max_age) for date in dates]
    momentum: list[float | None] = []
    for date, value in zip(dates, sampled):
        past = close_at(times, closes, date - lookback_seconds, max_age)
        momentum.append(None if value is None or past in (None, 0) else value / past - 1.0)

    gates: dict[str, list[float | None]] = {}
    if trend_bars or turnover is not None or gate_seconds:
        # Annualise volatility by the series' own bar length, not by the gate window:
        # a 30-day standard deviation scaled to 30 days is not a volatility figure.
        deltas = [b - a for a, b in zip(times, times[1:])]
        step = int(statistics.median(deltas)) if deltas else 86400
        bars_per_year = 365.0 * 86400 / step if step else 365.0
        below_peak: list[float | None] = []
        trend: list[float | None] = []
        liquid: list[float | None] = []
        vol: list[float | None] = []
        for date, value in zip(dates, sampled):
            index = bisect.bisect_right(times, date)
            seen = closes[:index]
            if value is None or index < 2:
                below_peak.append(None)
                trend.append(None)
                liquid.append(None)
                vol.append(None)
                continue
            peak = max(seen) if seen else value
            below_peak.append(value / peak - 1.0 if peak > 0 else None)
            trend.append(
                value / (sum(seen[-trend_bars:]) / min(len(seen), trend_bars)) - 1.0
                if trend_bars and len(seen) >= trend_bars
                else None
            )
            window = [
                moment for moment in times if date - (gate_seconds or 0) < moment <= date
            ]
            start = bisect.bisect_right(times, date - (gate_seconds or 0))
            if gate_seconds and index - start >= 3:
                turns = sorted(turnover[start:index]) if turnover else []
                liquid.append(
                    turns[len(turns) // 2] if turns else None
                )
                moves = [
                    math.log(closes[i] / closes[i - 1])
                    for i in range(start + 1, index)
                    if closes[i - 1] > 0 and closes[i] > 0
                ]
                vol.append(
                    statistics.pstdev(moves) * math.sqrt(bars_per_year)
                    if len(moves) > 2
                    else None
                )
            else:
                liquid.append(None)
                vol.append(None)
            del window
        gates = {
            "below_peak": below_peak,
            "trend": trend,
            "turnover": liquid,
            "volatility": vol,
        }
    return Panel(
        symbol=symbol, closes=sampled, momentum=momentum, gates=gates, grid=dates
    )


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


def read_closes(
    data_dir: Path | str, symbol: str, timeframe: str, *, with_volume: bool = False
) -> tuple[list[int], list[float]] | tuple[list[int], list[float], list[float]]:
    """Only the columns a panel needs — the fast path over a thousand series.

    `with_volume=True` also returns quote turnover (`close * volume`), which the
    liquidity gate needs and a pure close panel does not.
    """
    import pyarrow.parquet as pq

    files = data.series_files(data_dir, symbol, timeframe)
    if not files:
        raise FileNotFoundError(f"no parquet files for {symbol} {timeframe} under {data_dir}")
    wanted = ["time", "close", "volume"] if with_volume else ["time", "close"]
    pairs: dict[int, tuple[float, float]] = {}
    for path in files:
        table = pq.read_table(path, columns=wanted)
        columns = table.to_pydict()
        # The collector stores quote turnover as its own column (what the exchange
        # reported); `close * volume` is the same quantity to within a bar's move and is
        # only a fallback. A liquidity gate should read the number, not re-derive it.
        stored = pq.read_table(path, columns=["turnover"]).to_pydict().get("turnover") if with_volume else None
        for index, moment in enumerate(columns["time"]):
            close = columns["close"][index]
            if stored is not None and index < len(stored) and stored[index]:
                pairs[moment] = (close, float(stored[index]))
            else:
                volume = columns["volume"][index] if with_volume else 0.0
                pairs[moment] = (close, close * (volume or 0.0))
    ordered = sorted(pairs)
    closes = [pairs[moment][0] for moment in ordered]
    if not with_volume:
        return ordered, closes
    return ordered, closes, [pairs[moment][1] for moment in ordered]


def threshold_of(spec: str) -> float:
    """`0` -> 0.0, `5%` -> 0.05, `-3%` -> -0.03: the trailing return a symbol must beat."""
    text = spec.strip().rstrip("%")
    try:
        value = float(text)
    except ValueError:
        raise SystemExit(f"--threshold needs a number like 0 or 5%, got {spec!r}") from None
    return value / 100.0 if spec.strip().endswith("%") else value


SELECT_MODES = ("rank", "sign")


def select(
    momentum: dict[str, float],
    top: float,
    mode: str,
    *,
    select_mode: str = "rank",
    threshold: float = 0.0,
) -> tuple[list[str], list[str]]:
    """Which symbols to hold: the strongest slice (`rank`) or everything above a bar (`sign`).

    `rank` takes a slice of the sorted cross-section — `top` is a fraction of the
    universe below 1, or an absolute count at 1 or more. `sign` takes *every*
    symbol whose trailing return clears `threshold` (0 means "it rose"), which is
    a filter rather than a ranking: it holds more names when the market is up and
    fewer when it is down, and nothing at all when nothing qualifies. Ties are
    broken by symbol name so a run is reproducible.
    """
    if not momentum:
        return [], []
    if select_mode == "sign":
        longs = sorted(s for s, value in momentum.items() if value > threshold)
        shorts = (
            sorted(s for s, value in momentum.items() if value < -abs(threshold))
            if mode == "long-short"
            else []
        )
        return longs, shorts
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
    select_mode: str = "rank",
    gates: GateSpec | None = None,
    vol_target: VolTarget | None = None,
    quotes: tuple[str, ...] | None = ("USDT",),
    skipped_pairs: int = 0,
    threshold: float = 0.0,
) -> PortfolioResult:
    """Rank, hold, rebalance, and charge for the turnover."""
    if mode not in ("long-only", "long-short"):
        raise ValueError("mode must be 'long-only' or 'long-short'")
    if select_mode not in SELECT_MODES:
        raise ValueError(f"select must be one of {', '.join(SELECT_MODES)}")
    if top <= 0:
        raise ValueError("top must be positive")
    costs = costs or Costs()
    if len(dates) < 3 or not panels:
        raise ValueError("need at least a few rebalance dates and one symbol with data")
    # A panel is sampled *on* a grid: its close and momentum lists are positional. Handing
    # `run_portfolio` a different (sliced, rebased, reversed) date list reads the wrong bars
    # and reports a confident curve for a window that was never measured.
    for panel in panels:
        if panel.grid is not None and list(panel.grid) != list(dates):
            raise ValueError(
                f"{panel.symbol}: this panel was sampled on a different rebalance grid "
                f"({data.iso(panel.grid[0])[:10]}..{data.iso(panel.grid[-1])[:10]}, "
                f"{len(panel.grid)} dates) than the {len(dates)} dates it was handed "
                f"({data.iso(dates[0])[:10]}..{data.iso(dates[-1])[:10]}); rebuild the "
                "panel for the window you want — `kcs-portfolio --from/--to` does that"
            )
    # The curve has one point per rebalance, so that is the period to annualise by.
    per_year = bars_per_year / rebalance

    warnings: list[str] = []
    by_symbol = {panel.symbol: panel for panel in panels}
    weights: dict[str, float] = {}
    equity: list[float] = [1.0]
    rebalances: list[Rebalance] = []
    fees_paid = 0.0
    dropped = 0
    flat = 0

    wiped_out: int | None = None
    gated_total = 0
    vol = vol_target or VolTarget()
    book_returns: list[float] = []      # the unscaled book's own period returns
    scales: list[float] = []
    for k in range(len(dates) - 1):
        momentum = {}
        gated_here = 0
        for panel in panels:
            value = panel.momentum[k]
            if value is None or panel.closes[k] is None:
                continue
            if not gate_ok(panel, k, gates):
                gated_here += 1
                continue
            momentum[panel.symbol] = value
        gated_total += gated_here
        longs, shorts = select(
            momentum, top, mode, select_mode=select_mode, threshold=threshold
        )
        side = 0.5 if mode == "long-short" and shorts else 1.0
        unscaled: dict[str, float] = {}
        for symbol in longs:
            unscaled[symbol] = unscaled.get(symbol, 0.0) + side / len(longs)
        for symbol in shorts:
            unscaled[symbol] = unscaled.get(symbol, 0.0) - side / len(shorts)

        # Size the book to a volatility target, decided from the unscaled book's own
        # completed periods. Too little history means "stay unscaled" rather than "sell".
        scale = 1.0
        if vol.active:
            seen = book_returns[-vol.window :]
            if len(seen) >= vol.min_observations:
                sigma = statistics.pstdev(seen) * math.sqrt(per_year)
                scale = (
                    vol.cap
                    if sigma <= 0
                    else max(vol.floor, min(vol.cap, vol.target / sigma))
                )
        scales.append(scale)
        targets = {symbol: weight * scale for symbol, weight in unscaled.items()}

        # 1. Trade at this close, before the new book earns anything. `weights` is the
        #    book that arrived here (drifted to this close by the previous iteration), so
        #    the turnover below measures a trade rather than a price move. Charging the
        #    cost against the drifted book is what keeps a merely-held position free: an
        #    earlier version divided by growth twice and billed 61% turnover per rebalance
        #    for a single holding that had tripled.
        turnover = sum(
            abs(targets.get(symbol, 0.0) - weights.get(symbol, 0.0))
            for symbol in set(targets) | set(weights)
        )
        cost = equity[-1] * turnover * costs.rate
        fees_paid += cost
        # Carry the bill into the value the period starts from. Writing it back into
        # `equity[-1]` instead would, on the first rebalance, subtract it from
        # `equity[0]` — which is also the curve's normalising base — and the entry
        # commission would silently vanish from the result.
        value = max(equity[-1] - cost, 0.0)
        weights = targets
        rebalances.append(Rebalance(k, dates[k], longs, shorts, turnover, len(momentum)))
        if not targets:
            # Nothing qualified: the book goes to cash and pays to get there. This is
            # the sign rule's normal state in a falling market, not an edge case.
            flat += 1

        # 2. The book decided *at* this close earns the next period, exactly as
        #    `engine.py` does it: `targets[t]` decided on bar t's close is exposed to
        #    bar t -> t+1. Marking the previous book over this period instead — which is
        #    what this loop used to do — silently ran the whole strategy one rebalance
        #    late (seven bars on a weekly grid, thirty on a monthly one), so the measured
        #    rule was not the rule the report described.
        growth = 1.0
        full_growth = 1.0
        drifted: dict[str, float] = {}
        ratios: dict[str, float] = {}
        for symbol, weight in weights.items():
            panel = by_symbol[symbol]
            start, end = panel.closes[k], panel.closes[k + 1]
            if start is None:
                continue
            if end is None:  # no bar since: the position sits at its last print
                dropped += 1
                end = start
            ratio = end / start
            ratios[symbol] = ratio
            growth += weight * (ratio - 1.0)
            drifted[symbol] = weight * ratio
        for symbol, weight in unscaled.items():
            ratio = ratios.get(symbol)
            if ratio is None:
                panel = by_symbol[symbol]
                start, end = panel.closes[k], panel.closes[k + 1]
                if not start:
                    continue
                ratio = (end or start) / start
            full_growth += weight * (ratio - 1.0)
        book_returns.append(full_growth - 1.0)
        value_after = value * growth
        # Normalise the drifted book back to the capital it now represents, so the
        # weights sum to it again (1 for a long-only book, 0 for long/short) and the
        # next rebalance's turnover measures a trade rather than a price move.
        if growth > 0 and drifted:
            weights = {s: amount / growth for s, amount in drifted.items()}
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
        select_mode=select_mode,
        threshold=threshold,
        flat_rebalances=flat,
        universe=len(panels),
        holdings_mean=statistics.fmean(
            [len(r.longs) + len(r.shorts) for r in rebalances] or [0.0]
        ),
        fees_paid=fees_paid,
        dropped=dropped,
        gates=gates,
        gated_mean=gated_total / max(len(dates) - 1, 1),
        vol_target=vol_target,
        vol_scales=scales,
        quotes=quotes,
        skipped_pairs=skipped_pairs,
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
    if select_mode == "sign" and flat:
        result.warnings.append(
            f"nothing cleared the {threshold:+.1%} bar on {flat} of {len(rebalances)} "
            f"rebalances ({flat / max(len(rebalances), 1):.0%} of the time in cash)"
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


def gross_exposure(result_side: tuple[list[str], list[str]], mode: str) -> float:
    """How much of the account is at work: 0 in cash, 1 long-only, 0.5 per side long/short."""
    longs, shorts = result_side
    if not longs and not shorts:
        return 0.0
    return 0.5 if mode == "long-short" and shorts else 1.0


def cash_markers(result: PortfolioResult) -> list[report.Marker]:
    """A marker only where the book entered or left cash.

    Marking every rebalance would draw a hundred lines across the chart and say
    nothing: what a reader needs to see is when the rule stepped aside. Breadth moves
    far too often to mark (it swings from 1 name to 578 on a wide universe), so it is
    reported as a number instead, and only the cash moves are drawn. The count of names
    is in the tooltip, and the colours keep the vocabulary the rest of the toolkit uses
    — green for more capital at work, red for less.
    """
    markers: list[report.Marker] = []
    previous = 0.0          # the account starts in cash, so the first entry is a move
    for rebalance in result.rebalances:
        exposure = gross_exposure((rebalance.longs, rebalance.shorts), result.mode)
        if exposure != previous:
            colour = report.MARKER_ENTRY if exposure > previous else report.MARKER_EXIT
            what = "back in" if exposure > previous else "to cash"
            markers.append(
                (
                    rebalance.index,
                    colour,
                    f"{data.iso(rebalance.time)} UTC — {what}: {len(rebalance.longs)} long, "
                    f"{len(rebalance.shorts)} short of {rebalance.candidates} ranked, "
                    f"turnover {rebalance.turnover:.2f}",
                )
            )
        previous = exposure
    return markers


def write_chart(path: Path, result: PortfolioResult, dates: list[int]) -> None:
    """The portfolio against the equal-weight universe, with its cash moves marked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    portfolio_label = f"portfolio (select {result.select_mode})"
    benchmark_label = f"equal weight, {result.universe} names"
    report.write_curves(
        path,
        dates[: len(result.equity)],
        {portfolio_label: result.equity, benchmark_label: result.benchmark_equity},
        f"{result.label}: {result.mode}, lookback {result.lookback}, "
        f"rebalance {result.rebalance} ({result.costs})",
        colors={portfolio_label: "#1a73e8", benchmark_label: "#9aa0a6"},
        markers=cash_markers(result),
        levels={
            portfolio_label: result.performance.final_equity,
            benchmark_label: result.benchmark.final_equity,
        },
        marker_words={
            report.MARKER_ENTRY: ("back in", "green"),
            report.MARKER_EXIT: ("to cash", "red"),
            report.MARKER_SAME: ("size unchanged", "grey"),
        },
        marker_title="cash moves",
    )


#: Colours cycled through the drawn assets, in the order they are picked.
ASSET_COLORS = (
    "#1a73e8", "#188038", "#d93025", "#f9ab00", "#9334e6", "#0b8043", "#c5221f",
    "#3f51b5", "#00838f", "#6d4c41", "#ad1457", "#2e7d32", "#ef6c00", "#5e35b1",
)
#: What an asset that was never bought looks like.
NEVER_COLOR = "#9aa0a6"


def trade_log(result: PortfolioResult, panels: list[Panel], dates: list[int]):
    """Every buy and sell the book made, as `(symbol, kind, index, price)`.

    A symbol is bought on the rebalance that puts it in the book (the price is that
    date's close, which is the fill this module charges commission on) and sold on the
    rebalance that drops it. `kind` is `"buy"` or `"sell"`.
    """
    closes = {panel.symbol: panel.closes for panel in panels}
    longs = [set(balance.longs) for balance in result.rebalances]
    shorts = [set(balance.shorts) for balance in result.rebalances]
    events: list[tuple[str, str, int, float]] = []
    for k in range(len(longs)):
        before_long, before_short = (longs[k - 1], shorts[k - 1]) if k else (set(), set())
        for side, entry, exit_ in (
            (longs[k], "buy", "sell"),
            (shorts[k], "short", "cover"),
        ):
            was = before_long if side is longs[k] else before_short
            for symbol in sorted(side - was):
                price = closes.get(symbol, [None] * len(dates))[k]
                if price:
                    events.append((symbol, entry, k, float(price)))
            for symbol in sorted(was - side):
                price = closes.get(symbol, [None] * len(dates))[k]
                if price:
                    events.append((symbol, exit_, k, float(price)))
    return events


def pair_stats(result: PortfolioResult, panels: list[Panel], dates: list[int]) -> list[dict]:
    """Per pair: how often the book held it, and how often it went in and out.

    Every symbol of the universe gets a row, including the ones the rule never bought
    (`held == 0`), so the list is a census of what the strategy watched rather than
    only of what it did. `first_held`/`last_held` are the dates it was in the book.
    """
    by_symbol: dict[str, dict] = {
        panel.symbol: {
            "symbol": panel.symbol,
            "held": 0,
            "buys": 0,
            "sells": 0,
            "shorts": 0,
            "covers": 0,
            "first_held": "",
            "last_held": "",
        }
        for panel in panels
    }
    for symbol, kind, index, _ in trade_log(result, panels, dates):
        row = by_symbol.get(symbol)
        if row is None:
            continue
        row[{"buy": "buys", "sell": "sells", "short": "shorts", "cover": "covers"}[kind]] += 1
    for k, balance in enumerate(result.rebalances):
        for symbol in set(balance.longs) | set(balance.shorts):
            row = by_symbol.get(symbol)
            if row is None:
                continue
            row["held"] += 1
            when = data.iso(dates[k])[:10]
            row["first_held"] = row["first_held"] or when
            row["last_held"] = when
    return sorted(by_symbol.values(), key=lambda row: (-row["held"], row["symbol"]))


def write_pairs(path: Path, result: PortfolioResult, panels: list[Panel], dates: list[int]) -> None:
    """The full per-pair census as CSV: one row per symbol the run watched."""
    import csv as csv_module

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = pair_stats(result, panels, dates)
    with path.open("w", newline="") as fh:
        writer = csv_module.writer(fh)
        writer.writerow(
            ["symbol", "rebalances_held", "buys", "sells", "shorts", "covers",
             "first_held_utc", "last_held_utc", "ever_traded"]
        )
        for row in rows:
            writer.writerow(
                [
                    row["symbol"],
                    row["held"],
                    row["buys"],
                    row["sells"],
                    row["shorts"],
                    row["covers"],
                    row["first_held"],
                    row["last_held"],
                    "yes" if row["held"] else "no",
                ]
            )


def write_trades_chart(
    path: Path,
    result: PortfolioResult,
    panels: list[Panel],
    dates: list[int],
    *,
    limit: int = 40,
    never_held: int = 8,
) -> None:
    """One price line per asset, with the prices it was bought and sold at.

    Every line is that symbol's close on the rebalance grid, so the marked points *are*
    the fill prices this module charged commission on: a green dot where the book bought
    it, a red square where it sold. Assets the rule never bought are drawn as a grey
    dashed line, which is the honest picture of a filter — most of the universe is
    watched and never held. The chart is capped (``limit`` lines plus ``never_held``
    grey ones) because a thousand assets on one plot is a grey rectangle, not a chart:
    the drawn assets are the ones the book held for the most rebalances.
    """
    # The book the chart draws: how many rebalances each symbol was held for.
    holds = [set(b.longs) | set(b.shorts) for b in result.rebalances]
    counts: dict[str, int] = {}
    for held in holds:
        for symbol in held:
            counts[symbol] = counts.get(symbol, 0) + 1
    watched = [panel for panel in panels if panel.symbol not in counts]
    # Never-bought assets are the minority of the drawing: a fifth of the budget, so a
    # small cap still shows the book rather than a wall of grey.
    grey = max(1, min(never_held, limit // 5)) if watched else 0
    drawn = sorted(counts, key=lambda s: (-counts[s], s))[: max(limit - grey, 1)]
    never = sorted(
        watched,
        key=lambda p: (-sum(1 for value in p.closes if value is not None), p.symbol),
    )[:grey]
    chosen = [panel for panel in panels if panel.symbol in set(drawn)]
    chosen += never
    if not chosen:
        raise ValueError("no assets to draw: the universe is empty")

    events = trade_log(result, panels, dates)
    prices = [value for panel in chosen for value in panel.closes if value]
    if not prices:
        raise ValueError("no prices to draw")
    lo = math.floor(math.log10(min(prices)) * 4) / 4
    hi = math.ceil(math.log10(max(prices)) * 4) / 4
    span = (hi - lo) or 1.0
    n = len(dates)
    pad_l, pad_r, pad_t, pad_b = 70, 96, 40, 40
    width, height = report.WIDTH, report.HEIGHT + 60

    def x(index: int) -> float:
        return pad_l + (width - pad_l - pad_r) * index / (n - 1) if n > 1 else pad_l

    def y(price: float) -> float:
        return pad_t + (height - pad_t - pad_b) * (1 - (math.log10(price) - lo) / span)

    grid = []
    for decade in range(int(math.floor(lo)), int(math.ceil(hi)) + 1):
        for mult in (1, 2, 5):
            value = mult * 10.0**decade
            if not 10.0**lo * 0.999 <= value <= 10.0**hi * 1.001:
                continue
            grid.append(
                f'<line x1="{pad_l}" y1="{y(value):.2f}" x2="{width - pad_r}" y2="{y(value):.2f}" '
                f'stroke="#ececec" stroke-width="1"/>'
                f'<text x="{pad_l - 10}" y="{y(value) + 4:.2f}" font-size="10" fill="#666" '
                f'text-anchor="end">{value:g}</text>'
            )
    axis = "".join(
        f'<text x="{x(round((n - 1) * k / 5)):.2f}" y="{height - pad_b + 20}" font-size="11" '
        f'fill="#666" text-anchor="middle">{data.day(dates[round((n - 1) * k / 5)])}</text>'
        for k in range(6)
    )

    lines: list[str] = []
    labels: list[tuple[float, str, str]] = []
    for position, panel in enumerate(chosen):
        held = panel.symbol in counts
        colour = ASSET_COLORS[position % len(ASSET_COLORS)] if held else NEVER_COLOR
        style = "" if held else ' stroke-dasharray="4 3" stroke-opacity="0.45"'
        segments: list[list[tuple[float, float]]] = [[]]
        for index, price in enumerate(panel.closes):
            if not price:
                if segments[-1]:
                    segments.append([])
                continue
            segments[-1].append((x(index), y(float(price))))
        body = ""
        for points in segments:
            if len(points) < 2:
                continue
            body += (
                f'<polyline fill="none" stroke="{colour}" stroke-width="1.2"{style} '
                f'points="{" ".join(f"{px:.2f},{py:.2f}" for px, py in points)}"/>'
            )
        if not body:
            continue
        periods = counts.get(panel.symbol, 0)
        lines.append(
            f'<g class="asset"><title>{_xml(panel.symbol)} — held for {periods} of '
            f'{len(holds)} rebalances</title>{body}</g>'
        )
        last = next((i for i in range(n - 1, -1, -1) if panel.closes[i]), None)
        if last is not None:
            labels.append((y(float(panel.closes[last])), colour, panel.symbol))

    markers: list[str] = []
    buys = sells = 0
    drawn_set = {panel.symbol for panel in chosen}
    for symbol, kind, index, price in events:
        if symbol not in drawn_set or not dates:
            continue
        cx, cy = x(index), y(price)
        when = data.iso(dates[index])[:10]
        if kind in ("buy", "short"):
            buys += 1
            word = "bought" if kind == "buy" else "shorted"
            markers.append(
                f'<g class="trade"><title>{_xml(symbol)} {word} {when} at {price:g}</title>'
                f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="3.2" fill="{report.MARKER_ENTRY}" '
                f'fill-opacity="0.9"/></g>'
            )
        else:
            sells += 1
            word = "sold" if kind == "sell" else "covered"
            markers.append(
                f'<g class="trade"><title>{_xml(symbol)} {word} {when} at {price:g}</title>'
                f'<rect x="{cx - 3:.2f}" y="{cy - 3:.2f}" width="6" height="6" fill="none" '
                f'stroke="{report.MARKER_EXIT}" stroke-width="1.6" fill-opacity="0"/></g>'
            )

    # Label each line at its right-hand end, skipping the ones that would collide.
    placed: list[float] = []
    text_labels = []
    for label_y, colour, symbol in sorted(labels, key=lambda item: item[0]):
        if any(abs(label_y - other) < 10 for other in placed):
            continue
        placed.append(label_y)
        text_labels.append(
            f'<text x="{width - pad_r + 4}" y="{label_y + 3:.2f}" font-size="9" '
            f'fill="{colour}" font-family="sans-serif">{_xml(symbol)}</text>'
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    title = (
        f"{result.label}: every asset's close on the rebalance grid, entries and exits "
        f"({len(chosen)} of {len(panels)} drawn)"
    )
    footer = (
        f"bought = green dots ({buys}), sold = red squares ({sells}); "
        f"grey dashed = never bought ({len(never)} shown)"
    )
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" """
        f"""viewBox="0 0 {width} {height}">
<rect width="{width}" height="{height}" fill="#ffffff"/>
<text x="{pad_l}" y="24" font-size="14" font-family="sans-serif" fill="#111">{_xml(title)}</text>
{''.join(grid)}{axis}
{''.join(lines)}
{''.join(markers)}
{''.join(text_labels)}
<text x="{pad_l}" y="{height - 8}" font-size="11" font-family="sans-serif" fill="#666">{_xml(footer)}</text>
</svg>
"""
    )


def _xml(text: str) -> str:
    """`&` in a symbol must not break the SVG."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(result: PortfolioResult, dates: list[int]) -> str:
    """Console report: what was held, what it cost, and how it compares."""
    lines: list[str] = []
    add = lines.append
    add("=" * 78)
    rule = f"{result.mode}, select {result.select_mode}"
    add(f"{result.label} — {rule}, {result.costs}")
    add("=" * 78)
    quoted = (
        f"quoted in {'/'.join(result.quotes)}"
        if result.quotes
        else "any quote currency"
    )
    skipped = (
        f", {result.skipped_pairs} other pairs skipped" if result.skipped_pairs else ""
    )
    add(
        f"universe    : {result.universe} symbols {quoted}{skipped}, "
        f"{len(result.rebalances)} rebalances, ~{result.holdings_mean:.0f} positions each"
    )
    if result.vol_target is not None and result.vol_target.active:
        add(
            f"sizing      : {result.vol_target.describe()} — average multiplier "
            f"{result.mean_scale:.2f}, so ~{result.mean_scale:.0%} of the capital was "
            "at work"
        )
    if result.gates is not None and result.gates.active:
        add(
            f"gates       : {result.gates.describe()} — dropped ~{result.gated_mean:.0f} "
            "candidate(s) per rebalance"
        )
    selection = (
        f"every symbol above {result.threshold:+.1%}"
        if result.select_mode == "sign"
        else f"top {result.top:g} of the ranking"
    )
    add(
        f"settings    : lookback {result.lookback} bars, rebalance every {result.rebalance} bars, "
        f"{selection}"
    )
    add(
        f"turnover    : {result.mean_turnover:.2f} per rebalance, fees paid {pct(result.fees_paid)} "
        "of starting capital"
    )
    if result.select_mode == "sign":
        breadth = sorted(len(r.longs) + len(r.shorts) for r in result.rebalances)
        add(
            f"cash        : nothing cleared the bar on {result.flat_rebalances} of "
            f"{len(result.rebalances)} rebalances ({result.cash_share:.0%} of the time), "
            f"~{result.holdings_mean:.1f} positions when it is invested"
        )
        if breadth:
            add(
                f"breadth     : {breadth[0]} … {breadth[len(breadth) // 2]} … {breadth[-1]} "
                "names (min / median / max) — the filter breathes with the market"
            )
    if result.rebalances:
        last = result.rebalances[-1]
        add("")
        add(f"last rebalance {data.iso(last.time)} UTC — {last.candidates} symbols ranked")
        add(f"  long : {', '.join(last.longs[:12])}{'…' if len(last.longs) > 12 else ''}")
        if last.shorts:
            add(f"  short: {', '.join(last.shorts[:12])}{'…' if len(last.shorts) > 12 else ''}")
    if result.rebalances:
        rows = []
        for balance in result.rebalances:
            rows.extend(balance.longs)
            rows.extend(balance.shorts)
        traded = len(set(rows))
        never = result.universe - traded
        add(
            f"traded pairs: {traded} of {result.universe} were held at least once, "
            f"{never} {'was' if never == 1 else 'were'} never bought"
        )
        stats = result.pair_stats()
        add(f"  {'name':<14}{'rebalances':>11}{'in':>5}{'out':>5}  {'first held':<12}{'last held':<12}")
        for row in stats[:8]:
            add(
                f"  {row['symbol']:<14}{row['held']:>11}{row['buys'] + row['shorts']:>5}"
                f"{row['sells'] + row['covers']:>5}  {row['first_held']:<12}{row['last_held']:<12}"
            )
        if len(stats) > 8:
            add(f"  … and {len(stats) - 8} more rows (see --pairs-csv for all of them)")
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
    parser.add_argument("--top", type=float, default=0.2, help="fraction (<1) or count (>=1) per side (--select rank)")
    parser.add_argument(
        "--select",
        default="rank",
        choices=SELECT_MODES,
        help="`rank` takes the top slice, `sign` takes every symbol above --threshold (default: %(default)s)",
    )
    parser.add_argument(
        "--threshold",
        default="0",
        help="with --select sign: the trailing return a symbol must beat, e.g. 0 or 5%% (default: %(default)s)",
    )
    parser.add_argument("--mode", default="long-only", choices=("long-only", "long-short"))
    parser.add_argument(
        "--exclude-equities",
        action="store_true",
        help="drop tokenised equities (AAPLX, TSLAX, ...) from the universe; they trade "
        "like pairs but follow stocks, and the report names any it kept",
    )
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage", type=float, default=0.0)
    parser.add_argument(
        "--from",
        dest="since",
        default=None,
        metavar="DATE",
        help="start the reported window here (ISO date); with --to this evaluates one "
             "stretch, which is what a walk-forward needs",
    )
    parser.add_argument(
        "--to",
        dest="until",
        default=None,
        metavar="DATE",
        help="end the reported window here (default: the newest rebalance)",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="N",
        help="report only the last N rebalances (warm-up still uses the history before them)",
    )
    parser.add_argument("--min-bars", type=int, default=0, help="skip symbols shorter than this")
    parser.add_argument("--limit", type=int, default=None, help="use only the first N symbols")
    parser.add_argument(
        "--quote",
        default="USDT",
        help="keep only pairs quoted in these currencies, comma-separated "
        "(default: %(default)s); use 'any' for every pair on disk",
    )
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR, help="symbol whose bars define the schedule")
    parser.add_argument("--data-dir", type=Path, default=data.DEFAULT_DATA_DIR)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument(
        "--chart",
        type=Path,
        default=None,
        help="write the portfolio against its benchmark as an SVG chart here",
    )
    parser.add_argument(
        "--chart-trades",
        type=Path,
        default=None,
        help="write a second SVG: every asset's price with the prices it was bought and sold at",
    )
    parser.add_argument("--vol-target", default="0%",
                        help="hold this annualised volatility for the whole book, "
                             "e.g. 25%% (0%% = off)")
    parser.add_argument("--vol-window", type=int, default=12,
                        help="rebalance periods used to measure the book's volatility "
                             "(default: %(default)s)")
    parser.add_argument("--vol-cap", type=float, default=1.0,
                        help="never hold more than this share of the capital (default: %(default)s)")
    parser.add_argument("--vol-floor", type=float, default=0.0,
                        help="never hold less than this share (default: %(default)s)")
    parser.add_argument("--trend-gate", type=int, default=0,
                        help="require the close above its own mean of N bars (0 = off)")
    parser.add_argument("--trend-threshold", default="0%",
                        help="how far above that mean, e.g. 5%% (default: %(default)s)")
    parser.add_argument("--max-below-peak", default="0%",
                        help="drop names more than this far below their own running peak "
                             "(e.g. 90%%, 0%% = off)")
    parser.add_argument("--min-turnover", type=float, default=0.0,
                        help="median quote turnover per bar a name must trade (0 = off)")
    parser.add_argument("--min-volatility", default="0%",
                        help="annualised volatility floor, e.g. 10%% (0%% = off)")
    parser.add_argument("--gate-window", type=int, default=None,
                        help="bars used for the turnover/volatility gates (default: --lookback)")
    parser.add_argument(
        "--pairs-csv",
        type=Path,
        default=None,
        help="write one row per symbol the run watched: rebalances held, buys, sells, dates",
    )
    parser.add_argument(
        "--chart-symbols",
        type=int,
        default=40,
        help="how many assets the trades chart draws, most-held first (default: %(default)s)",
    )
    return parser.parse_args(argv)


#: The quote currency of a pair is everything after the last dash.
ANY_QUOTE = ("any", "all", "*")


def day_epoch(text: str | None, flag: str) -> int | None:
    """An ISO date (`2024-10-01`) as a UTC midnight epoch second."""
    try:
        return data.parse_date(text, flag)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def parse_quotes(spec: str) -> tuple[str, ...] | None:
    """`USDT`, `USDT,BTC` or `any` (which means "do not filter").

    Returns the quote currencies in upper case, or `None` for every pair on disk.
    """
    text = (spec or "").strip()
    if not text or text.lower() in ANY_QUOTE:
        return None
    quotes = tuple(part.strip().upper() for part in text.split(",") if part.strip())
    if not quotes:
        raise ValueError(f"no quote currency in {spec!r}; use e.g. USDT or any")
    return quotes


def quote_of(symbol: str) -> str:
    """The quote currency of `BASE-QUOTE`, whatever else the symbol contains."""
    _, _, quote = symbol.rpartition("-")
    return quote


def universes(
    data_dir: Path | str,
    timeframe: str,
    *,
    limit: int | None = None,
    quotes: tuple[str, ...] | None = ("USDT",),
) -> list[str]:
    """Every symbol that has a series of this timeframe, alphabetically.

    `quotes` keeps only pairs quoted in those currencies — the default is USDT only,
    because a cross pair like `ADA-BTC` is a different bet: its price is a ratio of two
    crypto assets, so the US dollar move cancels out and the book silently takes a
    second exposure it did not ask for. `None` (CLI: `--quote any`) keeps everything.
    """
    pairs = sorted(symbol for symbol, tf in data.available_series(data_dir) if tf == timeframe)
    if quotes is not None:
        wanted = set(quotes)
        pairs = [symbol for symbol in pairs if quote_of(symbol) in wanted]
    return pairs[:limit] if limit else pairs


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        quotes = parse_quotes(args.quote)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    every = universes(args.data_dir, args.timeframe, quotes=None)
    symbols = universes(args.data_dir, args.timeframe, limit=args.limit, quotes=quotes)
    symbols, equities = data.split_equities(symbols, args.exclude_equities)
    skipped = len(every) - len(symbols if args.limit is None else
                              universes(args.data_dir, args.timeframe, quotes=quotes))
    if len(symbols) < 2:
        raise SystemExit(f"not enough series for {args.timeframe} under {args.data_dir}")

    calendar_times = load_calendar(args.data_dir, args.timeframe, calendar=args.calendar)
    dates = rebalance_dates(calendar_times, args.rebalance)
    if len(dates) < 3:
        raise SystemExit(
            f"{args.rebalance}-bar rebalancing leaves {len(dates)} dates; use a shorter interval"
        )
    since = day_epoch(args.since, "--from")
    until = day_epoch(args.until, "--to")
    if args.last is not None and (since or until):
        raise SystemExit(
            "--last and --from/--to are two ways to say the same thing; keep one "
            "(--last counts rebalances, --from/--to take dates)"
        )
    if since or until:
        window = [
            moment for moment in dates
            if (since is None or moment >= since) and (until is None or moment <= until)
        ]
        if len(window) < 3:
            raise SystemExit(
                f"{len(window)} rebalance(s) between {data.iso(since or dates[0])[:10]} and "
                f"{data.iso(until or dates[-1])[:10]}; widen the range"
            )
        dates = window
    if args.last is not None:
        if args.last < 3:
            raise SystemExit("--last needs at least 3 rebalances to measure anything")
        if args.last >= len(dates):
            raise SystemExit(
                f"--last {args.last} but the archive only holds {len(dates)} rebalances "
                f"at {args.rebalance} bars"
            )
        dates = dates[-args.last:]
    step = int(statistics.median([b - a for a, b in zip(calendar_times, calendar_times[1:])]))
    lookback_seconds = args.lookback * step
    # A symbol that has not printed for five bars is treated as delisted.
    max_age = 5 * step

    try:
        gates = GateSpec(
            trend_bars=args.trend_gate,
            trend_threshold=threshold_of(args.trend_threshold) if args.trend_gate else 0.0,
            max_below_peak=abs(threshold_of(args.max_below_peak)),
            min_turnover=args.min_turnover,
            min_volatility=abs(threshold_of(args.min_volatility)),
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    try:
        vol_target = VolTarget(
            target=abs(threshold_of(args.vol_target)),
            window=args.vol_window,
            cap=args.vol_cap,
            floor=args.vol_floor,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    gate_seconds = (args.gate_window or args.lookback) * step

    panels: list[Panel] = []
    for symbol in symbols:
        try:
            times, closes, turnover = read_closes(
                args.data_dir, symbol, args.timeframe, with_volume=gates.active
            ) if gates.active else (*read_closes(args.data_dir, symbol, args.timeframe), None)
        except FileNotFoundError:
            continue
        if len(times) < args.min_bars:
            continue
        panel = build_panel(
            times, closes, symbol, dates, lookback_seconds, max_age,
            gate_seconds=gate_seconds if gates.active else None,
            trend_bars=gates.trend_bars or None,
            turnover=turnover if gates.active else None,
        )
        if any(value is not None for value in panel.momentum):
            panels.append(panel)
    print(f"loaded {len(panels)} symbols on {len(dates)} rebalance dates ({args.timeframe})")

    threshold = threshold_of(args.threshold)
    if args.select == "sign" and args.top != 0.2:
        print("note: --top is ignored with --select sign; the threshold decides who is held")
    label = (
        f"{args.timeframe} sign filter"
        if args.select == "sign"
        else f"{args.timeframe} cross-sectional momentum"
    )
    result = run_portfolio(
        panels,
        dates,
        lookback=args.lookback,
        rebalance=args.rebalance,
        top=args.top,
        mode=args.mode,
        costs=Costs(fee_per_side=args.fee, slippage_per_side=args.slippage),
        label=label,
        bars_per_year=data.bars_per_year(args.timeframe),
        select_mode=args.select,
        threshold=threshold,
        gates=gates,
        vol_target=vol_target,
        quotes=quotes,
        skipped_pairs=skipped,
    )
    result.pairs = pair_stats(result, panels, dates)
    if note := data.equities_note(equities, excluded=args.exclude_equities):
        print(note)
    print(render(result, dates))
    if args.pairs_csv:
        write_pairs(args.pairs_csv, result, panels, dates)
        print(f"\nwrote {args.pairs_csv}")
    if args.chart:
        write_chart(args.chart, result, dates)
        print(f"\nwrote {args.chart}")
    if args.chart_trades:
        write_trades_chart(args.chart_trades, result, panels, dates, limit=args.chart_symbols)
        print(f"wrote {args.chart_trades}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result.as_dict(), indent=2, default=str))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
