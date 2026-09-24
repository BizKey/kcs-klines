"""Volatility targeting: scale any strategy's exposure to a volatility budget.

This is not a signal, it is a position sizer wrapped around one. If a strategy's
realised volatility over the last `vol_window` bars is 80% annualised and the
budget is 40%, the wrapper holds half the exposure the inner strategy asked for;
if volatility is 20%, it holds all of it (up to `cap`).

Why it is worth having: on a volatile asset a plain long/full-exposure rule
carries most of its risk in a few regimes, and comparing strategies by CAGR alone
hides that. Volatility targeting makes the comparison like-for-like — the
question "did this signal help, or did it just happen to be exposed during the
calm part?" gets an answer.

The bar length is derived from the bars themselves (median spacing), so the
annualisation factor needs no timeframe parameter, and 1-day bars, 4-hour bars
and calendar months all work without special cases. Volatility is estimated from
returns *up to and including* the current bar, and an expanding window is used
until `vol_window` observations exist, so the strategy is never flat merely
because the estimator is warming up.
"""

from __future__ import annotations

import statistics

from ..data import Bar, SECONDS_PER_YEAR
from .base import Strategy
from .registry import register
from .sma_trend import SmaTrend, SmaTrendLongShort
from .tsmom import Tsmom, bar_step

__all__ = ["ScaledStrategy"]


class ScaledStrategy(Strategy):
    """Wrap a strategy and scale its exposure towards a volatility target."""

    name = "voltarget"
    sweep_param = "target_vol"

    def __init__(
        self,
        inner: Strategy,
        target_vol: float = 0.4,
        vol_window: int = 168,
        cap: float = 1.0,
        floor: float = 0.0,
        min_observations: int = 20,
    ):
        if target_vol <= 0:
            raise ValueError("target_vol must be positive (it is an annualised volatility)")
        if vol_window < 2:
            raise ValueError("vol_window must be at least 2 bars")
        if min_observations < 2:
            raise ValueError("min_observations must be at least 2")
        if not 0.0 <= floor <= cap <= 1.0:
            raise ValueError(f"need 0 <= floor <= cap <= 1 (got floor={floor}, cap={cap})")
        self.inner = inner
        self.target_vol = target_vol
        self.vol_window = vol_window
        self.cap = cap
        self.floor = floor
        self.min_observations = min_observations

    @property
    def slug(self) -> str:
        return f"vt{self.target_vol:g}-{self.inner.slug}"

    @property
    def warmup(self) -> int:
        return self.inner.warmup

    @property
    def params(self) -> dict:
        return {
            "target_vol": self.target_vol,
            "vol_window": self.vol_window,
            "cap": self.cap,
            "floor": self.floor,
            "inner": self.inner.params,
        }

    def targets(self, bars: list[Bar]) -> list[float]:
        inner_targets = self.inner.targets(bars)
        scale = self.scales(bars)
        return [
            max(-self.cap, min(self.cap, target * factor))
            for target, factor in zip(inner_targets, scale)
        ]

    def scales(self, bars: list[Bar]) -> list[float]:
        """The exposure multiplier for each bar, before the inner signal is applied."""
        closes = [bar.close for bar in bars]
        returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        step = bar_step(bars)
        periods_per_year = SECONDS_PER_YEAR / step if step > 0 else 1.0

        out: list[float] = [1.0]  # the first bar has no return to measure
        for index, _ in enumerate(returns, start=1):
            window = returns[max(0, index - self.vol_window) : index]
            if len(window) < self.min_observations:
                out.append(1.0)  # too little history to size anything: stay unscaled
                continue
            volatility = statistics.pstdev(window) * (periods_per_year**0.5)
            if volatility <= 0:
                out.append(self.cap if self.target_vol > 0 else self.floor)
                continue
            factor = self.target_vol / volatility
            out.append(max(self.floor, min(self.cap, factor)))
        return out

    def describe(self) -> str:
        return (
            f"{self.inner.describe()}, exposure scaled to {self.target_vol:.0%} annualised "
            f"volatility over {self.vol_window} bars (cap {self.cap:g})"
        )


@register("voltarget-sma")
def _voltarget_sma(
    window: int = 200,
    target_vol: float = 0.4,
    vol_window: int = 168,
    cap: float = 1.0,
    **_: object,
) -> Strategy:
    """SMA trend with exposure scaled to a volatility target."""
    return ScaledStrategy(SmaTrend(window=window), target_vol, vol_window, cap)


@register("voltarget-sma-ls")
def _voltarget_sma_ls(
    window: int = 200,
    target_vol: float = 0.4,
    vol_window: int = 168,
    cap: float = 1.0,
    **_: object,
) -> Strategy:
    """Long/short SMA trend with exposure scaled to a volatility target."""
    return ScaledStrategy(SmaTrendLongShort(window=window), target_vol, vol_window, cap)


@register("voltarget-tsmom")
def _voltarget_tsmom(
    lookback: int = 720,
    rebalance: int = 168,
    target_vol: float = 0.4,
    vol_window: int = 168,
    cap: float = 1.0,
    **_: object,
) -> Strategy:
    """Time-series momentum with exposure scaled to a volatility target."""
    return ScaledStrategy(
        Tsmom(lookback=lookback, rebalance=rebalance), target_vol, vol_window, cap
    )
