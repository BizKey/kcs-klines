"""The MACD crossover strategy.

MACD is the difference between a fast and a slow exponential moving average of
the close; its *signal line* is an EMA of that difference. The rule here is the
classic one: be long while MACD is above its signal line, be out (or short) while
it is below.

Two readings of "below" are supported through `mode`, matching the SMA family:

* `long-only` (default) — long above the signal line, flat below. Spot-friendly;
* `long-short` — long above, short below, so the account is always in the market.

Defaults are the textbook `MACD(12, 26, 9)`. A bar whose MACD equals its signal
line carries no signal, so the strategy is flat there in both modes (with float
prices this is rare). The zero-line variant of the same idea — long while
`MACD > 0`, i.e. while the fast EMA is above the slow one — is *not* what this
class does; it would need the slow average as the reference instead of the signal
line.
"""

from __future__ import annotations

from ..data import Bar
from ..metrics import macd as macd_lines
from .base import Strategy
from .registry import register

__all__ = ["MacdTrend", "MODES"]

#: What "MACD below its signal line" means.
MODES = ("long-only", "long-short")


class MacdTrend(Strategy):
    """Long while MACD is above its signal line, out (or short) below."""

    name = "macd"
    sweep_param = "signal"

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        mode: str = "long-only",
    ):
        if fast < 2 or slow < 2 or signal < 2:
            raise ValueError("fast, slow and signal must each be at least 2")
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be shorter than slow ({slow})")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.mode = mode

    @property
    def slug(self) -> str:
        suffix = "" if self.mode == "long-only" else "-ls"
        return f"macd{self.fast}-{self.slow}-{self.signal}{suffix}"

    @property
    def warmup(self) -> int:
        """Bars needed before the signal line exists: `slow` then `signal` more."""
        return self.slow + self.signal - 1

    @property
    def params(self) -> dict:
        return {"fast": self.fast, "slow": self.slow, "signal": self.signal, "mode": self.mode}

    def targets(self, bars: list[Bar]) -> list[float]:
        macd_line, signal_line, _ = macd_lines(
            [bar.close for bar in bars], self.fast, self.slow, self.signal
        )
        out: list[float] = []
        for macd_value, signal_value in zip(macd_line, signal_line):
            if macd_value is None or signal_value is None or macd_value == signal_value:
                out.append(0.0)  # warm-up, or a bar sitting exactly on the signal line
            elif macd_value > signal_value:
                out.append(1.0)
            else:
                out.append(-1.0 if self.mode == "long-short" else 0.0)
        return out

    def describe(self) -> str:
        name = f"MACD({self.fast}, {self.slow}, {self.signal})"
        if self.mode == "long-only":
            return f"long while {name} is above its signal line, flat below"
        return f"long above the {name} signal line, short below — always in the market"


@register("macd")
def _macd(fast: int = 12, slow: int = 26, signal: int = 9, mode: str = "long-only", **_: object) -> Strategy:
    """Long while MACD is above its signal line, flat below."""
    return MacdTrend(fast=fast, slow=slow, signal=signal, mode=mode)


@register("macd-ls")
def _macd_ls(fast: int = 12, slow: int = 26, signal: int = 9, **_: object) -> Strategy:
    """Long above the MACD signal line and short below it: always in the market."""
    return MacdTrend(fast=fast, slow=slow, signal=signal, mode="long-short")
