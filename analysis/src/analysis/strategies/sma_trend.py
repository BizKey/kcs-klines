"""Trend strategies built on a simple moving average of the close."""

from __future__ import annotations

from ..data import Bar
from ..metrics import sma
from .base import Strategy
from .registry import register

__all__ = ["SmaTrend", "SmaTrendLongShort"]


class SmaTrend(Strategy):
    """Long while the close is above its SMA, flat otherwise.

    The textbook trend filter: it never shorts, so a downtrend costs it nothing
    beyond the whipsaw commissions on the way out.
    """

    name = "sma"
    sweep_param = "window"

    def __init__(self, window: int = 200):
        if window < 2:
            raise ValueError("window must be at least 2")
        self.window = window

    @property
    def slug(self) -> str:
        return f"sma{self.window}"

    @property
    def warmup(self) -> int:
        return self.window

    @property
    def params(self) -> dict:
        return {"window": self.window}

    def targets(self, bars: list[Bar]) -> list[float]:
        closes = [b.close for b in bars]
        average = sma(closes, self.window)
        return [1.0 if (m is not None and c > m) else 0.0 for c, m in zip(closes, average)]

    def describe(self) -> str:
        return f"long while close > SMA({self.window}), flat below; no shorts"


class SmaTrendLongShort(Strategy):
    """Long above the SMA, short below it: always in the market, either way.

    Useful as a contrast — it doubles the number of fills a long-only version
    makes, so it shows how much of a result is signal and how much is turnover.
    """

    name = "sma-ls"
    sweep_param = "window"

    def __init__(self, window: int = 200):
        if window < 2:
            raise ValueError("window must be at least 2")
        self.window = window

    @property
    def slug(self) -> str:
        return f"sma{self.window}-ls"

    @property
    def warmup(self) -> int:
        return self.window

    @property
    def params(self) -> dict:
        return {"window": self.window}

    def targets(self, bars: list[Bar]) -> list[float]:
        closes = [b.close for b in bars]
        average = sma(closes, self.window)
        targets = []
        for close, mean in zip(closes, average):
            if mean is None:
                targets.append(0.0)
            else:
                targets.append(1.0 if close > mean else -1.0)
        return targets

    def describe(self) -> str:
        return f"long above SMA({self.window}), short below; always in the market"


@register("sma")
def _sma(window: int = 200, **_: object) -> Strategy:
    """Long while the close is above its SMA, flat below."""
    return SmaTrend(window=window)


@register("sma-ls")
def _sma_ls(window: int = 200, **_: object) -> Strategy:
    """Long above the SMA, short below it: always in the market."""
    return SmaTrendLongShort(window=window)
