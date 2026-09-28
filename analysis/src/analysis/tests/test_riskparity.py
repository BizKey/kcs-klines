"""The risk-parity book: selection, weights, the risk budget, and its bookkeeping."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from .. import data, riskparity
from ..engine import Costs
from .conftest import STEP, make_bars, write_archive

DAY = 86400
START = 1_507_161_600  # a Thursday, aligned to the daily grid
FREE = Costs(0)
PAID = Costs(fee_per_side=0.001)


def archive(tmp_path: Path, series: dict[str, list[float]], turnovers=None, name: str = "spot") -> Path:
    """A daily archive with one file per symbol, closes given, turnover settable."""
    root = tmp_path / name
    for symbol, closes in series.items():
        write_archive(
            root,
            symbol,
            "1d",
            make_bars(closes, start=START, step=DAY),
            turnovers=(turnovers or {}).get(symbol),
        )
    return root


def aligned_for(root: Path, timeframe: str = "1d", calendar: str | None = None):
    symbols = sorted(s for s, tf in data.available_series(root) if tf == timeframe)
    calendar = calendar or symbols[0]
    times = riskparity.read_series(root, calendar, timeframe).times
    return {
        symbol: riskparity.align(riskparity.read_series(root, symbol, timeframe), times, 3)
        for symbol in symbols
    }, times


def run(root: Path, **kwargs):
    aligned, times = aligned_for(root)
    defaults = dict(
        timeframe="1d",
        top=2,
        lookback=5,
        rebalance=10,
        weight_scheme="equal",
        vol_budget=None,
        vol_window=5,
        min_history=0,
        costs=PAID,
    )
    return riskparity.run_riskparity(aligned, times, **{**defaults, **kwargs})


# --- the two books ------------------------------------------------------------


def test_equal_weights_without_a_budget_are_the_passive_book(tmp_path: Path):
    """Nothing distinguishes the two books, so the two curves must be identical."""
    root = archive(tmp_path, {"AAA": [100 + i for i in range(60)], "BBB": [200 - i * 0.5 for i in range(60)]})
    result = run(root, weight_scheme="equal", vol_budget=None)
    assert result.equity == pytest.approx(result.benchmark_equity)
    assert result.fees_paid == pytest.approx(result.benchmark_fees)


def test_the_benchmark_holds_the_same_selection_at_full_size(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100 + i for i in range(80)], "BBB": [100.0] * 80})
    result = run(root, vol_budget=None)
    assert result.invested_mean == pytest.approx(1.0)
    assert result.equity == pytest.approx(result.benchmark_equity)


# --- the selection ------------------------------------------------------------


def test_the_universe_leaves_out_young_symbols(tmp_path: Path):
    old = make_bars([100.0 + i for i in range(120)], start=START, step=DAY)
    young = make_bars([50.0] * 40, start=START + 80 * DAY, step=DAY)
    root = tmp_path / "spot"
    write_archive(root, "OLD", "1d", old)
    write_archive(root, "YOUNG", "1d", young)
    aligned, times = aligned_for(root, calendar="OLD")
    assert aligned["YOUNG"].history_bars(len(times) - 1) == 39  # it only has 40 bars

    # with a 100-bar history requirement, only OLD is ever eligible
    result = run(root, top=2, min_history=100, rebalance=10)
    held = {symbol for rebalance in result.rebalances for symbol in rebalance.holdings}
    assert held == {"OLD"}


def test_the_universe_is_chosen_from_past_data_only(tmp_path: Path):
    """Rewrite the future and yesterday's selection must not move."""
    root = archive(tmp_path, {"AAA": [100.0] * 60, "BBB": [100.0] * 60}, turnovers={"AAA": [5.0] * 60, "BBB": [9.0] * 60})
    before = run(root, top=1, rebalance=10)
    # make AAA the busiest name, but only *after* the last decision
    bars = make_bars([100.0] * 60, start=START, step=DAY)
    write_archive(root, "AAA", "1d", bars, turnovers=[5.0] * 55 + [1000.0] * 5)
    after = run(root, top=1, rebalance=10)
    early_before = [r.holdings for r in before.rebalances if r.index <= 50]
    early_after = [r.holdings for r in after.rebalances if r.index <= 50 and r.index <= 50]
    # the last decision at index 50 looks back 5 bars, all of them untouched
    assert early_before == [r.holdings for r in after.rebalances if r.index <= 50][: len(early_before)]


