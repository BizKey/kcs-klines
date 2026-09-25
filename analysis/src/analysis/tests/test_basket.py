"""A basket of named symbols: the window, the weights, and the combined curve."""

from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path

import pytest

from .. import basket, data, engine, report
from ..engine import Costs, run_backtest
from ..strategies import SmaTrend, get_strategy
from .conftest import START, STEP, make_bars, ramp, wavy, write_archive

FREE = Costs(0)
PAID = Costs(fee_per_side=0.001)


def legs(**closes: list[float]) -> dict[str, list]:
    """`legs(A=[1,2,3])` -> `{"A": bars}` on the shared hourly grid."""
    return {symbol: make_bars(values) for symbol, values in closes.items()}


# --- the window ---------------------------------------------------------------


def test_the_window_shrinks_to_the_latest_listing():
    long_leg = make_bars(ramp(120))
    late_leg = make_bars(ramp(60), start=START + 60 * STEP)
    result = basket.run_basket(
        {"OLD-USDT": long_leg, "NEW-USDT": late_leg}, "sma", {"window": 2}, "1h", costs=FREE
    )
    assert result.times[0] == START + 60 * STEP
    assert result.times[-1] == long_leg[-1].time
    assert len(result.times) == 60


def test_the_timeline_is_the_bars_every_leg_has():
    left = make_bars(wavy(40))
    right = make_bars(wavy(40))
    missing = right[7].time
    del right[7]  # the second leg simply has no bar there
    result = basket.run_basket({"A-USDT": left, "B-USDT": right}, "sma", {"window": 2}, "1h", costs=FREE)
    assert missing not in result.times
    assert set(result.times) <= {bar.time for bar in right}
    assert result.dropped == 1  # one leg-bar inside the window did not survive


def test_legs_that_never_overlap_are_rejected():
    early = make_bars(ramp(10))
    late = make_bars(ramp(10), start=START + 500 * STEP)
    with pytest.raises(ValueError, match="share no window"):
        basket.run_basket({"A": early, "B": late}, "sma", {"window": 2}, "1h")


def test_a_window_of_two_bars_is_not_a_backtest():
    left = make_bars(ramp(10))
    right = make_bars(ramp(10), start=START + 8 * STEP)
    with pytest.raises(ValueError, match="not enough to measure"):
        basket.run_basket({"A": left, "B": right}, "sma", {"window": 2}, "1h")


# --- the legs and the combined curve ------------------------------------------


def test_every_leg_gets_the_same_weight():
    result = basket.run_basket(
        legs(A=wavy(50), B=wavy(50, drift=0.1), C=wavy(50, drift=-0.05)),
        "sma",
        {"window": 3},
        "1h",
        costs=FREE,
    )
    assert [leg.weight for leg in result.legs] == [pytest.approx(1 / 3)] * 3
    assert sum(leg.weight for leg in result.legs) == pytest.approx(1.0)


def test_the_basket_is_the_weighted_sum_of_its_legs():
    """The identity the whole module rests on: no hidden cross-leg cost."""
    result = basket.run_basket(
        legs(A=wavy(60), B=wavy(60, drift=-0.2, period=5.0), C=ramp(60)),
        "sma",
        {"window": 4},
        "1h",
        costs=PAID,
    )
    for k in range(len(result.times)):
        expected = 1.0 + sum(leg.weight * (leg.equity[k] - 1.0) for leg in result.legs)
        assert result.equity[k] == pytest.approx(expected, rel=1e-12)


def test_contributions_add_up_to_the_basket_return():
    result = basket.run_basket(
        legs(A=wavy(80), B=wavy(80, drift=0.2), C=wavy(80, drift=-0.1)),
        "sma",
        {"window": 3},
        "1h",
        costs=PAID,
    )
    assert sum(leg.contribution for leg in result.legs) == pytest.approx(
        result.performance.total_return, rel=1e-12
    )


def test_two_identical_legs_are_just_that_leg():
    one = get_strategy("sma", window=3)
    bars = make_bars(wavy(60))
    single = run_backtest(bars, one.targets(bars), "1h", FREE, label="sma")
    result = basket.run_basket({"A": bars, "B": bars}, "sma", {"window": 3}, "1h", costs=FREE)
    expected = single.performance.final_equity
    assert result.performance.final_equity == pytest.approx(expected, rel=1e-12)
    assert result.legs[0].equity == pytest.approx(result.legs[1].equity)


