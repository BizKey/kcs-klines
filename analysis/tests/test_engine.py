"""The execution model: timing, costs, trades and the equity curve.

These are the tests that matter most. A backtest can be wrong in only a few
ways, and the two that silently produce spectacular results are (a) letting a
position earn a move that happened before its own signal and (b) forgetting what
trading costs. Both are pinned down here.
"""

from __future__ import annotations

import pytest

from ..engine import Costs, bookkeeping_warning, buy_and_hold, close_fill_final_equity, run_backtest
from ..strategies import SmaTrend
from .conftest import make_bars, ramp


def test_no_costs_and_no_position_leaves_equity_flat():
    bars = make_bars([100, 200, 50, 75])
    result = run_backtest(bars, [0.0] * len(bars), "1h", Costs(0))
    assert result.final_equity == pytest.approx(1.0)
    assert result.trades == []
    assert result.exposure == 0.0


def test_a_position_earns_the_move_of_the_bar_it_was_held_through():
    # Signals: long from the close of bar 0, so the position spans bars 1 and 2.
    bars = make_bars([100, 100, 200], opens=[100, 100, 200])
    result = run_backtest(bars, [1.0, 1.0, 1.0], "1h", Costs(0))
    # open[1] -> open[2] is 100 -> 200, and close[2] == open[2].
    assert result.final_equity == pytest.approx(2.0)


def test_a_position_cannot_earn_the_move_that_produced_its_signal():
    """The look-ahead guard: the doubling happens *before* the signal exists."""
    # Bar 1 doubles (open 100 -> close 200); the signal only exists on its close.
    bars = make_bars([100, 200, 200, 200], opens=[100, 100, 200, 200])
    result = run_backtest(bars, [0.0, 1.0, 1.0, 1.0], "1h", Costs(0))
    # Entering at the open of bar 2 (already 200) earns nothing from the doubling.
    assert result.final_equity == pytest.approx(1.0)
    # Crediting the signal to its own bar instead would report a free 2x.
    look_ahead = (200 / 100) * (200 / 200) * (200 / 200)
    assert look_ahead == pytest.approx(2.0)


def test_one_round_trip_pays_two_sides_of_commission():
    bars = make_bars([100, 100, 100, 100], opens=[100, 100, 100, 100])
    result = run_backtest(bars, [1.0, 1.0, 0.0, 0.0], "1h", Costs(fee_per_side=0.001))
    # Open at the second bar, close at the fourth: price flat, so only fees move.
    assert result.final_equity == pytest.approx((1 - 0.001) ** 2, rel=1e-12)
    assert len(result.closed_trades) == 1
    assert result.closed_trades[0].net_return == pytest.approx((1 - 0.001) ** 2 - 1, rel=1e-12)


def test_a_trade_return_includes_both_commission_sides():
    bars = make_bars([100, 100, 110, 110], opens=[100, 100, 100, 110])
    result = run_backtest(bars, [1.0, 1.0, 0.0, 0.0], "1h", Costs(fee_per_side=0.001))
    trade = result.closed_trades[0]
    assert trade.gross_return == pytest.approx(0.10, rel=1e-12)  # 100 -> 110
    assert trade.net_return == pytest.approx(1.10 * 0.999**2 - 1, rel=1e-12)


def test_slippage_adds_to_the_per_side_cost():
    bars = make_bars([100, 100, 110, 110], opens=[100, 100, 100, 110])
    without = run_backtest(bars, [1.0, 1.0, 0.0, 0.0], "1h", Costs(fee_per_side=0.0))
    with_slippage = run_backtest(
        bars, [1.0, 1.0, 0.0, 0.0], "1h", Costs(fee_per_side=0.0, slippage_per_side=0.002)
    )
    assert with_slippage.final_equity < without.final_equity
    assert with_slippage.final_equity == pytest.approx(without.final_equity * 0.998**2, rel=1e-12)