def test_the_busiest_symbol_comes_first(tmp_path: Path):
    root = archive(
        tmp_path,
        {"AAA": [100.0] * 60, "BBB": [100.0] * 60},
        turnovers={"AAA": [1.0] * 60, "BBB": [9.0] * 60},
    )
    result = run(root, top=1, rebalance=10)
    assert result.rebalances
    assert all(r.holdings == ["BBB"] for r in result.rebalances)
    assert result.rebalances[0].candidates == 2


# --- the weights --------------------------------------------------------------


def test_inverse_volatility_gives_more_weight_to_the_calmer_symbol(tmp_path: Path):
    # both are real movers, one far calmer than the other; a pegged pair would be
    # dropped by the volatility floor before weights were ever computed
    calm = [100.0 * (1.03 if i % 2 else 0.97) for i in range(80)]
    wild = [100.0 * (1.35 if i % 2 else 0.74) for i in range(80)]
    root = archive(tmp_path, {"CALM": calm, "WILD": wild})
    result = run(root, top=2, weight_scheme="invvol", vol_budget=None, rebalance=20)
    assert result.rebalances
    weights = result.rebalances[-1].weights
    assert weights["CALM"] > weights["WILD"]
    assert sum(weights.values()) == pytest.approx(1.0)


def test_weights_always_sum_to_one_and_leave_the_rest_in_cash(tmp_path: Path):
    # a zigzag keeps the book genuinely volatile, so a 5% budget has to bite
    root = archive(tmp_path, {"AAA": [100.0 + 20 * (i % 2) for i in range(80)], "BBB": [100.0] * 80})
    result = run(root, top=2, vol_budget=0.05, rebalance=10)
    assert result.rebalances
    for rebalance in result.rebalances:
        assert 0 < rebalance.invested <= 1.0
        assert rebalance.scale <= 1.0
    assert result.vol_budget == 0.05
    assert result.invested_mean < 1.0


def test_the_budget_never_levers_the_book_up(tmp_path: Path):
    """A calm book asks for more size than it has; the scale must stay at 1."""
    root = archive(tmp_path, {"AAA": [100.0 * (1.0001**i) for i in range(120)], "BBB": [100.0] * 120})
    result = run(root, top=2, vol_budget=10.0, rebalance=10)
    assert result.rebalances
    assert all(rebalance.scale == pytest.approx(1.0) for rebalance in result.rebalances)


def test_a_wild_book_is_scaled_down_further_than_a_calm_one(tmp_path: Path):
    wild = archive(tmp_path, {"AAA": [100 * (1.4 if i % 2 else 0.7) ** (i // 2) for i in range(100)], "BBB": [100.0] * 100}, name="wild")
    calm = archive(tmp_path, {"AAA": [100.0 + 0.01 * i for i in range(100)], "BBB": [100.0] * 100}, name="calm")
    wild_result = run(wild, top=2, vol_budget=0.4, rebalance=10)
    calm_result = run(calm, top=2, vol_budget=0.4, rebalance=10)
    assert wild_result.invested_mean < calm_result.invested_mean


# --- timing and costs ---------------------------------------------------------


def test_a_decision_lands_on_the_close_after_it_is_taken(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0 + 2 * i for i in range(60)]})
    result = run(root, top=2, rebalance=10)
    assert result.rebalances
    for rebalance in result.rebalances:
        assert rebalance.filled_index == rebalance.index + 1
        assert result.times[rebalance.filled_index] == rebalance.time + DAY


def test_commission_is_charged_on_the_drifted_book(tmp_path: Path):
    """AAA triples between rebalances, so pulling it back to target costs real money."""
    # AAA triples between the second and third rebalance, so the book drifts and
    # has to be sold back down: that sale is what the commission lands on
    closes = [100.0] * 15 + [300.0] * 15
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * len(closes)})
    aligned, times = aligned_for(root)
    result = riskparity.run_riskparity(
        aligned,
        times,
        timeframe="1d",
        top=2,
        lookback=2,
        rebalance=10,
        weight_scheme="equal",
        vol_budget=None,
        vol_window=3,
        min_history=0,
        costs=PAID,
    )
    assert len(result.rebalances) >= 2
    # the first rebalance buys 50/50, the second must pull the tripled AAA back down
    assert result.rebalances[0].turnover == pytest.approx(1.0)
    assert result.rebalances[1].turnover > 0.4


