"""Cross-sectional momentum: ranking, weights, turnover and the benchmark."""

from __future__ import annotations

from pathlib import Path

import pytest

from .. import data, portfolio
from ..engine import Costs
from .conftest import make_bars, write_archive

STEP = 86400  # daily bars
START = 1_507_161_600  # a week after the epoch, so the rebalance grid lands on bars


def series(returns: list[float]) -> tuple[list[int], list[float]]:
    """Times and closes for a list of per-bar returns."""
    closes = [100.0]
    for value in returns:
        closes.append(closes[-1] * (1.0 + value))
    times = [START + i * STEP for i in range(len(closes))]
    return times, closes


def daily_dates(count: int = 12, first_day: int = 60) -> list[int]:
    """Rebalance dates that start after the lookback window, so momentum exists."""
    return [START + (first_day + i * 30) * STEP for i in range(count)]


def panel_fixture() -> tuple[list[portfolio.Panel], list[int]]:
    """Three symbols: a riser, a faller, and a flat one, on a 30-bar calendar."""
    dates = daily_dates()
    panels = [
        portfolio.build_panel(*series([0.02] * 500), "UP-USDT", dates, 30 * STEP),
        portfolio.build_panel(*series([-0.02] * 500), "DOWN-USDT", dates, 30 * STEP),
        portfolio.build_panel(*series([0.0] * 500), "FLAT-USDT", dates, 30 * STEP),
    ]
    return panels, dates


# --- helpers ----------------------------------------------------------------


def test_close_at_takes_the_last_bar_at_or_before_the_date():
    times = [100, 200, 300]
    closes = [1.0, 2.0, 3.0]
    assert portfolio.close_at(times, closes, 250) == 2.0
    assert portfolio.close_at(times, closes, 300) == 3.0
    assert portfolio.close_at(times, closes, 50) is None  # not listed yet


def test_rebalance_dates_land_on_the_calendar_bars():
    times = [START + i * STEP for i in range(200)]
    dates = portfolio.rebalance_dates(times, 30)
    assert dates[0] == times[0] or dates[0] > times[0]
    assert all(moment in times for moment in dates)
    gaps = [b - a for a, b in zip(dates, dates[1:])]
    assert all(gap == 30 * STEP for gap in gaps)


def test_rebalance_dates_reject_a_bad_interval():
    with pytest.raises(ValueError, match="at least 1"):
        portfolio.rebalance_dates([1, 2, 3], 0)
    assert portfolio.rebalance_dates([1], 5) == []


def test_select_takes_the_strongest_slice():
    momentum = {"A": 0.3, "B": 0.1, "C": -0.2, "D": 0.05}
    longs, shorts = portfolio.select(momentum, top=0.25, mode="long-only")
    assert longs == ["A"]
    assert shorts == []

    longs, shorts = portfolio.select(momentum, top=0.5, mode="long-short")
    assert longs == ["A", "B"]
    assert shorts == ["C", "D"]


def test_select_supports_an_absolute_count_and_breaks_ties_by_name():
    momentum = {"B": 0.1, "A": 0.1, "C": 0.1}
    longs, _ = portfolio.select(momentum, top=2, mode="long-only")
    assert longs == ["A", "B"]


def test_select_of_an_empty_cross_section_is_empty():
    assert portfolio.select({}, top=0.2, mode="long-only") == ([], [])


def test_build_panel_samples_closes_and_momentum():
    times, closes = series([0.01] * 100)
    dates = [times[i] for i in (0, 30, 60, 90)]
    panel = portfolio.build_panel(times, closes, "X-USDT", dates, 30 * STEP)
    assert panel.symbol == "X-USDT"
    assert panel.closes == [closes[0], closes[30], closes[60], closes[90]]
    assert panel.momentum[0] is None  # the lookback reaches before the series
    assert panel.momentum[1] == pytest.approx(closes[30] / closes[0] - 1.0)


def test_build_panel_leaves_momentum_empty_before_a_symbol_lists():
    times, closes = series([0.01] * 40)
    dates = [times[0] - 5 * STEP, times[10], times[39]]
    panel = portfolio.build_panel(times, closes, "LATE-USDT", dates, 30 * STEP)
    assert panel.closes[0] is None
    assert panel.momentum[0] is None
    assert panel.momentum[1] is None  # the lookback reaches before the listing
    assert panel.momentum[2] is not None


# --- the run ----------------------------------------------------------------


def test_portfolio_holds_the_strongest_symbol():
    panels, dates = panel_fixture()
    result = portfolio.run_portfolio(
        panels,
        dates,
        lookback=30,
        rebalance=30,
        top=0.34,  # one of three symbols
        bars_per_year=365.0,
    )
    assert result.rebalances
    assert result.rebalances[0].longs == ["UP-USDT"]
    assert result.performance.total_return > 0
    # Holding only the winner beats equal-weighting the whole universe.
    assert result.performance.total_return > result.benchmark.total_return


