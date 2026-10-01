"""Cross-sectional momentum: ranking, weights, turnover and the benchmark."""

from __future__ import annotations

import csv as csv_module
import json
import statistics as st
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
    # With no further trading the curve is the symbol's own price path, entered at the
    # first rebalance's close — the earliest price the rule can be filled at, since its
    # signal exists from that close — and paying one side to get in.
    assert result.performance.total_return == pytest.approx(
        (panel.closes[-1] / panel.closes[0]) * (1.0 - 0.001) - 1.0
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
    """A tiny daily archive: USDT pairs with different trends, plus one cross pair."""
    root = tmp_path / "spot"
    for symbol, drift in (
        ("UP-USDT", 0.01),
        ("MID-USDT", 0.002),
        ("DOWN-USDT", -0.008),
        ("UP-BTC", 0.004),          # a cross pair: filtered out unless --quote allows it
    ):
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


# --- the sign filter ----------------------------------------------------------


def test_sign_takes_every_symbol_above_the_bar_not_a_slice():
    momentum = {"A": 0.10, "B": 0.02, "C": -0.01, "D": -0.30}
    longs, shorts = portfolio.select(momentum, top=0.2, mode="long-only", select_mode="sign")
    assert longs == ["A", "B"]                       # two of four, not "the top 20%"
    assert shorts == []
    longs, shorts = portfolio.select(momentum, top=0.2, mode="long-short", select_mode="sign")
    assert longs == ["A", "B"]
    assert shorts == ["C", "D"]                      # everything strictly below zero
    longs, _ = portfolio.select(momentum, top=0.2, mode="long-only", select_mode="sign", threshold=0.05)
    assert longs == ["A"]
    longs, _ = portfolio.select(momentum, top=0.2, mode="long-only", select_mode="sign", threshold=0.5)
    assert longs == []
    longs, _ = portfolio.select(momentum, top=0.2, mode="long-only")   # ranking is untouched
    assert longs == ["A"]


def falling_panels():
    """Two symbols that only ever fall: nothing can clear a zero bar."""
    dates = daily_dates()
    return [
        portfolio.build_panel(*series([-0.02] * 500), "DOWN1-USDT", dates, 30 * STEP),
        portfolio.build_panel(*series([-0.01] * 500), "DOWN2-USDT", dates, 30 * STEP),
    ], dates


def rise_then_fall_panels():
    """One symbol that climbs for 300 bars and then slides: in, then out, then cash."""
    dates = daily_dates(count=16, first_day=60)
    returns = [0.03] * 300 + [-0.03] * 200
    return [portfolio.build_panel(*series(returns), "ROUND-USDT", dates, 30 * STEP)], dates


def test_a_sign_book_holds_nothing_when_nothing_rose():
    """The point of a filter: a falling cross-section means cash, not the least-bad names."""
    panels, dates = falling_panels()
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="falling",
    )
    assert result.flat_rebalances == len(result.rebalances)
    assert result.cash_share == pytest.approx(1.0)
    assert result.performance.final_equity == pytest.approx(1.0)
    assert any("nothing cleared the" in w for w in result.warnings)
    assert result.holdings_mean == 0.0


def test_a_sign_book_trades_in_and_out_and_pays_for_both():
    panels, dates = rise_then_fall_panels()
    free = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    paid = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(fee_per_side=0.01), label="round",
    )
    assert 0 < free.flat_rebalances < len(free.rebalances)   # it went to cash partway
    assert paid.fees_paid > 0
    assert paid.performance.final_equity < free.performance.final_equity


def test_the_sign_mode_decides_only_on_the_past():
    """Adding later rebalance dates must not change an earlier decision."""
    panels, dates = rise_then_fall_panels()
    full = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="full",
    )
    cut = 8
    short_panels = [
        portfolio.build_panel(
            *series([0.03] * 300 + [-0.03] * 200), panel.symbol, dates[:cut], 30 * STEP
        )
        for panel in panels
    ]
    short = portfolio.run_portfolio(
        short_panels, dates[:cut], lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="short",
    )
    # `short` has one fewer rebalance than it has dates, so compare the overlap
    overlap = len(short.rebalances)
    assert [r.longs for r in short.rebalances] == [
        r.longs for r in full.rebalances[:overlap]
    ]


