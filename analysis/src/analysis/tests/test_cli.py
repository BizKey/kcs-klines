"""The command line, exercised end to end on a tiny temp archive.

The point of these tests is the *workflow for adding a strategy*: a strategy
registered from outside the package must run through the CLI with its own
parameters, with no edits to `run_backtest.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .. import data, journal
from ..data import Bar
from ..run_backtest import main
from ..strategies import REGISTRY, Strategy, register
from .conftest import make_bars, wavy


class ThirdPartyBreakout(Strategy):
    """A strategy that lives outside the package, as a user's would.

    Named differently from the shipped `breakout` so the registry does not mind.
    """

    name = "thirdparty"
    sweep_param = "lookback"

    def __init__(self, lookback: int = 20, exit_bars: int = 5):
        self.lookback = lookback
        self.exit_bars = exit_bars

    @property
    def slug(self) -> str:
        return f"thirdparty{self.lookback}"

    @property
    def warmup(self) -> int:
        return self.lookback

    @property
    def params(self) -> dict:
        return {"lookback": self.lookback, "exit_bars": self.exit_bars}

    def targets(self, bars: list[Bar]) -> list[float]:
        out: list[float] = []
        entry: int | None = None
        for i, bar in enumerate(bars):
            if i < self.lookback:
                out.append(0.0)
                continue
            window = [b.high for b in bars[i - self.lookback : i]]
            if entry is not None and i - entry >= self.exit_bars:
                entry = None
            elif entry is None and bar.close > max(window):
                entry = i
            out.append(1.0 if entry is not None else 0.0)
        return out

    def describe(self) -> str:
        return f"long on a new {self.lookback}-bar high, out after {self.exit_bars} bars"


@pytest.fixture
def breakout_registered():
    """Register the external strategy, and remove it again afterwards."""
    register("thirdparty")(ThirdPartyBreakout)
    yield "thirdparty"
    REGISTRY.pop("thirdparty", None)


def test_list_shows_registered_strategies_with_their_parameters(capsys):
    assert main(["--list"]) == 0
    printed = capsys.readouterr().out
    assert "sma(window=200)" in printed
    assert "sma-ls(window=200)" in printed
    assert "breakout(lookback=20)" in printed
    assert "--sweep lookback" in printed
    assert "series" in printed


def test_an_external_strategy_runs_without_touching_the_cli(toy_archive: Path, breakout_registered, capsys):
    code = main(
        [
            "--strategy",
            breakout_registered,
            "--param",
            "lookback=10",
            "--param",
            "exit_bars=3",
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--no-artifacts",
            "--no-fee-grid",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "thirdparty10" in printed  # the slug is derived from the parameter
    assert "new 10-bar high" in printed
    assert "bookkeeping" in printed
    assert "WARNING" not in printed


def test_parameters_change_the_result(toy_archive: Path, breakout_registered, capsys):
    def total(extra: list[str]) -> str:
        main(
            [
                "--strategy",
                breakout_registered,
                "--symbol",
                "TOY-USDT",
                "--data-dir",
                str(toy_archive),
                "--no-artifacts",
                "--no-fee-grid",
                *extra,
            ]
        )
        return capsys.readouterr().out

    slow = total(["--param", "lookback=20", "--param", "exit_bars=10"])
    fast = total(["--param", "lookback=5", "--param", "exit_bars=2"])
    assert slow != fast


def test_an_unknown_parameter_is_rejected_with_the_accepted_ones_listed(tmp_path: Path):
    # The data directory does not even exist: flags must be validated first.
    with pytest.raises(SystemExit) as caught:
        main(["--strategy", "sma", "--param", "lookback=50", "--data-dir", str(tmp_path), "--no-artifacts"])
    message = str(caught.value)
    assert "has no parameter 'lookback'" in message
    assert "window (default 200)" in message


def test_a_parameter_without_a_value_is_rejected(toy_archive: Path):
    with pytest.raises(SystemExit, match="NAME=VALUE"):
        main(["--strategy", "sma", "--param", "window", "--data-dir", str(toy_archive), "--no-artifacts"])


def test_bare_sweep_uses_the_declared_parameter(toy_archive: Path, capsys):
    code = main(
        [
            "--strategy",
            "sma",
            "--param",
            "window=5",
            "--sweep",
            "3,5,8",
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--no-artifacts",
            "--no-fee-grid",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "parameter sweep over window" in printed
    for value in ("3", "5", "8"):
        assert f"  {value:>8}" in printed or f"        {value}" in printed


def test_a_named_sweep_works_for_any_parameter(toy_archive: Path, breakout_registered, capsys):
    code = main(
        [
            "--strategy",
            breakout_registered,
            "--sweep",
            "exit_bars=2,5",
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--no-artifacts",
            "--no-fee-grid",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "parameter sweep over exit_bars" in printed


def test_sweeping_an_unknown_parameter_is_rejected(toy_archive: Path):
    with pytest.raises(SystemExit, match="has no parameter"):
        main(["--strategy", "sma", "--sweep", "lookback=3,5", "--data-dir", str(toy_archive), "--no-artifacts"])


def test_artifacts_are_written_where_asked(toy_archive: Path, tmp_path: Path, capsys):
    out = tmp_path / "out"
    code = main(
        [
            "--strategy",
            "sma",
            "--param",
            "window=5",
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--out-dir",
            str(out),
            "--json",
            "--no-fee-grid",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == [
        "sma5_TOY-USDT_1h_equity.csv",
        "sma5_TOY-USDT_1h_equity.svg",
        "sma5_TOY-USDT_1h_metrics.json",
        "sma5_TOY-USDT_1h_trades.csv",
    ]
    assert "wrote" in printed


def test_json_curves_are_optional(toy_archive: Path, tmp_path: Path):
    import json

    out = tmp_path / "out"
    main(
        [
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--out-dir",
            str(out),
            "--json",
            "--json-curves",
            "--no-fee-grid",
            "--no-artifacts",
        ]
    )
    assert not out.exists()  # --no-artifacts wins


def test_the_default_run_is_the_documented_one():
    """No flags means SMA 200 on BTC-USDT 1h — the numbers in the README."""
    from ..run_backtest import parse_args

    args = parse_args([])
    assert (args.symbol, args.timeframe, args.strategy) == ("BTC-USDT", "1h", "sma")
    assert args.param == []
    assert args.fee == 0.001


def test_a_symbol_without_data_fails_loudly(toy_archive: Path):
    with pytest.raises(FileNotFoundError, match="backfill"):
        main(["--symbol", "NOPE-USDT", "--data-dir", str(toy_archive), "--no-artifacts"])


def test_the_toy_archive_is_readable(toy_archive: Path):
    bars = data.load_series(toy_archive, "TOY-USDT", "1h")
    assert len(bars) == 400
    assert bars[0].close == pytest.approx(wavy(400)[0])
    assert data.data_quality(bars, "1h").gaps == 0
    assert len(make_bars(wavy(3))) == 3


# --- journaling from the CLI ------------------------------------------------


def test_journal_flag_records_a_verifiable_entry(toy_archive: Path, tmp_path: Path, capsys):
    journal_dir = tmp_path / "journal"
    code = main(
        [
            "--strategy",
            "sma",
            "--param",
            "window=5",
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--out-dir",
            str(tmp_path / "out"),
            "--journal",
            str(journal_dir),
            "--note",
            "cli test",
            "--no-fee-grid",
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0
    assert "journalized" in printed

    entries = journal.load_runs(journal_dir)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.note == "cli test"
    assert entry.strategy == "sma"
    assert entry.params == {"window": 5}
    assert entry.data.symbol == "TOY-USDT"
    assert entry.trades_file is None

    verification = journal.verify(entry, toy_archive)
    assert verification.ok, verification.message


def test_journal_flag_defaults_to_the_repository_directory(toy_archive: Path, tmp_path: Path, monkeypatch, capsys):
    """`--journal` with no value means <repo>/journal, not the current directory.

    The default location is redirected here so the test never writes into the
    repository's real journal.
    """
    fake_default = tmp_path / "repo" / "journal"
    monkeypatch.setattr(journal, "DEFAULT_JOURNAL_DIR", fake_default)
    monkeypatch.chdir(tmp_path)

    main(
        [
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--out-dir",
            str(tmp_path / "out"),
            "--journal",
            "--no-fee-grid",
        ]
    )
    assert "journalized" in capsys.readouterr().out
    assert journal.runs_path(fake_default).is_file()
    assert not (tmp_path / "journal").exists()  # not the working directory

    # The resolution rule itself.
    assert journal.journal_dir_for("") == fake_default
    assert journal.journal_dir_for(None) == fake_default
    assert journal.journal_dir_for("elsewhere") == Path("elsewhere")


def test_keep_trades_copies_the_trade_table_into_the_journal(toy_archive: Path, tmp_path: Path, capsys):
    journal_dir = tmp_path / "journal"
    main(
        [
            "--strategy",
            "sma",
            "--param",
            "window=5",
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--out-dir",
            str(tmp_path / "out"),
            "--journal",
            str(journal_dir),
            "--keep-trades",
            "--no-fee-grid",
        ]
    )
    capsys.readouterr()
    entry = journal.load_runs(journal_dir)[0]
    assert entry.trades_file == f"{entry.run_id}.csv"
    stored = journal.trades_path(journal_dir, entry.run_id)
    assert stored.is_file()
    assert stored.read_text().startswith("side,entry_time_utc")
    # The copy is the one the run produced, byte for byte.
    produced = tmp_path / "out" / f"sma5_TOY-USDT_1h_trades.csv"
    assert stored.read_bytes() == produced.read_bytes()


def test_journal_without_artifacts_is_refused(toy_archive: Path, capsys):
    code = main(
        [
            "--symbol",
            "TOY-USDT",
            "--data-dir",
            str(toy_archive),
            "--journal",
            "--no-artifacts",
        ]
    )
    assert code == 2
    assert "needs the trade table" in capsys.readouterr().err
