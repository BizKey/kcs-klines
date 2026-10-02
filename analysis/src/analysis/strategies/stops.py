"""Take-profit, stop-loss and trailing exits — read on closes, not inside bars.

The inner strategy says *when to be in the market*; this wrapper says *when to give up on
that position*. Three exits, all optional and all switched off at zero:

* `stop_loss` — exit once the close is this far against the entry price.
* `take_profit` — exit once the close is this far in favour of it.
* `trail` — exit once the close falls this far back from the best close since entry.

**Why closes and not the stop price.** With OHLCV bars nobody knows whether the high or the
low came first inside a bar, so "my stop would have filled at 1% below entry" is an
assumption, not a measurement: a bar that pierces a stop and closes above it looks like a
winner, and counting it as a loss instead is exactly as arbitrary. This wrapper therefore
reacts to a close beyond the level and the engine fills it at the **next open** — which, when
the price gaps through the level overnight, is *worse* than the stop price. The measured cost
of these exits is consequently an honest one, and any intrabar version of the same rules would
look better than reality until `engine.py` has an explicit fill model.

**Why the cooldown matters.** Without it the inner signal immediately re-enters the position
it has just been stopped out of (the trend filter still says "above the average"), so every
stop becomes a round trip of commission that changes nothing. `cooldown` bars of forced
flatness is what makes the exit a decision rather than a fee.
"""

from __future__ import annotations

from ..data import Bar
from .base import Strategy
from .registry import register
from .sma_trend import SmaTrend

__all__ = ["StopsStrategy"]


class StopsStrategy(Strategy):
    """Wrap a strategy with close-based take-profit, stop-loss and trailing exits."""

    name = "stops"
    sweep_param = "stop_loss"

    def __init__(
        self,
        inner: Strategy,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
        trail: float = 0.0,
        cooldown: int = 0,
    ):
        if min(take_profit, stop_loss, trail) < 0:
            raise ValueError("take_profit, stop_loss and trail are fractions, not negatives")
        if cooldown < 0:
            raise ValueError("cooldown counts bars and cannot be negative")
        self.inner = inner
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.trail = trail
        self.cooldown = cooldown
        self._cache: tuple[int, int, list[float], list[float | None]] | None = None

    @property
    def slug(self) -> str:
        parts = [self.inner.slug]
        if self.take_profit:
            parts.append(f"tp{self.take_profit:g}")
        if self.stop_loss:
            parts.append(f"sl{self.stop_loss:g}")
        if self.trail:
            parts.append(f"tr{self.trail:g}")
        if self.cooldown:
            parts.append(f"cd{self.cooldown}")
        return "-".join(parts)

    @property
    def warmup(self) -> int:
        return self.inner.warmup

    @property
    def params(self) -> dict:
        payload = dict(self.inner.params)
        payload.update(
            take_profit=self.take_profit, stop_loss=self.stop_loss,
            trail=self.trail, cooldown=self.cooldown,
        )
        return payload

    def describe(self) -> str:
        exits = []
        if self.take_profit:
            exits.append(f"take profit {self.take_profit:.0%}")
        if self.stop_loss:
            exits.append(f"stop loss {self.stop_loss:.0%}")
        if self.trail:
            exits.append(f"trail {self.trail:.0%} off the best close")
        if not exits:
            exits.append("no exits of its own")
        cooldown = f", {self.cooldown} bars of cooldown after an exit" if self.cooldown else ""
        return f"{self.inner.describe()}; exits: {', '.join(exits)}{cooldown} (on closes)"

    def _level_hit(self, bar: Bar, long: bool, reference: float, best: float) -> float | None:
        """The price a level was touched at inside this bar, or `None`."""
        if not reference:
            return None
        if long:
            if self.stop_loss:
                level = reference * (1.0 - self.stop_loss)
                if bar.low <= level:
                    return min(level, bar.open)          # a gap through the stop is worse
            if self.take_profit:
                level = reference * (1.0 + self.take_profit)
                if bar.high >= level:
                    return level                          # a favourable gap is not booked
            if self.trail and best:
                level = best * (1.0 - self.trail)
                if bar.low <= level:
                    return min(level, bar.open)
            return None
        if self.stop_loss:
            level = reference * (1.0 + self.stop_loss)
            if bar.high >= level:
                return max(level, bar.open)
        if self.take_profit:
            level = reference * (1.0 - self.take_profit)
            if bar.low <= level:
                return level
        if self.trail and best:
            level = best * (1.0 + self.trail)
            if bar.high >= level:
                return max(level, bar.open)
        return None

    def targets(self, bars: list[Bar]) -> list[float]:
        return self._simulate(bars)[0]

    def intrabar_exits(self, bars: list[Bar]) -> list[float | None] | None:
        """The prices the levels were touched at — stop first, then take-profit.

        OHLC bars do not say whether the high or the low came first, so the convention has to
        be chosen and stuck to: **if a bar touched the stop and the take-profit, the stop is
        assumed to have filled**, which is the pessimistic reading. The fill is the level
        itself, except when the bar *opened* beyond it — a gap through the stop fills at the
        open, which is worse, and a gap through the take-profit still fills at the level,
        which declines to book a windfall the level never promised.
        """
        return self._simulate(bars)[1]

    def _simulate(self, bars: list[Bar]) -> tuple[list[float], list[float | None]]:
        """Exposure and intrabar exits from one pass, so the two can never disagree."""
        if self._cache is not None:
            cached_len, cached_last, cached_targets, cached_exits = self._cache
            if cached_len == len(bars) and bars and cached_last == bars[-1].time:
                return cached_targets, cached_exits
        targets, exits = self._walk(bars)
        if bars:
            self._cache = (len(bars), bars[-1].time, targets, exits)
        return targets, exits

    def _walk(self, bars: list[Bar]) -> tuple[list[float], list[float | None]]:
        wanted = self.inner.targets(bars)
        out: list[float] = []
        exits: list[float | None] = [None] * len(bars)
        position = 0.0            # the sign this wrapper is actually holding
        reference = 0.0           # the close the position was opened at
        best = 0.0                # best close since it was opened, for the trailing exit
        blocked_until = -1
        for index, (bar, want) in enumerate(zip(bars, wanted)):
            if position == 0.0:
                if want == 0.0 or index < blocked_until:
                    out.append(0.0)
                    continue
                position = 1.0 if want > 0 else -1.0
                reference = best = bar.close
                out.append(want)
                continue

            long = position > 0

            # A level inside this bar ends the position *here*, at the level: the stop first if
            # the bar reached both, because the bars cannot say which came first. The trailing
            # level is the one armed by the closes *before* this bar — arming it with this
            # bar's own close would let a bar's low stop out a peak that only happened later
            # in the same bar, which is an artefact of not knowing the path inside it.
            level = self._level_hit(bar, long, reference, best)
            if level is not None:
                exits[index] = level
                position = 0.0
                blocked_until = index + 1 + self.cooldown
                out.append(0.0)
                continue
            best = max(best, bar.close) if long else min(best, bar.close)

            if want == 0.0:
                position = 0.0
                out.append(0.0)
                continue
            if (want > 0) != long:                      # the inner rule flipped: a new entry
                position = 1.0 if want > 0 else -1.0
                reference = best = bar.close
                out.append(want)
                continue

            moved = (bar.close / reference - 1.0) if reference else 0.0
            if not long:
                moved = -moved
            pullback = (bar.close / best - 1.0) if long and best else (1.0 - bar.close / best if best else 0.0)
            hit = False
            if self.stop_loss and moved <= -self.stop_loss:
                hit = True
            if self.take_profit and moved >= self.take_profit:
                hit = True
            if self.trail and pullback <= -self.trail:
                hit = True
            if hit:
                # the close is beyond the level but the bar's range did not touch it (a gap
                # at the open already past it, say): exit here, filled at the next open
                position = 0.0
                blocked_until = index + 1 + self.cooldown
                out.append(0.0)
            else:
                out.append(want)
        return out, exits