def test_the_capital_is_split_evenly_at_the_start_and_drifts_with_performance():
    """Equal target weights are not equal money once the legs diverge."""
    riser = make_bars(ramp(60, start=100.0, step=2.0))
    flat = make_bars([100.0] * 60)
    result = basket.run_basket({"UP": riser, "FLAT": flat}, "sma", {"window": 2}, "1h", costs=FREE)

    assert [leg.weight for leg in result.legs] == [pytest.approx(0.5)] * 2  # the target
    shares = result.leg_shares
    assert sum(shares.values()) == pytest.approx(1.0)
    assert shares["UP"] > 0.5  # the winner owns more than its 50% target
    assert shares["FLAT"] < 0.5  # the laggard owns less
    assert result.legs[1].equity[-1] == pytest.approx(1.0)  # never in the market, so never moved


def test_a_share_is_the_legs_money_over_the_baskets_money():
    riser = make_bars(ramp(60, start=100.0, step=2.0))
    flat = make_bars([100.0] * 60)
    result = basket.run_basket({"UP": riser, "FLAT": flat}, "sma", {"window": 2}, "1h", costs=FREE)
    up = result.legs[0]
    assert result.leg_shares["UP"] == pytest.approx(
        0.5 * up.equity[-1] / result.performance.final_equity
    )


def test_capital_in_market_is_the_weighted_leg_exposure():
    riser = make_bars(ramp(60, start=100.0, step=2.0))
    result = basket.run_basket({"A": riser, "B": riser}, "sma", {"window": 2}, "1h", costs=FREE)
    assert result.capital_in_market == pytest.approx(sum(0.5 * leg.exposure for leg in result.legs))
    assert result.capital_in_market > 0.9  # a rising series keeps the SMA rule long

    still = basket.run_basket(
        {"A": make_bars([100.0] * 60), "B": make_bars([100.0] * 60)},
        "sma",
        {"window": 2},
        "1h",
        costs=FREE,
    )
    assert still.capital_in_market == pytest.approx(0.0)


def test_a_wiped_out_basket_reports_zero_shares():
    """A geared leg can take the account to zero; the shares must not divide by it."""
    panel = make_bars([100.0, 100.0, 100.0, 100.0])
    result = basket.run_basket({"A": panel, "B": panel}, "sma", {"window": 2}, "1h", costs=FREE)
    result.performance = dataclasses.replace(result.performance, final_equity=0.0, total_return=-1.0)
    assert result.leg_shares == {"A": 0.0, "B": 0.0}


def test_legs_are_rebased_to_one_at_the_window_start():
    """A leg that was already trading before the window starts the basket at 1.0."""
    long_leg = make_bars(ramp(200))  # rises steadily, so an SMA leg is long and profitable
    late_leg = make_bars(ramp(60), start=START + 140 * STEP)
    result = basket.run_basket({"OLD": long_leg, "NEW": late_leg}, "sma", {"window": 2}, "1h", costs=FREE)

    sliced = [bar for bar in long_leg if bar.time <= result.times[-1]]
    strategy = get_strategy("sma", window=2)
    raw = run_backtest(sliced, strategy.targets(sliced), "1h", FREE, label="sma")
    index = {bar.time: i for i, bar in enumerate(sliced)}
    raw_at_start = raw.equity[index[result.times[0]]]

    assert raw_at_start > 1.0  # it had already made money before the basket existed
    assert result.legs[0].equity[0] == pytest.approx(1.0)  # and that gain is not the basket's
    assert result.legs[0].equity[-1] == pytest.approx(raw.performance.final_equity / raw_at_start)
    assert result.legs[1].equity[0] == pytest.approx(1.0)  # the late leg starts where it listed


def test_a_flat_market_leaves_the_basket_flat():
    result = basket.run_basket(
        {"A": make_bars([100.0] * 40), "B": make_bars([50.0] * 40)},
        "sma",
        {"window": 3},
        "1h",
        costs=PAID,
    )
    assert result.equity == pytest.approx([1.0] * len(result.times))
    assert result.performance.total_return == pytest.approx(0.0)
    assert result.fees_paid == pytest.approx(0.0)


def test_a_leg_that_never_trades_costs_nothing():
    result = basket.run_basket(
        {"A": make_bars([100.0] * 30), "B": make_bars([100.0] * 30)},
        "sma",
        {"window": 3},
        "1h",
        costs=PAID,
    )
    assert all(leg.trades == 0 for leg in result.legs)
    assert all(leg.fees == pytest.approx(0.0) for leg in result.legs)


