"""Trade journal: an append-only record of backtest runs, and a way to re-check them.

The point is not to log numbers — it is to be able to trust them later. Every
entry therefore carries everything needed to reproduce the run:

* the strategy (registry name) and its **parameters**, verbatim;
* the costs and the execution convention;
* a **data fingerprint**: symbol, timeframe, bar count, first/last bar and a
  SHA-256 digest of the OHLCV values of exactly the bars that were evaluated;
* the full metric set the engine produced, plus the data audit.

`verify` then re-reads the archive, slices it back to the recorded window (so a
journal stays checkable after the collector appends more bars), re-runs the
strategy and compares every numeric field. A mismatch means one of three things,
and the report says which: the stored bars changed, the strategy or the engine
changed, or the entry was edited by hand.

Layout, all plain text and diff-friendly so it can live in the repository::

    journal/
      runs.jsonl              one JSON object per run, append-only
      verifications.jsonl     one JSON object per verification, append-only
      trades/<run_id>.csv     optional per-trade detail for a run
      README.md               the format, written for humans

Usage::

    uv run kcs-backtest --journal --note "first look at BTC"
    uv run kcs-journal report
    uv run kcs-journal verify --last 5
    uv run kcs-journal show --id 20260924T221530Z-sma200
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import struct
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import data, engine
from .data import Bar, QualityReport, iso, window_of
from .metrics import pct
from .strategies import get_strategy

#: Bumped when the entry schema changes in a way old readers must know about.
JOURNAL_FORMAT = 2

#: What kind of run an entry records. A backtest is one series; a basket is several legs
#: combined at fixed weights, and it carries a `legs` digest per symbol instead of one
#: `data` window.
KIND_BACKTEST = "backtest"
KIND_BASKET = "basket"

#: `journal/` next to the repository root, independent of the working directory.
DEFAULT_JOURNAL_DIR = data.repo_root() / "journal"

RUNS_FILE = "runs.jsonl"
VERIFICATIONS_FILE = "verifications.jsonl"
TRADES_DIR = "trades"

#: Relative tolerance for "the same number". The engine is deterministic, so this
#: only has to absorb float noise from reloading the archive.
TOLERANCE = 1e-9

STATUS_VERIFIED = "verified"
STATUS_DATA_CHANGED = "data-changed"
STATUS_MISMATCH = "mismatch"
STATUS_UNKNOWN_STRATEGY = "unknown-strategy"
STATUS_ERROR = "error"


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compact_stamp(recorded_at: str) -> str:
    """`2026-09-24T22:15:30Z` -> `20260924T221530Z`, for use inside a run id."""
    return recorded_at.replace("-", "").replace(":", "")


def fingerprint(bars: list[Bar]) -> str:
    """SHA-256 over the OHLCV values, in order.

    Two series produce the same digest exactly when they hold the same bars with
    the same numbers — file partitioning, parquet metadata and the order the
    partitions were merged in are all irrelevant.
    """
    digest = hashlib.sha256()
    digest.update(struct.pack("<q", len(bars)))
    for bar in bars:
        digest.update(
            struct.pack(
                "<qddddd",
                bar.time,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
            )
        )
    return digest.hexdigest()


@dataclass(frozen=True)
class DataWindow:
    """What exactly was evaluated, and what it contained."""

    symbol: str
    timeframe: str
    bars: int
    first: int
    last: int
    digest: str
    gaps: int
    missing_bars: int
    bars_violating_ohlc: int

    @classmethod
    def of(cls, bars: list[Bar], quality: QualityReport, symbol: str, timeframe: str) -> DataWindow:
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            bars=len(bars),
            first=bars[0].time,
            last=bars[-1].time,
            digest=fingerprint(bars),
            gaps=quality.gaps,
            missing_bars=quality.missing_bars,
            bars_violating_ohlc=quality.bars_violating_ohlc,
        )


@dataclass
class RunEntry:
    """One recorded run, and everything needed to reproduce it."""

    run_id: str
    recorded_at: str
    label: str
    strategy: str
    params: dict
    costs: dict
    metrics: dict
    data: DataWindow | None = None
    legs: dict[str, DataWindow] = field(default_factory=dict)
    #: How the reported stretch was cut out of the legs' history: `seconds` for `--last`,
    #: `since`/`until` for explicit dates. A basket needs it, because its legs are always
    #: run over their whole history and only the reported window moves.
    window: dict = field(default_factory=dict)
    #: Per-leg costs when they differ from `costs` (the spread model charges each pair its own
    #: estimated half-spread). The numbers are stored, not re-derived, so a later estimate from
    #: a grown archive cannot silently change what the entry is checked against.
    leg_costs: dict[str, dict] = field(default_factory=dict)
    kind: str = KIND_BACKTEST
    note: str = ""
    trades_file: str | None = None
    journal_format: int = JOURNAL_FORMAT

    @property
    def symbols(self) -> list[str]:
        """Every symbol the entry covers: one for a backtest, the legs for a basket."""
        if self.kind == KIND_BASKET:
            return sorted(self.legs)
        return [self.data.symbol] if self.data else []

    def to_json(self) -> str:
        payload = asdict(self)
        payload["data"] = asdict(self.data) if self.data else None
        payload["legs"] = {symbol: asdict(leg) for symbol, leg in self.legs.items()}
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict) -> RunEntry:
        payload = dict(payload)
        payload["data"] = DataWindow(**payload["data"]) if payload.get("data") else None
        payload["legs"] = {
            symbol: DataWindow(**leg) for symbol, leg in payload.get("legs", {}).items()
        }
        # entries written before the basket support carry neither key
        payload.setdefault("kind", KIND_BACKTEST)
        return cls(**payload)

    def _curve(self, name: str) -> float:
        """One number of the headline curve, whichever kind of run this is.

        A backtest stores the engine's own tree (`strategy.*`); a basket stores its combined
        curve under `return.*`, so the same property serves both and the console keeps working.
        """
        if self.kind == KIND_BASKET:
            return float(self.metrics["return"][name])
        return float(self.metrics["strategy"][name])

    @property
    def total_return(self) -> float:
        return self._curve("total_return")

    @property
    def cagr(self) -> float:
        return self._curve("cagr")

    @property
    def max_dd(self) -> float:
        return self._curve("max_dd")

    @property
    def sharpe(self) -> float:
        return self._curve("sharpe")

    @property
    def closed_trades(self) -> int:
        if self.kind == KIND_BASKET:
            return int(sum(leg["trades"] for leg in self.metrics["legs"].values()))
        return int(self.metrics["closed_trades"])

    @property
    def bars(self) -> int:
        """How many bars the entry covers, across every series it holds."""
        if self.kind == KIND_BASKET:
            return int(sum(leg.bars for leg in self.legs.values()))
        return int(self.data.bars) if self.data else 0

    @property
    def coverage(self) -> str:
        """The symbol column of the report: one series, or how many legs a basket has."""
        if self.kind == KIND_BASKET:
            return f"{len(self.legs)} legs"
        return self.data.symbol if self.data else "?"

    @property
    def params_text(self) -> str:
        return ", ".join(f"{key}={value}" for key, value in sorted(self.params.items()))


@dataclass
class Verification:
    """The outcome of re-running one entry."""

    run_id: str
    verified_at: str
    status: str
    bars: int
    digest_recorded: str
    digest_actual: str
    mismatches: list[dict] = field(default_factory=list)
    message: str = ""
    tolerance: float = TOLERANCE
    trades_checked: bool = False

    @property
    def ok(self) -> bool:
        return self.status == STATUS_VERIFIED

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict) -> Verification:
        return cls(**payload)


# --- recording --------------------------------------------------------------


def entry_for(
    result: engine.BacktestResult,
    quality: QualityReport,
    bars: list[Bar],
    strategy_name: str,
    params: dict,
    symbol: str,
    timeframe: str,
    *,
    note: str = "",
    trades_file: str | None = None,
    recorded_at: str | None = None,
) -> RunEntry:
    """Build the journal entry for a finished run.

    `strategy_name` is the registry name (e.g. `sma-rev`), while `params` are the
    exact arguments it was built with, so `get_strategy(name, **params)`
    reconstructs it later.
    """
    recorded_at = recorded_at or utc_now()
    return RunEntry(
        run_id=f"{compact_stamp(recorded_at)}-{result.label}",
        recorded_at=recorded_at,
        label=result.label,
        strategy=strategy_name,
        params=dict(params),
        costs={
            "fee_per_side": result.costs.fee_per_side,
            "slippage_per_side": result.costs.slippage_per_side,
        },
        data=DataWindow.of(bars, quality, symbol, timeframe),
        metrics=result.as_dict(),
        note=note,
        trades_file=trades_file,
    )


def _performance_dict(performance) -> dict:
    """The comparable numbers of one curve, with no lists inside so `compare` can walk it."""
    return {
        "total_return": performance.total_return,
        "cagr": performance.cagr,
        "ann_vol": performance.ann_vol,
        "sharpe": performance.sharpe,
        "max_dd": performance.max_dd,
        "years": performance.years,
        "final_equity": performance.final_equity,
    }


def basket_metrics(result) -> dict:
    """The numbers a basket entry is verified against.

    Only floats and nested dicts: `compare` walks dicts, so a list (the legs of a result, say)
    would be compared as one opaque leaf and always look different.
    """
    return {
        "return": _performance_dict(result.performance),
        "benchmark": _performance_dict(result.benchmark),
        "fees_paid": result.fees_paid,
        "legs": {
            leg.symbol: {
                "total_return": leg.performance.total_return,
                "sharpe": leg.performance.sharpe,
                "trades": leg.trades,
                "exposure": leg.exposure,
                "fees": leg.fees,
                "contribution": leg.contribution,
            }
            for leg in result.legs
        },
    }


def basket_entry_for(
    result,
    series: dict[str, list[Bar]],
    timeframe: str,
    *,
    window: dict | None = None,
    costs_by_symbol: dict | None = None,
    note: str = "",
    trades_file: str | None = None,
    recorded_at: str | None = None,
):
    """Build the journal entry for a finished basket run.

    `series` is what the run actually saw — every leg's full history, including the warm-up
    before the reported window — because that is what has to come back byte for byte for the
    entry to be checkable later. The window itself is in `result.times`.
    """
    recorded_at = recorded_at or utc_now()
    # the label of a basket has spaces and brackets in it, which makes an id awkward to type
    # into `kcs-journal verify --id`, so the id is built from what identifies the run instead
    return RunEntry(
        run_id=(
            f"{compact_stamp(recorded_at)}-basket-{timeframe}-{len(series)}legs"
        ),
        recorded_at=recorded_at,
        label=result.label,
        strategy=result.strategy,
        params=dict(result.params),
        costs={
            "fee_per_side": result.costs.fee_per_side,
            "slippage_per_side": result.costs.slippage_per_side,
        },
        metrics=basket_metrics(result),
        legs={
            symbol: DataWindow.of(
                bars, data.data_quality(bars, timeframe), symbol, timeframe
            )
            for symbol, bars in series.items()
        },
        window=dict(window or {}),
        leg_costs={
            symbol: {
                "fee_per_side": cost.fee_per_side,
                "slippage_per_side": cost.slippage_per_side,
            }
            for symbol, cost in (costs_by_symbol or {}).items()
        },
        kind=KIND_BASKET,
        note=note,
        trades_file=trades_file,
    )


def verify_basket(
    entry: RunEntry,
    data_dir: Path | str = data.DEFAULT_DATA_DIR,
    tolerance: float = TOLERANCE,
) -> Verification:
    """Re-run a basket entry leg by leg and compare its numbers."""
    from .basket import run_basket

    verified_at = utc_now()
    combined_recorded = "|".join(
        f"{symbol}:{leg.digest}" for symbol, leg in sorted(entry.legs.items())
    )
    series: dict[str, list[Bar]] = {}
    actual_digests: list[str] = []
    data_changed = False
    timeframe = next(iter(entry.legs.values())).timeframe if entry.legs else ""

    for symbol, leg in sorted(entry.legs.items()):
        try:
            bars = data.load_series(data_dir, symbol, leg.timeframe)
        except (FileNotFoundError, ValueError) as error:
            return Verification(
                run_id=entry.run_id, verified_at=verified_at, status=STATUS_ERROR,
                bars=0, digest_recorded=combined_recorded, digest_actual="",
                message=f"{symbol}: {error}",
            )
        sliced = window_of(bars, leg.first, leg.last)
        if not sliced:
            return Verification(
                run_id=entry.run_id, verified_at=verified_at, status=STATUS_ERROR,
                bars=0, digest_recorded=combined_recorded, digest_actual="",
                message=f"{symbol}: the archive no longer holds {leg.first}..{leg.last}",
            )
        digest = fingerprint(sliced)
        actual_digests.append(f"{symbol}:{digest}")
        data_changed = data_changed or digest != leg.digest
        series[symbol] = sliced

    digest_actual = "|".join(actual_digests)
    costs = engine.Costs(
        fee_per_side=entry.costs["fee_per_side"],
        slippage_per_side=entry.costs["slippage_per_side"],
    )
    try:
        result = run_basket(
            series, entry.strategy, entry.params, timeframe, costs=costs, label=entry.label,
            window=entry.window.get("seconds"), since=entry.window.get("since"),
            costs_by_symbol={
                symbol: engine.Costs(**leg) for symbol, leg in entry.leg_costs.items()
            } or None,
        )
    except (KeyError, TypeError, ValueError) as error:
        return Verification(
            run_id=entry.run_id, verified_at=verified_at, status=STATUS_UNKNOWN_STRATEGY,
            bars=sum(len(bars) for bars in series.values()),
            digest_recorded=combined_recorded, digest_actual=digest_actual,
            message=f"cannot rebuild the basket: {error}",
        )

    mismatches = compare(entry.metrics, basket_metrics(result), tolerance=tolerance)
    if mismatches:
        status, message = STATUS_MISMATCH, f"{len(mismatches)} field(s) differ"
    elif data_changed:
        status = STATUS_DATA_CHANGED
        message = "metrics reproduce, but the stored bars are not the ones recorded"
    else:
        status, message = STATUS_VERIFIED, "every leg and every metric reproduces exactly"
    return Verification(
        run_id=entry.run_id, verified_at=verified_at, status=status,
        bars=sum(len(bars) for bars in series.values()),
        digest_recorded=combined_recorded, digest_actual=digest_actual,
        mismatches=mismatches, message=message, tolerance=tolerance,
    )


def runs_path(journal_dir: Path | str) -> Path:
    return Path(journal_dir) / RUNS_FILE


def verifications_path(journal_dir: Path | str) -> Path:
    return Path(journal_dir) / VERIFICATIONS_FILE


def trades_path(journal_dir: Path | str, run_id: str) -> Path:
    return Path(journal_dir) / TRADES_DIR / f"{run_id}.csv"


def append(entry: RunEntry, journal_dir: Path | str = DEFAULT_JOURNAL_DIR) -> Path:
    """Append one run to the journal, creating the directory if needed.

    Append-only on purpose: a run is a fact about a moment, so a new run never
    rewrites an old one. Re-recording the same run twice yields two entries with
    different ids, and `report` shows both.
    """
    directory = Path(journal_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = runs_path(directory)
    existing = {loaded.run_id for loaded in load_runs(directory)}
    run_id = entry.run_id
    suffix = 2
    while run_id in existing:
        run_id = f"{entry.run_id}-{suffix}"
        suffix += 1
    entry.run_id = run_id
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.to_json() + "\n")
    return path


def load_runs(journal_dir: Path | str = DEFAULT_JOURNAL_DIR) -> list[RunEntry]:
    """Every recorded run, in the order it was written.

    A hand-edited journal is a normal thing, so a bad line is reported with its
    number and the reason instead of a bare `KeyError`.
    """
    path = runs_path(journal_dir)
    entries: list[RunEntry] = []
    for number, line in _numbered_lines(path):
        try:
            entries.append(RunEntry.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{path}:{number} is not a valid journal entry: {error!r}") from error
    return entries


def load_verifications(journal_dir: Path | str = DEFAULT_JOURNAL_DIR) -> list[Verification]:
    path = verifications_path(journal_dir)
    verifications: list[Verification] = []
    for number, line in _numbered_lines(path):
        try:
            verifications.append(Verification.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{path}:{number} is not a valid verification: {error!r}") from error
    return verifications


def record_verification(
    verification: Verification, journal_dir: Path | str = DEFAULT_JOURNAL_DIR
) -> Path:
    directory = Path(journal_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = verifications_path(directory)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(verification.to_json() + "\n")
    return path


def latest_status(journal_dir: Path | str = DEFAULT_JOURNAL_DIR) -> dict[str, Verification]:
    """The most recent verification per run id."""
    latest: dict[str, Verification] = {}
    for verification in load_verifications(journal_dir):
        latest[verification.run_id] = verification
    return latest


def _numbered_lines(path: Path):
    """`(line_number, text)` for every non-empty line of a JSONL file, if it exists."""
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            yield number, line


# --- verification -----------------------------------------------------------


def verify(
    entry: RunEntry,
    data_dir: Path | str = data.DEFAULT_DATA_DIR,
    tolerance: float = TOLERANCE,
    journal_dir: Path | str | None = None,
) -> Verification:
    """Re-run a recorded entry against the archive and compare every number.

    The archive is sliced back to the recorded window first, so entries stay
    checkable after `kcs-klines backfill` appends more bars. The status tells the
    three failure modes apart: `data-changed` (the stored bars are not what they
    were), `unknown-strategy` (the registry lost the strategy) or `mismatch`
    (the same inputs no longer produce the same output).
    """
    if entry.kind == KIND_BASKET:
        return verify_basket(entry, data_dir, tolerance)

    verified_at = utc_now()
    try:
        bars = data.load_series(data_dir, entry.data.symbol, entry.data.timeframe)
    except (FileNotFoundError, ValueError) as error:
        return Verification(
            run_id=entry.run_id,
            verified_at=verified_at,
            status=STATUS_ERROR,
            bars=0,
            digest_recorded=entry.data.digest,
            digest_actual="",
            message=str(error),
        )

    sliced = window_of(bars, entry.data.first, entry.data.last)
    if not sliced:
        return Verification(
            run_id=entry.run_id,
            verified_at=verified_at,
            status=STATUS_ERROR,
            bars=len(bars),
            digest_recorded=entry.data.digest,
            digest_actual="",
            message=(
                f"the archive no longer holds the recorded window "
                f"({entry.data.first}..{entry.data.last})"
            ),
        )

    digest_actual = fingerprint(sliced)
    data_changed = digest_actual != entry.data.digest

    try:
        strategy = get_strategy(entry.strategy, **entry.params)
    except (KeyError, TypeError, ValueError) as error:
        return Verification(
            run_id=entry.run_id,
            verified_at=verified_at,
            status=STATUS_UNKNOWN_STRATEGY,
            bars=len(sliced),
            digest_recorded=entry.data.digest,
            digest_actual=digest_actual,
            message=f"cannot rebuild {entry.strategy}({entry.params_text}): {error}",
        )

    costs = engine.Costs(
        fee_per_side=entry.costs["fee_per_side"],
        slippage_per_side=entry.costs["slippage_per_side"],
    )
    result = engine.run_backtest(
        sliced, strategy.targets(sliced), entry.data.timeframe, costs, label=entry.label
    )
    mismatches = compare(entry.metrics, result.as_dict(), tolerance=tolerance)
    trades_checked = False
    if journal_dir is not None and entry.trades_file:
        trades_checked = True
        mismatches.extend(verify_trades_file(entry, Path(journal_dir), result, tolerance))

    if mismatches:
        status = STATUS_MISMATCH
        message = f"{len(mismatches)} field(s) differ"
    elif data_changed:
        status = STATUS_DATA_CHANGED
        message = "metrics reproduce, but the stored bars are not the ones recorded"
    else:
        status = STATUS_VERIFIED
        message = "metrics and data reproduce exactly"

    return Verification(
        run_id=entry.run_id,
        verified_at=verified_at,
        status=status,
        bars=len(sliced),
        digest_recorded=entry.data.digest,
        digest_actual=digest_actual,
        mismatches=mismatches,
        message=message,
        tolerance=tolerance,
        trades_checked=trades_checked,
    )


def verify_trades_file(
    entry: RunEntry,
    journal_dir: Path,
    result: engine.BacktestResult,
    tolerance: float = TOLERANCE,
) -> list[dict]:
    """Re-check the per-trade table recorded next to a run, if there is one.

    Compared row by row against the re-run rather than in aggregate, because the
    CSV stores returns rounded to six decimals: an aggregate check would have to
    absorb that rounding and would then miss a small edit. Per-row tolerances are
    the file's own precision — `1e-8` relative on prices, `1e-6` absolute on
    returns — so anything a human typed shows up.
    """
    path = journal_dir / TRADES_DIR / str(entry.trades_file)
    if not path.is_file():
        return [{"field": "trades_file", "recorded": entry.trades_file, "actual": "missing"}]

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    if len(rows) != len(result.trades):
        return [{"field": "trades.rows", "recorded": len(rows), "actual": len(result.trades)}]

    price_tolerance = 1e-8
    return_tolerance = 1e-6
    found: list[dict] = []
    for index, (row, trade) in enumerate(zip(rows, result.trades)):
        expected = {
            "side": trade.side,
            "entry_time_utc": iso(trade.entry_time),
            "exit_time_utc": iso(trade.exit_time or 0),
            "bars_held": str(trade.bars_held),
            "open_at_end": "yes" if trade.open_at_end else "",
        }
        for field, want in expected.items():
            if row.get(field) != want:
                found.append({"field": f"trades[{index}].{field}", "recorded": row.get(field), "actual": want})
        for field, want, relative in (
            ("entry_price", trade.entry_price, price_tolerance),
            ("exit_price", trade.exit_price or 0.0, price_tolerance),
            ("gross_return", trade.gross_return or 0.0, return_tolerance),
            ("net_return", trade.net_return or 0.0, return_tolerance),
        ):
            try:
                got = float(row[field])
            except (KeyError, TypeError, ValueError):
                found.append({"field": f"trades[{index}].{field}", "recorded": row.get(field), "actual": want})
                continue
            allowed = abs(want) * relative if relative == price_tolerance else return_tolerance
            if abs(got - want) > allowed:
                found.append({"field": f"trades[{index}].{field}", "recorded": got, "actual": want})
        if len(found) >= 5:  # enough to see what happened; the rest is noise
            found.append({"field": "trades", "recorded": f"{index + 1} rows checked", "actual": "more…"})
            break
    return found


def compare(
    expected: dict, actual: dict, *, tolerance: float = TOLERANCE, path: str = ""
) -> list[dict]:
    """Numeric leaves that differ between two metric trees, relative to `expected`.

    Walks nested dicts, so a metric added to the engine later is compared without
    anyone remembering to add it here.
    """
    found: list[dict] = []
    for key in sorted(set(expected) | set(actual)):
        location = f"{path}.{key}" if path else key
        if key not in expected or key not in actual:
            found.append({"field": location, "recorded": expected.get(key), "actual": actual.get(key)})
            continue
        want, got = expected[key], actual[key]
        if isinstance(want, dict) and isinstance(got, dict):
            found.extend(compare(want, got, tolerance=tolerance, path=location))
        elif isinstance(want, bool) or isinstance(got, bool):
            continue
        elif isinstance(want, (int, float)) and isinstance(got, (int, float)):
            scale = max(abs(want), abs(got), 1e-12)
            if abs(want - got) / scale > tolerance:
                found.append({"field": location, "recorded": want, "actual": got})
        elif want != got:
            found.append({"field": location, "recorded": want, "actual": got})
    return found


# --- reading the journal ----------------------------------------------------

STATUS_MARK = {
    STATUS_VERIFIED: "ok",
    STATUS_DATA_CHANGED: "data!",
    STATUS_MISMATCH: "FAIL",
    STATUS_UNKNOWN_STRATEGY: "??",
    STATUS_ERROR: "err",
}


def report_lines(
    entries: list[RunEntry], statuses: dict[str, Verification] | None = None
) -> list[str]:
    """A console table of the journal, oldest run last."""
    statuses = statuses or {}
    lines = [
        f"{'run':<30}{'strategy':<18}{'symbol':<11}{'bars':>7}{'total':>11}{'CAGR':>9}"
        f"{'maxDD':>9}{'Sharpe':>8}{'trades':>8}{'check':>7}"
    ]
    lines.append("-" * len(lines[0]))
    for entry in sorted(entries, key=lambda item: item.recorded_at, reverse=True):
        verification = statuses.get(entry.run_id)
        mark = STATUS_MARK.get(verification.status, "—") if verification else "—"
        lines.append(
            f"{entry.run_id[:29]:<30}{entry.label[:17]:<18}{entry.coverage:<11}"
            f"{entry.bars:>7}{pct(entry.total_return):>11}{pct(entry.cagr):>9}"
            f"{pct(entry.max_dd):>9}{entry.sharpe:>8.2f}{entry.closed_trades:>8}{mark:>7}"
        )
    return lines


def summary_lines(entries: list[RunEntry], statuses: dict[str, Verification] | None = None) -> list[str]:
    """Per-strategy roll-up: how each strategy has done across recorded runs."""
    statuses = statuses or {}
    grouped: dict[tuple[str, str], list[RunEntry]] = {}
    for entry in entries:
        grouped.setdefault((entry.strategy, entry.params_text), []).append(entry)

    lines = [f"{'strategy':<34}{'runs':>5}{'last total':>13}{'best':>11}{'worst':>11}{'verified':>10}"]
    lines.append("-" * len(lines[0]))
    for (name, params), group in sorted(grouped.items()):
        latest = max(group, key=lambda item: item.recorded_at)
        totals = [item.total_return for item in group]
        checks = [statuses.get(item.run_id) for item in group]
        verified = sum(1 for check in checks if check and check.ok)
        label = f"{name}({params})" if params else name
        lines.append(
            f"{label:<34}{len(group):>5}{pct(latest.total_return):>13}"
            f"{pct(max(totals)):>11}{pct(min(totals)):>11}{f'{verified}/{len(group)}':>10}"
        )
    return lines


def show_lines(entry: RunEntry) -> list[str]:
    """Everything stored about one run, for a human reading the file."""
    payload = json.loads(entry.to_json())
    lines = [
        f"run          : {entry.run_id}",
        f"recorded     : {entry.recorded_at}",
        f"strategy     : {entry.strategy}({entry.params_text})",
        f"data         : {entry.data.symbol} {entry.data.timeframe}, {entry.data.bars:,} bars, "
        f"digest {entry.data.digest[:16]}…" if entry.data else
        f"data         : basket of {len(entry.legs)} legs, {entry.bars:,} bars, "
        f"{next(iter(entry.legs.values())).timeframe}",
        f"costs        : fee {entry.costs['fee_per_side']:.4%}/side, "
        f"slippage {entry.costs['slippage_per_side']:.4%}/side",
    ]
    if entry.note:
        lines.append(f"note         : {entry.note}")
    if entry.trades_file:
        lines.append(f"trades file  : {entry.trades_file}")
    lines.append("")
    lines.append(json.dumps(payload["metrics"], indent=2, ensure_ascii=False, sort_keys=True))
    return lines


def journal_dir_for(raw: str | None) -> Path:
    """`--journal` with no value means the default directory next to the repo root."""
    if raw in (None, ""):
        return DEFAULT_JOURNAL_DIR
    return Path(raw)


# --- CLI --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcs-journal",
        description="Read, verify and summarise the trade journal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--data-dir", type=Path, default=data.DEFAULT_DATA_DIR)
    parser.add_argument("--tolerance", type=float, default=TOLERANCE)
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="table of recorded runs and their checks")
    report.add_argument("--last", type=int, default=None, help="only the newest N runs")
    report.add_argument("--no-summary", action="store_true", help="skip the per-strategy roll-up")

    verify = sub.add_parser("verify", help="re-run entries and compare every metric")
    verify.add_argument("--id", default=None, help="verify one run id (prefix is enough)")
    verify.add_argument("--last", type=int, default=None, help="verify the newest N runs")
    verify.add_argument("--no-record", action="store_true", help="do not append to verifications.jsonl")
    verify.add_argument("--quiet", action="store_true", help="only failures and the exit code")

    show = sub.add_parser("show", help="print one entry in full")
    show.add_argument("--id", required=True)
    return parser.parse_args(argv)


def select(entries: list[RunEntry], *, run_id: str | None = None, last: int | None = None) -> list[RunEntry]:
    selected = sorted(entries, key=lambda item: item.recorded_at)
    if run_id:
        matches = [item for item in selected if item.run_id.startswith(run_id)]
        if not matches:
            raise SystemExit(f"no run id starts with {run_id!r}")
        if len(matches) > 1:
            raise SystemExit(f"{run_id!r} matches {len(matches)} runs; use a longer prefix")
        return matches
    if last is not None:
        return selected[-last:]
    return selected


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    entries = load_runs(args.journal_dir)

    if args.command == "report":
        if not entries:
            print(f"journal is empty ({runs_path(args.journal_dir)})")
            return 0
        statuses = latest_status(args.journal_dir)
        for line in report_lines(select(entries, last=args.last), statuses):
            print(line)
        if not args.no_summary:
            print()
            for line in summary_lines(entries, statuses):
                print(line)
        return 0

    if args.command == "show":
        entry = select(entries, run_id=args.id)[0]
        for line in show_lines(entry):
            print(line)
        return 0

    if not entries:
        print(f"journal is empty ({runs_path(args.journal_dir)}); nothing to verify")
        return 0

    chosen = select(entries, run_id=args.id, last=args.last)
    failures = 0
    for entry in chosen:
        verification = verify(entry, args.data_dir, args.tolerance, args.journal_dir)
        if not args.no_record:
            record_verification(verification, args.journal_dir)
        failed = not verification.ok
        failures += int(failed)
        if failed or not args.quiet:
            print(f"{STATUS_MARK.get(verification.status, '?'):>5} {entry.run_id}: {verification.message}")
            for mismatch in verification.mismatches[:10]:
                print(
                    f"        {mismatch['field']}: recorded {mismatch['recorded']!r} "
                    f"!= actual {mismatch['actual']!r}"
                )
            if len(verification.mismatches) > 10:
                print(f"        … and {len(verification.mismatches) - 10} more")
    print(f"\n{len(chosen) - failures}/{len(chosen)} runs verified")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    raise SystemExit(main())