def test_bad_selection_settings_are_rejected():
    panels, dates = panel_fixture()
    with pytest.raises(ValueError, match="select must be one of"):
        portfolio.run_portfolio(panels, dates, lookback=30, rebalance=30, select_mode="magic")
    assert portfolio.threshold_of("0") == 0.0
    assert portfolio.threshold_of("5%") == pytest.approx(0.05)
    assert portfolio.threshold_of("-3%") == pytest.approx(-0.03)
    assert portfolio.threshold_of("0.05") == pytest.approx(0.05)
    with pytest.raises(SystemExit, match="needs a number"):
        portfolio.threshold_of("пять процентов")


def test_cli_runs_the_sign_filter_and_names_it(universe_archive: Path, tmp_path: Path, capsys):
    out = tmp_path / "sign.json"
    code = portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign", "--threshold", "0", "--json", str(out),
    ])
    printed = capsys.readouterr().out
    assert code == 0
    assert "sign filter" in printed
    assert "every symbol above" in printed and "cash" in printed
    payload = json.loads(out.read_text())
    assert payload["select"] == "sign"
    assert "cash_share" in payload


def test_cli_reports_a_window_of_rebalances(universe_archive: Path, capsys):
    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--last", "3",
    ]) == 0
    assert "2 rebalances" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="at least 3 rebalances"):
        portfolio.main([
            "--data-dir", str(universe_archive), "--timeframe", "1d",
            "--rebalance", "30", "--calendar", "MID-USDT", "--last", "2",
        ])


# --- the chart ----------------------------------------------------------------


def test_the_chart_marks_the_moves_into_and_out_of_cash():
    panels, dates = rise_then_fall_panels()
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    markers = portfolio.cash_markers(result)
    assert markers, "the round trip must be marked"
    assert [colour for _, colour, _ in markers] == [portfolio.report.MARKER_ENTRY,
                                                    portfolio.report.MARKER_EXIT]
    assert "back in" in markers[0][2] and "to cash" in markers[1][2]
    # a ranking book enters once and never leaves: one marker, no exit
    ranked = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="rank", top=1.0, costs=Costs(0), label="rank",
    )
    assert [colour for _, colour, _ in portfolio.cash_markers(ranked)] == [
        portfolio.report.MARKER_ENTRY
    ]


def test_the_chart_is_written_with_both_curves(tmp_path: Path):
    panels, dates = rise_then_fall_panels()
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    path = tmp_path / "portfolio.svg"
    portfolio.write_chart(path, result, dates)
    text = path.read_text()
    assert "portfolio (select sign)" in text
    assert "equal weight, 1 names" in text
    assert "ends at" in text and "cash moves" in text
    assert text.count("<polyline") == 2


def test_cli_writes_the_chart(universe_archive: Path, tmp_path: Path, capsys):
    path = tmp_path / "chart.svg"
    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign", "--chart", str(path),
    ]) == 0
    printed = capsys.readouterr().out
    assert f"wrote {path}" in printed
    assert path.exists() and path.read_text().startswith("<svg")
    assert "breadth" in printed


# --- the per-asset trades chart -----------------------------------------------


def test_the_trades_chart_marks_each_buy_and_sell_at_its_fill_price():
    panels, dates = rise_then_fall_panels()
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    events = portfolio.trade_log(result, panels, dates)
    kinds = [kind for _, kind, _, _ in events]
    assert kinds[0] == "buy" and "sell" in kinds
    # the fill price is the close of the bar the rebalance happened on
    for symbol, kind, index, price in events:
        panel = next(p for p in panels if p.symbol == symbol)
        assert price == pytest.approx(panel.closes[index])


def test_the_trades_chart_draws_held_assets_and_greys_the_rest(tmp_path: Path):
    panels, dates = rise_then_fall_panels()
    panels.append(portfolio.build_panel(*series([-0.02] * 500), "NEVER-USDT", dates, 30 * STEP))
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    path = tmp_path / "trades.svg"
    portfolio.write_trades_chart(path, result, panels, dates)
    text = path.read_text()
    assert "ROUND-USDT" in text and "NEVER-USDT" in text
    assert 'stroke-dasharray="4 3"' in text            # the never-bought line is dashed
    assert "grey dashed = never bought (1 shown)" in text
    assert "bought" in text and "sold" in text
    assert text.count("<polyline") == 2


