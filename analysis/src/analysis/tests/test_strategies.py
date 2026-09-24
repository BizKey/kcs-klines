"""The strategies themselves, and the registry that builds them."""

from __future__ import annotations

import pathlib

import pytest

from ..strategies import (
    REGISTRY,
    SmaTrend,
    SmaTrendLongShort,
    available,
    describe_registry,
    get_strategy,
    parameters,
    sweep_parameter,
)
from ..metrics import sma
from ..strategies import SmaReversion
from ..strategies.base import Strategy
from .conftest import make_bars, ramp, wavy


def test_strategy_is_flat_until_the_window_is_full():
    strategy = SmaTrend(window=5)
    targets = strategy.targets(make_bars(ramp(10)))
    assert targets[:4] == [0.0] * 4  # no average exists yet
    assert strategy.warmup == 5


def test_strategy_is_long_only_while_the_close_is_above_the_average():
    # Up, up, up, then a sharp drop that puts the close below its average.
    closes = [10, 11, 12, 13, 14, 5]
    targets = SmaTrend(window=3).targets(make_bars(closes))
    assert targets == [0.0, 0.0, 1.0, 1.0, 1.0, 0.0]


def test_strategy_never_shorts_and_stays_within_bounds():
    targets = SmaTrend(window=4).targets(make_bars([10, 9, 8, 12, 7, 15, 3, 20, 2, 30]))
    assert set(targets) <= {0.0, 1.0}


def test_long_short_strategy_is_always_in_the_market_after_warmup():
    targets = SmaTrendLongShort(window=3).targets(make_bars([10, 11, 12, 13, 4, 5]))
    assert targets[:2] == [0.0, 0.0]
    assert set(targets[2:]) == {1.0, -1.0}


def test_long_short_strategy_takes_the_opposite_side_below_the_average():
    targets = SmaTrendLongShort(window=3).targets(make_bars([10, 11, 12, 4]))
    assert targets[-1] == -1.0


def test_strategy_rejects_a_window_shorter_than_two():
    with pytest.raises(ValueError):
        SmaTrend(window=1)
    with pytest.raises(ValueError):
        SmaTrendLongShort(window=0)


def test_strategy_metadata_is_stable():
    strategy = SmaTrend(window=50)
    assert strategy.slug == "sma50"
    assert strategy.name == "sma"
    assert strategy.params == {"window": 50}
    assert "SMA(50)" in strategy.describe()
    assert str(strategy) == "sma50"
    assert isinstance(strategy, Strategy)


def test_registry_builds_by_name():
    strategy = get_strategy("sma", window=7)
    assert isinstance(strategy, SmaTrend)
    assert strategy.window == 7


def test_registry_lists_what_it_has():
    assert available() == sorted(REGISTRY)
    assert {"sma", "sma-ls"} <= set(available())


def test_registry_rejects_an_unknown_name_and_says_what_exists():
    with pytest.raises(KeyError, match="available"):
        get_strategy("does-not-exist")


def sample_strategy(name: str) -> Strategy:
    """A small instance of a registered strategy, for contract tests.

    Parameters are turned down via the declared sweep parameter, so a series of
    ten bars is enough for the strategy to actually reach a signal.
    """
    sweep = sweep_parameter(name)
    return get_strategy(name, **({sweep: 3} if sweep else {}))


def test_every_registered_strategy_honours_the_interface():
    """A new strategy is covered by this the moment it is registered."""
    bars = make_bars([10, 11, 12, 11, 13, 12, 14, 9, 15, 8])
    for name in available():
        strategy = sample_strategy(name)
        targets = strategy.targets(bars)
        assert len(targets) == len(bars), name
        assert all(-1.0 <= t <= 1.0 for t in targets), name
        # `warmup` bars are needed before any signal can exist, so everything
        # before the last warm-up bar must be flat.
        assert targets[: strategy.warmup - 1] == [0.0] * (strategy.warmup - 1), name
        assert isinstance(strategy.params, dict), name
        assert strategy.slug and strategy.name != "strategy", name
        assert strategy.describe(), name
        assert strategy.targets([]) == [], name
        # Something must actually be traded over a wavy series.
        wavy_targets = strategy.targets(make_bars([10, 12, 11, 13, 9, 14, 8, 15, 7, 16, 6, 17]))
        assert any(wavy_targets), name


def test_registry_parameters_come_from_the_factory_signature():
    assert parameters("sma") == {"window": 200}
    assert parameters("sma-ls") == {"window": 200}
    with pytest.raises(KeyError, match="available"):
        parameters("nope")


def test_registry_knows_which_parameter_to_sweep():
    assert sweep_parameter("sma") == "window"
    assert sweep_parameter("sma-ls") == "window"


def test_registry_describes_itself():
    described = "\n".join(describe_registry())
    assert "sma(window=200)" in described
    assert "--sweep window" in described
    assert "SMA" in described


def test_strategy_modules_import_the_registry_not_the_package_root():
    """`from . import register` inside a strategy module is a circular import."""
    import analysis.strategies as package

    modules = [p for p in pathlib.Path(package.__file__).parent.glob("*.py") if p.name != "__init__.py"]
    assert modules
    for module in modules:
        source = module.read_text()
        assert "from . import" not in source, f"{module.name} would deadlock the package import"
        assert "from .. import" not in source, module.name
        assert "from ..data import" in source or "from .base import" in source or module.name == "registry.py"


