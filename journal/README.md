# Trade journal

An append-only record of backtest runs, kept in the repository so it survives
laptop reinstalls and can be re-checked months later. Written and verified by
`analysis/journal.py`; every file here is plain text and diff-friendly.

```
journal/
  runs.jsonl            one JSON object per run, append-only
  verifications.jsonl   one JSON object per verification, append-only
  trades/<run_id>.csv   optional per-trade table for a run (--keep-trades)
  README.md             this file
```

## Recording a run

```bash
.venv/bin/python -m analysis.run_backtest --strategy sma-rev --journal --note "why I ran this"
```

`--journal` alone writes into this directory; `--journal some/other/dir` puts it
elsewhere. Recording is explicit: nothing is written unless you ask for it.

## Reading it

```bash
.venv/bin/python -m analysis.journal report            # table of runs + per-strategy roll-up
.venv/bin/python -m analysis.journal show --id 20260924T221530Z-sma200
.venv/bin/python -m analysis.journal verify            # re-run everything and compare
.venv/bin/python -m analysis.journal verify --last 5   # or just the newest few
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
