"""The risk-off filter: leave the wild regime, and never quietly do anything else."""

from __future__ import annotations

from ..strategies import get_strategy
from ..strategies.volfilter import VolFilter
from .conftest import make_bars, ramp, wavy


class Always:
    name = "always"
    slug = "always"
    warmup = 0
    params: dict = {}

    def targets(self, bars):
        return [1.0] * len(bars)

    def describe(self) -> str:
        return "always long"


def _calm_then_wild() -> list:
    """A series that moves a little, then a lot — the regime the ceiling exists for."""
    closes = [100.0 + 0.1 * (index % 3) for index in range(60)]
    closes += [100.0 + 8.0 * ((-1) ** index) for index in range(60)]
    return make_bars(closes)


def test_the_filter_is_in_during_the_calm_regime_and_out_during_the_wild_one():
    bars = _calm_then_wild()
    strategy = VolFilter(Always(), max_vol=0.5, vol_window=20, min_observations=10)
    targets = strategy.targets(bars)
    assert any(targets[:60]), "the calm stretch should be tradeable"
    assert not any(targets[60:]), "the wild stretch should be refused"


def test_a_very_high_ceiling_makes_it_transparent():
    bars = _calm_then_wild()
    permissive = VolFilter(Always(), max_vol=1000.0, vol_window=20, min_observations=10)
    assert permissive.targets(bars) == [1.0] * len(bars)


def test_the_filter_can_only_take_exposure_away():
    bars = make_bars(wavy(300))
    inner = get_strategy("sma", window=20).targets(bars)
    filtered = get_strategy("volfilter-sma", window=20, max_vol=0.6, vol_window=30).targets(bars)
    assert all(0.0 <= f <= i for f, i in zip(filtered, inner))


def test_it_never_shorts_and_survives_a_falling_market():
    bars = make_bars(ramp(200, start=200.0, step=-0.5))
    targets = get_strategy("volfilter-sma", window=20, max_vol=0.8, vol_window=30).targets(bars)
    assert min(targets) >= 0.0
    assert not any(targets), "a downtrend below the mean should keep it out entirely"


def test_the_reading_never_uses_a_later_bar():
    bars = _calm_then_wild()
    strategy = VolFilter(Always(), max_vol=0.5, vol_window=20, min_observations=10)
    after = strategy.targets(bars)
    rewritten = [
        type(bar)(time=bar.time, open=bar.open, high=bar.high * 5, low=bar.low,
                  close=bar.close * 5, volume=bar.volume)
        for bar in bars[80:]
    ]
    before = strategy.targets(bars[:80] + rewritten)
    assert before[:80] == after[:80]


def test_the_family_is_registered():
    from ..strategies import available

    for name in ("volfilter-sma", "volfilter-tsmom", "voltarget-volfilter-sma"):
        assert name in available()
    assert "above 80% annualised" in get_strategy("volfilter-sma").describe()
