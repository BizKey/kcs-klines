# experiments

Scripts that check the repository's own claims. They are not part of the installed
package (`analysis/src/analysis/`), because they exist to audit it rather than to be
imported by it, and they are deliberately plain: a script nobody can read is a claim
nobody can check.

## `verify_conclusions.py` — do the quoted numbers still hold?

`CONCLUSIONS.md` is the product of this project, and every number in it was
transcribed by hand from a terminal until this script existed. It re-runs the
commands behind the headline claims and compares what comes back with what the
document says, twice: the **measured** value against the stated one, and the stated
one against the **document text**, so a claim edited out of prose is caught as well
as a number that moved.

```bash
uv run python analysis/experiments/verify_conclusions.py --list
uv run python analysis/experiments/verify_conclusions.py --quick      # ~1 min
uv run python analysis/experiments/verify_conclusions.py              # ~15 min
uv run python analysis/experiments/verify_conclusions.py --only basket-10-turnover
```

Exit code is non-zero when a claim fails, so it can sit in CI. Two kinds of claim:

* **measured** — a command, the metric to read from its `--json` summary, and the
  value the document states with a tolerance. Returns and drawdowns are written the
  way the prose writes them (percentages); a tolerance ending in `%` is relative,
  which is what a multi-year multiple needs, because the archive grows a bar an hour
  and an anchored window drifts with it.
* **fact** — a property of the repository rather than of a run (how many tests exist,
  how many strategies are registered), checked against the documents with a 3% band
  so that writing a test does not fail the check while real drift does. It has already
  caught one: `§8` claimed 444 tests when the suite held 472.

### Reading a failure

A failure means one of three things and the printed diff says which:

1. **The archive grew** past the tolerance. Normal for the tight claims; widen the
   tolerance only with the diff in front of you.
2. **The engine changed.** Re-run the command by hand and compare before touching
   either document.
3. **The document was edited.** The `MISSING from …` line names the quote that no
   longer appears.

### Open discrepancies

Two quoted numbers do not currently reproduce, and both are tracked here rather than
hidden: the checker still runs them and still prints the diff, but they do not fail it
(`known=True` in the claim), because a check that always fails is a check nobody runs.

* **`walkforward-btc-blend` — the worst of the two.** `CONCLUSIONS.md` §1.13 and
  `AGENTS.md` both quote **+3,097% at Sharpe 1.13** for `tsmom-blend` on BTC out of
  sample, called "the best return measured anywhere in this repository on BTC". The
  documented harness (`--train 300 --test 60`, short base) now produces **+1,234% at
  Sharpe 0.93** with `base=7`, and widening the grid makes it worse rather than better
  (`base=7,30` → +511%, `base=7,30,60,120` → +143%). Every variant is still positive and
  still beats holding on risk, so the *claim* survives; the **number** does not. Either the
  grid the table was built from was different, or the number predates the timing fixes that
  forced every portfolio figure to be re-measured (`AGENTS.md`, "Gotchas"). The out-of-sample
  walk-forward figures have not had that treatment, and this is the one worth doing first,
  because it is the most quoted.
* **`riskparity-top5-trend` — churn, not a bug.** It measures **+958%** where §1.4 states
  **+868%**, with matching Sharpe (0.67 vs 0.68) and drawdown (−49.6% vs −47.7%).
  `kcs-riskparity` re-selects its top five names every rebalance, so the newest week of the
  archive changes which names qualify and a five-name book moves by tens of points.

### Known churn-sensitive claim

`riskparity-top5-trend` currently measures **+958%** where `§1.4` states **+868%**,
with matching Sharpe (0.67 vs 0.68) and a matching drawdown (−49.6% vs −47.7%). The
gap is not a bug: `kcs-riskparity` re-selects its top five names every rebalance, so
the newest week of the archive changes which names qualify and a five-name book moves
by tens of points. The claim is left at a 6% band on purpose — it fails until someone
decides whether the document's number, the configuration, or the tolerance is wrong.
This is the fragility `CONCLUSIONS.md` warns about when it says to treat a single
multiple as noise.