def test_the_trades_chart_respects_its_cap(tmp_path: Path):
    panels = [
        portfolio.build_panel(*series([0.01] * 500), f"A{i}-USDT", daily_dates(), 30 * STEP)
        for i in range(12)
    ]
    dates = daily_dates()
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="many",
    )
    path = tmp_path / "capped.svg"
    portfolio.write_trades_chart(path, result, panels, dates, limit=4, never_held=1)
    assert path.read_text().count("<polyline") <= 4


def test_a_short_leg_is_marked_as_a_short_not_a_buy():
    """Gentle moves on purpose: a fast faller that then rallies wipes a short book out."""
    dates = daily_dates(count=12, first_day=60)
    panels = [
        portfolio.build_panel(
            *series([-0.004] * 300 + [0.004] * 200), "DOWN-USDT", dates, 30 * STEP
        )
    ]
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-short",
        select_mode="sign", costs=Costs(0), label="ls",
    )
    kinds = [kind for _, kind, _, _ in portfolio.trade_log(result, panels, dates)]
    assert kinds[0] == "short"          # not "buy"
    assert "cover" in kinds


def test_cli_writes_the_trades_chart(universe_archive: Path, tmp_path: Path, capsys):
    path = tmp_path / "trades.svg"
    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign", "--chart-trades", str(path), "--chart-symbols", "3",
    ]) == 0
    printed = capsys.readouterr().out
    assert f"wrote {path}" in printed
    assert path.read_text().startswith("<svg")


# --- the per-pair census ------------------------------------------------------


def test_pair_stats_agree_with_the_trade_log():
    panels, dates = rise_then_fall_panels()
    panels.append(portfolio.build_panel(*series([-0.02] * 500), "NEVER-USDT", dates, 30 * STEP))
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    rows = {row["symbol"]: row for row in portfolio.pair_stats(result, panels, dates)}
    for symbol, kind, _, _ in portfolio.trade_log(result, panels, dates):
        key = {"buy": "buys", "sell": "sells", "short": "shorts", "cover": "covers"}[kind]
        assert rows[symbol][key] >= 1
    assert rows["ROUND-USDT"]["buys"] == 1 and rows["ROUND-USDT"]["sells"] == 1
    assert rows["NEVER-USDT"]["held"] == 0            # watched, never bought
    assert rows["NEVER-USDT"]["buys"] == 0
    assert rows["ROUND-USDT"]["first_held"] < rows["ROUND-USDT"]["last_held"]
    # busiest first, and every symbol of the universe is in the census
    assert len(rows) == len(panels)
    assert [row["symbol"] for row in portfolio.pair_stats(result, panels, dates)][0] == "ROUND-USDT"


def test_the_pairs_csv_is_a_census_of_the_universe(tmp_path: Path):
    panels, dates = rise_then_fall_panels()
    panels.append(portfolio.build_panel(*series([-0.02] * 500), "NEVER-USDT", dates, 30 * STEP))
    result = portfolio.run_portfolio(
        panels, dates, lookback=30, rebalance=30, mode="long-only",
        select_mode="sign", costs=Costs(0), label="round",
    )
    path = tmp_path / "pairs.csv"
    portfolio.write_pairs(path, result, panels, dates)
    rows = list(csv_module.DictReader(path.open()))
    assert [row["symbol"] for row in rows] == ["ROUND-USDT", "NEVER-USDT"]
    assert rows[0]["ever_traded"] == "yes" and rows[1]["ever_traded"] == "no"
    assert rows[1]["rebalances_held"] == "0"
    assert rows[0]["first_held_utc"] and rows[0]["last_held_utc"]


def test_the_report_lists_the_traded_pairs(universe_archive: Path, tmp_path: Path, capsys):
    path = tmp_path / "pairs.csv"
    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign", "--pairs-csv", str(path),
    ]) == 0
    printed = capsys.readouterr().out
    assert "traded pairs:" in printed
    assert "never bought" in printed
    assert f"wrote {path}" in printed
    assert path.exists()


# --- the quote filter ---------------------------------------------------------


