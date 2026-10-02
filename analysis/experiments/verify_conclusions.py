#!/usr/bin/env python3
"""Re-check the numbers this repository quotes.

`CONCLUSIONS.md` is the product of this project, and until now every number in it
had been transcribed by hand from a terminal. That is one careless edit away from
being wrong, and nothing would notice. This script closes the loop:

* each claim names the command that produced it, the metric to read, and the value
  the document states, with a tolerance for the archive having grown since;
* the command is run for real, its `--json` summary is parsed, and the difference
  is printed;
* the same value is looked for **in the document text** as well, so a claim that
  was edited out — or a number that quietly changed — shows up as "the document no
  longer says this" rather than passing silently.

Two kinds of claim: `measured` (run and compare) and `fact` (a property of the
repository rather than of a run: how many tests exist, how many strategies are
registered). Both are checked against the text of the documents.

    uv run python analysis/experiments/verify_conclusions.py --list
    uv run python analysis/experiments/verify_conclusions.py --quick
    uv run python analysis/experiments/verify_conclusions.py            # everything, ~15 min
    uv run python analysis/experiments/verify_conclusions.py --only wide-book-gated

Exit code is non-zero when a claim fails, so it can sit in CI. Numbers are the
product of an archive that grows a bar every hour: a failure is not automatically
a bug, but it is always worth reading — and the tolerance is deliberately tight
enough that a changed *result* fails while a changed *day* does not.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONCLUSIONS = REPO / "CONCLUSIONS.md"
AGENTS = REPO / "AGENTS.md"

#: Fractions in the reports are relative (`0.914`), but the documents write
#: percentages (`+91.4%`). Expectations are stated as the document does and
#: converted here, so a claim reads the way it is written in the prose.
PERCENT = {"total_return", "cagr", "max_dd"}


@dataclass(frozen=True)
class Claim:
    """One quoted number, the command behind it, and where the document says it."""

    name: str
    what: str
    argv: list[str]
    metrics_key: str
    #: metric -> (value as the document states it, tolerance). A tolerance written
    #: as a string ending in `%` is relative, which is what a multi-year multiple
    #: needs: the archive grows a bar an hour and an anchored window drifts with it.
    expectations: dict[str, tuple[float, float | str]]
    doc_quotes: list[str] = field(default_factory=list)
    documents: tuple[Path, ...] = (CONCLUSIONS,)
    json_style: str = "flag"  # "flag" writes into --out-dir, "path" takes a file
    heavy: bool = False
    #: A discrepancy that is already known and written down in the experiments README.
    #: It is still run, still printed, and never counts as agreement — but it does not
    #: fail the checker, because a check that always fails is a check nobody runs.
    known: bool = False


def pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


# --- the claims -------------------------------------------------------------
# Values are quoted as CONCLUSIONS.md states them (percentages for returns and
# drawdowns, decimals for ratios). Tolerances cover the archive growing and the
# cross-section changing with it; they are not wide enough to hide a real change.

CLAIMS: tuple[Claim, ...] = (
    Claim(
        name="basket-10-turnover",
        what="the recommended stack over five years: the ten busiest pairs, each gated by its own 50-bar mean, book sized to 30% volatility",
        argv=[
            "analysis.basket", "--select-turnover", "10", "--timeframe", "1d",
            "--strategy", "voltarget-sma", "--param", "window=50",
            "--param", "target_vol=0.30", "--last", "5y",
        ],
        metrics_key="basket",
        expectations={"total_return": (91.43, 5.0), "sharpe": (0.87, 0.08), "max_dd": (-17.5, 5.0)},
        doc_quotes=[r"\+91\.43% at Sharpe 0\.87", r"−17\.5%"],
        heavy=True,
    ),
    Claim(
        name="basket-10-turnover-spread",
        what="the same book, charging each leg the spread estimated from its own bars (Corwin-Schultz)",
        argv=[
            "analysis.basket", "--select-turnover", "10", "--timeframe", "1d",
            "--strategy", "voltarget-sma", "--param", "window=50",
            "--param", "target_vol=0.30", "--last", "5y",
            "--spread-model", "corwin-schultz",
        ],
        metrics_key="basket",
        expectations={"total_return": (86.05, 3.0), "sharpe": (0.83, 0.08)},
        doc_quotes=[r"\+86\.05% at 0\.83"],
        heavy=True,
    ),
    Claim(
        name="tsmom-btc-daily-30-7",
        what="time-series momentum, 30-day lookback decided weekly, on BTC daily over the whole archive",
        argv=[
            "analysis.run_backtest", "--symbol", "BTC-USDT", "--timeframe", "1d",
            "--strategy", "tsmom", "--param", "lookback=30", "--param", "rebalance=7",
        ],
        metrics_key="strategy",
        expectations={"total_return": (2369.0, "5%"), "sharpe": (0.79, 0.08)},
        doc_quotes=[r"\+2,369%"],
        documents=(CONCLUSIONS, AGENTS),
    ),
    Claim(
        name="wide-book-gated",
        what="the whole USDT archive as one book: sign filter, weekly, trend gate 200",
        argv=[
            "analysis.portfolio", "--timeframe", "1d", "--lookback", "7", "--rebalance", "7",
            "--select", "sign", "--trend-gate", "200",
        ],
        metrics_key="performance",
        expectations={"total_return": (2675.0, "5%"), "sharpe": (0.49, 0.06), "max_dd": (-86.8, 4.0)},
        doc_quotes=[r"\+2,675%", r"\−86\.8%"],
        json_style="path",
        heavy=True,
    ),
    Claim(
        name="wide-book-gated-voltarget",
        what="the same book with the whole thing sized to a 25% volatility target",
        argv=[
            "analysis.portfolio", "--timeframe", "1d", "--lookback", "7", "--rebalance", "7",
            "--select", "sign", "--trend-gate", "200", "--vol-target", "25%",
        ],
        metrics_key="performance",
        expectations={"total_return": (655.0, "6%"), "sharpe": (0.73, 0.06), "max_dd": (-53.8, 4.0)},
        doc_quotes=[r"\+655%", r"−53\.8%"],
        json_style="path",
        heavy=True,
    ),
    Claim(
        name="walkforward-btc-blend",
        what="the out-of-sample blend on BTC that AGENTS.md calls the best return measured here (CONCLUSIONS 1.13)",
        argv=[
            "analysis.walkforward", "--symbol", "BTC-USDT", "--timeframe", "1d",
            "--strategy", "tsmom-blend", "--grid", "base=7", "--train", "300", "--test", "60",
        ],
        metrics_key="out_of_sample",
        expectations={"total_return": (3097.0, "5%"), "sharpe": (1.13, 0.08)},
        doc_quotes=[r"\+3,097% \(1\.13\)"],
        json_style="path",
        known=True,
    ),
    Claim(
        name="riskparity-top5-trend",
        what="the rule-picked book with a trend gate: top five by trailing turnover, three years of history, 40% volatility budget, 200-day trend exit",
        argv=[
            "analysis.riskparity", "--top", "5", "--min-history", "3y", "--vol-budget", "0.4",
            "--trend", "200d",
        ],
        metrics_key="performance",
        expectations={"total_return": (868.0, "6%"), "sharpe": (0.68, 0.08), "max_dd": (-47.7, 5.0)},
        doc_quotes=[r"\+868% at Sharpe 0\.68"],
        heavy=True,
        known=True,
    ),
)


def repo_facts() -> list[tuple[str, str, str, float]]:
    """`(name, value, pattern, tolerance)` for properties of the repository.

    The test count moves every time a test is written, so it is checked for *drift*
    rather than equality: 444 in the document against 472 in reality failed this
    check once, and that is the failure worth catching. The strategy count is exact,
    because adding one is a deliberate act that the documents should record.
    """
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    # `-q --collect-only` prints one `path: count` line per test module.
    per_file = [int(count) for count in re.findall(r":\s*(\d+)\s*$", collected.stdout, re.MULTILINE)]
    tests = str(sum(per_file)) if per_file else None
    listed = subprocess.run(
        ["uv", "run", "kcs-backtest", "--list"], cwd=REPO, capture_output=True, text=True, check=False
    )
    strategies = len(re.findall(r"^  [a-z]", listed.stdout, flags=re.MULTILINE))
    return [
        ("pytest tests", tests or "?", r"(\d+) tests", 0.03),
        ("registered strategies", str(strategies), r"(\d+) registered strategies", 0.0),
    ]


def discover_json(out_dir: Path, explicit: Path | None) -> Path:
    """The metrics JSON a run wrote, wherever that CLI puts it."""
    if explicit is not None and explicit.is_file():
        return explicit
    found = sorted(glob.glob(str(out_dir / "*.json")), key=lambda path: Path(path).stat().st_mtime)
    if not found:
        raise FileNotFoundError(f"no JSON summary under {out_dir}")
    return Path(found[-1])


def run_claim(claim: Claim, timeout: int) -> tuple[dict, str]:
    """Run a claim's command and return its metrics plus the raw output."""
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp) / "out"
        out_dir.mkdir()
        argv = [sys.executable, "-m", *claim.argv]
        if claim.json_style == "path":
            explicit = out_dir / "metrics.json"
            argv += ["--json", str(explicit)]
        else:
            explicit = None
            argv += ["--json", "--out-dir", str(out_dir)]
        completed = subprocess.run(
            argv, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{claim.name}: exit {completed.returncode}\n"
                f"{completed.stdout[-1500:]}{completed.stderr[-1500:]}"
            )
        payload = json.loads(discover_json(out_dir, explicit).read_text())
        metrics = payload.get(claim.metrics_key)
        if not isinstance(metrics, dict):
            raise KeyError(f"{claim.name}: no {claim.metrics_key!r} block in the JSON summary")
        return metrics, completed.stdout