### A caveat about running it

Do not edit the package while a verification run is in flight. The script starts each
claim in a new interpreter, and a file being rewritten mid-import produces a traceback
that has nothing to do with the code being tested (this happened once and looked like
a bug in `portfolio.render`).

## `dashboard_check.mjs` — running the dashboard page without a browser

The main page is TradingView's Lightweight Charts, which needs a real DOM, so nothing here
could execute it — and that is how the sidebar shipped a percentage that followed the wrong
window and a sort that could not put the losers on top. This script fakes the three things
the page needs and then asserts on behaviour rather than on syntax:

* **a DOM built from the page's own markup** — every `id`, every `<button>` and its
  `data-` attributes are parsed out of the HTML, so adding a control to the page does not
  mean editing the stub;
* **a `LightweightCharts` that records calls** — which series were created in which pane,
  what data they received, which price-scale modes were applied;
* **a `fetch` serving synthetic series** with a constant drift per symbol, so every
  expected percentage is exact.

```bash
node analysis/experiments/dashboard_check.mjs
```

It checks, among other things, the default order of the sidebar, that the ↓/↑ toggle puts
the biggest losers on top, that the Δ window really switches between one bar and 30 days
(`−13.96%` for a symbol that falls 0.5% a bar, verified to the second decimal), that the
turnover floor hides rows, that the scale buttons reach the price scale, that MACD lands in
its own pane, and that `/api/stats` reaches the stats line. Removing the sort direction on
purpose makes it fail with three named assertions, which is the point.

## `diff_page.py` — reading a change set

`git diff` in a terminal is fine for one file and unreadable for twenty. This renders the
working tree — modified, added, deleted and untracked files alike — as one self-contained
HTML page with a file list, per-file diffs, line numbers and change counts:

```bash
uv run python analysis/experiments/diff_page.py --open      # analysis/out/changes.html
uv run python analysis/experiments/diff_page.py --staged    # what is in the index
```

It never touches the index: untracked files are diffed against an empty file rather than
staged with `git add -N`, so reviewing cannot change what the next commit contains.
Generated artifacts under `analysis/out/` and caches are skipped.

## `render_check.mjs` — drawing the dashboard without a browser

The dashboard is a canvas page, so nothing in the Python suite notices a broken
drawing call, and there is no browser in the environment this repository is developed
in. This script stubs the DOM, feeds the page a real series, and drives every
interaction the page wires up — range buttons, zoom, pan, crosshair, SMA overlays, the
log scale, reset — then fails if anything throws or if the starting window collapses
back to a handful of bars (a regression it has already caught once: the chart used to
open on 260 bars regardless of the screen, which parked it at the right edge).

```bash
uv run python - <<'EOF'
import json, sys; sys.path.insert(0, "analysis/src")
from analysis import dashboard, data
archive = dashboard.Archive(data.DEFAULT_DATA_DIR)
bars = archive.bars("BTC-USDT", "1d")
payload = dashboard.bars_payload(bars, bars_wanted=5000)
payload.update(symbol="BTC-USDT", timeframe="1d", series_bars=len(bars))
json.dump({"bars": payload, "rows": archive.index("1d")[:50]}, open("/tmp/payload.json", "w"))
EOF
node analysis/experiments/render_check.mjs /tmp/payload.json
```

## Adding a claim

Add a `Claim(...)` to the tuple in `verify_conclusions.py`: name, the command as a
list of arguments (`analysis.basket`, `--symbols`, …), the JSON block to read
(`basket`, `performance`, `strategy`), the documented values with tolerances, and the
quotes as regular expressions. Then run it once with `--only` and paste what the
documents actually say — a claim is only worth having if the document really states
the number.