def test_a_short_position_profits_when_the_price_falls():
    bars = make_bars([100, 100, 50, 50], opens=[100, 100, 50, 50])
    result = run_backtest(bars, [-1.0] * 4, "1h", Costs(0))
    # Short from 100, price halves: -(-50%) = +50%.
    assert result.final_equity == pytest.approx(1.5)
    assert result.closed_trades == []  # still open at the end
    assert result.trades[-1].side == "short"


def test_flipping_from_long_to_short_pays_two_sides():
    bars = make_bars([100, 100, 100, 100], opens=[100, 100, 100, 100])
    result = run_backtest(bars, [1.0, -1.0, -1.0, -1.0], "1h", Costs(fee_per_side=0.001))
    # One side to go long, then two sides for the flip.
    assert result.final_equity == pytest.approx((1 - 0.001) ** 3, rel=1e-12)
    assert [t.side for t in result.trades] == ["long", "short"]
    assert result.bookkeeping_error < 1e-12


def test_exposure_reports_the_average_absolute_position():
    bars = make_bars([100] * 5, opens=[100] * 5)
    result = run_backtest(bars, [1.0, 1.0, 0.0, 0.0, 0.0], "1h", Costs(0))
    # Positions per bar: 0, 1, 1, 0, 0.
    assert result.exposure == pytest.approx(2 / 5)


def test_fractional_exposure_is_allowed():
    bars = make_bars([100, 100, 200, 200], opens=[100, 100, 100, 200])
    half = run_backtest(bars, [0.5] * 4, "1h", Costs(0))
    full = run_backtest(bars, [1.0] * 4, "1h", Costs(0))
    assert full.final_equity - 1 == pytest.approx(2 * (half.final_equity - 1), rel=1e-12)


def test_targets_and_bars_must_line_up():
    bars = make_bars(ramp(10))
    with pytest.raises(ValueError, match="targets for"):
        run_backtest(bars, [0.0] * 3, "1h")


def test_exposure_outside_the_allowed_range_is_rejected():
    bars = make_bars(ramp(10))
    with pytest.raises(ValueError, match="outside"):
        run_backtest(bars, [2.0] * 10, "1h")


def test_an_empty_series_is_rejected():
    with pytest.raises(ValueError):
        run_backtest([], [], "1h")


def test_costs_reject_negative_and_absurd_values():
    with pytest.raises(ValueError):
        Costs(fee_per_side=-0.001)
    with pytest.raises(ValueError):
        Costs(fee_per_side=1.5)


def test_costs_describe_themselves():
    assert "fee 0.100%/side" in str(Costs(fee_per_side=0.001))
    assert "slippage" in str(Costs(fee_per_side=0.001, slippage_per_side=0.0005))


def test_the_trade_book_reproduces_the_equity_curve():
    """The invariant that catches execution drift: compounding trades = curve."""
    bars = make_bars(
        [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3, 18, 2, 19, 1, 20],
        opens=[10, 10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3, 18, 2, 19, 1],
    )
    targets = SmaTrend(window=3).targets(bars)
    result = run_backtest(bars, targets, "1h", Costs(fee_per_side=0.002))
    assert result.trades
    assert result.bookkeeping_error < 1e-12
    assert result.bookkeeping == pytest.approx(result.final_equity, rel=1e-12)


def test_the_invariant_also_holds_for_a_long_short_strategy():
    bars = make_bars(
        [10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3, 18, 2, 19, 1, 20],
        opens=[10, 10, 11, 9, 12, 8, 13, 7, 14, 6, 15, 5, 16, 4, 17, 3, 18, 2, 19, 1],
    )
    from ..strategies import SmaTrendLongShort

    targets = SmaTrendLongShort(window=3).targets(bars)
    result = run_backtest(bars, targets, "1h", Costs(fee_per_side=0.002))
    assert result.bookkeeping_error < 1e-12


