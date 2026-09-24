# AGENTS.md — orientation for whoever (or whatever) reads this repo next

Written to be read in 60 seconds before touching anything. Two halves: what the
place is, and what has already been learned the hard way.

## What this is

* **`kcs-klines` (Rust, repo root)** — collects KuCoin **Spot** OHLCV and stores it
  as partitioned Parquet. One resumable command, per-timeframe incremental sync,
  rate limiting, atomic writes. No API keys needed.
* **`analysis/` (Python, uv workspace member, `src` layout)** — a toolkit for
  testing trading strategies on that archive: execution model, costs, metrics,
  a strategy registry, a walk-forward harness, a portfolio module, and a
  re-checkable trade journal.
* **`journal/`** — append-only JSONL run records, **tracked in git on purpose**.
  Currently empty: the human records their own runs there.
* `data/` (1.4 GB, 996 symbols, gitignored) is the archive. It is **alive**: a
  collector run can append bars while you are working.

## Map

| path | what |
|---|---|
| `src/` | Rust collector: `kucoin/client.rs`, `storage/parquet_store.rs`, `collector.rs`, `verify.rs`, `status.rs` |
| `tests/` | Rust tests; `live_api.rs` is `--ignored` and hits the real exchange |
| `analysis/src/analysis/` | `data.py` `metrics.py` `engine.py` `report.py` `journal.py` `walkforward.py` `portfolio.py` `run_backtest.py`, `strategies/`, `tests/` |
| `analysis/src/analysis/tests/` | 241 pytest tests (engine invariants, registry-wide strategy checks, CLI, journal, walk-forward, portfolio, real-data regression) |
| `analysis/out/` | artifacts (CSV/JSON/SVG), gitignored |
| `analysis/README.md` | the toolkit in detail; `journal/README.md` the journal format |
| root `README.md` | the collector in detail (KuCoin API traps, schema, scheduling) |

## Commands

```bash
cargo test && cargo clippy --all-targets      # Rust
uv sync                                       # Python env (installs the analysis member)
uv run pytest                                 # 241 tests, ~8 s
uv run kcs-backtest --list                    # 15 registered strategies + their parameters
uv run kcs-backtest --strategy tsmom --param lookback=720 --param rebalance=168
uv run kcs-backtest --journal --note "why"    # records a verifiable run in journal/
uv run kcs-journal report | verify | show --id …
uv run kcs-walkforward --strategy sma --grid window=50,100,200 --train 3000 --test 1000
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --top 0.2
```

## Invariants — break these and the numbers become lies

1. **Timing.** `targets[t]` is decided on the **close of bar `t`** and takes
   effect at the **open of bar `t+1`**; equity is marked at bar opens. A bar's
   move belongs to the exposure already held when that bar opened, never to the
   signal that bar produced. Violating this once produced a 1.2-billion-x curve
   where the honest answer is 1.89x.
2. **The trade book must reproduce the curve.** `prod(1 + trade.net_return)`
   equals `final_equity`; the gap is reported as `bookkeeping_error` (~1e-14
   healthy, order 1 when something is wrong). This is the guard that catches
   look-ahead: it caught exactly that bug during development (a 1.2-billion-x
   curve) and later the fractional-exposure break introduced by volatility
   targeting (an error of 2.3e6).
3. **Costs are charged on every change of exposure**, proportional to the
   notional traded. A *trade* is a holding period: it ends when the exposure
   reaches zero or flips sign, **not** when a strategy merely resizes it.
4. **Strategies never look ahead.** `targets[t]` may use `bars[0..t]` only.
   `test_targets_never_peek_at_later_bars` checks every registered strategy by
   rewriting one bar and asserting earlier targets did not move. It samples bars
   on purpose — a full sweep is O(n²) and took 200 s.
5. **Benchmarks.** `buy_and_hold` takes risk statistics from the price path and
   pays two commission sides in the headline. The portfolio return is
   `1 + Σ w·(ratio − 1)` — **not** `Σ w·ratio`, which is only valid when the
   weights sum to 1 (it silently inverted the sign of long/short books).

## Gotchas already paid for

* **The archive grows under your feet.** `test_regression.py` pins a *window*
  (`BASELINE_FIRST..BASELINE_LAST`) rather than "whatever is on disk"; if a
  number moves, run `./target/release/kcs-klines status -s BTC-USDT` first.
