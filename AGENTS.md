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
* **`CONCLUSIONS.md`** — what the measurements actually support: the five facts
  that decide an outcome, what was tested and rejected, the mistakes this project
  made and fixed, what is not modelled, and what to do next. Read it before acting
  on any number from this repository.
* **`journal/`** — append-only JSONL run records, **tracked in git on purpose**.
  Currently empty: the human records their own runs there.
* `data/` (1.4 GB, 996 symbols, gitignored) is the archive. It is **alive**: a
  collector run can append bars while you are working.

## Map

| path | what |
|---|---|
| `src/` | Rust collector: `kucoin/client.rs`, `storage/parquet_store.rs`, `collector.rs`, `verify.rs`, `status.rs` |
| `tests/` | Rust tests; `live_api.rs` is `--ignored` and hits the real exchange |
| `analysis/src/analysis/` | `data.py` `metrics.py` `engine.py` `report.py` `journal.py` `walkforward.py` `portfolio.py` `basket.py` `run_backtest.py`, `strategies/`, `tests/` |
| `analysis/src/analysis/tests/` | 326 pytest tests (engine invariants, registry-wide strategy checks, CLI, journal, walk-forward, portfolio, basket, real-data regression) |
| `analysis/out/` | artifacts (CSV/JSON/SVG), gitignored |
| `analysis/README.md` | the toolkit in detail; `journal/README.md` the journal format |
| root `README.md` | the collector in detail (KuCoin API traps, schema, scheduling) |

## Commands

