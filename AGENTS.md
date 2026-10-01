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
* `data/` (1.4 GB, **998 symbols / 4,990 series** — every symbol now has all five
  timeframes, gitignored) is the archive. It is
  **alive**: a collector run can append bars while you are working. Two things to know
  about what is in it. First, the exchange now lists **tokenised equities** alongside
  crypto (`AAPLX-USDT` near $338, `HOODX-USDT` near $119, `4STOCK-USDT`): they trade like
  spot pairs but follow stocks, and every universe in this repository selects by turnover
  without knowing the difference — include them on purpose or filter them, but do not
  take them by accident. Second, `WMTX-USDT` lags the collector persistently (a week
  behind on both 1h and 1d as of 2026-10-01) — worth a targeted `kcs-klines` sync, since
  every other series is current and the daily calendar ends on the newest bar available.

## Map

| path | what |
|---|---|
| `src/` | Rust collector: `kucoin/client.rs`, `storage/parquet_store.rs`, `collector.rs`, `verify.rs`, `status.rs` |
| `tests/` | Rust tests; `live_api.rs` is `--ignored` and hits the real exchange |
| `analysis/src/analysis/` | `data.py` `metrics.py` `engine.py` `report.py` `journal.py` `walkforward.py` `portfolio.py` `basket.py` `riskparity.py` `run_backtest.py`, `strategies/`, `tests/` |
| `analysis/src/analysis/tests/` | 413 pytest tests (engine invariants, registry-wide strategy checks, CLI, journal, walk-forward, portfolio, basket, real-data regression) |
| `analysis/out/` | artifacts (CSV/JSON/SVG), gitignored |
| `analysis/README.md` | the toolkit in detail; `journal/README.md` the journal format |
| root `README.md` | the collector in detail (KuCoin API traps, schema, scheduling) |

## Commands

