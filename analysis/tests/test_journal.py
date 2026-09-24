"""The trade journal: what gets recorded, and whether re-checking catches lies.

The value of a journal is that it can be *disproved*. These tests deliberately
tamper with the archive, with the recorded metrics and with the recorded
parameters, and assert that `verify` notices each one and says which it was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .. import data, engine, journal
from ..strategies import SmaTrend, get_strategy
from .conftest import make_bars, wavy, write_archive

FEE = 0.001


def build_run(bars, *, window: int = 5, label: str = "sma5"):
    strategy = SmaTrend(window=window)
    result = engine.run_backtest(
        bars, strategy.targets(bars), "1h", engine.Costs(fee_per_side=FEE), label=label
    )
    return strategy, result


def record(bars, archive: Path, journal_dir: Path, *, symbol: str = "TOY-USDT", window: int = 5, note: str = ""):
    strategy, result = build_run(bars, window=window)
    quality = data.data_quality(bars, "1h")
    entry = journal.entry_for(
        result,
        quality,
        bars,
        "sma",
        {"window": window},
        symbol,
        "1h",
        note=note,
    )
    journal.append(entry, journal_dir)
    return entry


@pytest.fixture
def archive(tmp_path: Path) -> tuple[Path, list]:
    """A temp archive with one wavy series, plus the bars that are in it."""
    root = tmp_path / "spot"
    bars = make_bars(wavy(200))
    write_archive(root, "TOY-USDT", "1h", bars)
    return root, bars


# --- fingerprints -----------------------------------------------------------


def test_fingerprint_is_stable_and_order_sensitive():
    bars = make_bars([1.0, 2.0, 3.0])
    assert journal.fingerprint(bars) == journal.fingerprint(list(bars))
    assert journal.fingerprint(bars) == journal.fingerprint(make_bars([1.0, 2.0, 3.0]))
    assert journal.fingerprint(bars) != journal.fingerprint(bars[::-1])


def test_fingerprint_notices_every_changed_number():
    bars = make_bars([1.0, 2.0, 3.0])
    baseline = journal.fingerprint(bars)
    for index in range(3):
        for field in ("open", "high", "low", "close", "volume"):
            touched = list(bars)
            bar = bars[index]
            touched[index] = data.Bar(
                bar.time,
                bar.open * 2 if field == "open" else bar.open,
                bar.high * 2 if field == "high" else bar.high,
                bar.low * 2 if field == "low" else bar.low,
                bar.close * 2 if field == "close" else bar.close,
                bar.volume * 2 if field == "volume" else bar.volume,
            )
            assert journal.fingerprint(touched) != baseline, (index, field)


def test_fingerprint_ignores_when_a_series_was_split_into_files(archive):
    root, bars = archive
    whole = data.load_series(root, "TOY-USDT", "1h")
    write_archive(root, "TOY-USDT", "1h", bars[:100], name="000-first.parquet")
    split = data.load_series(root, "TOY-USDT", "1h")
    assert len(split) == len(whole)
    assert journal.fingerprint(split) == journal.fingerprint(whole)


def test_window_of_slices_inclusively():
    bars = make_bars([1.0, 2.0, 3.0, 4.0])
    assert journal.window_of(bars, bars[1].time, bars[2].time) == bars[1:3]
    assert journal.window_of(bars, bars[0].time, bars[-1].time) == bars
    assert journal.window_of(bars, bars[-1].time + 60, bars[-1].time + 120) == []


# --- recording --------------------------------------------------------------


def test_an_entry_holds_everything_needed_to_reproduce_the_run(tmp_path):
    bars = make_bars(wavy(120))
    archive = tmp_path / "spot"
    write_archive(archive, "TOY-USDT", "1h", bars)
    journal_dir = tmp_path / "journal"
    entry = record(bars, archive, journal_dir, note="unit test")

    assert entry.note == "unit test"
    assert entry.strategy == "sma"
    assert entry.params == {"window": 5}
    assert entry.costs == {"fee_per_side": FEE, "slippage_per_side": 0.0}
    assert entry.data.symbol == "TOY-USDT"
    assert entry.data.bars == len(bars)
    assert entry.data.first == bars[0].time
    assert entry.data.last == bars[-1].time
    assert entry.data.digest == journal.fingerprint(bars)
    assert entry.metrics["strategy"]["final_equity"] > 0
    assert entry.run_id.endswith("-sma5")
    # The strategy can be rebuilt from the entry alone.
    rebuilt = get_strategy(entry.strategy, **entry.params)
    assert rebuilt.slug == "sma5"


def test_recording_is_append_only_and_ids_stay_unique(tmp_path):
    bars = make_bars(wavy(120))
    archive = tmp_path / "spot"
    write_archive(archive, "TOY-USDT", "1h", bars)
    journal_dir = tmp_path / "journal"

    first = record(bars, archive, journal_dir)
    second = record(bars, archive, journal_dir)
    assert first.run_id != second.run_id
    lines = journal.runs_path(journal_dir).read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # every line is independently valid JSON
    assert [entry.run_id for entry in journal.load_runs(journal_dir)] == [first.run_id, second.run_id]


def test_the_journal_file_is_human_readable_json_lines(tmp_path):
    bars = make_bars(wavy(60))
    archive = tmp_path / "spot"
    write_archive(archive, "TOY-USDT", "1h", bars)
    journal_dir = tmp_path / "journal"
    entry = record(bars, archive, journal_dir)
    payload = json.loads(journal.runs_path(journal_dir).read_text().strip())
    assert payload["run_id"] == entry.run_id
    assert payload["journal_format"] == journal.JOURNAL_FORMAT
    assert set(payload) >= {"run_id", "recorded_at", "strategy", "params", "costs", "data", "metrics"}


def test_an_empty_journal_reads_as_empty(tmp_path):
    assert journal.load_runs(tmp_path / "nowhere") == []
    assert journal.load_verifications(tmp_path / "nowhere") == []


def test_a_corrupt_line_is_reported_with_its_number(archive, tmp_path):
    """A hand-edited journal must fail with the line that is wrong, not a KeyError."""
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)

    path = journal.runs_path(journal_dir)
    path.write_text(entry.to_json() + "\nnot json\n")
    with pytest.raises(ValueError, match=":2 is not a valid journal entry"):
        journal.load_runs(journal_dir)

    path.write_text(entry.to_json() + "\n" + '{"run_id": "incomplete"}' + "\n")
    with pytest.raises(ValueError, match=":2 is not a valid journal entry"):
        journal.load_runs(journal_dir)

    path.write_text(entry.to_json() + "\n")
    assert len(journal.load_runs(journal_dir)) == 1


# --- verification -----------------------------------------------------------


def test_verify_reproduces_the_recorded_numbers(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    verification = journal.verify(entry, root)
    assert verification.status == journal.STATUS_VERIFIED
    assert verification.ok
    assert verification.mismatches == []
    assert verification.digest_actual == entry.data.digest
    assert verification.bars == entry.data.bars


def test_verify_catches_an_edited_bar(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)

    edited = list(bars)
    bar = bars[42]
    edited[42] = data.Bar(bar.time, bar.open, bar.high, bar.low, bar.close * 1.0001, bar.volume)
    write_archive(root, "TOY-USDT", "1h", edited, name="all.parquet")

    verification = journal.verify(entry, root)
    assert verification.status in (journal.STATUS_DATA_CHANGED, journal.STATUS_MISMATCH)
    assert not verification.ok
    assert verification.digest_actual != entry.data.digest


def test_verify_catches_metrics_edited_by_hand(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)

    entry.metrics["strategy"]["final_equity"] += 1.0  # wishful thinking
    verification = journal.verify(entry, root)
    assert verification.status == journal.STATUS_MISMATCH
    assert any(item["field"] == "strategy.final_equity" for item in verification.mismatches)


def test_verify_catches_parameter_edits(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir, window=5)

    entry.params = {"window": 20}  # the entry now claims a different strategy
    verification = journal.verify(entry, root)
    assert verification.status == journal.STATUS_MISMATCH
    assert not verification.ok


def test_verify_reports_an_unknown_strategy(tmp_path):
    bars = make_bars(wavy(60))
    archive = tmp_path / "spot"
    write_archive(archive, "TOY-USDT", "1h", bars)
    journal_dir = tmp_path / "journal"
    entry = record(bars, archive, journal_dir)
    entry.strategy = "strategy-that-never-existed"
    verification = journal.verify(entry, archive)
    assert verification.status == journal.STATUS_UNKNOWN_STRATEGY
    assert "cannot rebuild" in verification.message


def test_verify_reports_a_missing_series(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    verification = journal.verify(entry, tmp_path / "empty")
    assert verification.status == journal.STATUS_ERROR
    assert "backfill" in verification.message


def test_verify_still_works_after_the_archive_grew(archive, tmp_path):
    """The recorded window is re-sliced, so today's longer series still checks out."""
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)

    grown = bars + make_bars(wavy(30), start=bars[-1].time + 3600)
    write_archive(root, "TOY-USDT", "1h", grown, name="all.parquet")
    assert len(data.load_series(root, "TOY-USDT", "1h")) == len(bars) + 30

    verification = journal.verify(entry, root)
    assert verification.status == journal.STATUS_VERIFIED
    assert verification.bars == entry.data.bars  # the recorded window, not today's