# --- the benchmark ------------------------------------------------------------


def test_the_benchmark_is_the_same_names_bought_and_held():
    """Constant prices: the basket earns nothing, the benchmark pays two sides."""
    result = basket.run_basket(
        {"A": make_bars([100.0] * 30), "B": make_bars([200.0] * 30)},
        "sma",
        {"window": 3},
        "1h",
        costs=PAID,
    )
    assert result.benchmark_equity == pytest.approx([1.0] * len(result.times))
    assert result.benchmark.total_return == pytest.approx(0.999**2 - 1.0, rel=1e-12)
    assert result.benchmark.final_equity < 1.0


def test_the_benchmark_tracks_the_average_of_the_legs_price_paths():
    # opens[0] is far below opens[1]: a listing bar, which the benchmark must skip.
    listing = make_bars([10.0, 10.0, 10.0, 10.0], opens=[1.0, 10.0, 10.0, 10.0])
    flat = make_bars([100.0] * 4)
    result = basket.run_basket({"UP": listing, "FLAT": flat}, "sma", {"window": 2}, "1h", costs=FREE)
    expected_last = 0.5 * (listing[-1].close / listing[1].open) + 0.5 * 1.0
    assert result.benchmark_equity[0] == pytest.approx(1.0)  # cash on the first bar
    assert result.benchmark_equity[-1] == pytest.approx(expected_last, rel=1e-12)
    assert expected_last == pytest.approx(1.0)  # 10 -> 10, not 1 -> 10


def test_the_benchmark_ignores_a_listing_bar_the_strategy_cannot_trade():
    """The SUI/PYTH case: a 12.8x first bar must not become a 12.8x benchmark."""
    listing = make_bars([12.825, 1.2825, 1.2825, 1.2825], opens=[0.1, 1.2825, 1.2825, 1.2825])
    result = basket.run_basket({"SUI-USDT": listing}, "sma", {"window": 2}, "1h", costs=FREE)
    assert result.benchmark_equity == pytest.approx([1.0] * 4)
    assert result.benchmark.total_return == pytest.approx(0.0)


def test_one_leg_matches_the_single_series_backtest_exactly():
    """`kcs-basket --symbols X` must say what `kcs-backtest --symbol X` says."""
    bars = make_bars(wavy(120, amplitude=9.0, period=4.0))
    strategy = get_strategy("sma", window=5)
    single = run_backtest(bars, strategy.targets(bars), "1h", PAID, label="sma")
    result = basket.run_basket({"TOY": bars}, "sma", {"window": 5}, "1h", costs=PAID)

    assert result.performance.final_equity == pytest.approx(single.performance.final_equity, rel=1e-12)
    assert result.benchmark.final_equity == pytest.approx(single.benchmark.performance.final_equity, rel=1e-12)
    assert result.legs[0].trades == len(single.closed_trades)
    assert result.legs[0].fees == pytest.approx(single.fees_paid, rel=1e-12)


def test_one_leg_with_an_open_position_still_matches_the_single_series_backtest():
    """The trade count must not include the leg still open at the end."""
    bars = make_bars(ramp(80))  # rises all the way, so the leg is long at the last bar
    strategy = get_strategy("sma", window=3)
    single = run_backtest(bars, strategy.targets(bars), "1h", PAID, label="sma")
    assert single.open_position  # the case this test exists for
    result = basket.run_basket({"TOY": bars}, "sma", {"window": 3}, "1h", costs=PAID)
    assert result.legs[0].trades == len(single.closed_trades)
    assert result.legs[0].open_at_end
    assert any("still in the market" in warning for warning in result.warnings)


# --- commission accounting ----------------------------------------------------


def test_fees_in_window_matches_the_engine_when_the_window_is_everything():
    """The identity that keeps the basket's fee column honest as the engine changes."""
    bars = make_bars(wavy(120, amplitude=8.0, period=3.0))
    strategy = SmaTrend(window=3)
    run = run_backtest(bars, strategy.targets(bars), "1h", PAID, label="sma")
    assert engine.fees_in_window(run, PAID.rate, 0) == pytest.approx(run.fees_paid, rel=1e-12)
    assert run.fees_paid > 0


