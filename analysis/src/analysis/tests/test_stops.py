"""Exits around a signal: they must fire on closes, respect the cooldown, and never lie.

The interesting test here is the *redundancy* one: a stop that is looser than the exit the
inner strategy already has must change nothing at all, because that is the whole reason these
exits do nothing on a trend rule and something on a channel breakout.
"""

from __future__ import annotations

from ..data import Bar
from ..strategies import get_strategy
from ..strategies.stops import StopsStrategy
from .conftest import START, STEP, make_bars


def _bars(closes: list[float]) -> list[Bar]:
    return make_bars(closes)


class Always:
    """An inner 'strategy' that is always fully long — the simplest thing to wrap."""

    name = "always"
    slug = "always"
    warmup = 0
    params: dict = {}

    def targets(self, bars: list[Bar]) -> list[float]:
        return [1.0] * len(bars)

    def describe(self) -> str:
        return "always long"


def test_a_stop_loss_flattens_the_position_after_a_close_beyond_it():
    bars = _bars([100.0, 100.0, 95.0, 94.0, 94.0])
    strategy = StopsStrategy(Always(), stop_loss=0.05)
    # the stop forces flat on the bar whose close broke the level; with no cooldown the
    # inner signal is free to buy again on the very next bar, which is exactly the churn
    # the cooldown exists to prevent
    assert strategy.targets(bars) == [1.0, 1.0, 0.0, 1.0, 1.0]


def test_a_take_profit_exits_after_a_favourable_close():
    bars = _bars([100.0, 100.0, 130.0, 131.0])
    strategy = StopsStrategy(Always(), take_profit=0.25)
    assert strategy.targets(bars) == [1.0, 1.0, 0.0, 1.0]


def test_a_trailing_exit_follows_the_best_close_and_never_the_entry():
    bars = _bars([100.0, 120.0, 110.0, 109.0])
    strategy = StopsStrategy(Always(), trail=0.10)
    # 120 is the best close, so a 10% trail gives way at 108: 110 and 109 hold
    assert strategy.targets(bars) == [1.0, 1.0, 1.0, 1.0]
    deeper = _bars([100.0, 120.0, 107.0, 106.0])
    assert StopsStrategy(Always(), trail=0.10).targets(deeper) == [1.0, 1.0, 0.0, 1.0]


def test_the_cooldown_keeps_the_signal_out_after_it_has_been_stopped():
    bars = _bars([100.0, 100.0, 90.0, 100.0, 100.0, 100.0, 100.0])
    without = StopsStrategy(Always(), stop_loss=0.05).targets(bars)
    with_cooldown = StopsStrategy(Always(), stop_loss=0.05, cooldown=3).targets(bars)
    assert without[2] == 0.0 and without[3] == 1.0        # back in immediately
    assert with_cooldown[2:6] == [0.0, 0.0, 0.0, 0.0]     # and stayed out for the cooldown
    assert with_cooldown[6] == 1.0                        # then allowed back in


def test_a_stop_looser_than_the_inner_exit_changes_nothing():
    """The law that explains the measurements: dead code if it never binds."""
    import statistics

    closes = [100.0 + 4.0 * statistics.sin(index / 3.0) + 0.1 * index for index in range(400)]
    bars = _bars(closes)
    plain = get_strategy("sma", window=20).targets(bars)
    wrapped = get_strategy("stops-sma", window=20, stop_loss=0.90, cooldown=5).targets(bars)
    assert wrapped == plain


def test_the_stops_family_is_registered_and_describes_itself():
    from ..strategies import available

    for name in ("stops-sma", "stops-breakout", "voltarget-stops-sma"):
        assert name in available()
    described = get_strategy("stops-sma", window=50, stop_loss=0.1, cooldown=5).describe()
    assert "stop loss 10%" in described and "on closes" in described


def test_the_wrapper_refuses_a_negative_exit():
    import pytest

    with pytest.raises(ValueError):
        StopsStrategy(Always(), stop_loss=-0.1)
    with pytest.raises(ValueError):
        StopsStrategy(Always(), cooldown=-1)