* **`uv sync` must keep `analysis` installed.** It is a root dev dependency with
  `[tool.uv.sources] analysis = { workspace = true }`, and pytest is in the root
  dev group. A plain `uv sync` once silently removed the member, after which
  `uv run kcs-backtest` stopped existing.
* **`analysis/__init__.py` must not import `journal`, `run_backtest`,
  `walkforward` or `portfolio`** — otherwise `python -m analysis.X` prints a
  runpy re-import warning.
* **Strategy modules import the decorator from `.registry`**, never
  `from . import register` (circular import; a test forbids it). They register
  themselves, so adding a strategy = one module + one import line in
  `strategies/__init__.py`. `--param`, `--sweep` and `--list` need no code: the
  parameters come from the factory signature via `inspect`.
* **Test fixtures must use grid-aligned timestamps** (`conftest.START =
  1_507_161_600`). TSMOM decides on an absolute epoch grid, so an unaligned
  synthetic series would never rebalance.
* **Journal entries record the window they evaluated**, so they stay verifiable
  after the archive grows; `verify` re-runs and compares every metric plus the
  per-trade table row by row, and exits non-zero on any difference.

## What has been learned empirically

BTC-USDT, 0.1%/side, 2017-10 … 2026-09 unless noted. Buy & hold is ~+1857% at
Sharpe 0.38 on 1h.

* **Turnover decides everything on hourly bars.** SMA 200: 1,268 trades on 1h →
  net +89.7% (gross +2,300.9%); the same rule on 4h, 273 trades → +1,455%; on 1d,
  30 trades → +1,114%. MACD 1h: 3,028 trades → gross +216% but net −99.3%; MACD
  1d: 117 trades → +1,173% at Sharpe 0.65.
* **TSMOM (30-day lookback, weekly decision) is the strongest single-asset
  result**: 43 trades, net +4,231%, Sharpe 0.72, drawdown −64.7%. It beat buy &
  hold on both return and Sharpe in 10 of 12 long hourly series, in both halves
  of history, on 1h/4h/1d alike, and in walk-forward (+1,086% vs +875%, Sharpe
  0.66 vs 0.43).
* **…but it is a few-big-wins bet, and the headline multiples are fragile.**
  Leave-one-out: BTC without its single best trade is 6.87x against 19.57x for
  buy & hold; XRP without its best trade (Nov 2024 … Jan 2025, +341.6%) is 1.52x
  against 4.26x. Only ~43–48 trades, so "beats buy & hold" means "caught one big
  trend". Say so when quoting it.
* **Volatility targeting is a real improvement in risk shape**: exposure scaled
  to a volatility budget raised SMA 200 on 4h from Sharpe 0.65 to 1.07 and lifted
  the drawdown from −78% to −44%.
* **Counter-trend and mean reversion lost wherever they were tried**: SMA
  inversion on 1h −95% (negative gross too, so it is not a fee story), RSI(2)
  reversion on daily bars −59.6%.
* **Donchian needs a separate, shorter exit channel.** The first version used one
  window for both entry and exit: on 1h at `entry=10` it made 3,798 trades and
  lost 99.99% net. With an entry channel of 20 and an exit channel of 10 it makes
  42 trades on 1d and returns +1,205%. (Different timeframes — the point is the
  shape of the rule, not a like-for-like comparison.)
* **Cross-sectional momentum over 965 daily symbols lost in every simple form**
  (−81% against −47% for equal weight). The universe itself decays: it is every
  spot pair KuCoin ever listed, including hundreds listed then abandoned. The
  long/short variant was wiped out in Jan 2018.

## Open threads, in the order I would pick them up

1. Filter the portfolio universe by liquidity/volume instead of "all pairs" —
   that bias, not the ranking, is what killed the cross-section.
2. Leave-one-out across the 12 long symbols: how broad is the TSMOM edge really?
3. Per-symbol spread and slippage instead of a flat 0.1% taker (small pairs are
   worse, and the cross-section is full of them).
4. Walk-forward the portfolio, not just single series.
5. Intrabar stops (ATR trailing, break-even) need an explicit fill model in
   `engine.py`; close-based stops work with the current interface.
