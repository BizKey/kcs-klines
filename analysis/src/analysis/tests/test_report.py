"""The chart writer: log-scale curves, position markers, and a file that parses."""

from __future__ import annotations

from pathlib import Path

import pytest

from .. import report
from ..engine import Costs, run_backtest
from ..strategies import get_strategy
from .conftest import make_bars, wavy

TIMES = [1_507_161_600 + 3600 * i for i in range(9)]


def chart(tmp_path: Path, markers, curves=None, name="chart.svg") -> str:
    path = tmp_path / name
    report.write_curves(
        path,
        TIMES,
        curves or {"strategy": [1.0, 1.1, 1.05, 1.3, 1.2, 1.5, 0.9, 1.4, 1.6], "buy & hold": [1.0] * 9},
        "a test chart",
        markers=markers,
    )
    return path.read_text()


# --- which bars get a marker --------------------------------------------------


def test_entering_leaving_and_flipping_are_marked_but_resizing_is_not():
    positions = [0.0, 0.0, 1.0, 1.0, 0.5, 0.5, -1.0, -1.0, 0.0]
    markers = report.position_markers(TIMES, positions)
    assert [index for index, _, _ in markers] == [2, 6, 8]
    assert [colour for _, colour, _ in markers] == [
        report.MARKER_ENTRY,
        report.MARKER_FLIP,
        report.MARKER_EXIT,
    ]


def test_a_short_that_stays_short_is_not_a_flip():
    positions = [0.0, -1.0, -1.0, -0.5, -1.0]
    markers = report.position_markers(TIMES[:5], positions)
    assert [index for index, _, _ in markers] == [1]  # one entry, then only resizes


def test_a_strategy_that_never_trades_has_no_markers():
    assert report.position_markers(TIMES, [0.0] * len(TIMES)) == []


def test_marker_tooltips_carry_the_time_and_the_move():
    markers = report.position_markers(TIMES[:2], [0.0, 1.0])
    _, _, label = markers[0]
    assert "2017-10-05 01:00 UTC" in label
    assert "into the market" in label
    assert "0 → 1" in label


def test_positions_must_match_the_timeline():
    with pytest.raises(ValueError, match="positions for"):
        report.position_markers(TIMES, [0.0, 1.0])


# --- what lands in the file ---------------------------------------------------


def test_markers_are_drawn_behind_the_curves_and_tallied(tmp_path: Path):
    text = chart(tmp_path, report.position_markers(TIMES, [0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]))
    assert text.count("<polyline") == 2
    assert text.count('class="position-change"') == 2
    assert 'stroke="#137333"' in text  # the entry
    assert 'stroke="#c5221f"' in text  # the exit
    assert "in = green (1), out = red (1)" in text
    # document order: the markers come before the curves
    assert text.index('stroke="#137333"') < text.index("<polyline")


def test_a_chart_without_markers_says_nothing_about_them(tmp_path: Path):
    text = chart(tmp_path, None)
    assert "position changes" not in text
    assert 'class="position-change"' not in text
    assert 'stroke="#bbb"' in text  # the x-axis rule is still drawn


def test_more_markers_are_drawn_fainter(tmp_path: Path):
    few = chart(tmp_path, [(i, report.MARKER_ENTRY, "x") for i in range(1, 4)], name="few.svg")
    many = chart(tmp_path, [(i % 8 + 1, report.MARKER_ENTRY, "x") for i in range(1000)], name="many.svg")
    assert 'stroke-opacity="0.90"' in few
    assert 'stroke-opacity="0.15"' in many


def test_a_marker_outside_the_timeline_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="outside"):
        chart(tmp_path, [(99, report.MARKER_ENTRY, "x")])