def test_an_empty_book_costs_nothing_and_says_so(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0] * 40})
    result = run(root, top=1, min_history=1000, rebalance=10)
    assert result.rebalances == []
    assert result.equity == pytest.approx([1.0] * len(result.times))
    assert any("no symbol had enough history" in warning for warning in result.warnings)


# --- the summary and the artifacts -------------------------------------------


def test_the_summary_is_json_friendly(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    payload = run(root, vol_budget=0.4, rebalance=10).as_dict()
    assert payload["top"] == 2
    assert payload["weight_scheme"] == "equal"
    assert payload["vol_budget"] == 0.4
    assert payload["performance"]["total_return"] == pytest.approx(
        payload["benchmark_equal_weight"]["total_return"], abs=1.0
    )
    assert set(payload["last_rebalance"]) == {"time", "holdings", "weights", "invested"}


def test_the_report_names_the_selection_the_weights_and_the_budget(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    text = riskparity.render(run(root, vol_budget=0.4, top=1, rebalance=10))
    assert "top 1 of 2 symbols" in text
    assert "risk budget 40% a year" in text
    assert "equal-weight hold" in text
    assert "applied on the next close" in text


def test_the_artifact_name_says_which_book_it_was(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    equal = run(root, top=5, vol_budget=0.4)
    inverse = run(root, top=5, vol_budget=None, weight_scheme="invvol")
    assert riskparity.artifact_stem(equal) == "riskparity_eq5-b40_1d"
    assert riskparity.artifact_stem(inverse) == "riskparity_iv5-off_1d"
    assert riskparity.artifact_stem(equal) != riskparity.artifact_stem(inverse)


def test_the_artifacts_are_written(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    result = run(root, vol_budget=0.4, rebalance=10)
    csv_path, svg_path, json_path = tmp_path / "c.csv", tmp_path / "c.svg", tmp_path / "c.json"
    riskparity.write_equity(csv_path, result)
    riskparity.write_chart(svg_path, result)
    riskparity.write_metrics(json_path, result)
    rows = list(csv.DictReader(csv_path.open()))
    assert list(rows[0]) == ["time_utc", "portfolio", "equal_weight_hold", "rebalanced", "invested"]
    assert len(rows) == len(result.times)
    svg = svg_path.read_text()
    assert svg.startswith("<svg") and 'class="final-level"' in svg
    assert json.loads(json_path.read_text())["achieved_vol"] == pytest.approx(result.achieved_vol)


# --- validation ---------------------------------------------------------------


def test_bad_settings_are_rejected(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0] * 40, "BBB": [100.0] * 40})
    aligned, times = aligned_for(root)
    with pytest.raises(ValueError, match="weight scheme"):
        riskparity.run_riskparity(aligned, times, timeframe="1d", weight_scheme="magic")
    with pytest.raises(ValueError, match="top must be"):
        riskparity.run_riskparity(aligned, times, timeframe="1d", top=0)
    with pytest.raises(ValueError, match="three bars"):
        riskparity.run_riskparity(aligned, times[:2], timeframe="1d")


def test_durations_are_read_the_way_the_cli_takes_them():
    assert riskparity.bars_for("30d", "1d") == 30
    assert riskparity.bars_for("3y", "1d") == 1095
    assert riskparity.bars_for("7d", "1h") == 168
    with pytest.raises(SystemExit, match="cannot read"):
        riskparity.bars_for("soon", "1d")
    with pytest.raises(SystemExit, match="not a duration"):
        riskparity.bars_for("none", "1d")


def test_the_risk_budget_can_be_switched_off():
    assert riskparity.budget_for("0.4") == 0.4
    assert riskparity.budget_for("none") is None
    with pytest.raises(SystemExit, match="positive"):
        riskparity.budget_for("0")
    with pytest.raises(SystemExit, match="number"):
        riskparity.budget_for("lots")


def test_alignment_forward_fills_a_price_until_it_goes_stale(tmp_path: Path):
    root = tmp_path / "spot"
    bars = make_bars([100.0] * 10, start=START, step=DAY)
    write_archive(root, "AAA", "1d", bars[:3])
    series = riskparity.read_series(root, "AAA", "1d")
    calendar = [START + i * DAY for i in range(10)]
    row = riskparity.align(series, calendar, max_age=2)
    assert row.prices[0] == pytest.approx(100.0)
    assert row.prices[4] == pytest.approx(100.0)  # two days stale, still usable
    assert row.prices[5] != row.prices[5]  # three days stale: out of the universe
    assert row.first_index == 0


# --- the CLI ------------------------------------------------------------------


def test_cli_prints_a_book_and_writes_it(tmp_path: Path, capsys):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(80)], "BBB": [100.0] * 80}, name="spot")
    out = tmp_path / "out"
    code = riskparity.main(
        [
            "--data-dir", str(root), "--timeframe", "1d", "--top", "2",
            "--min-history", "0d", "--lookback", "5d", "--rebalance", "10d",
            "--vol-window", "5d", "--vol-budget", "0.4", "--calendar", "AAA",
            "--out-dir", str(out), "--json",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "top 2 by turnover" in printed
    assert "reading 2 1d series" in printed
    names = sorted(path.name for path in out.iterdir())
    assert names == [
        "riskparity_eq2-b40_1d_equity.csv",
        "riskparity_eq2-b40_1d_equity.svg",
        "riskparity_eq2-b40_1d_metrics.json",
    ]


def test_cli_explains_a_missing_calendar(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0] * 40})
    with pytest.raises(SystemExit, match="NOPE"):
        riskparity.main(["--data-dir", str(root), "--calendar", "NOPE", "--no-artifacts"])


