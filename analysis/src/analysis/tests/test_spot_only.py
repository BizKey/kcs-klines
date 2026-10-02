"""Spot means long or flat: no negative exposure, ever, unless you ask for it.

A spot balance cannot go below zero, so a short exposure is not a risk preference — it is an
instrument the account does not have. The registry names long/short variants with an `-ls`
suffix, and these tests make that a rule rather than a naming convention: the CLI refuses to
run a strategy that shorts, and every strategy *without* that suffix is checked to be long-only.
"""

from __future__ import annotations

import pytest

from .. import basket, engine
from ..strategies import available, get_strategy
from .conftest import make_bars, ramp, write_archive


def _both_ways() -> list:
    """Bars that rise, then fall hard: any shorting strategy will take the short side."""
    closes = ramp(120, start=100.0, step=2.0) + ramp(120, start=340.0, step=-2.0)
    return make_bars(closes)


def test_every_strategy_without_the_ls_suffix_is_long_only():
    bars = _both_ways()
    offenders = []
    for name in available():
        if name.endswith("-ls"):
            continue
        try:
            targets = get_strategy(name).targets(bars)
        except TypeError:
            continue                     # a factory that needs explicit parameters
        if min(targets, default=0.0) < 0:
            offenders.append(name)
    assert offenders == [], f"these strategies short and are not named -ls: {offenders}"


def test_the_ls_suffix_really_does_short():
    """The flip side: the convention is only useful if it means something."""
    bars = _both_ways()
    assert min(get_strategy("sma-ls", window=20).targets(bars)) < 0


def test_the_engine_refuses_a_short_when_it_is_told_this_is_spot():
    bars = _both_ways()
    targets = get_strategy("sma-ls", window=20).targets(bars)
    run = engine.run_backtest
    result = run(bars, targets, "1h", engine.Costs(0))
    assert result.performance.final_equity != 1.0        # it runs, and it does go short
    with pytest.raises(ValueError, match="spot account"):
        run(bars, targets, "1h", engine.Costs(0), spot_only=True)


def test_a_long_only_strategy_is_unaffected_by_the_guard():
    bars = _both_ways()
    targets = get_strategy("sma", window=20).targets(bars)
    with_guard = engine.run_backtest(bars, targets, "1h", engine.Costs(0), spot_only=True)
    without = engine.run_backtest(bars, targets, "1h", engine.Costs(0))
    assert with_guard.performance.final_equity == pytest.approx(
        without.performance.final_equity
    )


def test_the_backtest_cli_refuses_a_short_and_says_how_to_override(tmp_path, capsys):
    from ..run_backtest import main as backtest_main

    root = tmp_path / "spot"
    write_archive(root, "LONG-USDT", "1h", _both_ways())
    argv = [
        "--data-dir", str(root), "--symbol", "LONG-USDT", "--timeframe", "1h",
        "--strategy", "sma-ls", "--param", "window=20", "--no-artifacts",
    ]
    with pytest.raises(SystemExit) as refused:
        backtest_main(argv)
    assert "spot account" in str(refused.value)
    assert "--allow-short" in str(refused.value)

    assert backtest_main(argv + ["--allow-short"]) == 0
    capsys.readouterr()


def test_the_basket_cli_refuses_a_short_leg(tmp_path, capsys):
    root = tmp_path / "spot"
    write_archive(root, "LONG-USDT", "1h", _both_ways())
    write_archive(root, "OTHER-USDT", "1h", _both_ways())
    argv = [
        "--data-dir", str(root), "--timeframe", "1h",
        "--symbols", "LONG-USDT,OTHER-USDT", "--strategy", "sma-ls",
        "--param", "window=20", "--no-artifacts",
    ]
    with pytest.raises(SystemExit) as refused:
        basket.main(argv)
    assert "spot account" in str(refused.value)
    assert basket.main(argv + ["--allow-short"]) == 0
    capsys.readouterr()
