"""Long on a new N-bar high, flat otherwise."""

from __future__ import annotations

from ..data import Bar
from .base import Strategy
from .registry import register

__all__ = ["DonchianBreakout"]


class DonchianBreakout(Strategy):
    name = "breakout"
    sweep_param = "lookback"

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    @property
    def slug(self) -> str:
        return f"donchian{self.lookback}"

    @property
    def warmup(self) -> int:
        return self.lookback

    @property
    def params(self) -> dict:
        return {"lookback": self.lookback}

    def targets(self, bars: list[Bar]) -> list[float]:
        out: list[float] = []
        for i, bar in enumerate(bars):
            if i < self.lookback:
                out.append(0.0)
            else:
                window = [b.high for b in bars[i - self.lookback : i]]
                out.append(1.0 if bar.close > max(window) else 0.0)
        return out

    def describe(self) -> str:
        return f"long on a new {self.lookback}-bar high, flat otherwise"


@register("breakout")
def _breakout(lookback: int = 20, **_: object) -> Strategy:
    """Long on a new N-bar high, flat otherwise."""
    return DonchianBreakout(lookback=lookback)