def test_cli_rejects_a_nonsense_duration(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0] * 40})
    with pytest.raises(SystemExit, match="cannot read"):
        riskparity.main(["--data-dir", str(root), "--rebalance", "soon", "--no-artifacts"])


def test_a_stablecoin_is_not_a_position(tmp_path: Path):
    """Inverse volatility would park the book in the calmest pair — 91.6% measured."""
    stable = [1.0 + 0.00001 * (i % 3) for i in range(80)]      # a pegged pair
    normal = [100.0 + 10 * (i % 2) for i in range(80)]         # a real one
    root = archive(tmp_path, {"USDC-USDT": stable, "AAA-USDT": normal})
    result = run(root, top=2, weight_scheme="invvol", vol_budget=None, rebalance=20)
    assert result.rebalances
    weights = result.rebalances[-1].weights
    assert "USDC-USDT" not in weights  # below the volatility floor: excluded, not weighted
    assert weights["AAA-USDT"] == pytest.approx(1.0)


def test_no_single_name_may_dominate_the_book(tmp_path: Path):
    root = archive(
        tmp_path,
        {"CALM": [100.0 * (1.015 if i % 2 else 0.985) for i in range(80)],
         "MID": [100.0 * (1.05 if i % 2 else 0.95) for i in range(80)],
         "WILD": [100.0 * (1.3 if i % 2 else 0.77) for i in range(80)],
         "EDGE": [100.0 * (1.12 if i % 2 else 0.89) for i in range(80)]},
    )
    result = run(root, top=4, weight_scheme="invvol", vol_budget=None, rebalance=20, max_weight=0.25)
    assert result.rebalances
    weights = result.rebalances[-1].weights
    assert weights  # the calmest name is below the floor, so three are left
    assert max(weights.values()) <= 0.25 + 1e-9
    assert sum(weights.values()) <= 1.0 + 1e-9


def test_the_cap_returns_a_hand_computed_book():
    """Three names capped at a quarter can hold three quarters; the rest is cash."""
    capped = riskparity.cap_weights({"A": 0.9, "B": 0.05, "C": 0.05}, 0.25)
    assert capped == {"A": pytest.approx(0.25), "B": pytest.approx(0.25), "C": pytest.approx(0.25)}
    assert sum(capped.values()) == pytest.approx(0.75)
    assert riskparity.cap_weights({"A": 1.0}, 0.25) == {"A": 0.25}
    assert riskparity.cap_weights({"A": 0.6, "B": 0.4}, 1.0) == {"A": 0.6, "B": 0.4}  # no cap


def test_the_cap_grows_with_a_small_book():
    """A two-name book is not capped, or half the capital would sit in cash."""
    assert riskparity.auto_max_weight(2) == 1.0
    assert riskparity.auto_max_weight(5) == pytest.approx(0.4)
    assert riskparity.auto_max_weight(20) == pytest.approx(0.25)
    assert riskparity.auto_max_weight(100) == pytest.approx(0.25)


# --- reporting only the last stretch -----------------------------------------