def test_parse_quotes_and_quote_of():
    assert portfolio.parse_quotes("usdt") == ("USDT",)
    assert portfolio.parse_quotes(" USDT , btc ") == ("USDT", "BTC")
    assert portfolio.parse_quotes("any") is None
    assert portfolio.parse_quotes("ALL") is None
    assert portfolio.parse_quotes("") is None
    assert portfolio.quote_of("BTC-USDT") == "USDT"
    assert portfolio.quote_of("ADA-BTC") == "BTC"
    assert portfolio.quote_of("USDT-USDC") == "USDC"
    with pytest.raises(ValueError):
        portfolio.parse_quotes(",")


def test_universes_keep_only_the_requested_quote(universe_archive: Path):
    assert portfolio.universes(universe_archive, "1d") == [
        "DOWN-USDT", "MID-USDT", "UP-USDT",
    ]
    assert "UP-BTC" in portfolio.universes(universe_archive, "1d", quotes=None)
    assert portfolio.universes(universe_archive, "1d", quotes=("USDT", "BTC")) == [
        "DOWN-USDT", "MID-USDT", "UP-BTC", "UP-USDT",
    ]
    assert portfolio.universes(universe_archive, "1d", quotes=("BTC",)) == ["UP-BTC"]


def test_the_report_says_what_the_quote_filter_removed(universe_archive: Path, capsys):
    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign",
    ]) == 0
    printed = capsys.readouterr().out
    assert "3 symbols quoted in USDT" in printed
    assert "1 other pairs skipped" in printed

    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign", "--quote", "any",
    ]) == 0
    printed = capsys.readouterr().out
    assert "4 symbols any quote currency" in printed
    assert "skipped" not in printed


def test_a_usdt_only_run_never_trades_a_cross_pair(universe_archive: Path, tmp_path: Path):
    path = tmp_path / "pairs.csv"
    assert portfolio.main([
        "--data-dir", str(universe_archive), "--timeframe", "1d",
        "--lookback", "30", "--rebalance", "30", "--calendar", "MID-USDT",
        "--select", "sign", "--pairs-csv", str(path),
    ]) == 0
    rows = list(csv_module.DictReader(path.open()))
    assert {row["symbol"] for row in rows} == {"UP-USDT", "MID-USDT", "DOWN-USDT"}
    assert all(portfolio.quote_of(row["symbol"]) == "USDT" for row in rows)


def test_the_book_is_exposed_to_the_very_period_it_is_decided_in():
    """The decision at date k earns closes[k] -> closes[k+1], as `engine.py` does it.

    The signal turns positive only on the middle date, and the price halves right after
    it: a book that is filled at that close loses 50%. Running a rebalance late — which
    this module used to do — would show a flat curve instead, because the position would
    only start earning after the fall.
    """
    dates = [START + day * STEP for day in (60, 90, 120)]
    panel = portfolio.Panel("MID-USDT", [100.0, 100.0, 50.0], [0.0, 1.0, 0.0])
    result = portfolio.run_portfolio(
        [panel], dates, lookback=30, rebalance=30, select_mode="sign",
        threshold=0.0, costs=Costs(fee_per_side=0.0),
    )
    assert result.rebalances[1].longs == ["MID-USDT"]
    assert result.equity[-1] == pytest.approx(0.5)


def test_the_entry_commission_reaches_the_curve():
    """Regression: the first rebalance used to write the bill into `equity[0]`.

    That element is the base the curve is normalised by, so the entry commission
    cancelled itself out — it was counted in `fees_paid` and invisible in the result.
    """
    dates = [START + day * STEP for day in (60, 90, 120)]
    panel = portfolio.Panel("MID-USDT", [100.0, 100.0, 100.0], [1.0, 1.0, 1.0])
    result = portfolio.run_portfolio(
        [panel], dates, lookback=30, rebalance=30, select_mode="sign",
        threshold=0.0, costs=Costs(fee_per_side=0.001),
    )
    assert result.fees_paid == pytest.approx(0.001)
    assert result.equity[-1] == pytest.approx(1.0 - 0.001)
    assert result.performance.total_return == pytest.approx(-0.001)


# --- the health gates ---------------------------------------------------------


def _panel_with_gates(**gates):
    dates = [START + day * STEP for day in (60, 90, 120)]
    base = {name: [None] * len(dates) for name in
            ("below_peak", "trend", "turnover", "volatility")}
    base.update(gates)
    return portfolio.Panel("X-USDT", [100.0] * len(dates), [1.0] * len(dates), gates=base)