def test_fees_in_window_adds_up_across_a_split():
    bars = make_bars(wavy(120, amplitude=8.0, period=3.0))
    strategy = SmaTrend(window=3)
    run = run_backtest(bars, strategy.targets(bars), "1h", PAID, label="sma")
    first = engine.fees_in_window(run, PAID.rate, 0) - engine.fees_in_window(run, PAID.rate, 60)
    second = engine.fees_in_window(run, PAID.rate, 60)
    assert first + second == pytest.approx(run.fees_paid, rel=1e-12)
    assert second < run.fees_paid


def test_free_costs_charge_no_fees():
    bars = make_bars(wavy(60))
    strategy = SmaTrend(window=3)
    run = run_backtest(bars, strategy.targets(bars), "1h", FREE, label="sma")
    assert engine.fees_in_window(run, 0.0, 0) == 0.0


# --- the inputs ---------------------------------------------------------------


def test_symbols_are_parsed_and_tidied():
    assert basket.parse_symbols(" btc-usdt , eth-usdt ") == ["BTC-USDT", "ETH-USDT"]


def test_bad_symbol_lists_are_rejected():
    with pytest.raises(SystemExit, match="at least one symbol"):
        basket.parse_symbols(" , ")
    with pytest.raises(SystemExit, match="twice"):
        basket.parse_symbols("BTC-USDT,BTC-USDT")


def test_an_empty_basket_is_rejected():
    with pytest.raises(ValueError, match="at least one symbol"):
        basket.run_basket({}, "sma", {"window": 2}, "1h")


# --- the report and the artifacts ---------------------------------------------


def test_the_report_names_the_window_the_legs_and_the_benchmark():
    result = basket.run_basket(
        {"BTC-USDT": make_bars(wavy(40)), "ETH-USDT": make_bars(wavy(40, drift=0.05))},
        "sma",
        {"window": 3},
        "1h",
        costs=PAID,
    )
    text = basket.render(result)
    assert "BTC-USDT" in text and "ETH-USDT" in text
    assert "equal weight B&H" in text
    assert "never rebalanced" in text
    assert data.iso(result.times[0]) in text  # the window comes from the data


def test_the_summary_is_json_friendly():
    result = basket.run_basket(legs(A=wavy(40), B=wavy(40)), "sma", {"window": 3}, "1h", costs=PAID)
    payload = result.as_dict()
    assert payload["window"]["bars"] == len(result.times)
    assert [leg["symbol"] for leg in payload["legs"]] == ["A", "B"]
    assert payload["basket"]["total_return"] == pytest.approx(result.performance.total_return)
    assert "equity" not in payload

    with_curves = result.as_dict(include_curves=True)
    assert len(with_curves["equity"]) == len(result.times)
    assert set(with_curves["leg_equity"]) == {"A", "B"}


def test_a_wiped_out_curve_still_draws(tmp_path: Path):
    """A geared leg can reach zero; a log-scale chart must not crash on it."""
    path = tmp_path / "zero.svg"
    report.write_curves(path, [0, 3600, 7200], {"basket": [1.0, 0.5, 0.0], "B&H": [1.0, 0.9, 0.8]}, "zero")
    text = path.read_text()
    assert "B&amp;H" in text  # the label is escaped, not injected raw
    assert text.count("<polyline") == 2


def test_the_chart_and_the_csv_are_written(tmp_path: Path):
    result = basket.run_basket(legs(A=wavy(40), B=wavy(40, drift=0.1)), "sma", {"window": 3}, "1h", costs=PAID)
    csv_path = tmp_path / "basket_equity.csv"
    svg_path = tmp_path / "basket_equity.svg"
    json_path = tmp_path / "basket_metrics.json"
    basket.write_equity(csv_path, result)
    basket.write_chart(svg_path, result)
    basket.write_metrics(json_path, result, include_curves=True)

    rows = list(csv.DictReader(csv_path.open()))
    assert list(rows[0]) == ["time_utc", "basket", "benchmark_equal_weight", "leg_A", "leg_B"]
    assert len(rows) == len(result.times)
    assert float(rows[-1]["basket"]) == pytest.approx(result.performance.final_equity)
    svg = svg_path.read_text()
    assert svg.startswith("<svg") and "basket (sma)" in svg
    payload = json.loads(json_path.read_text())
    assert len(payload["equity"]) == len(result.times)


# --- the CLI ------------------------------------------------------------------


@pytest.fixture
def basket_archive(tmp_path: Path) -> Path:
    """A tiny hourly archive: three symbols with different trends."""
    root = tmp_path / "spot"
    for symbol, drift in (("UP-USDT", 0.6), ("MID-USDT", 0.2), ("DOWN-USDT", -0.4)):
        write_archive(root, symbol, "1h", make_bars(wavy(400, drift=drift)))
    return root