def test_a_window_is_the_tail_of_the_full_book(tmp_path: Path):
    closes = [100.0 * (1.01 ** i) for i in range(120)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0 + (i % 7) for i in range(120)]})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=20, rebalance=20,
        vol_budget=None, vol_window=10, min_history=0, costs=PAID,
    )
    windowed = riskparity.restrict(full, first=times[-40], text="40d", label="(last 40d)")
    assert windowed.times == times[-40:]  # both ends included
    assert windowed.equity[0] == pytest.approx(1.0)
    assert windowed.equity[-1] == pytest.approx(full.equity[-1] / full.equity[-40])
    assert windowed.benchmark_equity[-1] == pytest.approx(
        full.benchmark_equity[-1] / full.benchmark_equity[-40]
    )


def test_a_window_keeps_only_its_own_rebalances_and_its_own_fees(tmp_path: Path):
    closes = [100.0 * (1.005 ** i) for i in range(120)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * 120})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=20, rebalance=20,
        vol_budget=None, vol_window=10, min_history=0, costs=PAID,
    )
    assert len(full.rebalances) >= 3
    windowed = riskparity.restrict(full, first=times[-40], text="40d")
    assert 0 < len(windowed.rebalances) < len(full.rebalances)
    assert all(0 <= r.filled_index < len(windowed.times) for r in windowed.rebalances)
    assert windowed.fees_paid < full.fees_paid
    assert windowed.fees_paid == pytest.approx(
        sum(r.cost for r in windowed.rebalances) / full.equity[len(times) - 40]
    )


def test_a_window_with_a_buy_and_hold_line_still_restates_its_fees_correctly(tmp_path: Path):
    """The reference curve must not become the divisor for the window's commission.

    Naming a local `base` inside the rebase shadowed the window's own equity base, and
    a real run's fees came out 2.5x too small — invisible in a fixture whose equity is
    near 1.0, which is why this one is made to grow first.
    """
    closes = [100.0 * (1.01 ** i) for i in range(120)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * 120})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=20, rebalance=10,
        vol_budget=None, vol_window=10, min_history=0, costs=PAID, buy_hold="AAA",
    )
    assert full.equity[-41] > 1.2  # the divisor and the hold curve are not interchangeable
    assert full.buy_hold_equity[0] == pytest.approx(1.0 - PAID.rate)
    windowed = riskparity.restrict(full, first=times[-40], text="40d")
    assert windowed.fees_paid == pytest.approx(
        sum(r.cost for r in windowed.rebalances) / full.equity[len(times) - 40]
    )
    assert windowed.buy_hold_equity[0] == pytest.approx(1.0 - PAID.rate)


def test_a_window_indexes_its_own_timeline(tmp_path: Path):
    """Markers and rows must address the window, not the run it was cut from."""
    closes = [100.0 * (1.005 ** i) for i in range(140)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * 140})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=20, rebalance=20,
        vol_budget=None, vol_window=10, min_history=0, costs=PAID,
    )
    windowed = riskparity.restrict(full, first=times[-45], text="45d")
    assert windowed.rebalances
    for reb in windowed.rebalances:
        assert 0 <= reb.filled_index < len(windowed.times)
        assert 0 <= reb.index <= reb.filled_index
    # …and the chart draws, which it cannot if the markers point past the window
    path = tmp_path / "window.svg"
    riskparity.write_chart(path, windowed)
    text = path.read_text()
    assert text.count('class="position-change"') == len(windowed.rebalances)
    # the equity CSV lines its rebalance rows up with its own bars
    csv_path = tmp_path / "window.csv"
    riskparity.write_equity(csv_path, windowed)
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == len(windowed.times)


def test_a_window_says_the_book_was_taken_as_it_stood(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(80)], "BBB": [100.0] * 80})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=10, rebalance=10,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID,
    )
    windowed = riskparity.restrict(full, first=times[-30], text="30d")
    assert any("as it stood" in w for w in windowed.warnings)
    assert not any("2017" in w for w in windowed.warnings)  # the old history warning is gone


def test_a_window_at_the_very_start_is_the_same_book(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=10,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID,
    )
    assert riskparity.restrict(full, first=times[0], text="all") is full


def test_a_window_too_short_to_measure_is_rejected(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=10,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID,
    )
    with pytest.raises(ValueError, match="give a longer period"):
        riskparity.restrict(full, first=times[-2], text="1d")


