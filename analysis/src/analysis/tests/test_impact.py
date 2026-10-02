"""Market impact: the square root law, and the two things it must not get wrong."""

from __future__ import annotations

import math
from pathlib import Path

from .. import basket, impact
from .conftest import START, STEP, make_bars, wavy, write_archive


def test_doubling_the_order_raises_the_cost_by_the_square_root():
    small = impact.impact_fraction(10_000, 1_000_000, 0.04)
    big = impact.impact_fraction(40_000, 1_000_000, 0.04)
    assert small is not None and big is not None
    assert big == small * 2                      # 4x the order, sqrt(4) = 2x the cost


def test_a_thinner_pair_costs_more_at_the_same_size():
    thin = impact.impact_fraction(10_000, 100_000, 0.04)
    thick = impact.impact_fraction(10_000, 100_000_000, 0.04)
    assert thin > thick > 0


def test_a_calmer_pair_costs_less_at_the_same_participation():
    calm = impact.impact_fraction(10_000, 1_000_000, 0.01)
    wild = impact.impact_fraction(10_000, 1_000_000, 0.08)
    assert calm < wild


def test_an_unmeasurable_pair_has_no_opinion():
    assert impact.impact_fraction(1_000, 0.0, 0.05) is None
    assert impact.participation(1_000, 0.0) is None
    assert impact.impact_fraction(1_000, 1e6, 0.0) == 0.0


def test_the_coefficient_scales_the_whole_thing_linearly():
    mild = impact.impact_fraction(10_000, 1e6, 0.05, coefficient=0.1)
    harsh = impact.impact_fraction(10_000, 1e6, 0.05, coefficient=0.5)
    assert harsh == mild * 5


def test_leg_stats_are_measured_only_before_the_cutoff():
    bars = make_bars(wavy(400))
    times = [bar.time for bar in bars]
    closes = [bar.close for bar in bars]
    turnover = [1e6] * len(bars)
    cut = times[200]
    before = impact.leg_market_stats(times, closes, turnover, before=cut)
    assert before is not None
    # a much wilder second half must not reach the estimate
    wilder = list(closes)
    for index in range(200, len(wilder)):
        wilder[index] = wilder[index] * (1.5 if index % 2 else 0.6)
    after = impact.leg_market_stats(times, wilder, turnover, before=cut)
    assert after is not None and math.isclose(before[1], after[1])


def test_too_short_a_history_has_no_market_stats():
    times = [START + i * STEP for i in range(20)]
    assert impact.leg_market_stats(times, [100.0] * 20, [1e6] * 20) is None


def test_the_basket_charges_impact_and_warns_when_the_order_is_days_of_volume(
    tmp_path: Path, capsys
):
    root = tmp_path / "spot"
    for symbol, volume in (("THICK-USDT", 1e9), ("THIN-USDT", 1e4)):
        write_archive(root, symbol, "1h", make_bars(wavy(400)))
        # `write_archive`'s turnover column is what the market stats read
        write_archive(
            root, symbol, "1h", make_bars(wavy(400)),
            turnovers=[volume] * 400, name="all.parquet",
        )
    code = basket.main([
        "--data-dir", str(root), "--timeframe", "1h", "--symbols", "THICK-USDT,THIN-USDT",
        "--strategy", "sma", "--param", "window=20", "--capital", "100000",
        "--no-artifacts",
    ])
    printed = capsys.readouterr().out
    assert code == 0
    assert "impact at $100,000 of capital" in printed
    rows = {}
    for line in printed.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].endswith("-USDT") and parts[1].endswith("bp"):
            rows[parts[0]] = float(parts[3][:-2])       # over the flat fee
    assert rows["THIN-USDT"] > rows["THICK-USDT"]
    assert "days of the pair's volume" in printed