def test_verify_records_its_outcome(tmp_path, archive):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    journal.record_verification(journal.verify(entry, root), journal_dir)
    journal.record_verification(journal.verify(entry, root), journal_dir)

    stored = journal.load_verifications(journal_dir)
    assert len(stored) == 2
    latest = journal.latest_status(journal_dir)
    assert latest[entry.run_id].ok


def test_verify_checks_the_recorded_trade_table(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)

    # Keep the trade table the run produced.
    _, result = build_run(bars)
    stored = journal.trades_path(journal_dir, entry.run_id)
    stored.parent.mkdir(parents=True, exist_ok=True)
    from .. import report as report_module

    report_module.write_trades(stored, result)
    entry.trades_file = stored.name

    verification = journal.verify(entry, root, journal_dir=journal_dir)
    assert verification.ok, verification.message
    assert verification.trades_checked


def test_verify_catches_an_edited_trade_table(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    _, result = build_run(bars)
    stored = journal.trades_path(journal_dir, entry.run_id)
    stored.parent.mkdir(parents=True, exist_ok=True)

    from .. import report as report_module

    report_module.write_trades(stored, result)
    entry.trades_file = stored.name

    # Make one trade look better than it was.
    lines = stored.read_text().splitlines()
    header, first = lines[0].split(","), lines[1].split(",")
    first[header.index("net_return")] = "0.5"
    lines[1] = ",".join(first)
    stored.write_text("\n".join(lines) + "\n")

    verification = journal.verify(entry, root, journal_dir=journal_dir)
    assert not verification.ok
    assert any(item["field"] == "trades[0].net_return" for item in verification.mismatches), (
        verification.mismatches
    )


def test_verify_notices_a_missing_trade_table(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    entry.trades_file = "gone.csv"
    verification = journal.verify(entry, root, journal_dir=journal_dir)
    assert verification.status == journal.STATUS_MISMATCH
    assert verification.mismatches[0]["actual"] == "missing"


def test_verify_without_a_journal_dir_skips_the_trade_check(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    entry.trades_file = "gone.csv"
    verification = journal.verify(entry, root)  # no journal_dir given
    assert verification.ok
    assert not verification.trades_checked


def test_compare_tolerates_float_noise_but_not_real_differences():
    assert journal.compare({"a": 1.0}, {"a": 1.0 + 1e-12}) == []
    assert journal.compare({"a": 1.0}, {"a": 1.001})
    assert journal.compare({"a": 1.0}, {})[0]["field"] == "a"
    assert journal.compare({"a": {"b": 2.0}}, {"a": {"b": 3.0}})[0]["field"] == "a.b"
    # Bools and strings are neither compared numerically nor ignored silently.
    assert journal.compare({"a": True}, {"a": True}) == []
    assert journal.compare({"a": "x"}, {"a": "y"})


# --- tables and the CLI -----------------------------------------------------


def test_report_and_summary_lines_describe_the_journal(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    first = record(bars, root, journal_dir, window=5, note="a")
    second = record(bars, root, journal_dir, window=10, note="b")
    entries = journal.load_runs(journal_dir)

    table = "\n".join(journal.report_lines(entries, journal.latest_status(journal_dir)))
    assert first.run_id in table and second.run_id in table
    assert "TOY-USDT" in table
    assert "check" in table

    summary = "\n".join(journal.summary_lines(entries))
    assert "sma" in summary
    assert "window=5" in summary or "window=10" in summary


def test_show_lines_print_the_whole_entry(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir, note="why not")
    text = "\n".join(journal.show_lines(entry))
    assert entry.run_id in text
    assert "why not" in text
    assert "digest" in text
    assert "final_equity" in text


def test_cli_report_verify_and_show(archive, tmp_path, capsys):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)

    assert journal.main(["--journal-dir", str(journal_dir), "--data-dir", str(root), "report"]) == 0
    assert entry.run_id in capsys.readouterr().out

    assert journal.main(["--journal-dir", str(journal_dir), "--data-dir", str(root), "verify"]) == 0
    assert "1/1 runs verified" in capsys.readouterr().out
    assert journal.load_verifications(journal_dir)  # the outcome was recorded

    assert journal.main(["--journal-dir", str(journal_dir), "show", "--id", entry.run_id[:8]]) == 0
    assert entry.run_id in capsys.readouterr().out


def test_cli_verify_fails_loudly_on_a_mismatch(archive, tmp_path, capsys):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    entry = record(bars, root, journal_dir)
    entry.metrics["closed_trades"] = 999
    journal.runs_path(journal_dir).write_text(entry.to_json() + "\n")

    code = journal.main(["--journal-dir", str(journal_dir), "--data-dir", str(root), "verify"])
    printed = capsys.readouterr().out
    assert code == 1
    assert "0/1 runs verified" in printed
    assert "closed_trades" in printed


def test_cli_verify_quiet_only_speaks_on_failure(archive, tmp_path, capsys):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    record(bars, root, journal_dir)
    assert journal.main(["--journal-dir", str(journal_dir), "--data-dir", str(root), "verify", "--quiet"]) == 0
    assert "ok" not in capsys.readouterr().out.splitlines()[0]


def test_cli_verify_selects_a_subset(archive, tmp_path, capsys):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    record(bars, root, journal_dir, window=5)
    newest = record(bars, root, journal_dir, window=10)
    journal.main(["--journal-dir", str(journal_dir), "--data-dir", str(root), "verify", "--last", "1"])
    printed = capsys.readouterr().out
    assert newest.run_id in printed


def test_an_empty_journal_is_not_an_error(archive, tmp_path, capsys):
    """Nothing recorded yet is a state, not a failure — so exit code stays 0."""
    _, _ = archive
    journal_dir = tmp_path / "journal"
    assert journal.main(["--journal-dir", str(journal_dir), "verify"]) == 0
    assert "empty" in capsys.readouterr().out
    assert journal.main(["--journal-dir", str(journal_dir), "report"]) == 0
    assert "empty" in capsys.readouterr().out


def test_cli_rejects_an_unknown_run_id(archive, tmp_path):
    root, bars = archive
    journal_dir = tmp_path / "journal"
    record(bars, root, journal_dir)
    with pytest.raises(SystemExit, match="no run id starts with"):
        journal.main(["--journal-dir", str(journal_dir), "show", "--id", "nope"])
    with pytest.raises(SystemExit, match="matches 1 runs|no run id starts with"):
        journal.main(["--journal-dir", str(journal_dir), "show", "--id", "zzz"])

    # A prefix that matches several entries is ambiguous and says so.
    second = record(bars, root, journal_dir, window=9)
    prefix = second.run_id.split("-")[0]
    with pytest.raises(SystemExit, match="matches"):
        journal.main(["--journal-dir", str(journal_dir), "show", "--id", prefix])


def test_the_journal_directory_is_not_gitignored(tmp_path):
    """The user keeps the journal in the repository, so it must be trackable."""
    repo = Path(__file__).resolve().parents[2]
    if not (repo / ".git").is_dir():
        pytest.skip("not a git checkout")
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "journal/runs.jsonl"],
        cwd=repo,
        capture_output=True,
    )
    assert probe.returncode == 1, "journal/ must not be ignored by .gitignore"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "analysis/out/metrics.json"], cwd=repo, capture_output=True
    )
    assert ignored.returncode == 0, "analysis/out/ is expected to stay ignored"