def test_a_full_short_can_be_wiped_out_and_the_run_stops_cleanly():
    """A short equal to the whole account dies when the price doubles."""
    bars = make_bars([100, 100, 200, 200, 300], opens=[100, 100, 200, 200, 200])
    result = run_backtest(bars, [-1.0] * 5, "1h", Costs(0))
    assert result.final_equity == pytest.approx(0.0)
    assert result.warnings and "wiped out" in result.warnings[0]
    assert result.trades[-1].net_return == pytest.approx(-1.0)


def test_equity_cannot_go_negative_after_a_wipe_out():
    bars = make_bars([100, 100, 200, 400, 800], opens=[100, 100, 200, 200, 200])
    result = run_backtest(bars, [-1.0] * 5, "1h", Costs(0))
    assert all(value >= 0 for value in result.equity)
    # Nothing is held after the wipe-out, however the strategy shouts.
    assert result.positions[3] == 0.0
    assert result.positions[-1] == 0.0
    assert not result.open_position


def test_bookkeeping_warning_is_silent_on_a_healthy_run():
    bars = make_bars(ramp(30))
    result = run_backtest(bars, SmaTrend(window=3).targets(bars), "1h", Costs(0.001))
    assert bookkeeping_warning(result) is None


def test_bookkeeping_warning_fires_when_the_trade_book_drifts():
    """The guard that would have caught the look-ahead bug during development."""
    bars = make_bars(ramp(30))
    result = run_backtest(bars, SmaTrend(window=3).targets(bars), "1h", Costs(0.001))
    result.trades[0].net_return = 5.0  # an impossible trade return
    warning = bookkeeping_warning(result)
    assert warning is not None
    assert "disagree" in warning


def test_strict_mode_passes_on_a_healthy_run():
    bars = make_bars(ramp(30))
    result = run_backtest(bars, SmaTrend(window=3).targets(bars), "1h", Costs(0.001), strict=True)
    assert result.warnings == []


def test_gross_equity_ignores_costs_entirely():
    bars = make_bars([100, 100, 200, 200], opens=[100, 100, 100, 200])
    free = run_backtest(bars, [1.0] * 4, "1h", Costs(0))
    paid = run_backtest(bars, [1.0] * 4, "1h", Costs(fee_per_side=0.01))
    assert free.gross_equity == pytest.approx(2.0)
    assert paid.gross_equity == pytest.approx(2.0)
    assert paid.final_equity < free.final_equity


def test_buy_and_hold_pays_one_entry_and_one_exit():
    bars = make_bars([100, 120, 150], opens=[100, 100, 120])
    benchmark = buy_and_hold(bars, Costs(fee_per_side=0.001), "1h")
    assert benchmark.gross_equity == pytest.approx(1.5)  # 100 -> 150
    assert benchmark.performance.final_equity == pytest.approx(1.5 * 0.999**2, rel=1e-12)
    assert benchmark.label == "buy & hold"


def test_buy_and_hold_rejects_an_empty_series():
    with pytest.raises(ValueError):
        buy_and_hold([])


def test_close_fill_needs_matching_lengths():
    with pytest.raises(ValueError):
        close_fill_final_equity(make_bars(ramp(5)), [0.0] * 2)


def test_an_open_position_is_marked_to_market_at_the_last_close():
    bars = make_bars([100, 100, 150], opens=[100, 100, 100])
    result = run_backtest(bars, [1.0] * 3, "1h", Costs(0))
    assert result.open_position
    assert result.final_equity == pytest.approx(1.5)
    assert result.trades[-1].open_at_end


def test_result_summary_is_json_friendly():
    bars = make_bars(ramp(20))
    result = run_backtest(bars, SmaTrend(window=3).targets(bars), "1h", Costs(0.001), label="sma3")
    data = result.as_dict()
    assert data["label"] == "sma3"
    assert data["strategy"]["final_equity"] == pytest.approx(result.final_equity)
    assert data["buy_and_hold"]["final_equity"] > 0
    assert "equity" not in data
    assert "equity" in result.as_dict(include_curves=True)