def test_the_registry_module_can_be_imported_on_its_own():
    """The property that makes the two-line workflow safe."""
    import importlib

    solo = importlib.import_module("analysis.strategies.registry")
    assert solo.REGISTRY is REGISTRY


def test_registering_a_duplicate_name_is_an_error():
    with pytest.raises(ValueError, match="already registered"):
        from ..strategies import register

        register("sma")(SmaTrend)


def test_targets_never_peek_at_later_bars():
    """The contract that keeps a backtest honest, checked on every strategy."""
    bars = make_bars([10, 11, 12, 11, 13, 12, 14, 9, 15, 8])
    for strategy in [SmaTrend(window=3), SmaTrendLongShort(window=3)] + [
        sample_strategy(name) for name in available()
    ]:
        original = strategy.targets(bars)
        for changed_index in range(1, len(bars)):
            nudged = make_bars(
                [
                    bar.close * 10 if i == changed_index else bar.close
                    for i, bar in enumerate(bars)
                ]
            )
            after = strategy.targets(nudged)
            # A target at bar i may only depend on bars 0..i, so rewriting bar
            # `changed_index` must leave every earlier target untouched.
            assert after[:changed_index] == original[:changed_index], (
                f"{strategy.slug} changed an earlier target when bar {changed_index} was rewritten"
            )


def test_targets_are_one_value_per_bar():
    bars = make_bars(ramp(20))
    assert len(SmaTrend(window=5).targets(bars)) == len(bars)


def test_targets_of_an_empty_series_are_empty():
    assert SmaTrend(window=5).targets([]) == []


# --- the contrarian mirror -------------------------------------------------


def reversion_bars() -> list:
    """A wavy series whose closes never sit exactly on their own average."""
    return make_bars(wavy(60))


def test_reversion_buys_below_the_average_and_sells_above_it():
    bars = reversion_bars()
    strategy = SmaReversion(window=5)
    targets = strategy.targets(bars)
    average = sma([b.close for b in bars], 5)
    for close, mean, target in zip([b.close for b in bars], average, targets):
        if mean is None:
            assert target == 0.0
        elif close < mean:
            assert target == 1.0  # below the line: bought
        elif close > mean:
            assert target == 0.0  # above the line: sold out
        else:
            assert target == 0.0  # exactly on the line: no signal


def test_reversion_long_short_sells_short_above_the_average():
    bars = reversion_bars()
    targets = SmaReversion(window=5, mode="long-short").targets(bars)
    average = sma([b.close for b in bars], 5)
    for close, mean, target in zip([b.close for b in bars], average, targets):
        if mean is None or close == mean:
            assert target == 0.0
        else:
            assert target == (1.0 if close < mean else -1.0)


def test_reversion_is_the_mirror_of_the_trend_rule():
    """The two strategies must not agree on a single bar."""
    bars = reversion_bars()
    trend = SmaTrend(window=5).targets(bars)
    reversion = SmaReversion(window=5).targets(bars)
    average = sma([b.close for b in bars], 5)
    for close, mean, up, down in zip([b.close for b in bars], average, trend, reversion):
        if mean is None or close == mean:
            assert down == 0.0
        else:
            assert down == 1.0 - up
            assert down != up


def test_reversion_long_short_is_the_negated_trend_long_short():
    bars = reversion_bars()
    trend = SmaTrendLongShort(window=5).targets(bars)
    reversion = SmaReversion(window=5, mode="long-short").targets(bars)
    average = sma([b.close for b in bars], 5)
    for close, mean, up, down in zip([b.close for b in bars], average, trend, reversion):
        if mean is None or close == mean:
            assert down == 0.0
        else:
            assert down == -up


def test_a_flat_market_gives_the_reversion_strategy_no_signal():
    """Every close is exactly on the average, so there is nothing to bet on."""
    bars = make_bars([100.0] * 12)
    assert SmaReversion(window=4).targets(bars) == [0.0] * 12
    assert SmaReversion(window=4, mode="long-short").targets(bars) == [0.0] * 12


def test_reversion_metadata_and_validation():
    assert SmaReversion(window=50).slug == "sma50-rev"
    assert SmaReversion(window=50, mode="long-short").slug == "sma50-rev-ls"
    assert SmaReversion(window=50).params == {"window": 50, "mode": "long-only"}
    assert "contrarian" in SmaReversion(window=50).describe()
    with pytest.raises(ValueError, match="mode must be one of"):
        SmaReversion(window=50, mode="both-ways")
    with pytest.raises(ValueError):
        SmaReversion(window=1)


def test_reversion_is_registered_under_both_names():
    long_only = get_strategy("sma-rev", window=50)
    long_short = get_strategy("sma-rev-ls", window=50)
    assert (long_only.mode, long_short.mode) == ("long-only", "long-short")
    assert parameters("sma-rev") == {"window": 200, "mode": "long-only"}
    assert sweep_parameter("sma-rev") == "window"
    # The mode is a normal parameter too.
    assert get_strategy("sma-rev", window=50, mode="long-short").slug == long_short.slug
