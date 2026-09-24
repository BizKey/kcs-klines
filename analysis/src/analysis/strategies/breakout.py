"""Donchian channel breakout: enter on a new extreme, leave on a smaller one.

A channel system in the classic shape: go long when the close takes out the
highest high of the last `entry` bars, and stay in until the close breaks the
lowest low of the last `exit_window` bars (or until `min_hold` bars have passed,
whichever the parameters require).

The two windows are separate on purpose. The first version of this strategy in
the toolkit used a single window as both entry and exit — which means it exited
the moment the price dipped below the level it had just broken, turning one
trend into thousands of round trips (3,798 trades at `entry=10` on BTC-USDT 1h).
A *shorter* exit channel is what makes a breakout system hold a position through
a normal pullback.

`min_hold` blocks the exit channel for the first N bars of a trade, and
`mode="long-short"` mirrors the whole thing below the market (short a new N-bar
low, cover on the exit channel high).
"""

from __future__ import annotations

from ..data import Bar
from .base import Strategy
from .registry import register

__all__ = ["DonchianBreakout", "MODES"]

#: What a new N-bar low means.
MODES = ("long-only", "long-short")


class DonchianBreakout(Strategy):
    """Long on a new `entry`-bar high, out on a new `exit_window`-bar low."""

    name = "breakout"
    sweep_param = "entry"

    def __init__(
        self,
        entry: int = 20,
        exit_window: int = 10,
        min_hold: int = 0,
        mode: str = "long-only",
    ):
        if entry < 2 or exit_window < 2:
            raise ValueError("entry and exit_window must each be at least 2 bars")
        if min_hold < 0:
            raise ValueError("min_hold cannot be negative")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        self.entry = entry
        self.exit_window = exit_window
        self.min_hold = min_hold
        self.mode = mode

    @property
    def slug(self) -> str:
        suffix = "" if self.mode == "long-only" else "-ls"
        base = f"donchian{self.entry}-{self.exit_window}"
        if self.min_hold:
            base += f"-h{self.min_hold}"
        return base + suffix

    @property
    def warmup(self) -> int:
        return max(self.entry, self.exit_window)

    @property
    def params(self) -> dict:
        return {
            "entry": self.entry,
            "exit_window": self.exit_window,
            "min_hold": self.min_hold,
            "mode": self.mode,
        }

    def targets(self, bars: list[Bar]) -> list[float]:
        out: list[float] = []
        position = 0
        entry_index: int | None = None
        for i, bar in enumerate(bars):
            if i < self.warmup:
                out.append(0.0)  # the channels do not exist yet
                continue

            entry_high = max(b.high for b in bars[i - self.entry : i])  # excludes bar i
            entry_low = min(b.low for b in bars[i - self.entry : i])
            exit_high = max(b.high for b in bars[i - self.exit_window : i])
            exit_low = min(b.low for b in bars[i - self.exit_window : i])
            close = bar.close
            held = i - entry_index if entry_index is not None else 0

            if position == 0:
                if close > entry_high:
                    position, entry_index = 1, i
                elif self.mode == "long-short" and close < entry_low:
                    position, entry_index = -1, i
            elif position == 1:
                if self.mode == "long-short" and close < entry_low:
                    position, entry_index = -1, i  # straight through to the short side
                elif held >= self.min_hold and close < exit_low:
                    position, entry_index = 0, None
            else:
                if self.mode == "long-short" and close > entry_high:
                    position, entry_index = 1, i
                elif held >= self.min_hold and close > exit_high:
                    position, entry_index = 0, None

            out.append(float(position))
        return out

    def describe(self) -> str:
        hold = f", minimum hold {self.min_hold} bars" if self.min_hold else ""
        if self.mode == "long-only":
            return f"long on a new {self.entry}-bar high, exit on a {self.exit_window}-bar low{hold}"
        return f"long/short Donchian({self.entry}/{self.exit_window}) breakout{hold}"


@register("breakout")
def _breakout(
    entry: int = 20,
    exit_window: int = 10,
    min_hold: int = 0,
    mode: str = "long-only",
    **_: object,
) -> Strategy:
    """Long on a new N-bar high, out on a shorter low channel."""
    return DonchianBreakout(entry=entry, exit_window=exit_window, min_hold=min_hold, mode=mode)


@register("breakout-ls")
def _breakout_ls(
    entry: int = 20,
    exit_window: int = 10,
    min_hold: int = 0,
    **_: object,
) -> Strategy:
    """Long/short channel breakout: both sides of the range."""
    return DonchianBreakout(
        entry=entry, exit_window=exit_window, min_hold=min_hold, mode="long-short"
    )