def test_long_short_holds_both_sides():
    panels, dates = panel_fixture()
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, top=0.34, mode="long-short", bars_per_year=365.0
    )
    first = result.rebalances[0]
    assert first.longs == ["UP-USDT"]
    assert first.shorts == ["DOWN-USDT"]
    assert result.performance.total_return > 0
    # Half the book long a riser, half short a faller: both legs earn.
    assert result.performance.total_return != pytest.approx(
        portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=0.34).performance.total_return
    )


def test_a_long_short_book_earns_from_both_legs():
    """Hand-checked: the weights sum to zero, so `sum(w * ratio)` would be wrong."""
    dates = daily_dates(count=3)
    up = portfolio.build_panel(*series([0.10] * 500), "UP", dates, 30 * STEP)
    down = portfolio.build_panel(*series([-0.10] * 500), "DOWN", dates, 30 * STEP)
    result = portfolio.run_portfolio(
        [up, down], dates, lookback=30, rebalance=30, top=0.5, mode="long-short",
        costs=Costs(fee_per_side=0.0),
    )
    # One period: +0.5 * (1.1^30 - 1) on the long leg, +0.5 * (0.9^30 - 1) on the short.
    # The curve carries one leading point, so the first period is equity[1] -> equity[2].
    expected = 1.0 + 0.5 * (1.1**30 - 1.0) - 0.5 * (0.9**30 - 1.0)
    assert result.equity[2] / result.equity[1] == pytest.approx(expected, rel=1e-9)
    assert result.equity[2] > 1.0


def test_commission_is_charged_on_turnover():
    panels, dates = panel_fixture()
    free = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, top=0.34, costs=Costs(fee_per_side=0.0)
    )
    paid = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, top=0.34, costs=Costs(fee_per_side=0.001)
    )
    assert free.fees_paid == 0.0
    assert paid.fees_paid > 0
    assert paid.performance.total_return < free.performance.total_return
    # The first rebalance buys the whole book; a steady ranking trades little after.
    assert paid.rebalances[0].turnover == pytest.approx(1.0, abs=1e-9)
    assert paid.mean_turnover < 1.0


def test_a_stable_selection_trades_less_than_a_rotating_one():
    """Hand-built cross-sections, so the *ranking* is controlled, not just prices."""
    dates = daily_dates(count=6)
    rising = [
        portfolio.build_panel(*series([0.05] * 500), "A", dates, 30 * STEP),
        portfolio.build_panel(*series([0.01] * 500), "B", dates, 30 * STEP),
    ]
    stable = portfolio.run_portfolio(rising, dates, lookback=30, rebalance=30, top=0.5)
    assert stable.rebalances[0].turnover == pytest.approx(1.0)
    # A stable ranking still pays to pull the drifted weights back to equal, but
    # it stays well below the cost of replacing one holding with another.
    assert all(r.turnover < 1.0 for r in stable.rebalances[1:])

    flat = [1.0] * len(dates)
    alternating = [
        portfolio.Panel("A", flat, [1.0 if k % 2 == 0 else -1.0 for k in range(len(dates))]),
        portfolio.Panel("B", flat, [-1.0 if k % 2 == 0 else 1.0 for k in range(len(dates))]),
    ]
    rotating = portfolio.run_portfolio(alternating, dates, lookback=30, rebalance=30, top=0.5)
    # The leader changes side every rebalance: the whole book turns over, twice.
    assert rotating.mean_turnover > stable.mean_turnover
    assert rotating.rebalances[1].turnover == pytest.approx(2.0)


def test_holding_a_position_is_never_charged_for_its_own_price_move():
    """Regression: the drifted book was normalised twice.

    A book that had risen was thereby under-weighted, so every rebalance paid
    commission to "top it back up". A single holding that rose 3% a day booked
    61% turnover per rebalance while it was in fact never traded.
    """
    dates = daily_dates(count=6)
    panel = portfolio.build_panel(*series([0.03] * 500), "UP-USDT", dates, 30 * STEP)
    result = portfolio.run_portfolio(
        [panel], dates, lookback=30, rebalance=30, top=1, costs=Costs(fee_per_side=0.001)
    )
    # Bought once, then held: only the very first rebalance trades at all, so the
    # whole bill is one side of that one purchase.
    assert result.rebalances[0].turnover == pytest.approx(1.0)
    assert all(r.turnover == pytest.approx(0.0) for r in result.rebalances[1:])
    assert result.fees_paid == pytest.approx(0.001)
    # With no further trading the curve is the symbol's own price path, entered
    # one rebalance after the signal that chose it and paying one side to get in.
    assert result.performance.total_return == pytest.approx(
        (panel.closes[-1] / panel.closes[1]) * (1.0 - 0.001) - 1.0
    )