@register("stops-sma")
def _stops_sma(
    window: int = 200,
    take_profit: float = 0.0,
    stop_loss: float = 0.0,
    trail: float = 0.0,
    cooldown: int = 0,
    rebalance: int = 1,
) -> StopsStrategy:
    """`sma` with close-based exits: `--param stop_loss=0.1 --param take_profit=0.3`."""
    return StopsStrategy(
        SmaTrend(window=window, rebalance=rebalance),
        take_profit=take_profit,
        stop_loss=stop_loss,
        trail=trail,
        cooldown=cooldown,
    )


@register("voltarget-stops-sma")
def _voltarget_stops_sma(
    window: int = 200,
    take_profit: float = 0.0,
    stop_loss: float = 0.0,
    trail: float = 0.0,
    cooldown: int = 0,
    target_vol: float = 0.4,
    vol_window: int = 168,
    cap: float = 1.0,
    **_: object,
):
    """The winning basket's rule (`voltarget-sma`) with close-based exits added.

    Exists so the exits can be judged *inside* the configuration that is recommended, rather
    than against a different sizing at the same time.
    """
    from .scaled import ScaledStrategy

    return ScaledStrategy(
        StopsStrategy(
            SmaTrend(window=window), take_profit=take_profit, stop_loss=stop_loss,
            trail=trail, cooldown=cooldown,
        ),
        target_vol, vol_window, cap,
    )


@register("stops-breakout")
def _stops_breakout(
    entry: int = 20,
    exit_window: int = 10,
    min_hold: int = 0,
    take_profit: float = 0.0,
    stop_loss: float = 0.0,
    trail: float = 0.0,
    cooldown: int = 0,
) -> StopsStrategy:
    """Donchian breakout with close-based exits — the case where a stop *is* the only exit.

    A channel exit only fires on a new low, which on an hourly chart can be far below the
    entry; this is where a stop has something to do that the inner rule does not already do.
    """
    from .breakout import DonchianBreakout

    return StopsStrategy(
        DonchianBreakout(entry=entry, exit_window=exit_window, min_hold=min_hold),
        take_profit=take_profit, stop_loss=stop_loss, trail=trail, cooldown=cooldown,
    )
