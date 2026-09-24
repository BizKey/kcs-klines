"""Indicators and performance statistics."""

from __future__ import annotations

import pytest

from ..metrics import cagr, max_drawdown, pct, performance, sma


def test_sma_is_none_until_the_window_is_full():
    values = sma([1, 2, 3, 4, 5], 3)
    assert values[:2] == [None, None]
    assert values[2:] == [2.0, 3.0, 4.0]


def test_sma_includes_the_current_value():
    # A window of 1 is the value itself, which is what "close > SMA(1)" implies.
    assert sma([10, 20, 30], 1) == [10.0, 20.0, 30.0]


def test_sma_of_a_window_longer_than_the_series_is_all_none():
    assert sma([1, 2], 5) == [None, None]


def test_sma_rejects_a_non_positive_window():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


def test_sma_matches_a_hand_computed_average():
    window = sma([2, 4, 6, 8], 2)
    assert window == [None, 3.0, 5.0, 7.0]


def test_max_drawdown_finds_the_peak_and_the_trough():
    drawdown, peak, trough = max_drawdown([1.0, 2.0, 0.5, 2.5, 1.25])
    assert drawdown == pytest.approx(-0.75)
    assert (peak, trough) == (1, 2)


def test_max_drawdown_of_a_rising_curve_is_zero():
    assert max_drawdown([1.0, 1.5, 2.0]) == (0.0, 0, 0)


def test_max_drawdown_rejects_an_empty_curve():
    with pytest.raises(ValueError):
        max_drawdown([])


def test_performance_of_a_flat_curve_is_all_zero():
    perf = performance([1.0] * 10, bars_per_year=8760)
    assert perf.total_return == 0.0
    assert perf.ann_vol == 0.0
    assert perf.sharpe == 0.0
    assert perf.max_dd == 0.0


def test_performance_reports_the_annualised_growth_rate():
    # Doubling over one year of hourly bars: 8761 points, 8760 of them returns.
    curve = [1.0 * (2 ** (i / 8760)) for i in range(8761)]
    perf = performance(curve, bars_per_year=8760)
    assert perf.years == pytest.approx(8761 / 8760, rel=1e-12)
    assert perf.cagr == pytest.approx(1.0, rel=1e-3)
    assert perf.total_return == pytest.approx(1.0, rel=1e-9)


def test_performance_overrides_the_last_point_for_an_open_position():
    curve = [1.0, 1.0, 1.0]
    perf = performance(curve, bars_per_year=8760, final_equity=1.5)
    assert perf.final_equity == 1.5
    assert perf.total_return == pytest.approx(0.5)


def test_performance_maps_drawdown_indices_to_timestamps():
    curve = [1.0, 2.0, 1.0]
    stamps = [10, 20, 30]
    perf = performance(curve, bars_per_year=8760, timestamps=stamps)
    assert (perf.max_dd_start, perf.max_dd_end) == (20, 30)


def test_cagr_of_a_losing_curve_is_negative():
    assert cagr(0.5, years=1.0) == pytest.approx(-0.5)


def test_cagr_of_a_non_positive_final_is_zero():
    assert cagr(0.0, years=1.0) == 0.0


def test_pct_formats_two_decimals():
    assert pct(0.123456) == "12.35%"
    assert pct(-0.05) == "-5.00%"