```bash
cargo test && cargo clippy --all-targets      # Rust
uv sync                                       # Python env (installs the analysis member)
uv run pytest                                 # 326 tests, ~8 s
uv run kcs-backtest --list                    # 16 registered strategies + their parameters
uv run kcs-backtest --strategy tsmom --param lookback=720 --param rebalance=168
uv run kcs-backtest --symbol BTC-USDT --last 1y   # only the last year, warm history
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT
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
5. **Benchmarks.** `buy_and_hold` enters at the **second** bar's open, not the
   first's: `targets[0]` never trades, so `open[1]` is the earliest price any
   strategy can be filled at, and a benchmark entered at `open[0]` is credited
   with the first bar's move. On a listing bar that move *is* the result — PYTH's
   first hourly bar ran 0.06 → 0.319 (5.3x), SUI's 0.10 → 1.28 — and it turned a
   −78.8% series into a "+13.5% buy & hold". It pays two commission sides in the
   headline; risk statistics come from the price path. The portfolio return is
   `1 + Σ w·(ratio − 1)` — **not** `Σ w·ratio`, which is only valid when the
   weights sum to 1 (it silently inverted the sign of long/short books).

## Gotchas already paid for

* **The portfolio's drifted book is normalised exactly once.** `run_portfolio`
  marks the held weights to the next rebalance as `w·ratio / growth` so they sum
  back to the capital they now represent; dividing by `growth` twice (a leftover
  guard line) made every risen book look under-weighted and charged commission
  for a position that was merely held. The cross-section at 0.1%/side moved from
  −81.27% to −81.18% and its fees from 7.35% to 7.23% — small here *because* a
  monthly rebalance really does replace most of the book; on a held single
  position it was 61% phantom turnover per rebalance. Pinned by
  `test_holding_a_position_is_never_charged_for_its_own_price_move`.
* **The archive grows under your feet.** `test_regression.py` pins a *window*
  (`BASELINE_FIRST..BASELINE_LAST`) rather than "whatever is on disk"; if a
  number moves, run `./target/release/kcs-klines status -s BTC-USDT` first.
* **`uv sync` must keep `analysis` installed.** It is a root dev dependency with
  `[tool.uv.sources] analysis = { workspace = true }`, and pytest is in the root
  dev group. A plain `uv sync` once silently removed the member, after which
  `uv run kcs-backtest` stopped existing.
* **`analysis/__init__.py` must not import `journal`, `run_backtest`,
  `walkforward`, `portfolio` or `basket`** — otherwise `python -m analysis.X`
  prints a runpy re-import warning.
* **Strategy modules import the decorator from `.registry`**, never
  `from . import register` (circular import; a test forbids it). They register
  themselves, so adding a strategy = one module + one import line in
  `strategies/__init__.py`. `--param`, `--sweep` and `--list` need no code: the
  parameters come from the factory signature via `inspect`.
* **`SmaTrend` keeps two code paths on purpose.** `rebalance=1` (the default) maps
  its score every bar with a plain comprehension; only `rebalance > 1` goes through
  `on_decision_grid`. Routing the every-bar case through the grid machinery would
  silently change what an unaligned series (calendar months, say) does, and the
  default is pinned by the regression suite.
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
  Leave-one-out — compounding every trade except the best ones, *including* the
  position still open at the end (get this wrong and the numbers do not add up:
  an earlier version of this note omitted it): BTC 43.31x → 8.77x without its
  best trade, against 19.57x for buy & hold; ETH 41.97x → 19.70x, still above
  its 9.01x; XRP 6.69x → 1.52x against 4.36x; SUI 3.36x → 1.04x against 0.79x, and
  0.39x without the top two. **The SUI and XRP comparisons were re-measured after
  the benchmark base moved to `open[1]`** (see invariant 5): the "SUI loses to
  passive, 10.04x" reading this note used to carry was SUI's listing bar — its
  first hourly bar ran 0.10 → 1.28, a price nobody could buy at. With 16–48
  trades, "beats buy & hold" mostly means "caught a handful of big trends" —
  never quote a multiple without the trade count next to it.
* **The same rule on five majors, traded together** (`kcs-basket`, fixed 20%
  weights, 1h, 2021-08 … 2026-09 — the window all five share): BTC/ETH/SOL/XRP/BNB
  net **+207.3%** (3.07x) against **+124.7%** (2.25x) for holding the same five
  equally, Sharpe 0.53 vs 0.23, drawdown −47.0% against −83.9%, commission 10.2%
  of capital over the 5.14 years. Diversifying the *same* trend rule across liquid
  names is where its edge looks least fragile — it beats the passive hold on
  return and risk at once, which no single-asset result here did. The window
  starts where every leg has data and each leg is rebased there, so these numbers
  are window-relative, not since-listing.
* **The best `lookback` does not transfer between assets**: on 1h bars BTC
  preferred 720, XRP 336, SUI 1440. That is an argument for the walk-forward
  harness (which re-chooses per window) and against per-asset parameter tuning,
  which is how you fit noise.
* **Blending several horizons removes the parameter bet, and pays for it.** Over
  963 hourly series `BlendTsmom` (horizons of 1/2/4/8 weeks, decided weekly) beat
  every single lookback drawn from its own set on 58–67% of them, beat the average
  single lookback on 68%, had the family's best median drawdown (−71% against
  −77…−83%) and left nothing to tune — the spread between its two parameterizations
  is 0.14 against 0.60 between four single lookbacks. But on BTC judged
  out-of-sample it returned **+156.7%** where the single-lookback rule returned
  **+978.5%** (holding: +579.1%), because walk-forward re-chooses the lookback per
  window and BTC rewards 720 bars specifically. It is a robustness trade, not an
  upgrade: worth having when you cannot tune, worth less when the harness already
  does.
* **The rebalancing grid, not the signal, is what makes TSMOM work.** Separating
  the two on 976 hourly series: the weekly decision is worth **+0.38 Sharpe** for
  TSMOM and **+0.39** for an SMA of the same 720-bar window, and it helps on 82%
  and 81% of assets respectively; switching the *signal* from SMA to TSMOM is worth
  **+0.006** and wins on 51% — a coin flip. Without the grid both are equally bad
  (median Sharpe −0.57, 18–19% profitable); with it both are usable. Monthly is
  worse than weekly for both, so it is an optimum, not "slower is better". On BTC
  out-of-sample the grid alone takes SMA from −2.18% to +322.72%, and the signal
  adds the rest (+978.53%). Letting the walk-forward *choose* the frequency as well
  (24/168/720 per window) made it slightly worse — +900.74% against +978.53%, with
  the winning combination scattered across all six (19/15/12/10/10/9 wins of 75).
  The frequency has to be in the right ballpark and is not worth optimising: set
  it, do not tune it.
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

`CONCLUSIONS.md` §6 carries the same list with the measurements behind it, and two
of these have moved since they were written: the archive-wide screen says a
liquidity filter is worth about +9 points of median return and nothing more, while
**removing the hindsight from the hand-picked asset list** is now the first thing
to do — the best-looking result in this repository rests on five names chosen
knowing they survived.

1. Filter the portfolio universe by liquidity/volume instead of "all pairs" —
   that bias, not the ranking, is what killed the cross-section.
2. Leave-one-out across the 12 long symbols: how broad is the TSMOM edge really?
3. Per-symbol spread and slippage instead of a flat 0.1% taker (small pairs are
   worse, and the cross-section is full of them).
4. Walk-forward the portfolio, not just single series.
5. Intrabar stops (ATR trailing, break-even) need an explicit fill model in
   `engine.py`; close-based stops work with the current interface.
