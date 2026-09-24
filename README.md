# kcs-klines

Collect historical **KuCoin Spot** OHLCV klines and store them locally as
partitioned **Parquet** (Zstd).

Built for a scheduled job on a server: one resumable collection command, per-timeframe
incremental syncing, rate limiting that respects KuCoin's public weight pool, and
atomic writes.

```
kcs-klines backfill --symbol BTC-USDT --timeframe 1m   # collect; re-run to resume
kcs-klines status                                      # what is already on disk
kcs-klines verify                                      # look for holes
```

---

## Why this exists (and the two KuCoin traps it avoids)

KuCoin's `/api/v1/market/candles` is easy to use *incorrectly* in ways that
silently corrupt a dataset. Both traps below were measured against the live API
while building this, and both are covered by regression tests.

### 1. You must send **both** `startAt` and `endAt`

| request | rows returned |
|---|---|
| `?type=1min&symbol=BTC-USDT` | 100 |
| `?type=1min&symbol=BTC-USDT&endAt=…` | 100 |
| `?type=1min&symbol=BTC-USDT&startAt=…&endAt=…` | up to 1500 |

Sending only one bound caps the response at 100 bars, and a range wider than 1500
slots is silently truncated **to the newest bars**, leaving a hole in the middle
of what you asked for. `KucoinClient::candles_window` therefore always sends both
and *refuses* an oversized window rather than letting the hole happen.

### 2. `endAt` is **exclusive**

| slots requested | rows returned |
|---|---|
| 1 | 1 |
| 4 | 4 |
| 1000 | 999 |
| 1500 | 1499 |
| 1600 | 1500 (the **oldest** bars are dropped) |

A naive "inclusive" cursor therefore loses exactly one bar per window: a 34-window
backfill of BTC-USDT 1h produced **33 holes**, one per window boundary, which only
an independent continuity check over the stored Parquet revealed. The client takes
a half-open `[from, to_exclusive)` window and the collector steps by whole windows,
so adjacent windows tile the range with no gaps.

### Bonus: the row layout is `[time, open, close, high, low, volume, turnover]`

Close comes **before** high/low — not the near-universal `O,H,L,C`. Reading it the
intuitive way fails `high >= max(open, close)` on the majority of real rows
(measured: 290/1500, 105/1500, 33/1500, 0/580 for a few symbols/timeframes),
while the documented order is valid for 100% of 6272 sampled rows.

### 3. The calendar month is not a fixed interval

`1mon` is supported, but it is the one timeframe whose bars are 28-31 days long, so
`time + n * interval` is meaningless for it. Every piece of time arithmetic here goes
through `Timeframe::align` / `shift` / `slots_between` instead: the scan walks windows
month by month, the gap detector asks the timeframe what the next label should be (so
a missing month counts as **one** missing bar, not 30), and grid validation checks
"is this the 1st of a month at 00:00 UTC" rather than a modulo. KuCoin labels the bar
with that first day; a partial opening month has no bar upstream at all, which is why
the BTC-USDT monthly series starts at 2017-11-01 even though the pair listed on
2017-10-19.

The collector also validates every bar (`high >= max(o,c)`, `low <= min(o,c)`,
grid alignment, non-negative volume). Rare bars that fail — KuCoin really does
serve BTC-USDT 1h on 2017-11-29 with `high < low` — are stored **verbatim** and
counted in the run report, because dropping them would leave holes in the archive
that do not exist upstream; `verify` lists them separately from missing bars. Set
`collection.strict_validation = true` to abort on such a bar instead.

---

## Install

```bash
git clone <this repo> && cd kcs-klines
cargo build --release
./target/release/kcs-klines init      # writes kcs-klines.toml
```

Requires a recent stable Rust toolchain (developed against 1.98). No API keys are
needed: every endpoint used is public market data.

