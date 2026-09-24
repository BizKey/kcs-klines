"""Regression test against the real archive.

These numbers were verified by hand against an independent implementation of the
same execution model before this package was split up, so they pin the toolkit's
behaviour: any change to timing, cost handling or trade booking shows up here.

The series is BTC-USDT 1h as collected by `kcs-klines backfill`. Tests are
skipped when the archive is not present (fresh clone, CI without data).
"""

from __future__ import annotations

import pytest

from .. import data
from ..engine import Costs, bookkeeping_warning
from ..metrics import performance
from ..strategies import SmaTrend, SmaTrendLongShort
from .conftest import make_bars

FEE = 0.001

# The reference window. The archive is alive — `kcs-klines backfill` keeps
# appending bars — so the regression pins a window rather than "whatever is on
# disk today". New bars are fine; rewriting these ones is not.
BASELINE_FIRST = 1_507_089_600  # 2017-10-04 04:00 UTC
BASELINE_LAST = 1_790_269_200  # 2026-09-24 17:00 UTC
EXPECTED_BARS = 78_264
EXPECTED_FINAL_EQUITY = 1.8888675286753447
EXPECTED_GROSS_EQUITY = 23.90880508
EXPECTED_CLOSED_TRADES = 1268


@pytest.fixture(scope="module")
def baseline_window(btc_hourly):
    """Exactly the bars the reference numbers were measured on."""
    window = data.window_of(btc_hourly, BASELINE_FIRST, BASELINE_LAST)
    assert len(window) == EXPECTED_BARS, "the baseline window changed size"
    return window


def test_the_archive_still_contains_the_baseline_window(btc_hourly, baseline_window):
    assert baseline_window[0].time == BASELINE_FIRST
    assert baseline_window[-1].time == BASELINE_LAST
    # The archive may have grown since, but it must not have been rewritten.
    assert len(btc_hourly) >= EXPECTED_BARS
    assert btc_hourly[0].time <= BASELINE_FIRST
    assert btc_hourly[-1].time >= BASELINE_LAST


def test_baseline_window_shape_is_unchanged(baseline_window):
    report = data.data_quality(baseline_window, "1h")
    assert report.bars == EXPECTED_BARS
    assert report.gaps == 62
    assert report.missing_bars == 398
    assert report.largest_gap_slots == 68
    assert report.bars_violating_ohlc == 1


@pytest.fixture(scope="module")
def sma200_result(baseline_window):
    strategy = SmaTrend(window=200)
    return strategy, run(baseline_window, strategy)


def run(bars, strategy, fee: float = FEE):
    from ..engine import run_backtest

    return run_backtest(
        bars,
        strategy.targets(bars),
        "1h",
        Costs(fee_per_side=fee),
        label=strategy.slug,
    )


def test_headline_numbers_are_unchanged(sma200_result):
    _, result = sma200_result
    assert result.final_equity == pytest.approx(EXPECTED_FINAL_EQUITY, rel=1e-9)
    assert result.gross_equity == pytest.approx(EXPECTED_GROSS_EQUITY, rel=1e-6)
    assert len(result.closed_trades) == EXPECTED_CLOSED_TRADES
    assert result.performance.total_return == pytest.approx(0.8888675287, rel=1e-6)
    assert result.performance.cagr == pytest.approx(0.073779, rel=1e-4)
    assert result.performance.ann_vol == pytest.approx(0.529536, rel=1e-4)
    assert result.performance.sharpe == pytest.approx(0.135069, rel=1e-4)
    assert result.performance.max_dd == pytest.approx(-0.792662, rel=1e-4)
    assert result.exposure == pytest.approx(0.521581, rel=1e-5)
    assert result.win_rate == pytest.approx(0.124606, rel=1e-4)
    assert result.avg_trade == pytest.approx(0.001879, rel=1e-3)
    assert result.median_trade == pytest.approx(-0.005835, rel=1e-3)
    assert result.best_trade == pytest.approx(1.479062, rel=1e-4)
    assert result.worst_trade == pytest.approx(-0.062070, rel=1e-4)
    assert result.avg_bars_held == pytest.approx(32, abs=0.5)


def test_benchmark_numbers_are_unchanged(sma200_result):
    _, result = sma200_result
    bench = result.benchmark
    assert bench.gross_equity == pytest.approx(19.5295101, rel=1e-6)
    assert bench.performance.final_equity == pytest.approx(19.4904706, rel=1e-6)
    assert bench.performance.ann_vol == pytest.approx(0.886450, rel=1e-4)
    assert bench.performance.sharpe == pytest.approx(0.379380, rel=1e-3)
    assert bench.performance.max_dd == pytest.approx(-0.838409, rel=1e-4)


def test_the_trade_book_still_reproduces_the_equity_curve(sma200_result):
    """The invariant that a look-ahead bug breaks while the curve looks plausible."""
    _, result = sma200_result
    assert result.bookkeeping_error < 1e-9
    assert bookkeeping_warning(result) is None


def test_commission_still_decides_the_outcome(sma200_result, baseline_window):
    """The headline finding: gross beats buy & hold, net loses badly to it."""
    _, result = sma200_result
    assert result.gross_equity > result.benchmark.gross_equity  # signal has value
    assert result.final_equity < result.benchmark.performance.final_equity  # costs eat it
    free = run(baseline_window, SmaTrend(window=200), fee=0.0)
    assert free.final_equity == pytest.approx(EXPECTED_GROSS_EQUITY, rel=1e-6)
    assert free.trades[0].net_return == pytest.approx(free.trades[0].gross_return, rel=1e-12)


def test_fill_timing_sensitivity_is_still_measured(sma200_result):
    _, result = sma200_result
    # Filling at the signal bar's own close is a different (and much worse here)
    # world; the two must not collapse into each other.
    assert result.close_fill_equity == pytest.approx(0.7818, rel=1e-3)
    assert result.close_fill_equity != pytest.approx(result.final_equity, rel=0.5)


def test_default_costs_are_the_kucoin_spot_taker_rate():
    assert Costs().fee_per_side == 0.001


def test_long_short_variant_turns_over_more_than_long_only(baseline_window):
    long_only = run(baseline_window, SmaTrend(window=200))
    long_short = run(baseline_window, SmaTrendLongShort(window=200))
    assert len(long_short.trades) > len(long_only.trades)
    assert long_short.bookkeeping_error < 1e-9


def test_a_window_sweep_is_cheap_and_stable(baseline_window):
    """Every window in the sweep must produce a self-consistent book."""
    for window in (50, 100, 200):
        result = run(baseline_window, SmaTrend(window=window))
        assert result.bookkeeping_error < 1e-9
        assert result.performance.years == pytest.approx(8.934, rel=1e-2)


def test_performance_of_a_synthetic_series_is_reproducible():
    """A tiny fixture keeps a no-data environment from losing all coverage."""
    bars = make_bars([100, 110, 90, 120, 80, 130], opens=[100] * 6)
    strategy = SmaTrend(window=2)
    result = run(bars, strategy)
    assert result.bookkeeping_error < 1e-9
    assert performance([1.0, 1.0], 8760).total_return == 0.0
