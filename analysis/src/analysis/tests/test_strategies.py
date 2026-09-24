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
from ..data import SECONDS_PER_YEAR
from ..data import Bar
from ..engine import Costs, run_backtest
from ..metrics import macd, sma
from ..strategies import DonchianBreakout, MacdTrend, RsiReversion, ScaledStrategy, SmaReversion, Tsmom
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
    """A new strategy is covered by this the moment it is registered.

    The series is long enough for the slowest registered defaults (TSMOM looks
    back 720 bars) to reach a signal, otherwise "it never trades" would pass the
    check below by accident.
    """
    bars = make_bars(wavy(900))
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
        # Something must actually be traded over that series.
        assert any(targets), f"{name} never takes a position over 900 wavy bars"


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
    bars = make_bars(wavy(900))
    for strategy in [SmaTrend(window=3), SmaTrendLongShort(window=3)] + [
        sample_strategy(name) for name in available()
    ]:
        original = strategy.targets(bars)
        assert any(original), f"{strategy.slug} never signals, so the check would be vacuous"
        # A sample of bars is enough: rewriting every single one would make this
        # O(bars^2) per strategy for no extra signal.
        stride = max(1, len(bars) // 25)
        for changed_index in range(1, len(bars), stride):
            nudged = list(bars)
            bar = bars[changed_index]
            nudged[changed_index] = Bar(bar.time, bar.open, bar.high, bar.low, bar.close * 10, bar.volume)
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


# --- MACD -------------------------------------------------------------------


def macd_bars():
    return make_bars(wavy(400))


def test_macd_is_long_above_its_signal_line_and_flat_below():
    bars = macd_bars()
    strategy = MacdTrend(fast=12, slow=26, signal=9)
    targets = strategy.targets(bars)
    line, signal, _ = macd([b.close for b in bars], 12, 26, 9)
    for m, s, target in zip(line, signal, targets):
        if m is None or s is None or m == s:
            assert target == 0.0  # warm-up, or exactly on the line
        elif m > s:
            assert target == 1.0
        else:
            assert target == 0.0


def test_macd_long_short_sells_below_the_signal_line():
    bars = macd_bars()
    short_targets = MacdTrend(fast=12, slow=26, signal=9, mode="long-short").targets(bars)
    line, signal, _ = macd([b.close for b in bars], 12, 26, 9)
    for m, s, target in zip(line, signal, short_targets):
        if m is None or s is None or m == s:
            assert target == 0.0
        else:
            assert target == (1.0 if m > s else -1.0)
    assert set(short_targets) <= {-1.0, 0.0, 1.0}
    assert -1.0 in short_targets and 1.0 in short_targets


def test_the_two_macd_modes_agree_above_the_line_and_mirror_below_it():
    """`long-short` is `long-only` with the flat side turned into a short side."""
    bars = macd_bars()
    long_only = MacdTrend().targets(bars)
    long_short = MacdTrend(mode="long-short").targets(bars)
    line, signal, _ = macd([b.close for b in bars], 12, 26, 9)
    for m, s, flat, both in zip(line, signal, long_only, long_short):
        if m is None or s is None or m == s:
            assert (flat, both) == (0.0, 0.0)  # no signal: both stay out
        elif m > s:
            assert (flat, both) == (1.0, 1.0)  # both modes are long above the line
        else:
            assert (flat, both) == (0.0, -1.0)  # only the long-short mode sells


def test_macd_stays_flat_through_its_warmup():
    bars = macd_bars()
    strategy = MacdTrend(fast=12, slow=26, signal=9)
    assert strategy.warmup == 26 + 9 - 1
    targets = strategy.targets(bars)
    assert targets[: strategy.warmup - 1] == [0.0] * (strategy.warmup - 1)
    assert any(targets[strategy.warmup - 1 :])  # it does trade afterwards


def test_macd_metadata_slugs_and_validation():
    assert MacdTrend().slug == "macd12-26-9"
    assert MacdTrend(mode="long-short").slug == "macd12-26-9-ls"
    assert MacdTrend(fast=5, slow=13, signal=4).slug == "macd5-13-4"
    assert MacdTrend().params == {"fast": 12, "slow": 26, "signal": 9, "mode": "long-only"}
    assert "MACD(12, 26, 9)" in MacdTrend().describe()
    assert "always in the market" in MacdTrend(mode="long-short").describe()

    with pytest.raises(ValueError, match="shorter than slow"):
        MacdTrend(fast=30, slow=26)
    with pytest.raises(ValueError, match="at least 2"):
        MacdTrend(slow=1)
    with pytest.raises(ValueError, match="mode must be one of"):
        MacdTrend(mode="both")


def test_macd_is_registered_under_both_names():
    assert get_strategy("macd").slug == "macd12-26-9"
    assert get_strategy("macd-ls").slug == "macd12-26-9-ls"
    assert get_strategy("macd", fast=5, slow=13, signal=4).slug == "macd5-13-4"
    assert parameters("macd") == {"fast": 12, "slow": 26, "signal": 9, "mode": "long-only"}
    assert sweep_parameter("macd") == "signal"
    assert "MACD" in "\n".join(describe_registry())


# --- TSMOM: a slow decision, taken rarely -----------------------------------


def test_tsmom_only_changes_its_mind_on_the_rebalance_grid():
    bars = make_bars(wavy(600))
    step = 3600
    rebalance = 50
    period = rebalance * step
    targets = Tsmom(lookback=10, rebalance=rebalance).targets(bars)

    # A grid bucket is a run of bars sharing `time // period`; the decision may
    # only change on the first bar of a bucket (after the warm-up).
    for i in range(11, len(bars)):
        same_bucket = bars[i].time // period == bars[i - 1].time // period
        if same_bucket:
            assert targets[i] == targets[i - 1], f"target changed mid-bucket at bar {i}"


def test_tsmom_decides_on_the_first_bar_of_each_grid_bucket():
    bars = make_bars(wavy(600))
    period = 50 * 3600
    targets = Tsmom(lookback=10, rebalance=50).targets(bars)
    bucket_starts = [
        i for i in range(11, len(bars)) if bars[i].time // period != bars[i - 1].time // period
    ]
    decisions = {i for i in range(11, len(bars)) if targets[i] != targets[i - 1]}
    assert decisions
    assert decisions <= set(bucket_starts)


def test_tsmom_holds_its_position_between_decisions():
    bars = make_bars(wavy(600))
    targets = Tsmom(lookback=10, rebalance=120).targets(bars)
    changes = sum(1 for a, b in zip(targets, targets[1:]) if a != b)
    # 600 bars / 120-bar grid: at most a handful of decisions, not one per bar.
    assert changes <= 600 / 120 + 2


def test_tsmom_with_rebalance_one_is_the_plain_momentum_filter():
    bars = make_bars(wavy(200))
    targets = Tsmom(lookback=5, rebalance=1).targets(bars)
    for i in range(5, len(bars)):
        expected = 1.0 if bars[i].close > bars[i - 5].close else 0.0
        assert targets[i] == expected, i


def test_tsmom_still_rebalances_when_the_series_misses_the_grid():
    """Timestamps that never land on a grid multiple must not silence the rule."""
    from .conftest import make_bars as build

    bars = build(wavy(400), start=1_507_161_600 + 1234)  # deliberately off-grid
    targets = Tsmom(lookback=10, rebalance=24).targets(bars)
    assert any(targets)


def test_tsmom_decisions_do_not_move_when_the_series_is_extended():
    """Appending bars must not rewrite the decisions already taken."""
    bars = make_bars(wavy(600))
    strategy = Tsmom(lookback=20, rebalance=40)
    full = strategy.targets(bars)
    assert full[:400] == strategy.targets(bars[:400])


def test_tsmom_threshold_skips_weak_signals():
    bars = make_bars(wavy(600))
    loose = Tsmom(lookback=20, rebalance=40).targets(bars)
    strict = Tsmom(lookback=20, rebalance=40, threshold=0.5).targets(bars)
    assert sum(1 for t in strict if t) <= sum(1 for t in loose if t)
    assert any(strict) or sum(1 for t in strict if t) == 0  # may legitimately be always flat


def test_tsmom_long_short_takes_both_sides():
    bars = make_bars(wavy(600))
    targets = Tsmom(lookback=20, rebalance=40, mode="long-short").targets(bars)
    assert set(targets) <= {-1.0, 0.0, 1.0}
    assert 1.0 in targets and -1.0 in targets


def test_tsmom_metadata_and_validation():
    assert Tsmom(lookback=720, rebalance=168).slug == "tsmom720-168"
    assert Tsmom(lookback=30, rebalance=7, mode="long-short").slug == "tsmom30-7-ls"
    assert Tsmom(lookback=30, rebalance=7, threshold=0.05).slug == "tsmom30-7-t0.05"
    assert Tsmom().warmup == 720
    assert "every 168 bars" in Tsmom().describe()
    with pytest.raises(ValueError, match="lookback"):
        Tsmom(lookback=0)
    with pytest.raises(ValueError, match="rebalance"):
        Tsmom(rebalance=0)
    with pytest.raises(ValueError, match="mode must be one of"):
        Tsmom(mode="both")
    with pytest.raises(ValueError, match="cannot be negative"):
        Tsmom(threshold=-0.1)


def test_tsmom_is_registered():
    assert get_strategy("tsmom", lookback=30, rebalance=7).slug == "tsmom30-7"
    assert get_strategy("tsmom-ls", lookback=30, rebalance=7).slug == "tsmom30-7-ls"
    assert parameters("tsmom")["rebalance"] == 168
    assert sweep_parameter("tsmom") == "lookback"


# --- Donchian channel -------------------------------------------------------


def channel_bars() -> list:
    """20 flat bars, a clean rise, a shallow pullback, then a deeper one."""
    closes = [100.0] * 20
    closes += [100.0 + i for i in range(1, 21)]  # 101 .. 120
    closes += [119.0, 118.0]  # shallow pullback: the channel survives
    closes += [110.0, 105.0]  # breaks the exit channel
    return make_bars(closes)


def test_donchian_enters_on_a_new_high():
    bars = channel_bars()
    targets = DonchianBreakout(entry=10, exit_window=5).targets(bars)
    assert targets[:10] == [0.0] * 10  # warm-up
    assert any(targets)


def test_donchian_holds_through_a_shallow_pullback_and_exits_on_a_deep_one():
    bars = channel_bars()
    targets = DonchianBreakout(entry=10, exit_window=5).targets(bars)
    entry = targets.index(1.0)
    # The shallow pullback (119, 118) must not close the position.
    shallow = len(bars) - 4
    assert targets[shallow] == 1.0
    assert targets[-1] == 0.0  # the deeper break does
    assert entry < shallow


def test_donchian_min_hold_blocks_an_early_exit():
    # 20 flat bars, one jump to 110 (a new 5-bar high), then a sustained fall.
    closes = [100.0] * 20 + [110.0, 95.0, 90.0, 85.0, 80.0, 75.0]
    bars = make_bars(closes)
    quick = DonchianBreakout(entry=5, exit_window=3).targets(bars)
    patient = DonchianBreakout(entry=5, exit_window=3, min_hold=3).targets(bars)

    entry = quick.index(1.0)
    assert entry == 20  # the jump bar itself
    assert quick[entry + 1] == 0.0  # without a minimum hold it exits at once
    assert patient[entry : entry + 3] == [1.0, 1.0, 1.0]  # held through the fall
    assert patient[entry + 3] == 0.0


def test_a_tighter_exit_channel_trades_more_often():
    bars = make_bars(wavy(600))
    tight = DonchianBreakout(entry=20, exit_window=2).targets(bars)
    wide = DonchianBreakout(entry=20, exit_window=40).targets(bars)
    changes = lambda ts: sum(1 for a, b in zip(ts, ts[1:]) if a != b)  # noqa: E731
    assert changes(tight) > changes(wide)


def test_donchian_long_short_shorts_a_new_low():
    closes = [100.0] * 20 + [100.0 - i for i in range(1, 21)]
    bars = make_bars(closes)
    shorts = DonchianBreakout(entry=10, exit_window=5, mode="long-short").targets(bars)
    longs = DonchianBreakout(entry=10, exit_window=5).targets(bars)
    assert -1.0 in shorts
    assert 1.0 not in longs  # a falling market never breaks a high


def test_donchian_metadata_and_validation():
    assert DonchianBreakout(entry=20, exit_window=10).slug == "donchian20-10"
    assert DonchianBreakout(entry=20, exit_window=10, min_hold=5).slug == "donchian20-10-h5"
    assert DonchianBreakout(mode="long-short").slug == "donchian20-10-ls"
    assert "20-bar high" in DonchianBreakout().describe()
    with pytest.raises(ValueError, match="at least 2"):
        DonchianBreakout(entry=1)
    with pytest.raises(ValueError, match="min_hold"):
        DonchianBreakout(min_hold=-1)
    with pytest.raises(ValueError, match="mode must be one of"):
        DonchianBreakout(mode="both")


# --- RSI reversion ----------------------------------------------------------


def test_rsi_reversion_buys_oversold_and_leaves_on_the_exit_level():
    closes = [100.0] * 15 + [100.0 - 3 * i for i in range(1, 9)] + [80.0, 85.0, 90.0, 95.0, 100.0]
    bars = make_bars(closes)
    strategy = RsiReversion(window=2, oversold=10.0, exit_level=50.0, max_hold=20)
    targets = strategy.targets(bars)
    assert 1.0 in targets
    entry = targets.index(1.0)
    assert targets[entry] == 1.0
    assert targets[-1] == 0.0  # RSI recovered above the exit level
    assert len(targets) == len(bars)


def test_rsi_reversion_time_stop_closes_a_losing_trade():
    # A long, slow decline keeps RSI low, so only `max_hold` can end the trade.
    closes = [100.0] * 15 + [100.0 - 0.5 * i for i in range(1, 60)]
    bars = make_bars(closes)
    targets = RsiReversion(window=3, oversold=20.0, exit_level=85.0, overbought=95.0, max_hold=4).targets(bars)
    entry = targets.index(1.0)
    assert targets[entry : entry + 4] == [1.0] * 4
    assert targets[entry + 4] == 0.0


def test_rsi_reversion_long_short_sells_strength():
    closes = [100.0] * 15 + [100.0 + 3 * i for i in range(1, 12)]
    bars = make_bars(closes)
    both = RsiReversion(window=2, mode="long-short").targets(bars)
    only_long = RsiReversion(window=2).targets(bars)
    assert -1.0 in both
    assert 1.0 not in only_long  # a straight rise is never oversold


def test_rsi_reversion_metadata_and_validation():
    assert RsiReversion(window=2).slug == "rsi2-10-50-h10"
    assert RsiReversion(window=2, mode="long-short").slug == "rsi2-10-50-h10-ls"
    assert RsiReversion().warmup == 3
    assert "RSI(2) < 10" in RsiReversion().describe()
    assert "exit when" in RsiReversion().describe()
    with pytest.raises(ValueError, match="thresholds"):
        RsiReversion(oversold=60.0, exit_level=50.0)
    with pytest.raises(ValueError, match="max_hold"):
        RsiReversion(max_hold=0)
    with pytest.raises(ValueError, match="mode must be one of"):
        RsiReversion(mode="both")
    with pytest.raises(ValueError, match="window"):
        RsiReversion(window=1)


# --- volatility targeting ---------------------------------------------------


def alternating_bars(count: int = 400) -> list:
    """Closes alternating +1% / -1%: a known, constant volatility."""
    closes = [100.0]
    for i in range(1, count):
        closes.append(closes[-1] * (1.01 if i % 2 else 0.99))
    return make_bars(closes)


def test_vol_target_scales_down_when_volatility_exceeds_the_budget():
    bars = alternating_bars()
    realized = 0.01 * (SECONDS_PER_YEAR / 3600) ** 0.5  # 1% per bar, hourly
    sized = ScaledStrategy(SmaTrend(window=5), target_vol=realized, vol_window=50)
    scales = sized.scales(bars)
    # Asked for exactly the realised volatility: hold full exposure.
    assert scales[100] == pytest.approx(1.0, rel=1e-6)

    half = ScaledStrategy(SmaTrend(window=5), target_vol=realized / 2, vol_window=50)
    assert half.scales(bars)[100] == pytest.approx(0.5, rel=1e-6)


def test_vol_target_respects_the_cap_and_keeps_the_sign():
    bars = alternating_bars()
    realized = 0.01 * (SECONDS_PER_YEAR / 3600) ** 0.5
    sized = ScaledStrategy(
        SmaTrendLongShort(window=5), target_vol=realized * 10, vol_window=50, cap=0.7
    )
    scales = sized.scales(bars)
    assert scales[100] == pytest.approx(0.7)
    targets = sized.targets(bars)
    assert all(-0.7 <= t <= 0.7 for t in targets)
    assert any(t < 0 for t in targets)  # the short side survives scaling


def test_vol_target_stays_unscaled_until_it_has_enough_observations():
    bars = alternating_bars(30)
    sized = ScaledStrategy(SmaTrend(window=2), target_vol=0.1, vol_window=100, min_observations=20)
    scales = sized.scales(bars)
    assert scales[:20] == [1.0] * 20
    assert scales[20] < 1.0  # high volatility, low budget -> scaled down


def test_vol_targeted_exposure_is_fractional_where_the_inner_signal_is_full():
    bars = alternating_bars()
    inner = SmaTrend(window=5)
    sized = ScaledStrategy(inner, target_vol=0.05, vol_window=50)
    inner_targets = inner.targets(bars)
    targets = sized.targets(bars)
    for i, (raw, scaled) in enumerate(zip(inner_targets, targets)):
        if raw == 1.0 and i >= 20:
            assert 0.0 < scaled < 1.0


def test_vol_target_wrapper_metadata():
    sized = ScaledStrategy(SmaTrend(window=200), target_vol=0.4)
    assert sized.slug == "vt0.4-sma200"
    assert sized.warmup == 200  # the wrapper adds no warm-up of its own
    assert sized.params["inner"] == {"window": 200}
    assert sized.params["target_vol"] == 0.4
    assert "annualised volatility" in sized.describe()
    with pytest.raises(ValueError, match="target_vol"):
        ScaledStrategy(SmaTrend(), target_vol=0.0)
    with pytest.raises(ValueError, match="floor"):
        ScaledStrategy(SmaTrend(), cap=0.5, floor=0.9)
    with pytest.raises(ValueError, match="vol_window"):
        ScaledStrategy(SmaTrend(), vol_window=1)


def test_vol_target_composites_are_registered():
    for name in ("voltarget-sma", "voltarget-sma-ls", "voltarget-tsmom"):
        assert name in available()
        assert sweep_parameter(name) == "target_vol"
    built = get_strategy("voltarget-sma", window=50, target_vol=0.3)
    assert built.slug == "vt0.3-sma50"
    assert parameters("voltarget-sma") == {
        "window": 200,
        "target_vol": 0.4,
        "vol_window": 168,
        "cap": 1.0,
    }


def test_a_vol_targeted_run_still_reconciles_its_trade_book():
    bars = alternating_bars(600)
    sized = ScaledStrategy(SmaTrend(window=20), target_vol=0.2, vol_window=50)
    result = run_backtest(bars, sized.targets(bars), "1h", Costs(fee_per_side=0.001), label=sized.slug)
    assert result.bookkeeping_error < 1e-9
    assert result.exposure < 1.0  # never fully invested at this budget