def test_cli_prints_a_basket_without_writing_anything(basket_archive: Path, capsys):
    code = basket.main(
        [
            "--data-dir",
            str(basket_archive),
            "--symbols",
            "UP-USDT,MID-USDT,DOWN-USDT",
            "--strategy",
            "sma",
            "--param",
            "window=10",
            "--no-artifacts",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "UP-USDT" in printed and "DOWN-USDT" in printed
    assert "equal weight B&H" in printed
    assert "best leg" in printed


def test_cli_writes_the_curve_the_chart_and_the_json(basket_archive: Path, tmp_path: Path, capsys):
    out = tmp_path / "out"
    code = basket.main(
        [
            "--data-dir",
            str(basket_archive),
            "--symbols",
            "UP-USDT,MID-USDT",
            "--strategy",
            "sma",
            "--param",
            "window=10",
            "--out-dir",
            str(out),
            "--json",
        ]
    )
    assert code == 0
    assert "wrote" in capsys.readouterr().out
    names = sorted(path.name for path in out.iterdir())
    assert names == [
        "basket_sma10_MID-USDT+UP-USDT_1h_equity.csv",
        "basket_sma10_MID-USDT+UP-USDT_1h_equity.svg",
        "basket_sma10_MID-USDT+UP-USDT_1h_metrics.json",
    ]
    payload = json.loads((out / "basket_sma10_MID-USDT+UP-USDT_1h_metrics.json").read_text())
    assert [leg["symbol"] for leg in payload["legs"]] == ["UP-USDT", "MID-USDT"]
    assert payload["timeframe"] == "1h"


def test_two_different_baskets_do_not_overwrite_each_other(basket_archive: Path, tmp_path: Path):
    """The legs are in the filename, so a one-leg run cannot clobber a two-leg one."""
    out = tmp_path / "out"
    one = basket.run_basket(
        {"UP-USDT": data.load_series(basket_archive, "UP-USDT", "1h")}, "sma", {"window": 10}, "1h"
    )
    two = basket.run_basket(
        {
            "UP-USDT": data.load_series(basket_archive, "UP-USDT", "1h"),
            "MID-USDT": data.load_series(basket_archive, "MID-USDT", "1h"),
        },
        "sma",
        {"window": 10},
        "1h",
    )
    assert basket.artifact_stem(one) != basket.artifact_stem(two)
    assert basket.artifact_stem(one).startswith("basket_sma10_UP-USDT_")


def test_a_long_leg_list_falls_back_to_a_digest():
    """Fifty symbols cannot go in a filename; the digest keeps them distinct."""
    symbols = [f"COIN{index:02d}-USDT" for index in range(50)]
    series = {symbol: make_bars(wavy(40)) for symbol in symbols}
    wide = basket.run_basket(series, "sma", {"window": 3}, "1h", costs=FREE)
    stem = basket.artifact_stem(wide)
    assert "50legs-" in stem
    assert len(stem) < 60
    # …and the tag depends on *which* symbols, not just how many.
    other = dict(series)
    other["COIN00-USDT"] = make_bars(wavy(40, drift=0.5))
    assert basket.artifact_stem(
        basket.run_basket(other, "sma", {"window": 3}, "1h", costs=FREE)
    ) == stem  # same symbols, different prices: same basket name


def test_cli_explains_a_symbol_that_is_not_collected(basket_archive: Path):
    with pytest.raises(SystemExit, match="NOPE-USDT"):
        basket.main(["--data-dir", str(basket_archive), "--symbols", "NOPE-USDT", "--no-artifacts"])


def test_cli_rejects_a_parameter_the_strategy_does_not_have(basket_archive: Path):
    with pytest.raises(SystemExit, match="no parameter"):
        basket.main(
            [
                "--data-dir",
                str(basket_archive),
                "--symbols",
                "UP-USDT,MID-USDT",
                "--strategy",
                "sma",
                "--param",
                "nope=3",
                "--no-artifacts",
            ]
        )


def test_the_basket_reads_only_bars_inside_the_window(basket_archive: Path):
    """A leg's later bars must not move the basket: no look-ahead across legs."""
    full = data.load_series(basket_archive, "UP-USDT", "1h")
    cut = full[:200]
    other = data.load_series(basket_archive, "MID-USDT", "1h")[:200]
    long_run = basket.run_basket({"UP": full, "MID": other}, "sma", {"window": 10}, "1h", costs=PAID)
    short_run = basket.run_basket({"UP": cut, "MID": other}, "sma", {"window": 10}, "1h", costs=PAID)
    assert long_run.times == short_run.times
    assert long_run.equity == pytest.approx(short_run.equity)


# --- the chart marks every swap -----------------------------------------------


def test_the_basket_marks_the_bars_where_any_leg_moved():
    result = basket.run_basket(
        {"A": make_bars(wavy(200, amplitude=10.0, period=5.0)),
         "B": make_bars(wavy(200, amplitude=10.0, period=7.0, drift=0.3))},
        "sma",
        {"window": 4},
        "1h",
        costs=PAID,
    )
    expected = {
        k
        for leg in result.legs
        for k in range(1, len(result.times))
        if leg.positions[k] != leg.positions[k - 1]
    }
    assert expected  # the fixture must actually trade
    markers = basket.rebalance_markers(result)
    assert {index for index, _, _ in markers} == expected


def test_a_swap_tooltip_names_the_leg_and_the_direction():
    result = basket.run_basket(
        {"A": make_bars(wavy(200, amplitude=10.0, period=5.0)), "B": make_bars([100.0] * 200)},
        "sma",
        {"window": 4},
        "1h",
        costs=PAID,
    )
    markers = basket.rebalance_markers(result)
    assert markers
    index, colour, label = markers[0]
    assert colour in (report.MARKER_ENTRY, report.MARKER_EXIT)
    assert "A" in label and "capital in the market" in label
    assert "UTC" in label


def test_a_basket_that_never_trades_has_no_markers():
    result = basket.run_basket(
        {"A": make_bars([100.0] * 60), "B": make_bars([50.0] * 60)}, "sma", {"window": 3}, "1h", costs=PAID
    )
    assert basket.rebalance_markers(result) == []


def test_the_basket_chart_carries_the_markers(tmp_path: Path):
    result = basket.run_basket(
        {"A": make_bars(wavy(200, amplitude=10.0, period=5.0)), "B": make_bars([100.0] * 200)},
        "sma",
        {"window": 4},
        "1h",
        costs=PAID,
    )
    path = tmp_path / "basket.svg"
    basket.write_chart(path, result)
    text = path.read_text()
    assert text.count('class="position-change"') == len(basket.rebalance_markers(result)) > 0
    assert "position changes:" in text


# --- --last: only the reported window moves -----------------------------------


def test_a_window_shortens_the_basket_without_truncating_the_legs():
    series = {"A": make_bars(wavy(400, drift=0.4)), "B": make_bars(wavy(400, drift=-0.2))}
    full = basket.run_basket(series, "sma", {"window": 5}, "1h", costs=PAID)
    short = basket.run_basket(series, "sma", {"window": 5}, "1h", costs=PAID, window=100 * STEP)

    window_bars = len(short.times)
    assert window_bars == 101  # 100 hours of hourly bars, both ends included
    assert short.times == full.times[-window_bars:]
    # each leg is the tail of its own full curve, rebased where the window starts,
    # so the basket starts with equal weights again rather than carrying the drift
    for leg_short, leg_full in zip(short.legs, full.legs):
        assert leg_short.positions == leg_full.positions[-window_bars:]
        assert leg_short.equity[0] == pytest.approx(1.0)
        assert leg_short.equity[-1] == pytest.approx(leg_full.equity[-1] / leg_full.equity[-window_bars])


def test_a_window_keeps_the_signals_the_legs_really_had():
    """Warm history and a cold truncation are not the same backtest."""
    bars = make_bars(wavy(400, drift=0.4))
    warm = basket.run_basket({"A": bars}, "sma", {"window": 5}, "1h", costs=PAID, window=100 * STEP)
    cold = basket.run_basket({"A": bars[-100:]}, "sma", {"window": 5}, "1h", costs=PAID)
    assert len(cold.times) == 100 and len(warm.times) == 101
    assert warm.legs[0].exposure != pytest.approx(cold.legs[0].exposure)
    assert warm.legs[0].equity[-1] != pytest.approx(cold.legs[0].equity[-1])


def test_the_basket_cli_takes_a_period(basket_archive: Path, capsys):
    code = basket.main(
        [
            "--data-dir", str(basket_archive), "--symbols", "UP-USDT,MID-USDT",
            "--strategy", "sma", "--param", "window=10", "--last", "100d", "--no-artifacts",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "(last 100d)" in printed
    assert "never rebalanced" in printed
