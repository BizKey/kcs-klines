"""Time-series momentum (TSMOM): a slow decision, taken rarely.

The rule is deliberately boring — long while the price is higher than it was
`lookback` bars ago, flat (or short) otherwise — but the *timing of the decision*
is the point. It is taken only on a fixed calendar grid every `rebalance` bars,
and the resulting exposure is then held until the next grid point.

That matters because of what the hourly results in this repository showed: the
same SMA rule on BTC-USDT traded 1,268 times on 1h (net 89.7%) and 30 times on
1d (net 1,114%). On a 1-hour series most of the signal's edge is spent on
commission, so the cheapest experiment available is not a cleverer indicator but
a longer decision interval.

Decision dates sit on an absolute grid — multiples of `rebalance * step` seconds
since the epoch — and the decision is taken on the first available bar at or
after each grid point. So a weekly `rebalance=168` on hourly bars always lands on
the same weekday/hour, whether the series starts last week or ten years ago, and
extending the history does not move the decisions that were already taken. The
snapping matters: a series whose timestamps happen to miss the grid exactly still
rebalances, instead of silently never trading.
"""

from __future__ import annotations

import statistics

from ..data import Bar
from .base import Strategy
from .registry import register

__all__ = ["Tsmom", "MODES"]

#: What a negative lookback return means.
MODES = ("long-only", "long-short")


class Tsmom(Strategy):
    """Long while the price rose over `lookback` bars, decided every `rebalance`."""

    name = "tsmom"
    sweep_param = "lookback"

    def __init__(
        self,
        lookback: int = 720,
        rebalance: int = 168,
        mode: str = "long-only",
        threshold: float = 0.0,
    ):
        if lookback < 1:
            raise ValueError("lookback must be at least 1 bar")
        if rebalance < 1:
            raise ValueError("rebalance must be at least 1 bar")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        if threshold < 0:
            raise ValueError("threshold is a magnitude and cannot be negative")
        self.lookback = lookback
        self.rebalance = rebalance
        self.mode = mode
        self.threshold = threshold

    @property
    def slug(self) -> str:
        suffix = "" if self.mode == "long-only" else "-ls"
        base = f"tsmom{self.lookback}-{self.rebalance}"
        if self.threshold:
            base += f"-t{self.threshold:g}"
        return base + suffix

    @property
    def warmup(self) -> int:
        return self.lookback

    @property
    def params(self) -> dict:
        return {
            "lookback": self.lookback,
            "rebalance": self.rebalance,
            "mode": self.mode,
            "threshold": self.threshold,
        }

    def targets(self, bars: list[Bar]) -> list[float]:
        if len(bars) < 2:
            return [0.0] * len(bars)

        step = bar_step(bars)
        period = self.rebalance * step
        # The first decision waits for the first bar at or after the first grid
        # point at which a lookback return exists. An absolute grid (multiples of
        # `period` since the epoch) keeps decision dates identical whether the
        # series starts last week or ten years ago; snapping to the next
        # available bar means the rule still fires on a series whose timestamps
        # are not aligned to that grid at all.
        first = bars[self.lookback].time if len(bars) > self.lookback else bars[-1].time
        grid = -(-first // period) * period  # ceiling division: the next grid point

        out: list[float] = []
        held = 0.0
        for i, bar in enumerate(bars):
            if i < self.lookback:
                out.append(0.0)  # no lookback return exists yet
                continue
            if bar.time >= grid:
                change = bar.close / bars[i - self.lookback].close - 1.0
                if change > self.threshold:
                    held = 1.0
                elif change < -self.threshold and self.mode == "long-short":
                    held = -1.0
                else:
                    held = 0.0
                grid += period
            out.append(held)
        return out

    def describe(self) -> str:
        window = f"{self.lookback}-bar return"
        every = f"every {self.rebalance} bars"
        if self.mode == "long-only":
            return f"long while the {window} is positive, rebalanced {every}"
        return f"long/short on the sign of the {window}, rebalanced {every}"


def bar_step(bars: list[Bar]) -> int:
    """The series' bar length in seconds, taken as the median spacing.

    Derived from the data rather than passed in, so a strategy stays a pure
    function of the bars it is given, and calendar months (28-31 days) do not
    need a special case.
    """
    deltas = [b.time - a.time for a, b in zip(bars, bars[1:])]
    if not deltas:
        return 1
    return int(statistics.median(deltas))


@register("tsmom")
def _tsmom(
    lookback: int = 720,
    rebalance: int = 168,
    mode: str = "long-only",
    threshold: float = 0.0,
    **_: object,
) -> Strategy:
    """Long while the price rose over N bars, decided once every M bars."""
    return Tsmom(lookback=lookback, rebalance=rebalance, mode=mode, threshold=threshold)


@register("tsmom-ls")
def _tsmom_ls(
    lookback: int = 720,
    rebalance: int = 168,
    threshold: float = 0.0,
    **_: object,
) -> Strategy:
    """Long or short on the sign of the N-bar return, decided every M bars."""
    return Tsmom(lookback=lookback, rebalance=rebalance, mode="long-short", threshold=threshold)