The repository is also a [uv](https://docs.astral.sh/uv/) workspace: the Rust
collector is the product, and the `analysis/` member is a Python toolkit for
testing strategies on the data it collects. `uv sync` builds the Python
environment from `pyproject.toml` + `uv.lock`; the collector itself does not need
Python at all.

---

## How it works

```
                  KuCoin Spot REST                 local disk
                  ─────────────────           ─────────────────────
        GET /api/v1/market/candles   ──▶   data/kucoin/spot/BTC-USDT/1m/
             (1500 bars per call)              2017/2017-09.parquet
```

* **Backwards scan.** History is walked from the newest finished bar towards the past,
  one half-open window at a time, flushing each completed period to disk as it goes.
  An interrupted run keeps everything it already fetched.
* **Only finished bars are written** — one rule, every timeframe. A bar is stored once
  its own period has finished: a minute for `1m`, an hour for `1h`, a month for `1mon`
  (which is why a monthly series never contains the month you are living in). The scan
  simply stops at the start of the currently forming bar, so nothing half-formed is
  ever on disk.
* **Which makes syncing trivial.** The archive always ends on a complete bar, so a run
  continues right after the last stored bar — one minute of missing `1m` data, one
  month of missing `1mon` data — and only that missing tail is fetched. A series that
  is already up to date costs **zero** requests, and a pair listed last week is
  collected for the first time without any special handling.
* **Free period jumps.** The time coverage of every Parquet file is read from its
  footer statistics, so a second backfill does not re-request periods it already
  holds. In practice a repeat run of a 5-year hourly series costs **1 request**
  instead of 34.
* **Exact start of history, recorded in the data.** A pair with nothing on disk is
  probed with *daily* candles (1500 days per request, ~4 calls for a decade) to
  find where it was listed. A series that already has data needs no probe at all:
  covered periods cost nothing, so the walk simply continues down and confirms the
  bottom itself, probing deeper in exponentially growing steps so a long delisting
  gap cannot silently truncate history. The answer is then written into the Parquet
  footer of the series, which is why a repeat run — on this machine or on any other
  — costs **zero** requests. Measured on the real pair: the daily series of
  `BTC-USDT` starts `2017-10-19`, while its hourly series really begins
  `2017-10-04 04:00` with 27 sparse bars in between; trusting the probe over the
  data would have hidden them.
* **Merge, never truncate.** Files are rewritten only when their content changes;
  on a timestamp collision the fresh bar wins, which is how a candle that was
  still forming last run gets corrected.
* **Atomic writes.** Data goes to a sibling `.tmp` file, is fsynced, then renamed.
  A crash cannot leave a half-written Parquet file behind.
* **Polite by construction.** A shared weight-based token bucket models the VIP0
  public pool (2000 weight / 30 s, `/market/candles` costs 3), honours
  `gw-ratelimit-remaining` / `gw-ratelimit-reset`, and backs off exponentially on
  `429`/`5xx`/network failures.

---

## Data layout and schema

```
data/kucoin/spot/{SYMBOL}/{TIMEFRAME}/{YEAR}/{YEAR-MONTH}.parquet   # 1m … 30m
data/kucoin/spot/{SYMBOL}/{TIMEFRAME}/{YEAR}.parquet                # 1h … 1d
data/kucoin/spot/{SYMBOL}/{TIMEFRAME}/all.parquet                   # 1w, 1mon
```

| column | type | notes |
|---|---|---|
| `time` | `BIGINT` | bar **open** time, unix seconds UTC, grid-aligned |
| `open`, `high`, `low`, `close` | `DOUBLE` | |
| `volume` | `DOUBLE` | base volume |
| `turnover` | `DOUBLE` | quote volume (optional, `storage.include_turnover`) |
| `symbol`, `timeframe` | `VARCHAR` | self-describing files (optional) |

Only **complete** bars are stored: because KuCoin's `endAt` is exclusive, the
still-forming candle is never written.

Read it with anything:

```sql
SELECT * FROM read_parquet('data/kucoin/spot/BTC-USDT/1m/**/*.parquet');
```

```python
import polars as pl
pl.scan_parquet("data/kucoin/spot/BTC-USDT/1m/**/*.parquet").group_by("symbol").len().collect()
```

Verified independently with DuckDB: Zstd on every column, footer statistics on
`time` (which the collector uses for its coverage checks), and no duplicate or
out-of-order timestamps.

Each file also carries one footer key/value pair, `kcs.history_start`: the oldest
bar the exchange has for that pair, i.e. how far back the archive has been
verified (see [Where the history floor lives](#where-the-history-floor-lives)).
Nothing else about a series lives outside `data/`.

```sql
SELECT file_name, value FROM parquet_kv_metadata('data/kucoin/spot/BTC-USDT/1h/2017.parquet')
WHERE key = 'kcs.history_start';
```

---

## Commands

| command | purpose |
|---|---|
| `backfill` | collect every selected series, resuming right after the last stored bar |
| `status` | what is on disk: bars, range, files, size, and where the next run will resume |
| `verify` | integrity report: coverage, gaps, duplicates, invalid rows; `--json` for machines |
| `symbols` | list the symbols that would be collected (the config list, or every pair on the exchange) |
| `init` | write a commented example configuration |

Useful flags: `backfill` takes `--symbol`/`-s` (repeatable or comma separated),
`--timeframe`/`-t`, `--start`, `--jobs`, `--dry-run`; `status` takes `-s`, `-t`,
`--json`, and `--tail N` / `--head N` to print actual bars.

```bash
# A week of 1-minute data for two pairs, without writing anything
kcs-klines backfill -s BTC-USDT,ETH-USDT -t 1m --start 2026-09-01 --dry-run

# Everything matching the discovery filters, 8 series in parallel
kcs-klines backfill --jobs 8

# Machine-readable integrity report
kcs-klines verify -t 1m --json report.json --strict
```

Exit codes: `0` success, `1` partial failure (some series failed, or `verify
--strict` found anomalies), `2` fatal (bad config, unreachable exchange).

`verify` measures completeness against the ideal candle grid, so intervals KuCoin
itself does not serve — it publishes nothing for periods without trades, and its
first weeks of 2017 are full of such holes — are reported as missing bars. Bars
that are present but violate OHLC invariants are counted separately as
`invalid_rows`. Cross-checked against the live API: for BTC-USDT 1h the stored
set of bars is *identical* to the exchange's (1708/1708 for the holey Oct–Dec
2017 stretch, 8759/8759 for all of 2018).

---

## Configuration

`kcs-klines.toml` is picked up automatically; see
[`kcs-klines.example.toml`](kcs-klines.example.toml) for every option with
comments. Env overrides: `KCS_DATA_DIR`, `KCS_BASE_URL`, `KCS_CONCURRENCY`,
`KCS_LOG_LEVEL`, `KCS_KLINES_CONFIG`.

Supported timeframes: `1m` `3m` `5m` `15m` `30m` `1h` `2h` `4h` `6h` `8h` `12h`
`1d` `1w` `1mon`.

Minimal working config:

```toml
[collection]
symbols = ["BTC-USDT", "ETH-USDT"]
timeframes = ["1m", "1h", "1d"]
start = "listing"
```

### Collecting everything the exchange lists

`symbols = []` means exactly that: take **every** pair from `/api/v2/symbols`, with
no filtering at all — all quoted currencies (USDT, USDC, BTC, ETH, …), leveraged
tokens and delisted pairs included. Currently that is 996 pairs, and the list grows
as KuCoin lists new coins. Pin an explicit `symbols` list when you want a subset.

One command covers both jobs: `backfill` collects a pair that has nothing on disk and
merely extends the ones that do, so it is what you schedule. A pair listed last week
is picked up on the next run with no extra configuration.

### Where the history floor lives

Everything about what is stored is read from the Parquet files: their footers give
the bars, the range and the coverage per partition, which is how `backfill` decides
what to fetch, how `status` builds its table and how `verify` checks integrity.

One fact cannot be derived from bar counts alone: *"nothing older than this exists
upstream"*. A missing file means either "never collected" or "the pair was not
listed yet", and no amount of reading the archive tells the two apart — so the
collector measures it once (by walking down to the bottom, or by probing daily
candles for a pair with nothing on disk) and records the answer **in the data**:
every Parquet file carries `kcs.history_start` in its footer key/value metadata.

That is deliberate. Nothing about a series lives outside `data/`, so the knowledge
survives copying the directory to another machine or pushing it to a dataset
repository: a fresh host that downloads `data/` spends **zero** requests on
re-learning it.

```bash
# Which series still don't say how deep their history goes?
kcs-klines status --json | jq -r '.series[] | select(.history_floor == null) | .symbol'
```

A directory written before this existed is migrated automatically: the next run
walks down, learns the floor and stamps it into the series' oldest file. That costs
~1–3 requests and one footer rewrite **per series, once** — after which every later
run, on any machine, is free (measured: 0 requests for a repeat run of a migrated
series, and `backfill` + `verify` agree bar for bar afterwards).

The record is never trusted over the data: if the files hold bars *older* than the
recorded floor, the record is provably wrong, is ignored and the walk continues
deeper. A `--start` bounds where a run *looks*, so it proves nothing about what lies
deeper and never loosens a recorded floor.

---

## Scheduling on a server

```bash
sudo cp deploy/kcs-klines-backfill.service /etc/systemd/system/kcs-klines@.service
sudo cp deploy/kcs-klines-backfill.timer   /etc/systemd/system/
sudo systemctl enable --now kcs-klines-backfill.timer
```

Or plain cron:

```cron
17 2 1,15 * * cd /home/you/kcs-klines && ./target/release/kcs-klines backfill >> logs/cron.log 2>&1
```

`notifications.healthcheck_url` pings `/start`, then the bare URL on success and
`/fail` on failure, so a job that silently stops running is noticed. Set
`logging.file` to keep a rotating log on disk.

---

## Volumes and legal notes

Rough Parquet sizes (Zstd, 4–5× smaller than CSV):

| data | size |
|---|---|
| one month of 1-minute bars, one pair | ~0.6–1.1 MB |
| one pair, 1h, full history | a few MB |
| ~1000 pairs, 1h | 1–2 GB |
| ~1000 pairs, 1m, full history | 10–15 GB |

A multi-gigabyte backfill writes a lot of small files, so keep `data_dir` on a
local disk rather than a network mount.

KuCoin's Terms of Use (Articles 90–91) restrict systematic collection of content
to build databases, and commercial use without written consent.

---

## Development

```bash
cargo test                      # hermetic tests, no network
cargo clippy --all-targets      # clean
cargo test --test live_api -- --ignored --nocapture   # hits the real KuCoin API
```

The test suite includes a `FakeExchange` responder that emulates the real
endpoint's half-open range semantics, truncation and newest-first ordering, so
pagination is tested against behaviour rather than against hand-computed window
boundaries. The ignored `live_api` tests guard the assumptions that a mock cannot:
field order, range semantics, weekly grid alignment, and deep history.

---

## Testing strategies on the collected data

`analysis/` is a uv workspace member — a small Python toolkit that runs trading
strategies over the archive this collector produces, with `pyarrow` for reading
Parquet and `pytest` for its own tests. See
[`analysis/README.md`](analysis/README.md) for the details.

```bash
uv sync                                                # environment from uv.lock
uv run kcs-backtest                                    # SMA 200 on BTC-USDT 1h, 0.1%/side
uv run kcs-backtest --list                             # strategies, their parameters, stored series
uv run kcs-backtest --strategy sma-ls --param window=100 --sweep 50,100,200
uv run kcs-backtest --journal --note "why I ran this"
uv run kcs-journal verify                              # re-run and compare, for later
uv run kcs-walkforward --strategy sma --grid window=50,100,200 --train 3000 --test 1000
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --top 0.2
uv run pytest                                          # 241 tests
```

It reports a strategy against buy & hold over the same window and the same
costs, writes the trade list, the equity curve, an SVG chart and the metrics into
`analysis/out/` (gitignored), and checks two invariants on every run: commissions are charged
on every change of exposure, and compounding the trades must reproduce the
equity curve (which is what catches a look-ahead bug — the difference between a
1.89x and a 1.2-billion-x backtest on this data). Adding a strategy means adding
one file that maps bars to exposure; the CLI takes its parameters from the
registry, and the interface and look-ahead tests cover it automatically.

`--journal` keeps a re-checkable record of runs in [`journal/`](journal/README.md),
which is tracked in the repository: each entry stores the strategy, its
parameters, the costs, the exact data window with a SHA-256 digest of its bars,
and the metrics, so `uv run kcs-journal verify` can re-run it later and prove the
numbers still hold.

## License

MIT — see [LICENSE](LICENSE).

HF_HUB_DISABLE_XET=1 hf download Kizyanov/kcs-klines --repo-type dataset --local-dir ./data
HF_XET_CLIENT_READ_TIMEOUT=5s HF_XET_CLIENT_RETRY_BASE_DELAY=1s HF_XET_FIXED_UPLOAD_CONCURRENCY=4 uv run hf upload Kizyanov/kcs-klines ./data . --type dataset --commit-message "Update KuCoin klines $(date -u +%Y-%m-%d)"


