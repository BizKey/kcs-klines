"""Contrarian strategies: buy below the average, sell above it.

The exact mirror of `sma_trend`. Where `SmaTrend` follows the price across its
moving average, this bets against the stretch: every bar whose close is *below*
`SMA(window)` is bought, every bar above it is sold.

Two readings of "sell" are supported through `mode`:

* `long-only` (default) — long below the average, flat above it. Spot-friendly:
  "sell" means closing the position, never borrowing the asset;
* `long-short` — long below, short above, so the account is always in the market.

The tie rule: a bar whose close is exactly on the average carries no signal, so
the strategy is flat there in both modes (with float prices this is rare).
Note that "buy every bar below the average" in a fully invested model is the
same as holding long while the price stays below it — there is no separate
accumulation or pyramiding.
"""

from __future__ import annotations

from ..data import Bar
from ..metrics import sma
from .base import Strategy
from .registry import register

__all__ = ["SmaReversion", "MODES"]

#: What "sell" means when the close is above the average.
MODES = ("long-only", "long-short")


class SmaReversion(Strategy):
    """Contrarian SMA strategy: buy below the average, sell above it."""

    name = "sma-rev"
    sweep_param = "window"

    def __init__(self, window: int = 200, mode: str = "long-only"):
        if window < 2:
            raise ValueError("window must be at least 2")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        self.window = window
        self.mode = mode

    @property
    def slug(self) -> str:
        suffix = "" if self.mode == "long-only" else "-ls"
        return f"sma{self.window}-rev{suffix}"

    @property
    def warmup(self) -> int:
        return self.window

    @property
    def params(self) -> dict:
        return {"window": self.window, "mode": self.mode}

    def targets(self, bars: list[Bar]) -> list[float]:
        closes = [b.close for b in bars]
        average = sma(closes, self.window)
        out: list[float] = []
        for close, mean in zip(closes, average):
            if mean is None or close == mean:
                out.append(0.0)  # warm-up, or a bar sitting exactly on the line
            elif close < mean:
                out.append(1.0)
            else:
                out.append(-1.0 if self.mode == "long-short" else 0.0)
        return out

    def describe(self) -> str:
        if self.mode == "long-only":
            return f"long while close < SMA({self.window}), flat above — contrarian"
        return f"long below SMA({self.window}), short above — contrarian, always in the market"


@register("sma-rev")
def _sma_rev(window: int = 200, mode: str = "long-only", **_: object) -> Strategy:
    """Buy below the moving average, sell above it (mirror of `sma`)."""
    return SmaReversion(window=window, mode=mode)


@register("sma-rev-ls")
def _sma_rev_ls(window: int = 200, **_: object) -> Strategy:
    """Buy below the moving average and short above it: always in the market."""
    return SmaReversion(window=window, mode="long-short")
