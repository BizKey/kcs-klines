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

__all__ = ["Tsmom", "BlendTsmom", "MODES", "on_decision_grid"]

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

    def scores(self, bars: list[Bar]) -> list[float | None]:
        """`+1`/`-1`/`0` from the lookback return, `None` while it does not exist.

        The sign carries the mode: in long-only a negative return is `-1`, which
        `on_decision_grid` turns into "flat" as well, so the shorting decision is
        made in one place instead of two.
        """
        out: list[float | None] = []
        for i, bar in enumerate(bars):
            if i < self.lookback or bars[i - self.lookback].close <= 0:
                out.append(None)
                continue
            change = bar.close / bars[i - self.lookback].close - 1.0
            if change > self.threshold:
                out.append(1.0)
            elif change < -self.threshold and self.mode == "long-short":
                out.append(-1.0)
            else:
                out.append(0.0)
        return out

    def targets(self, bars: list[Bar]) -> list[float]:
        if len(bars) < 2:
            return [0.0] * len(bars)
        return on_decision_grid(
            self.scores(bars), bars, self.rebalance, short=self.mode == "long-short"
        )

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


def on_decision_grid(
    scores: list[float | None],
    bars: list[Bar],
    rebalance: int,
    *,
    short: bool = False,
) -> list[float]:
    """Targets read from `scores` only at the grid points, held in between.

    Shared by `Tsmom` and `BlendTsmom` so the timing rule cannot drift between
    them: `scores[t]` is what the rule reads at bar `t`'s close, the exposure
    becomes `1.0` when it is positive and `0.0` otherwise, and it is re-read only
    at the next grid point. `short=True` lets a negative score mean a short
    position rather than cash; a blend stays long-only, because every measurement
    in `CONCLUSIONS.md` says the short leg loses. `None` means "this rule cannot
    speak yet" — the grid is not advanced while that lasts, so a blend whose
    longest horizon is missing starts deciding on the first bar that has it.

    The grid is the one the module docstring describes: absolute multiples of
    `rebalance * step` seconds since the epoch, snapped forward to the first
    available bar, so the decision dates do not move when the archive grows.
    """
    if len(scores) != len(bars):
        raise ValueError(f"got {len(scores)} scores for {len(bars)} bars")
    if rebalance < 1:
        raise ValueError("rebalance must be at least 1 bar")
    out = [0.0] * len(bars)
    start = next((i for i, score in enumerate(scores) if score is not None), None)
    if start is None:
        return out

    step = bar_step(bars)
    period = rebalance * step
    grid = -(-bars[start].time // period) * period  # ceiling division: the next grid point
    held = 0.0
    for i, bar in enumerate(bars):
        if i >= start and bar.time >= grid:
            score = scores[i]
            if score is not None:
                held = 1.0 if score > 0 else (-1.0 if (short and score < 0) else 0.0)
            grid += period
        out[i] = held
    return out


class BlendTsmom(Strategy):
    """Multi-horizon momentum: a strict majority of horizons must be positive.

    The single-horizon rule has one parameter that decides its result, and on this
    archive that parameter does not transfer: measured over 962 hourly series the
    spread of Sharpe between four plausible lookbacks is 0.61 at the median on the
    *same* asset, while the spread between two ways of blending them is 0.14. So
    the blend is not a cleverer signal, it is a rule with nothing left to fit — it
    beat the average single lookback on 66% of those series, and only the hindsight
    of picking the best one per asset beats it.

    The horizons are `base` bars and every doubling of it, so `base=168, horizons=4`
    on hourly bars is one, two, four and eight weeks. The exposure is long when more
    than half of them are positive and flat otherwise — a *strict* majority, so with
    four horizons two up and two down is a coin toss, and a coin toss stays in cash.

    There is no `threshold` here on purpose: the weekly decision already smooths the
    signal, and a dead zone measured at 2% changed the result on 3% of series.
    """

    name = "tsmom-blend"
    sweep_param = "horizons"

    def __init__(self, base: int = 168, horizons: int = 4, rebalance: int = 168):
        if base < 1:
            raise ValueError("base must be at least 1 bar")
        if horizons < 2:
            raise ValueError("a blend needs at least two horizons")
        if rebalance < 1:
            raise ValueError("rebalance must be at least 1 bar")
        self.base = base
        self.horizons = horizons
        self.rebalance = rebalance

    @property
    def lookbacks(self) -> tuple[int, ...]:
        """The horizons, shortest first: `base * 2**k` for `k` in `0..horizons-1`."""
        return tuple(self.base * 2**k for k in range(self.horizons))

    @property
    def slug(self) -> str:
        return f"blend{self.base}x{self.horizons}-{self.rebalance}"

    @property
    def warmup(self) -> int:
        return self.lookbacks[-1]

    @property
    def params(self) -> dict:
        return {"base": self.base, "horizons": self.horizons, "rebalance": self.rebalance}

    def scores(self, bars: list[Bar]) -> list[float | None]:
        """Mean of `sign(return over each horizon)`, `None` until the longest exists."""
        closes = [bar.close for bar in bars]
        out: list[float | None] = []
        for i in range(len(bars)):
            signs = []
            for lookback in self.lookbacks:
                if i < lookback or closes[i - lookback] <= 0:
                    signs = []
                    break
                change = closes[i] / closes[i - lookback] - 1.0
                signs.append(1.0 if change > 0 else (-1.0 if change < 0 else 0.0))
            out.append(sum(signs) / len(signs) if signs else None)
        return out

    def targets(self, bars: list[Bar]) -> list[float]:
        # long-only on purpose: see the class docstring and CONCLUSIONS.md §2
        return on_decision_grid(self.scores(bars), bars, self.rebalance)

    def describe(self) -> str:
        horizons = ", ".join(str(lookback) for lookback in self.lookbacks)
        return (
            f"long while most of the {horizons}-bar returns are positive, "
            f"rebalanced every {self.rebalance} bars"
        )


@register("tsmom-blend")
def _tsmom_blend(
    base: int = 168,
    horizons: int = 4,
    rebalance: int = 168,
    **_: object,
) -> Strategy:
    """Long while most of several horizons are positive, decided every M bars."""
    return BlendTsmom(base=base, horizons=horizons, rebalance=rebalance)
