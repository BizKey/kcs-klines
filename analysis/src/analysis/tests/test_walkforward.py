"""Walk-forward validation: the splits, the choice, and the stitched result."""

from __future__ import annotations

from pathlib import Path

import pytest

from .. import data, walkforward
from ..metrics import Performance, performance
from ..strategies import SmaTrend
from .conftest import make_bars, wavy


def perf(total: float, years: float = 1.0) -> Performance:
    """A `Performance` with a chosen total return, for testing the choice itself."""
    curve = [1.0, 1.0 + total]
    return performance(curve, bars_per_year=1.0 / max(years, 1e-9))


# --- splits -----------------------------------------------------------------


def test_split_bounds_tile_the_series():
    splits = walkforward.split_bounds(1000, train=300, test=100)
    assert len(splits) == 7
    assert splits[0].train_start == 0
    assert splits[0].train_end == 300
    assert splits[0].test_end == 400
    assert splits[1].train_start == 100
    assert splits[-1].test_end <= 1000
    # Test windows follow one another without a gap and without overlap.
    for earlier, later in zip(splits, splits[1:]):
        assert earlier.test_end == later.train_end


def test_split_step_controls_the_gap_between_windows():
    dense = walkforward.split_bounds(1000, train=300, test=100, step=50)
    sparse = walkforward.split_bounds(1000, train=300, test=100, step=200)
    assert len(dense) > len(sparse)
    assert sparse[1].train_end - sparse[0].train_end == 200


def test_split_bounds_reject_impossible_windows():
    with pytest.raises(ValueError, match="at least 2"):
        walkforward.split_bounds(1000, train=1, test=100)
    with pytest.raises(ValueError, match="step"):
        walkforward.split_bounds(1000, train=300, test=100, step=0)
    assert walkforward.split_bounds(200, train=300, test=100) == []


def test_expand_grid_is_the_cartesian_product():
    grid = walkforward.expand_grid({"window": [50, 100], "mode": ["long-only", "long-short"]})
    assert len(grid) == 4
    assert grid[0] == {"mode": "long-only", "window": 50}
    assert {"mode": "long-short", "window": 100} in grid


def test_expand_grid_rejects_empty_input():
    with pytest.raises(ValueError, match="empty"):
        walkforward.expand_grid({})
    with pytest.raises(ValueError, match="no values"):
        walkforward.expand_grid({"window": []})


def test_slice_equity_normalises_its_own_first_bar_to_one():
    equity = [1.0, 2.0, 4.0, 8.0]
    assert walkforward.slice_equity(equity, 0, 3) == [1.0, 2.0, 4.0]
    assert walkforward.slice_equity(equity, 2, 4) == [1.0, 2.0]  # 4 -> 8, based on 4.0


# --- choosing ---------------------------------------------------------------


def test_pick_best_takes_the_highest_metric():
    scored = [
        ({"window": 50}, perf(0.1)),
        ({"window": 100}, perf(0.5)),
        ({"window": 200}, perf(0.3)),
    ]
    assert walkforward.pick_best(scored, "total_return") == 1
    assert walkforward.pick_best(scored, "sharpe") in (0, 1, 2)  # sharpe differs from total here


def test_pick_best_is_stable_on_ties():
    scored = [({"window": 50}, perf(0.2)), ({"window": 100}, perf(0.2))]
    assert walkforward.pick_best(scored, "total_return") == 0


def test_pick_best_rejects_an_unknown_metric():
    with pytest.raises(ValueError, match="metric must be one of"):
        walkforward.pick_best([({}, perf(0.1))], "profit")


# --- the whole run ----------------------------------------------------------


def toy_bars(count: int = 400) -> list:
    return make_bars(wavy(count))


def test_walkforward_stitches_the_test_windows():
    bars = toy_bars()
    result = walkforward.run_walkforward(
        bars, "sma", {"window": [5, 20]}, "1h", train=100, test=50, metric="total_return"
    )
    splits = walkforward.split_bounds(len(bars), 100, 50)
    assert len(result.outcomes) == len(splits)
    assert len(result.oos_equity) == 1 + 50 * len(splits)
    assert len(result.benchmark_equity) == len(result.oos_equity)

    # The stitched curve is the product of the windows, in order.
    compounded = 1.0
    for outcome in result.outcomes:
        compounded *= outcome.test_equity[-1]
    assert result.oos.total_return == pytest.approx(compounded - 1.0, rel=1e-9)


def test_walkforward_chooses_on_the_train_window_only():
    """Rewriting the future must not change what the past windows decided."""
    bars = toy_bars()
    baseline = walkforward.run_walkforward(
        bars, "sma", {"window": [5, 20]}, "1h", train=100, test=50, metric="total_return"
    )

    edited = list(bars)
    first_test_end = baseline.outcomes[0].split.test_end
    for index in range(first_test_end, len(edited)):
        bar = edited[index]
        edited[index] = data.Bar(bar.time, bar.open, bar.high, bar.low, bar.close * 3, bar.volume)

    after = walkforward.run_walkforward(
        edited, "sma", {"window": [5, 20]}, "1h", train=100, test=50, metric="total_return"
    )
    first_before, first_after = baseline.outcomes[0], after.outcomes[0]
    assert first_after.params == first_before.params
    assert first_after.test.total_return == pytest.approx(first_before.test.total_return)


