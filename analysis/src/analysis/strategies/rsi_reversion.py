"""Mean reversion on daily bars, measured with RSI.

The mirror-image idea that failed on hourly bars in this repository
(`SmaReversion` on BTC-USDT 1h: net -95%) deserves a fair test on a timeframe
where mean reversion has a history: daily bars, with an explicit exit rule
instead of "hold until the average is crossed again".

The rule is a state machine:

* enter long when RSI(`window`) falls below `oversold`;
* leave when RSI rises above `exit_level`, or after `max_hold` bars, whichever
  comes first;
* in `long-short` mode, mirror it: short when RSI rises above `overbought`, and
  cover on `exit_level` or after `max_hold` bars.

`window=2` with `oversold=10` is the classic short-term version (two down days
push RSI(2) under 10); `window=14` with `oversold=30` is the textbook one. The
time stop matters: without it a mean-reversion entry has no exit at all while the
price keeps falling.
"""

from __future__ import annotations

from ..data import Bar
from ..metrics import rsi
from .base import Strategy
from .registry import register

__all__ = ["RsiReversion", "MODES"]

#: Which side of the market to trade.
MODES = ("long-only", "long-short")


class RsiReversion(Strategy):
    """Buy oversold RSI, exit on the exit level or after a fixed number of bars."""

    name = "rsi-rev"
    sweep_param = "window"

    def __init__(
        self,
        window: int = 2,
        oversold: float = 10.0,
        overbought: float = 90.0,
        exit_level: float = 50.0,
        max_hold: int = 10,
        mode: str = "long-only",
    ):
        if window < 2:
            raise ValueError("window must be at least 2")
        if not 0.0 < oversold < exit_level < overbought < 100.0:
            raise ValueError(
                "thresholds must satisfy 0 < oversold < exit_level < overbought < 100 "
                f"(got {oversold}, {exit_level}, {overbought})"
            )
        if max_hold < 1:
            raise ValueError("max_hold must be at least 1 bar")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        self.window = window
        self.oversold = oversold
        self.overbought = overbought
        self.exit_level = exit_level
        self.max_hold = max_hold
        self.mode = mode

    @property
    def slug(self) -> str:
        suffix = "" if self.mode == "long-only" else "-ls"
        return f"rsi{self.window}-{self.oversold:g}-{self.exit_level:g}-h{self.max_hold}{suffix}"

    @property
    def warmup(self) -> int:
        return self.window + 1

    @property
    def params(self) -> dict:
        return {
            "window": self.window,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "exit_level": self.exit_level,
            "max_hold": self.max_hold,
            "mode": self.mode,
        }

    def targets(self, bars: list[Bar]) -> list[float]:
        values = rsi([bar.close for bar in bars], self.window)
        out: list[float] = []
        position = 0
        entry_index: int | None = None
        for i, value in enumerate(values):
            if value is None:
                out.append(0.0)  # warm-up
                continue

            held = i - entry_index if entry_index is not None else 0
            if position == 0:
                if value < self.oversold:
                    position, entry_index = 1, i
                elif self.mode == "long-short" and value > self.overbought:
                    position, entry_index = -1, i
            elif position == 1:
                if held >= self.max_hold or value > self.exit_level:
                    position, entry_index = 0, None
            else:
                if held >= self.max_hold or value < self.exit_level:
                    position, entry_index = 0, None

            out.append(float(position))
        return out

    def describe(self) -> str:
        entry = f"RSI({self.window}) < {self.oversold:g}"
        exit_rule = f"RSI > {self.exit_level:g} or {self.max_hold} bars"
        if self.mode == "long-only":
            return f"long when {entry}, exit when {exit_rule}"
        return f"long when {entry}, short above {self.overbought:g}, exit on {exit_rule}"


@register("rsi-rev")
def _rsi_rev(
    window: int = 2,
    oversold: float = 10.0,
    overbought: float = 90.0,
    exit_level: float = 50.0,
    max_hold: int = 10,
    mode: str = "long-only",
    **_: object,
) -> Strategy:
    """Buy oversold RSI, leave on the exit level or after a time stop."""
    return RsiReversion(
        window=window,
        oversold=oversold,
        overbought=overbought,
        exit_level=exit_level,
        max_hold=max_hold,
        mode=mode,
    )


@register("rsi-rev-ls")
def _rsi_rev_ls(
    window: int = 2,
    oversold: float = 10.0,
    overbought: float = 90.0,
    exit_level: float = 50.0,
    max_hold: int = 10,
    **_: object,
) -> Strategy:
    """Mean reversion both ways: buy oversold, sell overbought."""
    return RsiReversion(
        window=window,
        oversold=oversold,
        overbought=overbought,
        exit_level=exit_level,
        max_hold=max_hold,
        mode="long-short",
    )