def test_the_window_shows_up_in_the_artifact_name(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=10,
        vol_budget=0.4, vol_window=5, min_history=0, costs=PAID,
    )
    windowed = riskparity.restrict(full, first=times[-40], text="40d")
    assert riskparity.artifact_stem(full) != riskparity.artifact_stem(windowed)
    assert riskparity.artifact_stem(windowed) == "riskparity_eq2-b40_last40d_1d"


def test_cli_reports_only_the_last_stretch(tmp_path: Path, capsys):
    closes = [100.0 * (1.004 ** i) for i in range(200)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * 200})
    code = riskparity.main(
        [
            "--data-dir", str(root), "--timeframe", "1d", "--top", "2", "--min-history", "0d",
            "--lookback", "20d", "--rebalance", "20d", "--vol-window", "10d",
            "--vol-budget", "none", "--calendar", "AAA", "--last", "60d", "--no-artifacts",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "(last 60d)" in printed
    assert "the window starts with the book as it stood" in printed


def test_cli_rejects_a_window_that_is_too_short(tmp_path: Path):
    root = archive(tmp_path, {"AAA": [100.0 + i for i in range(60)], "BBB": [100.0] * 60})
    with pytest.raises(SystemExit):
        riskparity.main(
            [
                "--data-dir", str(root), "--calendar", "AAA", "--min-history", "0d",
                "--lookback", "5d", "--rebalance", "10d", "--last", "1d", "--no-artifacts",
            ]
        )


def test_a_duration_shorter_than_a_bar_is_explained_not_silently_clamped():
    """`6m` is six minutes: the mistake should say so, not become one bar."""
    with pytest.raises(SystemExit, match="`m` is a minute and `mon` a month"):
        riskparity.bars_for("6m", "1d")
    with pytest.raises(SystemExit, match="`m` is a minute and `mon` a month"):
        riskparity.bars_for("30m", "1d")
    assert riskparity.bars_for("6mon", "1d") == 180
    assert riskparity.bars_for("0d", "1d") == 0  # no age filter, for --min-history only
    with pytest.raises(SystemExit, match="must cover at least one 1d bar"):
        riskparity.main(["--data-dir", "nope", "--lookback", "0d"])
    assert riskparity.bars_for("12h", "1h") == 12


def test_the_chart_says_what_its_red_and_green_lines_mean(tmp_path: Path):
    """A red line on a rebalance chart is de-risking, not an exit: the footer says so."""
    calm = [100.0 * (1.001**i) for i in range(60)]
    wild = [calm[-1] * (1.4 if i % 2 else 0.7) ** (i // 2 + 1) for i in range(60)]
    root = archive(tmp_path, {"AAA": calm + wild, "BBB": [100.0] * 120})
    result = run(root, top=2, vol_budget=0.4, rebalance=10, vol_window=5)
    previous, up, down, same = 0.0, 0, 0, 0
    for rebalance in result.rebalances:
        change = rebalance.invested - previous
        up, down, same = (
            (up + 1, down, same) if change > 0.005
            else (up, down + 1, same) if change < -0.005
            else (up, down, same + 1)
        )
        previous = rebalance.invested
    assert up and down  # the fixture must actually de-risk part way through

    path = tmp_path / "chart.svg"
    riskparity.write_chart(path, result)
    text = path.read_text()
    assert f"rebalances: more invested = green ({up}), less invested = red ({down})" in text
    if same:
        assert f"size unchanged = grey ({same})" in text
    assert "position changes" not in text  # these are rebalances, not entries and exits
    # a red line shows what it took off, not just where the book ended up
    assert "100% → " in text or "→ " in text


# --- the passive reference ----------------------------------------------------


def test_buy_and_hold_is_the_price_ratio_less_one_entry_side(tmp_path: Path):
    """Hand-computed: doubling in price, one commission paid on the way in."""
    closes = [100.0 * (1.02**i) for i in range(40)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * 40})
    result = run(root, top=2, buy_hold="AAA")
    ratio = closes[-1] / closes[0]
    assert ratio == pytest.approx(1.02**39)
    assert result.buy_hold is not None
    assert result.buy_hold.final_equity == pytest.approx(ratio * (1.0 - PAID.rate))
    assert result.buy_hold_label == "AAA"
    # the bench hold of the pair, by contrast, is diluted by the flat symbol
    assert result.benchmark.final_equity < result.buy_hold.final_equity


def test_a_late_listing_sits_in_cash_until_it_has_a_price(tmp_path: Path):
    """A symbol listed after the calendar starts cannot have been held before it."""
    root = archive(tmp_path, {"AAA": [100.0] * 40})
    write_archive(
        root, "BBB", "1d", make_bars([100.0, 200.0], start=START + 30 * DAY, step=DAY)
    )
    aligned, times = aligned_for(root)
    result = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=1, lookback=5, rebalance=10,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID, buy_hold="BBB",
    )
    assert result.buy_hold_equity[0] == pytest.approx(1.0)     # in cash, no fee yet
    assert result.buy_hold_equity[30] == pytest.approx(1.0 - PAID.rate)
    assert result.buy_hold.final_equity == pytest.approx(2.0 * (1.0 - PAID.rate))


def test_the_reference_can_be_switched_off_or_named(tmp_path: Path, capsys):
    closes = [100.0 * (1.01**i) for i in range(40)]
    root = archive(tmp_path, {"AAA": closes, "BBB": [100.0] * 40})
    common = ["--data-dir", str(root), "--calendar", "AAA", "--top", "1", "--vol-budget", "none",
              "--lookback", "5d", "--rebalance", "10d", "--vol-window", "5d",
              "--min-history", "0d", "--no-artifacts"]
    assert riskparity.main([*common, "--buy-hold", "none"]) == 0
    assert "buy & hold" not in capsys.readouterr().out
    assert riskparity.main(common) == 0                       # defaults to the calendar symbol
    out = capsys.readouterr().out
    assert "buy & hold AAA" in out
    with pytest.raises(SystemExit, match="no series for it on the calendar"):
        riskparity.main([*common, "--buy-hold", "NOPE-USDT"])


# --- the trend rule: risk parity plus a signal --------------------------------


def crashing(tmp_path: Path, name: str = "trend"):
    """AAA climbs, then falls hard; BBB never moves. 80 daily bars."""
    up = [100.0 * (1.01**i) for i in range(50)]
    down = [up[-1] * (0.97**i) for i in range(1, 31)]
    return archive(tmp_path, {"AAA": up + down, "BBB": [100.0] * 80}, name=name)


def test_a_name_that_loses_its_trend_is_sold_on_the_next_close(tmp_path: Path):
    root = crashing(tmp_path)
    aligned, times = aligned_for(root)
    closes = [float(v) for v in aligned["AAA"].prices]
    # the first close that is below its level five bars earlier
    signal = next(i for i in range(5, len(closes)) if closes[i] <= closes[i - 5])
    result = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=40,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID, trend=5,
    )
    exits = [i for i, _, _ in result.trend_exits]
    assert exits, "the fall must trigger the rule"
    assert exits[0] == signal + 1          # decided on the signal bar, sold on the next close
    assert not [s for i, s, _ in result.trend_exits if i == signal]
    assert result.invested_by_bar[exits[0]] < result.invested_by_bar[signal]
    assert result.trend_bars == 5


def test_an_exited_name_comes_back_only_on_the_rebalance_grid(tmp_path: Path):
    """Entries stay on the slow clock; only exits act between rebalances."""
    root = crashing(tmp_path)
    aligned, times = aligned_for(root)
    result = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=40,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID, trend=5,
    )
    fills = {r.filled_index for r in result.rebalances}
    exits = {i for i, _, _ in result.trend_exits}
    entries = [
        i for i in range(1, len(result.invested_by_bar))
        if result.invested_by_bar[i - 1] <= 0 < result.invested_by_bar[i]
    ]
    assert entries, "the book must come back at some point"
    assert not (exits & fills), "an exit is not a rebalance"
    assert all(i in fills for i in entries), (entries, sorted(fills))


