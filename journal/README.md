# Trade journal

An append-only record of backtest runs, kept in the repository so it survives
laptop reinstalls and can be re-checked months later. Written and verified by
`analysis/journal.py`; every file here is plain text and diff-friendly.

Commands below assume you are in the repository root with the uv environment
synced (`uv sync`, or just let the first `uv run` do it).

```
journal/
  runs.jsonl            one JSON object per run, append-only
  verifications.jsonl   one JSON object per verification, append-only
  trades/<run_id>.csv   optional per-trade table for a run (--keep-trades)
  README.md             this file
```

## Recording a run

```bash
uv run kcs-backtest --strategy sma-rev --journal --note "why I ran this"
uv run kcs-basket --symbols BTC-USDT,ETH-USDT --strategy sma --param window=200 \
    --journal --note "the pair, not the single series"
```

`--journal` alone writes into this directory; `--journal some/other/dir` puts it
elsewhere. Recording is explicit: nothing is written unless you ask for it.

**Basket runs are first class.** `kcs-basket --journal` writes `kind: "basket"`, one
`legs.<symbol>` window (bars, first, last, digest, the data audit) per pair instead of a
single `data` window, the `window` the reported stretch was cut with, and — when
`--spread-model` charged each leg its own estimated spread — the `leg_costs` that were
actually used, stored rather than re-derived, so a later estimate from a grown archive
cannot silently change what the entry is checked against. The run id is built from the
timestamp, the timeframe and the leg count (`…-basket-1d-10legs`) so it is easy to type
into `--id`. Verification re-runs every leg and compares the combined curve, the benchmark
and each leg's numbers; a basket has no single per-trade table, so `--keep-trades` is not
offered there.

## Reading it

```bash
uv run kcs-journal report            # table of runs + per-strategy roll-up
uv run kcs-journal show --id 20260924T221530Z-sma200
uv run kcs-journal verify            # re-run everything and compare
uv run kcs-journal verify --last 5   # or just the newest few
```

`verify` exits non-zero when anything fails, so it can sit in CI.

## What one entry holds

| field | why it is there |
|---|---|
| `run_id`, `recorded_at` | identity and time of the run |
| `strategy`, `params` | `get_strategy(name, **params)` rebuilds the exact strategy |
| `costs` | commission and slippage per side — a result means nothing without them |
| `data.symbol`, `data.timeframe` | which series |
| `data.bars`, `data.first`, `data.last` | the exact window that was evaluated |
| `data.digest` | SHA-256 over the OHLCV values of that window |
| `data.gaps`, `data.missing_bars`, `data.bars_violating_ohlc` | the audit the run saw |
| `kind` | `backtest` (one series) or `basket` (several legs) |
| `legs` / `window` / `leg_costs` | a basket's per-leg windows, the reported stretch, and its per-leg costs |
| `metrics` | everything the engine reported, including `bookkeeping_error` |
| `note` | your own words about why the run happened |
| `trades_file` | the per-trade table, when `--keep-trades` was used |

## How re-checking works

`verify` reloads the series, **slices it back to the recorded window** (so the
entry stays checkable after `kcs-klines backfill` appends more bars), rebuilds
the strategy from `strategy` + `params`, re-runs the engine with the recorded
costs, and compares every numeric field against `metrics` with a relative
tolerance of 1e-9.

If the entry has a `trades_file`, that table is checked too, **row by row**
against the re-run: side, both timestamps, bars held, both prices, gross and net
return. Per-row tolerances are the file's own precision (1e-8 relative on prices,
1e-6 on returns), so a number typed by hand shows up rather than being absorbed
by rounding.

The status tells three failure modes apart:

| status | meaning |
|---|---|
| `verified` | data digest and every metric reproduce |
| `data-changed` | metrics reproduce, but the stored bars are not the ones recorded (the archive was edited, or history was re-fetched differently) |
| `mismatch` | same inputs, different numbers — the strategy or the engine changed, or the entry was edited by hand |
| `unknown-strategy` | the registry no longer has this strategy name |
| `error` | the series or the recorded window is gone |

A `data-changed` entry is not automatically wrong — a re-fetched bar with a
different volume, say, is a fact worth knowing about. An entry that shows
`mismatch` should be treated as suspect until the cause is found.

Because a run records the window it evaluated, the journal is a history of the
same question asked at different times: re-running a strategy as the archive
grows adds a new entry, and `report` shows how its statistics moved.

## Size

Measured on BTC-USDT 1h with 1,268 round trips:

| file | size |
|---|---|
| one entry in `runs.jsonl` | ~1.7 KB (one line) |
| `trades/<run_id>.csv` with `--keep-trades` | ~145 KB |

The run summaries are cheap enough to keep every run forever; the per-trade
tables are worth it for the runs whose trades you actually want to inspect.
