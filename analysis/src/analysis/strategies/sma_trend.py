"""Trend strategies built on a simple moving average of the close."""

from __future__ import annotations

from ..data import Bar
from ..metrics import sma
from .base import Strategy
from .registry import register
from .tsmom import on_decision_grid

__all__ = ["SmaTrend", "SmaTrendLongShort"]


class SmaTrend(Strategy):
    """Long while the close is above its SMA, flat otherwise.

    The textbook trend filter: it never shorts, so a downtrend costs it nothing
    beyond the whipsaw commissions on the way out.

    `rebalance` moves the decision onto the same absolute grid `Tsmom` uses: with
    the default of 1 the rule is read on every bar, and with 168 it is read once a
    week and held in between. The two paths are kept separate on purpose — the
    every-bar mapping is a plain list comprehension over aligned data, and pushing
    it through the grid machinery would quietly change what a monthly series does.
    The measurement that makes the parameter worth having is in
    `CONCLUSIONS.md` §1.6: on 976 hourly series the weekly grid is worth about
    +0.39 Sharpe for this rule and for TSMOM alike, while the choice of signal is
    worth +0.006.
    """

    name = "sma"
    sweep_param = "window"

    def __init__(self, window: int = 200, rebalance: int = 1):
        if window < 2:
            raise ValueError("window must be at least 2")
        if rebalance < 1:
            raise ValueError("rebalance must be at least 1 bar")
        self.window = window
        self.rebalance = rebalance

    @property
    def slug(self) -> str:
        if self.rebalance == 1:
            return f"sma{self.window}"
        return f"sma{self.window}-r{self.rebalance}"

    @property
    def warmup(self) -> int:
        return self.window

    @property
    def params(self) -> dict:
        return {"window": self.window, "rebalance": self.rebalance}

    def scores(self, bars: list[Bar]) -> list[float | None]:
        """`+1` above the average, `-1` below it, `None` while it does not exist."""
        closes = [b.close for b in bars]
        average = sma(closes, self.window)
        out: list[float | None] = []
        for close, mean in zip(closes, average):
            if mean is None:
                out.append(None)
            elif close > mean:
                out.append(1.0)
            elif close < mean:
                out.append(-1.0)
            else:
                out.append(0.0)
        return out

    def targets(self, bars: list[Bar]) -> list[float]:
        scores = self.scores(bars)
        if self.rebalance == 1:
            return [0.0 if score is None else (1.0 if score > 0 else 0.0) for score in scores]
        return on_decision_grid(scores, bars, self.rebalance)

    def describe(self) -> str:
        every = "" if self.rebalance == 1 else f", decided every {self.rebalance} bars"
        return f"long while close > SMA({self.window}), flat below{'' if not every else every}; no shorts"


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
def _sma(window: int = 200, rebalance: int = 1, **_: object) -> Strategy:
    """Long while the close is above its SMA, flat below."""
    return SmaTrend(window=window, rebalance=rebalance)


@register("sma-ls")
def _sma_ls(window: int = 200, **_: object) -> Strategy:
    """Long above the SMA, short below it: always in the market."""
    return SmaTrendLongShort(window=window)