def test_the_trend_rule_never_reaches_into_the_benchmark(tmp_path: Path):
    """Gating the benchmark too would hide the very rule being measured."""
    root = crashing(tmp_path)
    aligned, times = aligned_for(root)
    plain = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=20,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID,
    )
    gated = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=20,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID, trend=5,
    )
    assert gated.gated_out or gated.trend_exits
    assert gated.benchmark_equity == pytest.approx(plain.benchmark_equity)


def test_without_the_rule_nothing_moves(tmp_path: Path):
    """`--trend none` must be the old module, to the last bit."""
    root = crashing(tmp_path)
    aligned, times = aligned_for(root)
    kwargs = dict(
        timeframe="1d", top=2, lookback=5, rebalance=20, vol_budget=0.4,
        vol_window=5, min_history=0, costs=PAID,
    )
    before = riskparity.run_riskparity(aligned, times, **kwargs)
    after = riskparity.run_riskparity(aligned, times, trend=None, **kwargs)
    assert after.equity == before.equity
    assert after.trend_exits == [] and after.gated_out == []
    assert after.invested_bar_mean == before.invested_bar_mean  # flat only before the first fill
    gated = riskparity.run_riskparity(aligned, times, trend=5, **kwargs)
    assert gated.flat_bar_share > before.flat_bar_share
    assert gated.invested_bar_mean < before.invested_bar_mean