def test_walkforward_reports_which_parameters_won_and_how_often():
    result = walkforward.run_walkforward(
        toy_bars(), "sma", {"window": [5, 20]}, "1h", train=100, test=50
    )
    counts = result.parameter_counts
    assert sum(counts.values()) == len(result.outcomes)
    assert all(key.startswith("window=") for key in counts)
    assert 0.0 <= result.winning_share <= 1.0


def test_walkforward_warns_when_there_is_little_to_go_on():
    result = walkforward.run_walkforward(
        toy_bars(250), "sma", {"window": [5]}, "1h", train=100, test=50
    )
    assert any("too few" in warning for warning in result.warnings)


def test_walkforward_rejects_bad_input():
    bars = toy_bars()
    with pytest.raises(ValueError, match="not enough"):
        walkforward.run_walkforward(bars, "sma", {"window": [5]}, "1h", train=10_000, test=10)
    with pytest.raises(ValueError, match="metric"):
        walkforward.run_walkforward(bars, "sma", {"window": [5]}, "1h", train=100, test=50, metric="x")
    with pytest.raises(KeyError):
        walkforward.run_walkforward(bars, "no-such-strategy", {"window": [5]}, "1h", train=100, test=50)


def test_walkforward_summary_is_json_friendly():
    result = walkforward.run_walkforward(
        toy_bars(), "sma", {"window": [5, 20]}, "1h", train=100, test=50
    )
    payload = result.as_dict()
    assert payload["splits"] == len(result.outcomes)
    assert payload["candidates"] == 2
    assert "out_of_sample" in payload and "buy_and_hold" in payload
    assert payload["out_of_sample"]["total_return"] == pytest.approx(result.oos.total_return)


def test_walkforward_report_mentions_the_essentials():
    bars = toy_bars()
    result = walkforward.run_walkforward(bars, "sma", {"window": [5, 20]}, "1h", train=100, test=50)
    text = walkforward.render(result, bars)
    assert "parameter stability" in text
    assert "out-of-sample" in text
    assert "buy & hold" in text
    assert iso_in(text, bars[result.outcomes[0].split.train_end].time)


def iso_in(text: str, timestamp: int) -> bool:
    from ..data import iso

    return iso(timestamp) in text


# --- CLI --------------------------------------------------------------------


def test_parse_grids_reads_names_and_values():
    assert walkforward.parse_grids(["window=5,20"], "sma") == {"window": [5, 20]}
    grid = walkforward.parse_grids(["window=5", "mode=long-short"], "sma-rev")
    assert grid == {"window": [5], "mode": ["long-short"]}


def test_parse_grids_rejects_a_missing_value_or_unknown_parameter():
    with pytest.raises(SystemExit, match="NAME=V"):
        walkforward.parse_grids(["window"], "sma")
    with pytest.raises(SystemExit, match="has no parameter"):
        walkforward.parse_grids(["lookback=5"], "sma")
    with pytest.raises(SystemExit, match="no values"):
        walkforward.parse_grids(["window=,"], "sma")


def test_coerce_reads_numbers_and_booleans():
    assert walkforward.coerce("50") == 50
    assert walkforward.coerce("2.5") == 2.5
    assert walkforward.coerce("true") is True
    assert walkforward.coerce("long-only") == "long-only"


def test_cli_runs_on_a_toy_archive(toy_archive: Path, tmp_path: Path, capsys):
    out = tmp_path / "wf.json"
    code = walkforward.main(
        [
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--strategy",
            "sma",
            "--grid",
            "window=5,20",
            "--train",
            "100",
            "--test",
            "50",
            "--metric",
            "total_return",
            "--json",
            str(out),
        ]
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "out-of-sample" in printed
    assert out.is_file()

    import json

    payload = json.loads(out.read_text())
    assert payload["strategy"] == "sma"
    assert payload["splits"] >= 1


def test_cli_requires_a_grid(toy_archive: Path):
    with pytest.raises(SystemExit, match="--grid is required"):
        walkforward.main(["--data-dir", str(toy_archive), "--symbol", "TOY-USDT"])


def test_cli_refuses_an_absurdly_large_grid(toy_archive: Path):
    with pytest.raises(SystemExit, match="combinations"):
        walkforward.main(
            [
                "--data-dir",
                str(toy_archive),
                "--symbol",
                "TOY-USDT",
                "--strategy",
                "sma",
                "--grid",
                "window=" + ",".join(str(v) for v in range(2, 500)),
            ]
        )