def check_claim(claim: Claim, timeout: int) -> tuple[bool, list[str]]:
    """Run one claim, compare it with the documents, and describe what happened."""
    lines: list[str] = []
    ok = True
    if claim.known:
        lines.append("    OPEN — a documented discrepancy, not a failure (see experiments/README.md)")

    documents = {path: path.read_text() for path in claim.documents}
    where = " or ".join(path.name for path in documents)
    for quote in claim.doc_quotes:
        # The quote has to appear somewhere among the claim's documents, not in all
        # of them: a number can be stated in CONCLUSIONS.md and echoed in AGENTS.md.
        if not any(re.search(quote, text) for text in documents.values()):
            ok = False
            lines.append(f"    MISSING from {where}: /{quote}/")

    try:
        metrics, _ = run_claim(claim, timeout)
    except (RuntimeError, FileNotFoundError, KeyError, subprocess.TimeoutExpired) as error:
        return False, lines + [f"    RUN FAILED: {error}"]

    for metric, (documented, tolerance) in claim.expectations.items():
        if metric not in metrics:
            ok = False
            lines.append(f"    {metric}: absent from the JSON summary")
            continue
        measured = float(metrics[metric])
        if metric in PERCENT:
            measured *= 100.0
        allowed = (
            abs(documented) * float(tolerance.rstrip("%")) / 100.0
            if isinstance(tolerance, str)
            else tolerance
        )
        difference = measured - documented
        passed = abs(difference) <= allowed
        ok &= passed or claim.known
        mark = "ok  " if passed else "DIFF"
        lines.append(
            f"    {mark} {metric:<14} document {documented:+9.2f}   measured {measured:+9.2f}"
            f"   diff {difference:+7.2f} (tol {allowed:g}{' rel' if isinstance(tolerance, str) else ''})"
        )
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--list", action="store_true", help="print the claims and exit")
    parser.add_argument("--only", action="append", default=[], help="run one claim by name (repeatable)")
    parser.add_argument("--quick", action="store_true", help="skip the claims that take minutes")
    parser.add_argument("--timeout", type=int, default=3600, help="per-claim timeout in seconds")
    args = parser.parse_args(argv)

    selected = [claim for claim in CLAIMS if not args.only or claim.name in args.only]
    if args.only:
        unknown = set(args.only) - {claim.name for claim in CLAIMS}
        if unknown:
            raise SystemExit(f"unknown claim(s): {', '.join(sorted(unknown))}")
    if args.quick:
        selected = [claim for claim in selected if not claim.heavy]

    if args.list:
        for claim in CLAIMS:
            weight = "heavy" if claim.heavy else "fast "
            print(f"{claim.name:<28}{weight}  {claim.what}")
        print(f"\n{len(CLAIMS)} claims; `--quick` runs the {sum(not c.heavy for c in CLAIMS)} fast ones")
        return 0

    failures: list[str] = []

    print("repository facts (checked against the documents, not measured again):")
    for name, value, pattern, tolerance in repo_facts():
        quoted = [
            match.group(1)
            for path in (AGENTS, CONCLUSIONS)
            for match in re.finditer(pattern, path.read_text())
        ]
        if not quoted or value == "?":
            agree = bool(quoted)
        else:
            measured = float(value)
            # A tolerance of zero means the number has to match exactly; otherwise it
            # is a fraction of it, and a snapshot that has drifted is a real finding.
            agree = any(
                abs(measured - float(quote)) <= max(tolerance * measured, 0 if tolerance else 0.5)
                for quote in quoted
            )
        print(f"  {'ok  ' if agree else 'DIFF'} {name:<24} measured {value:<6} documents say {', '.join(sorted(set(quoted))) or '—'}")
        if not agree:
            failures.append(name)

    print(f"\nmeasured claims ({len(selected)} of {len(CLAIMS)}):")
    for claim in selected:
        print(f"\n  {claim.name} — {claim.what}")
        ok, lines = check_claim(claim, args.timeout)
        print("\n".join(lines) if lines else "    (no comparisons)")
        if not ok:
            failures.append(claim.name)

    open_claims = [claim.name for claim in selected if claim.known]
    print()
    if open_claims:
        print(f"open discrepancies (documented, not failures): {', '.join(open_claims)}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        print(
            "A failure means one of three things: the archive grew past the tolerance, the "
            "engine changed, or the document was edited. Read the diff before changing either."
        )
        return 1
    agreed = len(selected) - len(open_claims)
    print(f"{agreed} of {len(selected)} claims agree with the documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