def test_the_rule_never_looks_ahead(tmp_path: Path):
    """Rewriting a later bar must not change the exits decided before it."""
    root = crashing(tmp_path)
    aligned, times = aligned_for(root)
    kwargs = dict(
        timeframe="1d", top=2, lookback=5, rebalance=20, vol_budget=None,
        vol_window=5, min_history=0, costs=PAID, trend=5,
    )
    original = riskparity.run_riskparity(aligned, times, **kwargs)
    cut = 60
    tampered = {s: replace_row(row, cut) for s, row in aligned.items()}
    rewritten = riskparity.run_riskparity(tampered, times, **kwargs)
    assert [i for i, _, _ in original.trend_exits if i <= cut] == [
        i for i, _, _ in rewritten.trend_exits if i <= cut
    ]


def replace_row(row, index: int):
    """One price multiplied by ten at `index` — a look-ahead would notice."""
    prices = list(row.prices)
    if prices[index] == prices[index]:
        prices[index] = prices[index] * 10
    return type(row)(**{**row.__dict__, "prices": prices})


def test_the_rule_is_named_in_the_report_the_csv_and_the_artifact(tmp_path: Path, capsys):
    root = crashing(tmp_path)
    out_dir = tmp_path / "out"
    common = ["--data-dir", str(root), "--calendar", "AAA", "--top", "2", "--vol-budget", "none",
              "--lookback", "5d", "--rebalance", "20d", "--vol-window", "5d", "--min-history", "0d",
              "--out-dir", str(out_dir)]
    assert riskparity.main([*common, "--trend", "5d"]) == 0
    printed = capsys.readouterr().out
    assert "trend rule" in printed and "at work" in printed
    stem = "riskparity_eq2-off_tr5_1d"
    rows = list(csv.DictReader((out_dir / f"{stem}_equity.csv").open()))
    # the exposure is written for every bar, not only on the rebalance rows
    assert len(rows) == 80 and all(row["invested"] for row in rows)
    assert min(float(row["invested"]) for row in rows) == pytest.approx(0.0)
    assert "trend exit" in (out_dir / f"{stem}_equity.svg").read_text()
    assert riskparity.main([*common, "--trend", "none"]) == 0
    assert "trend rule" not in capsys.readouterr().out


def test_a_trend_lookback_shorter_than_a_bar_is_rejected(tmp_path: Path):
    with pytest.raises(SystemExit, match="`m` is a minute"):
        riskparity.main(["--trend", "5m"])


def test_a_window_counts_the_commission_an_exit_paid(tmp_path: Path):
    """Exits cost money between rebalances, and the window has to own that."""
    root = crashing(tmp_path)
    aligned, times = aligned_for(root)
    full = riskparity.run_riskparity(
        aligned, times, timeframe="1d", top=2, lookback=5, rebalance=20,
        vol_budget=None, vol_window=5, min_history=0, costs=PAID, trend=5,
    )
    exits = [i for i, _, _ in full.trend_exits]
    assert exits
    windowed = riskparity.restrict(full, first=times[exits[0] - 1], text="20d")
    base = full.equity[len(times) - len(windowed.times)]
    assert windowed.fees_paid == pytest.approx(
        sum(full.cost_by_bar[i] for i, t in enumerate(times) if t >= times[exits[0] - 1]) / base
    )
    inside_exits = sum(paid for i, _, paid in full.trend_exits if times[i] >= times[exits[0] - 1])
    assert inside_exits > 0
    rebalance_only = sum(r.cost for r in windowed.rebalances) / base
    assert windowed.fees_paid > rebalance_only