```bash
cargo test && cargo clippy --all-targets      # Rust
uv sync                                       # Python env (installs the analysis member)
uv run pytest                                 # 413 tests, ~28 s
uv run kcs-backtest --list                    # 16 registered strategies + their parameters
uv run kcs-backtest --strategy tsmom --param lookback=720 --param rebalance=168
uv run kcs-backtest --symbol BTC-USDT --last 1y   # only the last year, warm history
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT
uv run kcs-riskparity --top 5 --min-history 3y --vol-budget 0.4
uv run kcs-riskparity --top 5 --min-history 3y --last 1y   # only the last year
uv run kcs-backtest --journal --note "why"    # records a verifiable run in journal/
uv run kcs-journal report | verify | show --id …
uv run kcs-walkforward --strategy sma --grid window=50,100,200 --train 3000 --test 1000
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --top 0.2
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --select sign --threshold 0
uv run kcs-portfolio --timeframe 1d --lookback 7 --rebalance 7 --select sign --trend-gate 200
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
* **`run_portfolio` used to run the whole strategy one rebalance late.** The book decided
  at rebalance `k` has to earn `closes[k] -> closes[k+1]` — that is what `engine.py` does
  (`targets[t]` decided on bar t's close is exposed to bar t -> t+1) and what the report
  claims ("every symbol above +0.0%"). The loop instead marked the *previous* book over
  that period and swapped the new one in only at the end, so the decision took effect one
  rebalance period later: seven bars on a weekly grid, thirty on a monthly one. Found by
  replicating the loop to attribute P&L per pair: the replication matched the module to the
  digit under the lagged convention, and the gross curve under the two conventions differs
  by a factor — 22.76x (decided at k, earns k -> k+1) against 56.70x (as it ran) on the 7/7
  sign book. Every portfolio number in this repository was re-measured after the fix
  (`kcs-riskparity` was always correct — its loop documents "yesterday's decision takes
  effect at this close, never at its own").
* **The first rebalance's commission must not be written into `equity[0]`.** Charging the
  entry cost as `equity[-1] -= cost` looks harmless, but on the first iteration `equity[-1]`
  *is* `equity[0]`, which is the base the curve is normalised by (`curve = [v / base]`) — so
  the entry commission cancelled itself out: counted in `fees_paid` and invisible in the
  result. Carry the bill in the value the period starts from instead. Pinned by
  `test_the_entry_commission_reaches_the_curve`.
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
  adds the rest (+978.53%). **It is a filter, not a discount**: the best grid is one
  week at a 0.00% fee just as at 0.20%, and reading the same signal every bar is
  worse even when trading is free (−0.26 against −0.10 median Sharpe), because a slow
  signal sampled hourly whipsaws across zero. The optimum is a broad plateau (one
  hour −0.57, one day −0.32, three days −0.21, one week −0.13, two weeks −0.19, 30
  days −0.31) and the day of the week is worth only +0.04 Sharpe (Thursday, the epoch
  day the default inherited, beats the other six on 58% of series). The lookback and
  the grid are therefore not independent: "this lookback is best" is conditional on
  the frequency it was read at. Letting the walk-forward *choose* the frequency as well
  (24/168/720 per window) made it slightly worse — +900.74% against +978.53%, with
  the winning combination scattered across all six (19/15/12/10/10/9 wins of 75).
  The frequency has to be in the right ballpark and is not worth optimising: set
  it, do not tune it.
* **A rule-picked book, held unequally and de-risked (`kcs-riskparity`).** Top 5
  by trailing turnover with three years of history, equal weights, a 40% volatility
  budget, re-selected monthly: **15.47x** over 8.93 years (CAGR 35.9%) against
  11.76x for the same selection held at full size at equal weight — Sharpe 0.65
  against 0.46, drawdown −64.5% against −73.7%, at 48% against 60% volatility, and
  paying 33% of capital in commission instead of 24%. Inverse-volatility weights on
  the same book: 14.26x, Sharpe 0.68, drawdown −56.3%, so the unequal weighting buys
  about 8 points of drawdown and nothing else. It is the only configuration here
  whose asset list is picked by a rule rather than by hand, so it is the only one
  without hindsight in its selection. Three things measured along the way: the
  **history filter matters more than the weighting** (three years instead of one cut
  the commission bill from 145% of capital to 17% and the volatility from 111% to
  33%); **inverse volatility parks the book in a stablecoin** — unguarded, a top-20
  inverse-vol book held **91.6% of USDC-USDT** and looked wonderfully safe because it
  was 92% cash, which is why `MIN_VOLATILITY` (10% a year) and a single-name cap
  exist; and **the cap has to grow as the book shrinks**, or a two-name book would
  sit half in cash by arithmetic rather than by decision.
* **On a wide book the weighting scheme is nearly a rounding error.** The same
  `kcs-riskparity` command over the last six months of a top-100 universe
  (`--top 100 --last 6mon`): equal weights give every name 1.00% and, after the 40%
  volatility budget scaled the book to 0.73, 0.7% each; inverse volatility instead
  spreads 0.11% (LSK, 710% a year) to 5.33% (BDX, 15%), a 46x range about a 0.81%
  median, and drops the three stablecoin pairs as too calm to be positions. The result
  barely moves: 20.21% (Sharpe 0.81, drawdown −35.6%) against 22.37% (0.94, −30.8%),
  both behind the same selection held at full size and equal weight (28.96%, 1.01,
  −37.1%) in a window that was a bull market — de-risking costs return exactly when the
  market pays. With 100 names the book *is* the index, and moving money between them
  changes almost nothing; the scheme earns its keep on a narrow book, where the
  eight-year top-5 run put equal at 15.47x/0.65 and inverse-vol at 14.26x/0.68.
* **Risk parity has no exit rule, and that is what a down year exposes.** It is
  always long its universe and only ever changes *size*, so when everything falls the
  most it can do is hold less of everything — one month late. The `--top 2 --last
  12mon` window (2025-10-02 … 2026-09-27) is a clean example: the two most traded names
  were BTC-USDT (−29.9%) and ETH-USDT (−40.0%), and re-equalising them to 1/2 every
  month **averaged down into the weaker leg** — the same book without the budget lost
  −34.53% against BTC's −29.99%. The 40% budget cut volatility (53.0% → 47.7%) and the
  drawdown a little (−59.93% → −59.40%) but cost 2.75 points of return (−34.53% →
  −37.28%), because it sells after volatility has already risen. `--weight invvol` was
  worth about a point in the same window (−36.24% with the budget) and does not fix the
  cause. Neither scheme implements "when it falls, move into something calmer": equal
  weights have no volatility in them at all, and inverse volatility only moves the
  *target* — on 2025-10-14 the book still **bought** ETH (+3.1pp) at 78% volatility
  because it had drifted below its already-reduced target. What does react faster is the
  window: at `--vol-window 7d` the same window returns −30.36% (drawdown −54.8%) against
  −36.24% (−58.6%) at 30d, and over the full 8.93 years 2046.7% at Sharpe 0.66 and
  drawdown −69.2% against 1777.7%, 0.66 and −70.0%. Over that same history buy & hold
  BTC made +1542% at Sharpe 0.46 with an −82.9% drawdown, so the book does win the long
  game; it simply loses single bad years to holding the coin. The missing piece is an
  **entry/exit signal** (the TSMOM half of this toolkit), which is not connected to the
  sizing half yet.
* **Risk parity plus a signal (`--trend`): it works, and most of the "edge" is being in
  cash.** The gate holds a name only while its close is above its close N bars ago, sells
  on the next close when that fails and re-enters only on the rebalance grid. Measured on
  the window that exposed the problem (`--top 2 --last 12mon`, invvol, 40% budget): trend
  off −36.24% at 88% of capital at work; `--trend 30d` −3.53%, drawdown −17.6% against
  −58.6%, but at work only **25%** of the time; `--trend 7d` +0.41% at **7%** at work and
  91% of bars flat. Compared against the honest yardstick — the same average exposure held
  passively — the timing was worth +4.0 points (book −3.53% against 25% of the selection
  in cash at −7.54%), so the rule does more than de-risk, but "−3.5% against BTC's −30%"
  is mostly exposure, not skill, and quoting it without the "at work" column is the same
  mistake as quoting a multiple without its trade count. Over the eight years the rule
  costs return unless it is slow: top 5, invvol, 40% budget — off +1071% at Sharpe 0.62
  and −62.8% drawdown; `30d` +177%, 0.40; `90d` +196%, 0.36; `200d` **+868%, 0.68,
  −47.7%** (against BTC +1542%, 0.46, −82.9%). So a slow gate buys ~15 points of drawdown
  for ~20% of the return, and a fast one whipsaws the compounding away. **The lookback is
  a new parameter bet and the spread is huge** (12 months: +0.41% at 7d, −3.53% at 30d,
  −28.70% at 90d), and the best value differs per horizon — it needs the walk-forward
  treatment before any of these numbers is believed. With the gate on, the volatility
  budget rarely fires at all: the book's own volatility is already low because it is
  often in cash.
* **Where each knob belongs, measured on one common five-year window.** Volatility
  sizing is for *concentrated* positions and blending is for *diversified* ones, and
  swapping them costs money. On BTC daily, `voltarget-sma` (SMA 200 plus a 40% vol
  target) returned +185.4% at **Sharpe 0.71 and a −26.3% drawdown with 19 trades in
  five years** — the best risk-adjusted row measured — where plain SMA 200 was 0.66 at
  −36.2% and TSMOM 30d/weekly was 0.55 at −37.5%; its parameter is a **plateau** (SMA
  windows 50/100/150/200 → Sharpe 0.78/0.70/0.77/0.71, degrading only past 250). On
  the five-major basket the same vol target merely de-risked: `voltarget-tsmom` +82.4%
  at Sharpe 0.52 against plain TSMOM's +202.9% at 0.52. Blending did the opposite:
  `tsmom-blend` on the basket matched the tuned lookback (+199.2% against +202.9%) with
  the drawdown cut from −51.4% to −31.7% and Sharpe 0.52 → 0.61 **and no lookback to
  choose**, while on BTC alone it was far worse (+62.4% against +154.1%). Dominated on
  the same window: MACD +84.9% at 0.37, Donchian 20/10 +41.2% at 0.22, RSI reversion
  −29.2% at −0.25. And **more names is not more diversification**: adding ADA, DOGE,
  AVAX, LINK and DOT to the basket took it from +199.2% to +124.6% while their passive
  hold lost −10.6%. One correction to the "never read the signal every bar" rule: it was
  measured on *hourly* bars, and on daily bars it is not true — SMA 200 every bar
  (+219.2%, 0.66) beat the weekly grid (+197.4%, 0.62) on BTC.
* **Retail families (DCA, value averaging, grid, martingale, RSI), measured on BTC daily
  over the same five years.** They are *schedules*, not signals: they decide when money
  goes in, never when it comes out, and the data says which shapes are worse. Weekly DCA
  earned a money-weighted **28.0%** a year against **14.9%** for a lump sum of the same
  26,100 — a fact about the window (it opened with a bear market), not an edge; filtering
  contributions by a 200-day trend cut the IRR to 12.3%, and adding a trend exit finished
  **flat in five years** (0.99x, −0.3%/yr) where the trend rules made 2.5–3.2x. Value
  averaging demanded **83,858 paid in to end at 26,100** (657k to end at 347k at a +1%/week
  target) — it needs unbounded capital exactly when prices fall, and its 40.7% IRR is an
  artefact of withdrawing into strength. A grid's best of five configurations was **1.51x
  against 2.00x** for holding, with a −61% drawdown at half the exposure; re-centring it
  monthly made it 0.64x and weekly 0.52x, because re-centring realises losses, and fees
  (5% of the budget at 2% spacing over 1,135 fills) were the smaller problem. Martingale:
  on a 10,000 budget **every sizing ran out of cash in the first big decline** (base 500 on
  2021-12-09 at 47,549; base 1,000 on 2021-11-26 at 53,723) and then held a bag for years,
  −67.5% and −70.7% drawdowns for 1.24x and 1.28x. RSI: buying oversold 0.52x at a −67.8%
  drawdown, RSI(14) > 50 as a filter 1.35x at −50.3%, and only "buy strength" (RSI > 70)
  respectable at 1.93x with −20.7% — still behind a plain 200-day SMA (2.97x). Grid and
  martingale buy most aggressively at the bottom, which is exactly where the trend rules
  are in cash.
* **What "survival" means in this archive, measured.** There are **no delisted pairs** in
  it: of 982 daily series, 0 stopped printing bars before the end (0 by 90 days, 0 by 365) —
  the collector fetches the current listing, so every series here is still listed by
  construction and every survival result is an upper bound. And **still listed is not
  alive**: the median pair is **−93.6% from its own peak** (59% are more than 90% below it,
  median total return −86.5%), with the 5–7-year cohort the worst at a −98.3% median and
  80% more than 90% off peak, while the under-2-year cohort looks healthiest (−86.1%) only
  because it has not had time to fall from its listing pump. So `--min-history` is not a
  health screen: it buys **continuity and liquidity** (a pair that has traded for years can
  still be executed in and out of), which is why it cut the commission bill from 145% to
  17% of capital and volatility from 111% to 33%.
* **Time-series momentum and cross-sectional momentum are different bets, and only one
  of them works here.** TSMOM compares an asset with **its own** past (long while its own
  trailing return is positive, flat otherwise) — it needs a trend and it is what this
  repository validated: BTC 1d weekly +2,394% over 8.8 years at Sharpe 0.80, and across
  the archive it loses on the median asset (−24.5%) while beating holding on **88%** of
  966 series. Cross-sectional momentum compares assets **with each other** (rank by
  trailing return, buy the top quantile, optionally short the bottom) — it needs
  *dispersion*, and here it loses in every form: all pairs −81.39% against −49.31% for
  equal weight; with the survivor filter the argument demands (412 names, three years of
  history) **−82.49%** against −40.66%; the top 10 names by rank −77.70% with a −98.4%
  drawdown; and long/short was **−100% by 2017-12-20**, dead after three rebalances.
  Note what that says: the history filter moved the *passive baseline* nine points and the
  *ranking* none, so it is the ranking — not the graveyard universe — that makes the
  cross-section lose. Related names that are **not** cross-sectional: `tsmom-ls`/`sma-ls`
  (still time-series, just always in the market) and `tsmom-blend` (several horizons of
  the *same* asset). Dual momentum (relative pick + absolute filter) is not measured here.
* **The quote filter is a modelling choice, not a free lunch (`--quote`, default USDT).**
  A cross pair such as `ADA-BTC` is a *different* bet: its price is a ratio of two crypto
  assets, the dollar move cancels out, and holding it adds a second exposure (and a second
  currency to fund) that a rule written for dollar-priced assets never asked for. So
  `kcs-portfolio` keeps only pairs quoted in the asked-for currencies — default **USDT**,
  `--quote USDT,USDC` for both dollar stables, `--quote any` for everything on disk — and
  the report says what it removed (`836 symbols quoted in USDT, 156 other pairs skipped`).
  **But do not expect the filter to pay.** Measured on the same 7/7 sign book after the
  timing fix: USDT-only **+1,141.23%** (CAGR 32.5%, Sharpe 0.30, −92.6%) against
  `--quote any` **+3,339.12%** (48.4%, 0.57, −81.4%) — the full archive is *better*, and the
  opposite reading I first published (USDT +2,953% against all-pairs +1,651%) was an artefact
  of the one-period lag: the lagged book happened to suit the narrower universe. On the
  monthly grid the same flip: USDT-only sign +610.85% against `--quote any` +1,782.88%, and
  `rank --top 0.2` −45.38% against +238.47%. What the filter still does is remove the pair
  that cannot move: `USDT-USDC` is quoted in USDC, so the USDT rule drops the single busiest
  row of the trade census (241 trades on a stablecoin), and `--quote USDT,USDC` measures
  within a point of `--quote USDT`. Choose the quote because of what the *strategy* is
  (one settlement currency, one exposure per asset), not because of what it earned in this
  archive.
* **The sign filter is now a tool, not a script (`kcs-portfolio --select sign`).**
  `--select rank` (default) takes the top slice as before; `--select sign --threshold 0`
  takes *every* symbol whose trailing return beats the bar, so its breadth moves with the
  market and an empty book means cash — the liquidation is charged, counted in the report
  (`cash`) and warned about. `--last N` reports the newest N rebalances with warm history.
  Run over the whole daily archive (30-bar lookback and rebalance, 0.1%/side, after the
  timing fix), the filter beats the ranking in both universes but no longer by a landslide:
  `--quote any` gives `sign --threshold 0` **+1,782.88%** at Sharpe 0.43 against
  `rank --top 0.2` **+238.47%** (0.19), with the equal-weight universe at −47.39%; the
  default USDT-only universe gives `sign` **+610.85%** (0.21) against `rank` **−45.38%**
  (−0.07), passive −50.62%. Over the last five years the 7/7 book loses **−65.4%** (USDT) and
  **−59.4%** (`--quote any`) with the passive book at −49.2% and −47.1%: the filter is the
  least bad rule on a graveyard universe, not a solution to it.
  `--chart PATH` draws it: both curves, a dashed final-multiple line each, and vertical
  markers *only* at cash moves (green back in, red to cash, names and turnover in the
  tooltip), and `--chart-trades PATH` gives the per-asset view: one price line per symbol on
  the rebalance grid, a green dot at every buy and a red square at every sell (the marker
  *is* the fill price, `BTC-USDT bought 2017-10-26 at 6010`), grey dashed for assets the rule
  never bought, `--chart-symbols N` to cap it — a weekly rule on a 146-name book marks
  10,763 trades in nine years, which is the picture the reader needs and also the reason the
  cap exists. The report also prints **which pairs were traded** (how many of the universe
  were held at least once, how many never, plus the busiest eight with rebalances held,
  entries, exits and dates), and `--pairs-csv PATH` writes the whole census — one row per
  symbol *watched*, never-bought ones included (`ever_traded=no`, `rebalances_held=0`). On
  the 7/7 run over 992 pairs that census says **980 pairs were held at least once and 12
  were never bought**: the filter is not selective about *which* pairs it trades, it is
  selective about *when* (breadth 1 … 688), and the busiest rows are the cost story —
  `USDT-USDC` 241 trades, `LTC-BTC` 235, `LTC-USDT` 234. `USDT-USDC` being near the top is
  the missing-filter defect in one line: a stablecoin clears a zero bar on ±0.01% and is
  then traded back and forth all year. Breadth is reported as a number rather than marked — on a wide universe it
  swings 1 … 578 names and would put forty lines on a hundred rebalances.
* **The monthly "buy what rose last month" rule, measured (2019-03 … 2026-09, monthly
  decisions, equal weight, cash when nothing qualifies).** It is monthly *time-series*
  momentum as a sign filter rather than a ranking, and both forms were run on the same
  engine. All pairs, buy what rose: 6.53x at Sharpe 0.43 and −77.1% drawdown, **142 names
  held on average, 1.39 books turned over per month, 82% of the starting capital paid in
  commission** (~10.9%/yr), and **0.61x over the last five years** — the 6.53x is
  2019–2021. Buying what *fell* instead returns 1.71x on the same universe, so the sign does
  carry information; on the liquid top-20 by turnover the same filter gives 2.96x against
  6.04x for the *ranked* version, i.e. a history filter leaves a ranking dead (412 names,
  −82%) while a **turnover** filter revives it (20 names, +504%) — liquidity, not age, is
  what makes a cross-section usable. The best version of the idea is on one asset: the
  monthly sign rule on BTC made 17.65x at Sharpe **0.86** and −55.7% drawdown with 44% of
  months in cash, better risk than holding BTC (0.64, −76.6%) at similar return. And the
  grid is the expensive part: on BTC, over exactly that period, the same 30-day signal
  decided **weekly** returns **32.87x at Sharpe 1.06 and −44.2%**, against 13.71x at 0.74
  and −65.1% decided monthly.
* **The sign filter and TSMOM read the same signal and are completely different
  strategies.** `Tsmom.threshold` *is* the portfolio's `--threshold`: both hold while the
  close is above its level `lookback` bars ago, on the same absolute grid, held between
  decisions. Same 7/7 configuration, same nine years, asset vs universe: TSMOM on BTC is
  **out of the market 46% of the time** (mean exposure 0.54), makes 114 trades, pays ~20% of
  capital in commission, ends at **10.87x** (Sharpe 0.55, −75.0%) and **+86.7% over the last
  five years**; the same rule on 836 USDT pairs is **almost never** out of the market (4% of
  466 rebalances), makes ~109 names' worth of turnover every week, pays **860.6% of capital**
  (turnover 1.30 per rebalance), ends at **12.4x** (Sharpe 0.30, −92.6%) and **−65.4% over the
  last five years**. The signal did not change — what changed is that a 109-name book
  neutralises the *exit*, which is the whole measured value of the rule, and each 0.9%
  position is too small for its own signal to matter. That construction is fee-fragile:
  0.00%/0.05%/0.10%/0.20%/0.30% per side gives +3,197%/+2,303%/+1,651%/+829%/+392% on the
  `--quote any` book (measured before the timing fix — the *shape* of the fee sensitivity is
  what it shows, not the levels), and a control that holds every pair with the same weekly
  re-equalisation and **no** filter returns +350% at 0.11 turnover instead of 1.30. Apply the
  signal per asset (one position, or a handful of liquid names); never to ~1000 pairs at
  equal weight.
* **The filter for dying assets is "distance from your own high", not liquidity.**
  `kcs-portfolio` now has health gates, all computed from bars at or before the rebalance
  date: `--trend-gate N` (close at or above its own N-bar mean), `--max-below-peak P` (drop
  names more than P below their own *running* peak), `--min-turnover X` and
  `--min-volatility P` (annualised floor, which is what removes dead-calm stablecoins);
  `--gate-window` sets the bars the last two look at. Measured on the 7/7 sign book over
  836 USDT pairs, with a **placebo** that rotates the gate readings between symbols (same
  thresholds, same average breadth, no information):

  | filter | total | Sharpe | max DD | breadth | losers still traded | placebo (median / best Sharpe) |
  |---|---|---|---|---|---|---|
  | none | +1,141% | 0.30 | −93% | 109 | 100% | — |
  | trend 50 | +2,200% | 0.37 | −87% | 58 | — | — |
  | trend 100 | **+4,372%** | **0.52** | −91% | 44 | — | — |
  | trend 200 | +2,675% | 0.49 | −87% | 31 | 72% | −33% / 0.09 |
  | trend 300 | +2,508% | 0.48 | −83% | 23 | — | — |
  | peak within −30% | **+177,786%** | **0.90** | −83% | 13 | — | −54% / 0.45 |
  | peak within −50% | +7,779% | 0.56 | −92% | 21 | 84% | — |
  | peak within −90% | +1,552% | 0.34 | −93% | 67 | 100% | −31% / 0.07 |
  | turnover ≥ 1e3 / 1e4 / 1e5 / 1e6 | +758% / +408% / +120% / **−87%** | 0.27 / 0.21 / 0.10 / −0.30 | −89…−96% | 108…21 | 100 / 100 / 94 / 64% | −1% / 0.11 |
  | volatility ≥ 5% / 10% | +628% / +701% | 0.25 / 0.26 | −89% | 108 / 107 | 100% / 99% | — |

  Three things fall out. (1) **The trend gate is the honest one**: Sharpe 0.30 → 0.49–0.52
  across a wide plateau (100–300 bars, 50 is too short), drawdown −93% → −83…−91%, it stops
  trading 72% of the pairs that went on to lose money, it beats its placebo by a mile
  (−33% median with the readings shuffled), and out of the 2019–21 bull it still helps:
  over the last five years baseline −65% becomes −39%. It is the same "hold strength, not
  weakness" idea the repo kept finding, applied per name instead of per book.
  (2) **Distance from the running peak is stronger but scale-free in a worrying way**: the
  return rises monotonically as the threshold tightens (−90% → +1,552%, −50% → +7,779%,
  −30% → +177,786% at Sharpe 0.90), which is a slope, not a plateau, and a tighter gate
  means a 13–21 name book whose placebo still reaches Sharpe 0.45 — that is small-book luck
  mixed with a real signal. It is also the only family that turned the recent five years
  positive (−50% → +19%, −30% → **+1,067%** at Sharpe 0.75), so it carries information worth
  keeping; treat the threshold as a walk-forward parameter, not a constant. Top-3 pairs are
  6–19% of the profit in every gated variant (12% ungated), so no single name drives them.
  (3) **Liquidity filters hurt this rule at every level** (≥1e6 → −87%): the liquid majors
  are where it loses (`BTC-USDT` is in the loss column) and its winners are illiquid early
  names (`KCS-USDT` +13,801% while held). That is the *opposite* of what the cross-sectional
  **ranking** needed, so do not port that filter here — the two rules want different
  universes, which is only visible because both were measured.
* **Most pairs lose money; the result is a handful of winners (`--pairs-csv`, per-pair P&L).**
  Attributing every rebalance's money to the pair that earned it (replicating the loop, which
  matches the module to the digit) on the 7/7 USDT book: **518 of 824 pairs lost money (63%)**
  for a total of **−100.2** units of starting capital against **+111.6** from the 306 winners —
  the whole 12.4x is the 11-unit difference, and it is thin: the **top 10 pairs are 29% of all
  profit, the top 50 are 69%**, and 118 pairs are chronic (held ≥50 rebalances, won fewer than
  45% of them). Two distinct failure modes show up in the same table. (a) *The asset died while
  we held it*: `API3-USDT` −3.65 (140 rebalances), `GAS-USDT` −3.12, `SUSHI-USDT` −2.23,
  `NUM-USDT` −1.69 — price and held period both −70…−100%, because a long-only filter keeps
  buying the bounces of a collapsing pair. (b) *The timing, not the asset*: `SNX-USDT` −2.25
  while the price rose **+205% during the periods it was held**, `TLOS-USDT` +31% while held and
  still −1.35. And the mirror image: the winners are the up-legs of pairs that mostly die —
  `KCS-USDT` +6.51 with **+13,801%** while held, `TEL-USDT` +3.79 (+10,087%), `GALA-USDT` +3.52
  — and **BTC-USDT itself is in the loss column (−1.56 over 252 rebalances, 55% winning
  periods, price +1,227% while held)**: churning a rising asset weekly can lose money. Full
  table: `analysis/out/sign_pnl_by_pair.csv`.
* **A "best rebalancing period per pair" is noise, and it is measurable.** On 765 daily
  pairs, running the same rule (hold while the close is above its level `lookback` bars
  ago, re-decide every `rebalance` bars, 0.1%/side) over a 4x5 grid and then applying the
  best of the 20 configurations from the **first half** to the **second half**: the tuned
  pick scored **+0.58** median Sharpe in sample and **−0.36** out of sample, against
  **−0.35** for a fixed untuned 30/7 — no gain at all. The in-sample best repeated in the
  second half in **43 of 765 cases (6%)**, where random would be 5%. What *is* real is the
  grid as a population property: median Sharpe by cell is best at lookback 7 / rebalance 7
  (**−0.11**), worst at rebalance 1 (−0.33, and −0.28 even at lookback 30), and negative
  everywhere because the median asset loses (see the universe bullet). Set the grid; do not
  tune it per asset.
* **A hand-rolled backtest can be wrong three ways at once — calibrate it on `engine.py`.**
  While building the per-pair sweep above, three bugs in a row produced confident nonsense:
  (1) the position earned the very move that produced its signal (look-ahead), which showed
  up as a median Sharpe of **2.84** and looked *stable* — the in-sample/out-of-sample split
  agreed 88% of the time because a biased harness is consistently biased, so out-of-sample
  testing alone does not protect you; (2) comparing my curve from bar 250 with the engine's
  curve from bar 0 (different windows) doubled the apparent error; (3) forgetting to pass
  the timestamps made the grid test always true, so `rebalance` was silently ignored and all
  five columns of the sweep came out identical. The fix each time was the same: run one rule
  through both the harness and `kcs-backtest` and compare (7/7 on BTC: engine 9.6239x,
  harness 9.6372x once aligned).
* **A sweep whose columns are all identical is a broken sweep, not a discovery.** Two of my
  own experiment scripts failed this way while hunting the health filter, and both produced
  confident tables: (1) the trend-window sweep gave byte-identical results for 50/100/150/
  200/250/300 bars, because the panels had been built once with `trend_bars=200` and the
  `GateSpec` window only *selected* which pre-computed series to read — the parameter never
  reached the data; (2) the "last five years" table showed full-history numbers because the
  slicing argument was accepted and then ignored. The tell is in the shape of the output:
  identical figures across a parameter, or a windowed result matching the full one exactly.
  Rebuild the input for every value you are sweeping, and assert that two configurations
  actually differ before believing either.
* **The indicator zoo is one trade in thirty costumes.** Thirty classic indicator rules
  through one harness (signal on a close, held over the next close-to-close move, 0.1% a
  side, long or cash), then twenty of them screened over **529 daily series** with 500+
  bars. On BTC — a survivor — they all work and the ranking is soft: over 8.8 years
  Ichimoku above the cloud 24.67x at Sharpe 0.97, ADX(14)>25 with +DI>−DI 21.66x at 1.14,
  RSI(14)>50 20.39x at 0.87, Donchian 20/10 13.79x at 0.78, SMA 200 12.15x at 0.70, against
  13.00x at 0.50 for holding — but the Spearman correlation between the 5-year and 8.8-year
  Sharpe rankings is only **+0.70** (3 of 5 top names shared), and what separates the rows
  is mostly **how much time they spend out** (ADX 29% exposure, Ichimoku 45%, SMA 54%).
  Across the archive **no indicator has a positive median**: best median Sharpe −0.10
  (Bollinger breakout at 22% exposure, Keltner −0.10, TRIX −0.12, Ichimoku −0.14), worst
  −0.53 (ADX, which latches to 100% exposure on dying series and cannot be validated),
  and the median asset loses money under every one of them (0.91x at best against 0.20x
  for holding it). Mean reversion is dead in every form (Bollinger reversion 0.71x median,
  RSI(2) 0.65x on BTC with 314 trades), and the exotic trend proxies are no better than a
  moving average (Supertrend 7.49x, Heikin-Ashi 5.75x, Parabolic SAR 4.31x, linear
  regression 7.66x on BTC, all below SMA 200's 12.15x, usually with worse exits).
  Volume-based indicators (OBV 0.42x median, A/D, VWAP 4.26x on BTC) are trend proxies
  with a noisier input; MFI is the one exception and it is RSI with volume in it. The
  family is measurably one trade: twelve trend rules agree on **71% of days** on average
  (45% SMA 200 vs MACD, 95% SMA 200 vs EMA 200), and a **majority vote of all twelve**
  made 15.84x at Sharpe 0.80 and −45.2% drawdown at 52% exposure — better than the median
  single rule (12.41x, 0.75), worse than the best (Ichimoku 24.67x), and the honest choice
  when you refuse to pick a winner on hindsight. So: pick the simplest trend rule you will
  follow, or average several, and spend the effort on the universe, the grid and the exit.
* **A drawdown overlay is a dial, not an edge — and a zero floor is a trap.** Implemented
  in the engine (`DrawdownScale`, `--dd-scale 10,40,25`): the target exposure is multiplied
  each bar by a factor that is 1.0 within 10% of the account's own high-water mark, falls
  linearly to 0.25 at −40%, and recovers on its own, reading the equity the same bar
  produced so it cannot look ahead. Measured across three rules on BTC daily over 8.8
  years: TSMOM 24.94x at Sharpe 0.80 and −65.6% drawdown → **12.25x at 0.78 and −44.1%**;
  SMA 200 12.15x/0.67/−64.1% → 6.51x/**0.68**/−43.3%; `voltarget-sma` 8.12x/0.79/−45.4% →
  5.26x/0.77/−35.8%; a gentler 20,50,50 splits the difference (18.88x/0.78/−55.3%). Sharpe
  does not move — return and drawdown fall together, which makes it a preference dial, not
  an improvement, and on the last five years it costs more Sharpe than it saves (0.55 →
  0.48) because it de-risks after the fall and re-risks after the recovery. **`15,30,0`
  turned 24.94x into 1.36x** (CAGR 3.5%, Sharpe 0.23): a flat account's drawdown never
  shrinks, so the factor never returns — the docstring says so and the measurement agrees.
  The book still balances with the overlay on (`prod(1 + net) == final equity`, error
  1.1e-16), and the multiplier is kept per bar in `exposure_scale` and sliced through
  `restrict` like everything else.
* **Two accounting bugs the trend rule exposed.** (1) A gated rebalance used to skip the
  *benchmark* as well, because the book's fill and the passive fill shared one `if pending`
  block — the passive comparison must re-equalise on the grid whether or not the book has
  anything to buy, otherwise the rule being measured leaks into the thing it is measured
  against. Caught by `test_the_trend_rule_never_reaches_into_the_benchmark`. (2) Windowed
  commission was `sum(rebalance.cost)`, which cannot see an exit that happens *between*
  rebalances; the book now keeps a per-bar commission ledger and a window restates its own
  bill from it. Pinned by `test_a_window_counts_the_commission_an_exit_paid`.
* **A passive reference is not a benchmark.** `kcs-riskparity` draws two comparisons
  and they answer different questions. "Equal-weight hold" is the *same universe*
  re-selected and re-equalised on the book's own grid, so it isolates what the weighting
  and the risk budget did. `--buy-hold` (default: the `--calendar` symbol) is one price
  held, which answers "did any of this beat just holding BTC?" — verified against the raw
  parquet, not just against itself: over the 2026-03-31 … 2026-09-27 window BTC ran
  68,282.40 → 84,466.00 (1.2370x), less one 0.1% side = 1.2358x = +23.58%, which is what
  the report printed. In that window the ranking was inverted from the eight-year one:
  BTC buy & hold +23.58% at Sharpe 1.14 beat both the top-100 book (+20.21%, 0.81) and
  the equal-weight hold of that book (+28.96% on return, 1.01 on Sharpe, at 51% vol
  against BTC's 38%) — a book of a hundred alts is a high-volatility bet on alts, not a
  safer way to hold crypto.
* **`m` is a minute; `mon` is a month.** `--last 6m` is six *minutes*, which on daily
  bars is a window of one bar. Both `kcs-riskparity` and `--rebalance`-style flags now
  refuse a duration shorter than one bar of the chosen timeframe and name the two
  possible readings, instead of clamping to one bar and silently trading every bar.
* **The same green and red mean different things on different charts.** On a
  `kcs-backtest`/`kcs-basket` chart the vertical lines are *position changes* — green
  in, red out, purple through zero — and a mere resize is deliberately not marked. On
  the `kcs-riskparity` chart they are *rebalances*: green when the book puts more
  capital to work, red when it takes risk off, which is why a red line there is
  "100% → 67% invested" rather than an exit, and a reshuffle that leaves the size
  alone gets its own grey (`MARKER_SAME`) instead of counting as "more invested". The
  footer used to say "position changes: in = green, out = red" on both, which is a lie
  on the second one; it now says "rebalances: more invested = green (3), less invested
  = red (2), size unchanged = grey (1)", and each tooltip carries the transition it
  marks. The comparison band is half a percentage point, so float dust cannot choose a
  colour.
* **Sharpe is not comparable across conventions.** The same `kcs-riskparity` curve
  is 0.65 with the toolkit's definition (standard deviation of *log* returns, in
  `metrics.performance`) and 0.88 with simple returns. Exploratory scripts in this
  repository quoted the higher number; the toolkit's number is the one to quote.
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

`CONCLUSIONS.md` §7 carries the same list with the measurements behind it, and two
of these have moved since they were written: the archive-wide screen says a
liquidity filter is worth about +9 points of median return and nothing more, while
**removing the hindsight from the hand-picked asset list** is now the first thing
to do — the best-looking result in this repository rests on five names chosen
knowing they survived.

1. Walk-forward the health-gate thresholds. `--max-below-peak` improves the recent five
   years most (−50% → +19%, −30% → +1,067%) but has no plateau, so its threshold is a
   parameter to re-choose per window, not a constant; `--trend-gate` is the plateau one and
   is the safe default. The old "filter the universe by liquidity" thread is **answered and
   rejected** for this rule: every turnover floor made it worse (≥1e6 → −87%), because the
   liquid majors are where it loses and its winners are illiquid early names.
2. Leave-one-out across the 12 long symbols: how broad is the TSMOM edge really?
3. Per-symbol spread and slippage instead of a flat 0.1% taker (small pairs are
   worse, and the cross-section is full of them).
4. Walk-forward the portfolio, not just single series.
5. Intrabar stops (ATR trailing, break-even) need an explicit fill model in
   `engine.py`; close-based stops work with the current interface.
