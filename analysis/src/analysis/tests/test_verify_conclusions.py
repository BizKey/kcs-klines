"""The audit script's own guards.

`analysis/experiments/verify_conclusions.py` sits outside the installed package, because it
audits the package rather than shipping with it. That means nothing else in this suite would
notice if its checks started lying — and on 2026-10-19 one of them did: the "registered
strategies" fact shelled out to `uv run kcs-backtest --list`, `uv` was not on PATH, and
`check=False` turned the empty output into a count of **0** against the documents' 22. A gate
that fails for the wrong reason is worse than no gate, so the two cases below pin the fix:
the count must come from the registry, and an empty listing must be an error rather than a zero.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]


def _audit_module():
    """Load the experiments script as a module (it lives outside the package)."""
    path = REPO / "analysis" / "experiments" / "verify_conclusions.py"
    spec = importlib.util.spec_from_file_location("_verify_conclusions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_verify_conclusions", module)
    spec.loader.exec_module(module)
    return module


def test_the_fact_check_counts_every_registered_strategy():
    from ..strategies import available

    facts = {name: measured for name, measured, _stated, _band in _audit_module().repo_facts()}
    assert int(facts["registered strategies"]) == len(available())


def test_an_empty_listing_is_an_error_and_not_a_count_of_zero(monkeypatch):
    """The failure the real run hit: no output must not read as "no strategies"."""
    module = _audit_module()

    def empty(*args, **kwargs):
        return subprocess.CompletedProcess(args=["--list"], returncode=1, stdout="", stderr="uv: not found")

    monkeypatch.setattr(module.subprocess, "run", empty)
    with pytest.raises(RuntimeError, match="could not list the strategies"):
        module.repo_facts()
