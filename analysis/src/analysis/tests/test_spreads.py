"""The spread estimator: sane numbers, no peeking, and a fallback when it cannot speak."""

from __future__ import annotations

from pathlib import Path

import pytest

from .. import basket, spreads
from .conftest import START, STEP, make_bars, ramp


def _bars(high_low: float, count: int = 300, *, start: int = START, spacing: int = STEP):
    """A flat price with a high-low range of `high_low` percent of it, bar after bar.

    With no net move the two-day range is exactly one bar's range, so the estimator reads
    the range itself as the spread — which is the cleanest possible probe, and also the
    reason the method breaks down when the price travels further within two bars than the
    ranges can explain (a strongly trending pair reads as zero, not as a wide spread).
    """
    bars = []
    for index in range(count):
        close = 100.0
        bar = make_bars([close])[0]
        bars.append(
            type(bar)(
                time=start + index * spacing,
                open=bar.open,
                high=close + high_low / 2,
                low=close - high_low / 2,
                close=close,
                volume=bar.volume,
            )
        )
    return bars


def test_a_wider_high_low_range_reads_as_a_wider_spread():
    calm = spreads.corwin_schultz(_bars(0.05))
    wide = spreads.corwin_schultz(_bars(2.0))
    assert calm is not None and wide is not None
    assert 0.0 < calm < wide
    assert wide < 1.0                      # a fraction, not a percentage


def test_the_estimate_is_a_fraction_and_never_negative():
    for high_low in (0.001, 0.01, 0.5, 5.0, 50.0):
        value = spreads.corwin_schultz(_bars(high_low))
        if value is not None:
            assert 0.0 <= value < 1.0


def test_too_short_a_series_has_no_opinion():
    assert spreads.corwin_schultz(make_bars(ramp(2))) is None
    assert spreads.corwin_schultz([]) is None


def test_the_estimate_never_reads_a_bar_at_or_after_the_cutoff():
    bars = _bars(0.5)
    cut = bars[120].time
    before = spreads.corwin_schultz(bars, before=cut)
    rewritten = list(bars)
    for index in range(120, len(rewritten)):
        bar = rewritten[index]
        rewritten[index] = type(bar)(
            time=bar.time, open=bar.open, high=bar.high * 3, low=bar.low,
            close=bar.close, volume=bar.volume,
        )
    assert spreads.corwin_schultz(rewritten, before=cut) == before


def test_the_window_keeps_the_latest_bars_only():
    bars = _bars(0.5)
    short = spreads.corwin_schultz(bars, window=10)
    assert short == spreads.corwin_schultz(bars[-10:])


def test_spread_by_symbol_measures_what_it_can(tmp_path: Path):
    series = {
        "CALM-USDT": _bars(0.02),
        "WILD-USDT": _bars(1.5),
        "TINY-USDT": make_bars(ramp(2)),
    }
    measured = spreads.spread_by_symbol(series)
    assert "TINY-USDT" not in measured
    assert measured["CALM-USDT"] < measured["WILD-USDT"]


def test_the_basket_can_charge_each_leg_its_own_spread(tmp_path: Path, capsys):
    root = tmp_path / "spot"
    from .conftest import write_archive

    for symbol, high_low in (("UP-USDT", 0.02), ("MID-USDT", 1.5)):
        bars = _bars(high_low)
        write_archive(root, symbol, "1d", bars)
        write_archive(root, symbol, "1h", bars)
    code = basket.main([
        "--data-dir", str(root), "--timeframe", "1d",
        "--symbols", "UP-USDT,MID-USDT", "--strategy", "sma", "--param", "window=20",
        "--spread-model", "corwin-schultz", "--spread-window", "100",
        "--no-artifacts",
    ])
    printed = capsys.readouterr().out
    assert code == 0
    assert "cost model: fee" in printed and "corwin-schultz half-spreads" in printed
    assert "UP-USDT" in printed and "MID-USDT" in printed
    # the calm leg pays less per side than the wild one
    rows = {}
    for line in printed.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].endswith("-USDT") and parts[1].endswith("bp"):
            rows[parts[0]] = float(parts[1][:-2])
    assert rows["UP-USDT"] < rows["MID-USDT"]


def test_a_leg_without_a_spread_estimate_falls_back_to_the_flat_fee(tmp_path: Path, capsys):
    root = tmp_path / "spot"
    from .conftest import write_archive

    write_archive(root, "ONLY-USDT", "1d", _bars(0.5))
    code = basket.main([
        "--data-dir", str(root), "--timeframe", "1d", "--symbols", "ONLY-USDT",
        "--strategy", "sma", "--param", "window=20", "--spread-model", "corwin-schultz",
        "--spread-timeframe", "1h", "--no-artifacts",
    ])
    printed = capsys.readouterr().out
    assert code == 0
    assert "no spread estimate" in printed
