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
| `analysis/src/analysis/tests/` | 423 pytest tests (engine invariants, registry-wide strategy checks, CLI, journal, walk-forward, portfolio, basket, real-data regression) |
| `analysis/out/` | artifacts (CSV/JSON/SVG), gitignored |
| `analysis/README.md` | the toolkit in detail; `journal/README.md` the journal format |
| root `README.md` | the collector in detail (KuCoin API traps, schema, scheduling) |

## Commands

```bash
cargo test && cargo clippy --all-targets      # Rust
uv sync                                       # Python env (installs the analysis member)
uv run pytest                                 # 423 tests, ~32 s
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
uv run kcs-portfolio --timeframe 1d --lookback 7 --rebalance 7 --select sign --trend-gate 200 --vol-target 25%
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50 --param target_vol=0.30 --last 5y
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

The measurements live in `CONCLUSIONS.md`; this is the short version, and each line names the
section that proves it. BTC-USDT, 0.1%/side, 2017-10 … 2026-09 unless noted.

### Where this stands

> **The best configuration measured: the handful of pairs that trade the most, each held only
> while its close is above its own ~50-bar mean, the whole book sized to a 25-30% annual
> volatility target, and no rebalancing between the names.** Over the last five years, chosen
> by rule as of the window's first day (the ten busiest USDT pairs), that is **+91% at Sharpe
> 0.87 and a −17.5% drawdown**, against **−28%** for holding the same ten. One command:
> `kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50
> --param target_vol=0.30 --last 5y`. Hand-picked names are not what makes it work: the same
> rule on the hand-picked five majors gives +126.9% at Sharpe 0.99, and on the top ten by
> turnover **+163.9% at 1.17** (§1.4–1.5). It also survives a **walk-forward**: run as five
> separate one-year windows with the names chosen by turnover strictly before each window, the
> compounded return is **+75.9%**, no year loses more than **−7.85%**, the worst drawdown is
> **−15.3%**, and the passive hold of the same names compounds to **−52.4%** over the same five
> stretches. It loses to the passive in the two strongest bull years and wins the disasters;
> re-selecting the names every year does not help (it costs ~15 points against choosing once)

### The landscape, on comparable windows

| what | window | return | Sharpe | max DD |
|---|---|---|---|---|
| BTC held | 5 y | +74.8% | — | — |
| BTC with any trend rule (SMA 200 / voltarget / TSMOM) | 5 y | −49…−61% | −0.4…−0.6 | −67…−77% |
| five majors held equally | 5.16 y | +114.2% | ~0.35 | −78.6% |
| five majors, `voltarget-sma` 50/30% | 5.16 y | +126.9% | **0.99** | **−19.9%** |
| top ten by turnover, same rule | 5 y | +91.4% | 0.87 | −17.5% |
| wide book (836 USDT + trend gate), 9 y | 9 y | +2,675% | 0.49 | −86.8% |
| the same + `--vol-target 25%` | 9 y | +655% | **0.73** | **−53.8%** |
| TSMOM 30/7 on BTC for reference | 9 y | +2,369% | 0.79 | −65.6% |

### The lessons worth remembering

* **The signal is the cheapest part.** SMA vs TSMOM is +0.006 Sharpe and wins on 51% of series;
  the grid they are read on is worth +0.38 (§1.1).
* **The exit matters more than the entry.** Every rule here that survived did so by being out
  of the market in bad stretches (TSMOM flat 46% of bars on BTC), and the sign filter's whole
  measured value disappears when a 100-name book neutralises that exit (§1.10).
* **The universe decides.** The same rule: −49…−61% on BTC alone over five years, +91% on ten
  liquid pairs, −45% on 836 pairs. Liquidity *filtering* helps a cross-sectional ranking and
  actively hurts the sign rule (§1.2, §1.3, §1.5).
* **Sizing is the strongest single knob on a book.** A 25% volatility target took the gated
  wide book from Sharpe 0.49 to 0.73 and its drawdown from −86.8% to −53.8%; on the basket it
  is the difference between Sharpe 0.42 and 0.99 (§1.4–1.5).
* **Never rebalance a five-name trend book monthly.** `kcs-riskparity`'s version returns +50%
  at Sharpe 0.19 where the fixed-weight basket returns +127% at 0.99 — re-equalising averages
  down into the weakest leg (§1.5, §1.9).
* **Costs decide everything on hourly bars**, and fee fragility is brutal: the wide 7/7 book
  halves its result per extra 0.1% per side (§1.6).
* **Trend following on liquid survivors is the only thing that survived** across markets, and
  its positive mean is a handful of assets — leave-one-out removes most of the multiple
  (§1.7–1.8).
* **Diversifying the same rule across liquid names is where its edge looks least fragile**
  (§1.9), as long as the names are chosen by a rule and not rebalanced away.
* **The filter for dying assets is distance from the pair's own high, not liquidity.** The
  trend gate lifts Sharpe from 0.30 to ~0.50 and refuses 72% of the pairs that later lost;
  a turnover floor makes the same rule worse at every level, up to −87% (§1.3).
* **Rejected by measurement, in one list**: cross-sectional ranking, shorting, grid trading,
  martingale, value averaging, RSI mean reversion, inverting an SMA, reading a slow signal
  every bar on hourly data, and leverage (§2).
* **Corrections that changed published numbers**: the portfolio ran one rebalance late (fixed;
  every portfolio figure was re-measured), the entry commission cancelled itself out against
  the curve's normalising base, and the quote-filter result reversed once the timing was
  right (§3).
* **Not modelled**: spread and slippage beyond a flat fee, delistings (the archive has none by
  construction), intrabar stops, and funding — read §4 before trusting any number.
* **Two working habits that caught real bugs**: calibrate every hand-rolled backtest against
  `engine.py` on a rule both can run, and treat a sweep whose columns are identical — or a
  windowed result that equals the full one — as broken, not as a finding (§1.1, §3).

## Open threads, in the order I would pick them up

`CONCLUSIONS.md` §7 carries the same list with the measurements behind it, and two
of these have moved since they were written. The archive-wide screen says a liquidity
filter is worth about +9 points of median return and nothing more. **"Remove the
hindsight from the hand-picked asset list" is now answered for the configuration that
matters**: the same rule on the top five by turnover as of 2021-08-04 returns +123.62% at
Sharpe 0.87 and on the top ten +163.94% at 1.17, against +126.87% at 0.99 for the
hand-picked five — the rule does not need the hindsight, and what is left to do with it is
walk-forward the window, not de-bias the names.

1. **Walk-forward the portfolio's own dials.** The winning basket has had it (§1.5a: five
   yearly windows, compounded +75.9%, worst year −7.85%), but the wide book's gate thresholds
   and `kcs-portfolio --vol-target/--vol-window` were picked on the whole history. The pattern
   to copy is `kcs-basket --from … --to …` with the selection made before each window.
   `--trend-gate` is a plateau and is the safe default; `--max-below-peak` is not — it
   improves the recent five years most (−50% → +19%, −30% → +1,067%) and its threshold is a
   parameter to re-choose per window. The old "filter the universe by liquidity" thread is
   **answered and rejected** for this rule: every turnover floor made it worse (≥1e6 → −87%).
2. Per-symbol spread and slippage instead of a flat 0.1% taker (small pairs are
   worse, and the cross-section is full of them).
3. Leave-one-out across the twelve long symbols: how broad is the TSMOM edge really?
4. Intrabar stops (ATR trailing, break-even) need an explicit fill model in
   `engine.py`; close-based stops work with the current interface.