def test_a_gate_that_is_off_lets_everything_through():
    panel = _panel_with_gates()
    assert portfolio.gate_ok(panel, 1, None)
    assert portfolio.gate_ok(panel, 1, portfolio.GateSpec())
    assert not portfolio.GateSpec().active


def test_each_gate_compares_its_reading_and_refuses_missing_history():
    panel = _panel_with_gates(trend=[-0.1, 0.0, 0.2], below_peak=[-0.95, -0.5, -0.2],
                              turnover=[1e3, 1e5, 1e6], volatility=[0.01, 0.2, 0.4])
    # trend: at or above its own mean by the threshold (the report prints ">= ...")
    assert not portfolio.gate_ok(panel, 0, portfolio.GateSpec(trend_bars=200))
    assert portfolio.gate_ok(panel, 1, portfolio.GateSpec(trend_bars=200))
    assert not portfolio.gate_ok(panel, 1, portfolio.GateSpec(trend_bars=200, trend_threshold=0.05))
    # distance from the running peak
    assert not portfolio.gate_ok(panel, 0, portfolio.GateSpec(max_below_peak=0.5))
    assert portfolio.gate_ok(panel, 1, portfolio.GateSpec(max_below_peak=0.5))
    # liquidity and the volatility floor
    assert not portfolio.gate_ok(panel, 0, portfolio.GateSpec(min_turnover=1e4))
    assert portfolio.gate_ok(panel, 1, portfolio.GateSpec(min_turnover=1e4))
    assert not portfolio.gate_ok(panel, 0, portfolio.GateSpec(min_volatility=0.05))
    assert portfolio.gate_ok(panel, 1, portfolio.GateSpec(min_volatility=0.05))
    # a reading that does not exist yet counts as failed, not as passed
    empty = portfolio.Panel("Y-USDT", [100.0] * 3, [1.0] * 3)
    assert not portfolio.gate_ok(empty, 1, portfolio.GateSpec(trend_bars=200))
    assert portfolio.gate_ok(empty, 1, portfolio.GateSpec())


def test_a_gated_run_drops_names_and_says_so(tmp_path: Path, capsys):
    root = tmp_path / "spot"
    # one symbol falls below its own long mean, one stays above it
    for symbol, drift in (("STRONG-USDT", 0.01), ("DYING-USDT", -0.01)):
        closes = [100.0]
        for _ in range(400):
            closes.append(closes[-1] * (1.0 + drift))
        write_archive(root, symbol, "1d", make_bars(closes, start=START, step=STEP))
    path = tmp_path / "pairs.csv"
    assert portfolio.main([
        "--data-dir", str(root), "--timeframe", "1d", "--lookback", "30",
        "--rebalance", "30", "--calendar", "STRONG-USDT", "--select", "sign",
        "--trend-gate", "100", "--pairs-csv", str(path),
    ]) == 0
    printed = capsys.readouterr().out
    assert "gates       : trend 100 bars" in printed
    assert "dropped ~1 candidate(s) per rebalance" in printed
    # the falling symbol is watched and never bought: the gate is the only reason
    rows = {row["symbol"]: row for row in csv_module.DictReader(path.open())}
    assert rows["DYING-USDT"]["ever_traded"] == "no"
    assert rows["STRONG-USDT"]["ever_traded"] == "yes"


def test_the_gates_never_see_the_future():
    """A gate reading at date k must not move when a later close changes."""
    dates = [START + day * STEP for day in (60, 90, 120)]
    closes = [100.0 + 10 * i for i in range(200)]
    before = portfolio.build_panel(
        [START + i * STEP for i in range(200)], closes, "X-USDT", dates, 30 * STEP,
        gate_seconds=30 * STEP, trend_bars=100, turnover=[1e5] * 200,
    )
    rewritten = list(closes)
    rewritten[-1] *= 10.0                      # a later bar, after every gate reading
    after = portfolio.build_panel(
        [START + i * STEP for i in range(200)], rewritten, "X-USDT", dates, 30 * STEP,
        gate_seconds=30 * STEP, trend_bars=100, turnover=[1e5] * 200,
    )
    for name in ("below_peak", "trend", "volatility"):
        assert before.gates[name][:2] == after.gates[name][:2]