def test_a_symbol_without_a_bar_keeps_its_last_price_and_says_so():
    # A series that stops early: the later rebalances have no bar for it.
    times, closes = series([0.02] * 120)
    dates = daily_dates(count=8, first_day=60)
    panels = [
        portfolio.build_panel(times, closes, "GONE-USDT", dates, 30 * STEP, max_age=5 * STEP)
    ]
    result = portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=1)
    assert result.dropped > 0
    assert any("last print" in warning for warning in result.warnings)


def test_a_thin_universe_is_flagged():
    dates = daily_dates(count=4)
    panels = [portfolio.build_panel(*series([0.02] * 500), "ONLY", dates, 30 * STEP)]
    result = portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=1)
    assert any("breadth" in warning for warning in result.warnings)


def test_the_curve_is_marked_once_per_rebalance():
    panels, dates = panel_fixture()
    result = portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=0.34)
    assert len(result.equity) == len(dates)
    assert len(result.benchmark_equity) == len(dates)
    assert result.equity[0] == 1.0


def test_a_short_leg_that_explodes_wipes_the_book_out_instead_of_going_negative():
    dates = daily_dates(count=4)
    flat = [1.0] * len(dates)
    # A: climbs a hundredfold while it is the short leg; B is the long.
    panels = [
        portfolio.Panel("B", flat, [0.5] * len(dates)),
        portfolio.Panel("A", [1.0, 1.0, 100.0, 100.0], [-0.5, -0.5, -0.5, -0.5]),
    ]
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, top=0.5, mode="long-short", costs=Costs(0)
    )
    assert all(value >= 0 for value in result.equity)
    assert any("wiped out" in warning for warning in result.warnings)


def test_portfolio_rejects_impossible_settings():
    panels, dates = panel_fixture()
    with pytest.raises(ValueError, match="mode"):
        portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, mode="both")
    with pytest.raises(ValueError, match="top"):
        portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=0)
    with pytest.raises(ValueError, match="at least a few"):
        portfolio.run_portfolio(panels, dates[:2], lookback=30, rebalance=30)


def test_portfolio_summary_is_json_friendly():
    panels, dates = panel_fixture()
    result = portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=0.34)
    payload = result.as_dict()
    assert payload["universe"] == 3
    assert payload["last_rebalance"]["longs"] == ["UP-USDT"]
    assert payload["performance"]["total_return"] == pytest.approx(result.performance.total_return)


def test_the_report_mentions_the_settings_and_the_last_holdings():
    panels, dates = panel_fixture()
    result = portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, top=0.34)
    text = portfolio.render(result, dates)
    assert "lookback 30 bars" in text
    assert "UP-USDT" in text
    assert "equal weight" in text


# --- reading the archive and the CLI ----------------------------------------


@pytest.fixture
def universe_archive(tmp_path: Path) -> Path:
    """A tiny daily archive: three symbols with different trends."""
    root = tmp_path / "spot"
    for symbol, drift in (("UP-USDT", 0.01), ("MID-USDT", 0.002), ("DOWN-USDT", -0.008)):
        closes = [100.0]
        for _ in range(400):
            closes.append(closes[-1] * (1.0 + drift))
        write_archive(root, symbol, "1d", make_bars(closes, start=START, step=STEP))
    return root


def test_read_closes_reads_only_what_the_panel_needs(universe_archive: Path):
    times, closes = portfolio.read_closes(universe_archive, "UP-USDT", "1d")
    assert len(times) == len(closes) == 401
    assert times == sorted(times)
    assert closes[-1] > closes[0]  # the rising symbol


def test_cli_runs_on_a_toy_universe(universe_archive: Path, tmp_path: Path, capsys):
    out = tmp_path / "portfolio.json"
    code = portfolio.main(
        [
            "--data-dir",
            str(universe_archive),
            "--timeframe",
            "1d",
            "--lookback",
            "30",
            "--rebalance",
            "30",
            "--top",
            "1",
            "--calendar",
            "MID-USDT",
            "--json",
            str(out),
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "loaded 3 symbols" in printed
    assert "equal weight" in printed

    import json

    payload = json.loads(out.read_text())
    assert payload["universe"] == 3
    assert payload["last_rebalance"]["longs"] == ["UP-USDT"]  # the strongest trend


def test_cli_needs_a_usable_calendar(universe_archive: Path):
    with pytest.raises((SystemExit, FileNotFoundError, ValueError)):
        portfolio.main(
            [
                "--data-dir",
                str(universe_archive),
                "--timeframe",
                "1d",
                "--calendar",
                "NOPE-USDT",
                "--rebalance",
                "30",
            ]
        )


def test_universes_lists_symbols_of_one_timeframe(universe_archive: Path):
    symbols = portfolio.universes(universe_archive, "1d")
    assert symbols == ["DOWN-USDT", "MID-USDT", "UP-USDT"]
    assert portfolio.universes(universe_archive, "1d", limit=2) == ["DOWN-USDT", "MID-USDT"]
    assert portfolio.universes(universe_archive, "1h") == []
