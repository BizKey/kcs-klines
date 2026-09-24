"""Loading a series and auditing the data itself."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from .. import data
from .conftest import START, make_bars


def test_interval_seconds_covers_the_stored_timeframes():
    assert data.interval_seconds("1m") == 60
    assert data.interval_seconds("1h") == 3600
    assert data.interval_seconds("4h") == 14400
    assert data.interval_seconds("1d") == 86400
    assert data.interval_seconds("1w") == 604800


def test_calendar_month_has_no_fixed_interval():
    with pytest.raises(ValueError, match="calendar timeframe"):
        data.interval_seconds("1mon")


def test_unknown_timeframe_is_rejected_with_the_known_ones_listed():
    with pytest.raises(ValueError, match="unknown timeframe"):
        data.interval_seconds("7h")


def test_bars_per_year():
    assert data.bars_per_year("1h") == 8760
    assert data.bars_per_year("1d") == 365
    assert data.bars_per_year("1w") == pytest.approx(52.142857, rel=1e-6)
    assert data.bars_per_year("1mon") == 12


def test_next_label_advances_one_bar_for_fixed_timeframes():
    assert data.next_label(START, "1h") == START + 3600
    assert data.next_label(START, "1d") == START + 86400


def test_next_label_advances_one_calendar_month():
    january = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    february = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())
    assert data.next_label(january, "1mon") == february


def test_next_label_clamps_the_day_for_short_months():
    january_31 = int(datetime(2026, 1, 31, tzinfo=timezone.utc).timestamp())
    # 2026 is not a leap year, so a month later is February 28th.
    expected = int(datetime(2026, 2, 28, tzinfo=timezone.utc).timestamp())
    assert data.next_label(january_31, "1mon") == expected


def test_quality_of_a_clean_series():
    report = data.data_quality(make_bars([100, 101, 102, 103]), "1h")
    assert report.bars == 4
    assert report.gaps == 0
    assert report.missing_bars == 0
    assert report.largest_gap_slots == 1
    assert report.bars_violating_ohlc == 0


def test_quality_counts_missing_bars_from_a_gap():
    bars = make_bars([100, 101, 102])
    # Drop the middle bar: the series now jumps two hours at once.
    del bars[1]
    report = data.data_quality(bars, "1h")
    assert report.gaps == 1
    assert report.missing_bars == 1
    assert report.largest_gap_slots == 2


def test_quality_counts_a_missing_calendar_month_as_one_bar():
    bars = [
        data.Bar(int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), 1, 1, 1, 1, 1.0),
        data.Bar(int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()), 1, 1, 1, 1, 1.0),
    ]
    report = data.data_quality(bars, "1mon")
    assert report.gaps == 1
    assert report.missing_bars == 1
    assert report.largest_gap_slots == 2


def test_quality_flags_impossible_bars_instead_of_dropping_them():
    bars = make_bars([100, 101])
    bars[1] = data.Bar(bars[1].time, open=101, high=99, low=105, close=101, volume=1.0)
    report = data.data_quality(bars, "1h")
    assert report.bars == 2
    assert report.bars_violating_ohlc == 1


def test_quality_rejects_an_empty_series():
    with pytest.raises(ValueError):
        data.data_quality([], "1h")


def test_load_series_requires_collected_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="backfill"):
        data.load_series(tmp_path, "NOPE-USDT", "1h")


def test_available_series_is_empty_without_data(tmp_path):
    assert data.available_series(tmp_path) == []


def test_iso_renders_utc_minutes():
    assert data.iso(START).startswith("2017-")


# --- real archive ---------------------------------------------------------


def test_real_series_is_sorted_and_deduplicated(btc_hourly):
    times = [b.time for b in btc_hourly]
    assert times == sorted(times)
    assert len(set(times)) == len(times)
    assert all(b.open > 0 and b.close > 0 for b in btc_hourly)


def test_real_series_lists_its_files(data_dir):
    files = data.series_files(data_dir, "BTC-USDT", "1h")
    assert len(files) >= 5
    assert all(p.suffix == ".parquet" for p in files)


def test_real_archive_exposes_series_pairs(data_dir):
    pairs = data.available_series(data_dir)
    assert ("BTC-USDT", "1h") in pairs
    assert len(pairs) > 100