def test_each_final_level_is_a_dashed_horizontal_line_labelled_in_x(tmp_path: Path):
    path = tmp_path / "levels.svg"
    report.write_curves(
        path,
        TIMES,
        {"strategy": [1.0] * 9, "buy & hold": [1.2] * 9},
        "levels",
        levels={"strategy": 2.5, "buy & hold": 0.8},
    )
    text = path.read_text()
    assert len(text.split('<g class="final-level">')) - 1 == 2
    assert text.count('stroke-dasharray="6 4"') == 2
    assert "2.50x" in text and "0.80x" in text  # the multiples, for reading off
    assert "strategy ends at 2.50x" in text and "buy &amp; hold ends at 0.80x" in text
    # a level line spans the plot and is horizontal
    for part in text.split('<g class="final-level">')[1:]:
        line = part.split("<line ")[1].split("/>")[0]
        y1 = float(line.split('y1="')[1].split('"')[0])
        y2 = float(line.split('y2="')[1].split('"')[0])
        assert y1 == y2
        assert 'x1="70"' in line and f'x2="{report.WIDTH - 24}"' in line


def test_a_chart_without_levels_has_no_dashed_lines(tmp_path: Path):
    text = chart(tmp_path, None)
    assert "stroke-dasharray" not in text


def test_a_level_for_a_missing_curve_or_a_bad_value_is_rejected(tmp_path: Path):
    path = tmp_path / "bad.svg"
    with pytest.raises(ValueError, match="not one of the curves"):
        report.write_curves(path, TIMES, {"strategy": [1.0] * 9}, "x", levels={"nope": 2.0})
    with pytest.raises(ValueError, match="must be positive"):
        report.write_curves(path, TIMES, {"strategy": [1.0] * 9}, "x", levels={"strategy": 0.0})


def test_the_chart_shows_the_headline_multiple(tmp_path: Path):
    """The number on the chart is the number the report prints."""
    bars = make_bars(wavy(200, amplitude=15.0, period=6.0))
    strategy = get_strategy("sma", window=5)
    result = run_backtest(bars, strategy.targets(bars), "1h", Costs(fee_per_side=0.001))
    path = tmp_path / "headline.svg"
    report.write_chart(path, bars, result, "test")
    text = path.read_text()
    assert f"{result.performance.final_equity:,.2f}x" in text
    assert f"{result.benchmark.performance.final_equity:,.2f}x" in text


def test_marker_lines_stay_inside_the_plot_area(tmp_path: Path):
    """A swap near either end of the timeline must still be drawn inside the plot."""
    last = len(TIMES) - 1
    positions = [0.0] * last + [1.0]
    positions[1], positions[2] = 1.0, 0.0  # a change early on too
    text = chart(tmp_path, report.position_markers(TIMES, positions))
    spans = [
        (float(part.split('x1="')[1].split('"')[0]), float(part.split('x2="')[1].split('"')[0]))
        for part in text.split('<g class="position-change">')[1:]
    ]
    assert spans
    for left, right in spans:
        assert left == right  # vertical
        assert 70 <= left <= report.WIDTH - 24
    assert max(left for left, _ in spans) == pytest.approx(report.WIDTH - 24, abs=0.01)
    assert min(left for left, _ in spans) > 70


def test_a_label_with_an_ampersand_does_not_break_the_file(tmp_path: Path):
    text = chart(tmp_path, [(4, report.MARKER_ENTRY, "BTC & ETH swap")])
    assert "BTC &amp; ETH swap" in text
    assert text.startswith("<svg") and text.rstrip().endswith("</svg>")


# --- and through the CLI's own writer -----------------------------------------


def test_the_backtest_chart_marks_every_trade(tmp_path: Path):
    bars = make_bars(wavy(200, amplitude=15.0, period=6.0))
    strategy = get_strategy("sma", window=5)
    result = run_backtest(bars, strategy.targets(bars), "1h", Costs(fee_per_side=0.001))
    path = tmp_path / "chart.svg"
    report.write_chart(path, bars, result, "test")
    text = path.read_text()
    changes = sum(1 for i in range(1, len(result.positions)) if result.positions[i] != result.positions[i - 1])
    assert changes > 0
    assert text.count('class="position-change"') == changes
    assert "position changes:" in text
