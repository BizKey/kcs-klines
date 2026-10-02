"""Risk-off by regime: hold the signal only while the asset is calm enough.

This is the sibling of `voltarget`, and the difference is the whole point. Volatility targeting
*shrinks* the position as volatility rises (factor = target/realised, capped at 1); this wrapper
*leaves* — above `max_vol` the exposure is zero, below it the position is full size. So the two
answer different questions: "how much should I hold of a wild asset?" against "is this asset
worth holding at all right now?".

It is a damage limiter, not a return seeker: by construction it can only cut exposure, never
raise it (the multiplier is 0 or 1), and it never shorts. Everything it does is a subtraction —
the only open question, which the measurements answer, is whether what it subtracts (the losses
inside high-volatility stretches) is bigger than what it gives up (the recoveries that start
from them).

Like `voltarget`, the volatility is measured over the series' **own** bar spacing, so no
timeframe parameter is needed, and the reading uses returns up to and including the current bar
— the engine fills the change at the next open, as always.
"""

from __future__ import annotations

import statistics

from ..data import SECONDS_PER_YEAR, Bar
from .base import Strategy
from .registry import register
from .scaled import ScaledStrategy
from .sma_trend import SmaTrend
from .tsmom import Tsmom, bar_step

__all__ = ["VolFilter"]


class VolFilter(Strategy):
    """Wrap a strategy and go flat while realised volatility is above a ceiling."""

    name = "volfilter"
    sweep_param = "max_vol"

    def __init__(
        self,
        inner: Strategy,
        max_vol: float = 0.8,
        vol_window: int = 168,
        min_observations: int = 20,
    ):
        if max_vol <= 0:
            raise ValueError("max_vol is an annualised volatility ceiling and must be positive")
        if vol_window < 2:
            raise ValueError("vol_window must be at least 2 bars")
        self.inner = inner
        self.max_vol = max_vol
        self.vol_window = vol_window
        self.min_observations = min_observations

    @property
    def slug(self) -> str:
        return f"vf{self.max_vol:g}-{self.inner.slug}"

    @property
    def warmup(self) -> int:
        return self.inner.warmup

    @property
    def params(self) -> dict:
        return {
            "max_vol": self.max_vol,
            "vol_window": self.vol_window,
            "inner": self.inner.params,
        }

    def calm(self, bars: list[Bar]) -> list[bool]:
        """Is the asset below the volatility ceiling at each bar?"""
        closes = [bar.close for bar in bars]
        returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        step = bar_step(bars)
        periods_per_year = SECONDS_PER_YEAR / step if step > 0 else 1.0
        out = [True]            # the first bar has no return to measure: do not block it
        for index, _ in enumerate(returns, start=1):
            window = returns[max(0, index - self.vol_window) : index]
            if len(window) < self.min_observations:
                out.append(True)  # too little history to judge: stay in
                continue
            volatility = statistics.pstdev(window) * (periods_per_year**0.5)
            out.append(volatility <= self.max_vol)
        return out

    def targets(self, bars: list[Bar]) -> list[float]:
        inner = self.inner.targets(bars)
        return [value if calm else 0.0 for value, calm in zip(inner, self.calm(bars))]

    def describe(self) -> str:
        return (
            f"{self.inner.describe()}; flat while realised volatility is above "
            f"{self.max_vol:.0%} annualised over {self.vol_window} bars"
        )


@register("volfilter-sma")
def _volfilter_sma(
    window: int = 200,
    max_vol: float = 0.8,
    vol_window: int = 168,
    **_: object,
) -> Strategy:
    """SMA trend, but only while the asset is calmer than `max_vol` a year."""
    return VolFilter(SmaTrend(window=window), max_vol=max_vol, vol_window=vol_window)


@register("volfilter-tsmom")
def _volfilter_tsmom(
    lookback: int = 30,
    rebalance: int = 7,
    max_vol: float = 0.8,
    vol_window: int = 168,
    **_: object,
) -> Strategy:
    """Short-lookback TSMOM (daily-sensible defaults) behind the same volatility ceiling."""
    return VolFilter(
        Tsmom(lookback=lookback, rebalance=rebalance),
        max_vol=max_vol,
        vol_window=vol_window,
    )


@register("voltarget-volfilter-sma")
def _voltarget_volfilter_sma(
    window: int = 200,
    max_vol: float = 0.8,
    vol_window: int = 168,
    target_vol: float = 0.4,
    cap: float = 1.0,
    **_: object,
) -> Strategy:
    """Both damage limiters at once: leave the wild regime, and size what is left."""
    return ScaledStrategy(
        VolFilter(SmaTrend(window=window), max_vol=max_vol, vol_window=vol_window),
        target_vol=target_vol,
        vol_window=vol_window,
        cap=cap,
    )
