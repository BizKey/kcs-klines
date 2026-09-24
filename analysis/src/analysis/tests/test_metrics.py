"""Indicators and performance statistics."""

from __future__ import annotations

import pytest

from ..metrics import cagr, ema, macd, max_drawdown, pct, performance, rsi, sma


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


# --- exponential averages and MACD ------------------------------------------


def test_ema_is_seeded_with_the_simple_mean_and_then_recurses():
    # window 3 -> alpha = 0.5, seed = mean(1, 2, 3) = 2.
    assert ema([1, 2, 3, 4, 5], 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_of_a_constant_series_is_that_constant():
    assert ema([7.0] * 6, 3) == [None, None, 7.0, 7.0, 7.0, 7.0]


def test_ema_skips_a_warmup_prefix_of_nones():
    # The average starts where the values start, not where the list starts.
    assert ema([None, None, 1, 2, 3, 4], 2) == [None, None, None, 1.5, 2.5, 3.5]


def test_ema_is_all_none_when_there_is_not_enough_data():
    assert ema([1, 2], 5) == [None, None]
    assert ema([None, None, None], 2) == [None, None, None]
    assert ema([], 3) == []


def test_ema_rejects_a_non_positive_window():
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)


def test_macd_line_is_the_difference_of_the_two_averages():
    values = [float(i) for i in range(60)]
    line, signal, histogram = macd(values, fast=3, slow=6, signal=4)
    fast_ema, slow_ema = ema(values, 3), ema(values, 6)
    for i in range(len(values)):
        if fast_ema[i] is None or slow_ema[i] is None:
            assert line[i] is None
        else:
            assert line[i] == pytest.approx(fast_ema[i] - slow_ema[i])


def test_macd_histogram_is_the_gap_to_the_signal_line():
    line, signal, histogram = macd([float(i) for i in range(40)], 3, 6, 4)
    for i, (m, s, h) in enumerate(zip(line, signal, histogram)):
        if m is None or s is None:
            assert h is None
        else:
            assert h == pytest.approx(m - s)


def test_macd_warmup_indices_match_the_documented_ones():
    values = [float(i) for i in range(60)]
    line, signal, _ = macd(values, fast=12, slow=26, signal=9)
    assert all(v is None for v in line[:25])
    assert line[25] is not None  # slow - 1
    assert all(v is None for v in signal[:33])
    assert signal[33] is not None  # slow + signal - 2


def test_macd_rejects_impossible_windows():
    values = [float(i) for i in range(60)]
    with pytest.raises(ValueError, match="shorter than slow"):
        macd(values, fast=26, slow=12)
    with pytest.raises(ValueError, match="at least 2"):
        macd(values, fast=1, slow=26)
    with pytest.raises(ValueError):
        macd(values, fast=12, slow=26, signal=1)


# --- RSI --------------------------------------------------------------------


def test_rsi_of_a_hand_computed_series():
    # window 2: changes +1, +1 -> all gains; then -1, -1 -> Wilder smoothing.
    assert rsi([1, 2, 3, 2, 1], 2) == [None, None, 100.0, 50.0, 25.0]


def test_rsi_is_a_hundred_when_every_bar_gains_and_zero_when_every_bar_loses():
    assert rsi([float(i) for i in range(10)], 3)[-1] == 100.0
    assert rsi([float(-i) for i in range(10)], 3)[-1] == 0.0


def test_rsi_of_a_flat_market_is_the_neutral_fifty():
    assert rsi([5.0] * 10, 3)[-1] == 50.0


def test_rsi_stays_inside_its_bounds():
    values = [100 + (i % 7) * 3 - (i % 3) * 5 for i in range(200)]
    for value in rsi(values, 14):
        if value is not None:
            assert 0.0 <= value <= 100.0


def test_rsi_is_none_until_the_window_is_full():
    assert rsi([1, 2, 3], 5) == [None, None, None]
    assert rsi([1, 2, 3, 4, 5, 6], 5)[:5] == [None] * 5
    assert rsi([1, 2, 3, 4, 5, 6], 5)[5] is not None


def test_rsi_rejects_a_window_below_two():
    with pytest.raises(ValueError):
        rsi([1, 2, 3], 1)
