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
* **`journal/`** — append-only JSONL run records, **tracked in git on purpose**. It holds
  one entry: the recommended configuration from `CONCLUSIONS.md` §1.4-1.5a, recorded by
  `kcs-basket --journal` (a *basket* entry: per-leg windows and digests, the reported window,
  and the per-leg spread costs), re-checkable with
  `uv run kcs-journal verify --id 20261002T081142Z`.
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
| `analysis/src/analysis/tests/` | 466 pytest tests (engine invariants, registry-wide strategy checks, CLI, journal, walk-forward, portfolio, basket, real-data regression) |
| `analysis/out/` | artifacts (CSV/JSON/SVG), gitignored |
| `analysis/README.md` | the toolkit in detail; `journal/README.md` the journal format |
| root `README.md` | the collector in detail (KuCoin API traps, schema, scheduling) |

## Commands

```bash
cargo test && cargo clippy --all-targets      # Rust
uv sync                                       # Python env (installs the analysis member)
uv run pytest                                 # 466 tests, ~35 s
uv run kcs-backtest --list                    # 19 registered strategies + their parameters
uv run kcs-backtest --strategy tsmom --param lookback=720 --param rebalance=168
uv run kcs-backtest --symbol BTC-USDT --last 5y --strategy stops-sma --param window=200 --param stop_loss=0.10 --param cooldown=5
uv run kcs-backtest --symbol BTC-USDT --last 5y --strategy sma-ls    # refused: a spot account cannot short (`--allow-short` overrides)
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
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50 --param target_vol=0.30 --last 5y --spread-model corwin-schultz
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50 --param target_vol=0.30 --last 5y --spread-model corwin-schultz --journal
uv run kcs-journal verify --id 20261002T081142Z     # the recorded winner, re-run and compared
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50 --param target_vol=0.30 --last 5y --capital 60000
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma --param window=50 --param target_vol=0.30 --last 5y --min-turnover-now 300000
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
5. **Spot means long or flat, never short.** A spot balance cannot go below zero, so a
   negative exposure is not a risk preference — it is an instrument the account does not have.
   `kcs-backtest`, `kcs-basket` and `kcs-walkforward` therefore **refuse** a strategy whose
   targets go negative, and say so instead of quietly shorting: pass `--allow-short` to run it
   anyway. The long/short variants are named with an `-ls` suffix (`sma-ls`, `breakout-ls`,
   `voltarget-sma-ls`, …) and one test checks that **every strategy without that suffix is
   long-only** on bars that rise and then fall, so the convention is enforced rather than
   assumed. `kcs-portfolio --mode` defaults to `long-only`; `kcs-riskparity` never shorts. Every
   number recommended anywhere in this repository was produced under that guard.
6. **Benchmarks.** `buy_and_hold` enters at the **second** bar's open, not the
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
* **A panel is positional, and `run_portfolio` now checks it.** `Panel.closes` and
  `.momentum` are indexed by the rebalance date list the panel was sampled on, so passing a
  *sliced* date list with full-history panels reads the wrong bars and returns a confident
  curve for a window that was never measured — exactly how a walk-forward script of mine
  produced five byte-identical years. `build_panel` now records the grid it used and
  `run_portfolio` refuses a mismatch by name; `kcs-portfolio --from/--to` rebuilds the panels
  for the window, which is why the CLI was right while the script was not.
* **Test fixtures must use grid-aligned timestamps** (`conftest.START =
  1_507_161_600`). TSMOM decides on an absolute epoch grid, so an unaligned
  synthetic series would never rebalance.
* **Journal entries record the window they evaluated**, so they stay verifiable
  after the archive grows; `verify` re-runs and compares every metric plus the
  per-trade table row by row, and exits non-zero on any difference. A **basket** entry
  (`kcs-basket --journal`, `kind: "basket"`) stores a window *per leg* plus the reported
  stretch (`window`) and — when `--spread-model` was used — the per-leg costs it charged, so
  verification replays exactly what ran instead of re-deriving a spread from a grown archive.
  Its `run_id` is `…-basket-<timeframe>-<n>legs` because a basket's label has spaces in it.

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
> re-selecting the names every year does not help (it costs ~15 points against choosing once).
>
> **The high-return branch is the sized wide book with a rolling drawdown gate**: 840 USDT
> pairs, sign 7/7, trend gate 200, `--vol-target 25%`, and `--max-below-peak` **re-chosen on the
> prior two years only** — five positive years, compounded **+612%**, worst year −38%, in the
> market ~51% (§1.5c). It beats the basket on return and loses badly on drawdown. Its own
> placebo — the same thresholds with the readings rotated between symbols, so the book size is
> the same (194 vs 207 names, 739 vs 747) — compounds to **−67.4%**, so the gate is carrying
> information rather than just buying fewer names. The one discount left is the archive itself:
> it has no delistings, which flatters any drawdown gate and an 840-name book especially (§4).

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
  wide book from Sharpe 0.49 to 0.73, its drawdown from −86.8% to −53.8%, and — across five
  separate yearly windows — from **−62.1% compounded to +46.9%**, beating the passive hold of
  the same 840 pairs (−36.8%) by 84 points (§1.5b). On the basket it is the difference between
  Sharpe 0.42 and 0.99 (§1.4–1.5). It is *nearly all* of the work the gates are credited with:
  the trend gate helps in one of the five years and hurts in the others.
* **Narrow beats wide, but the wide book is not dead once sized.** Over the same five yearly
  windows: the ten busiest pairs with the same rule compound to **+75.9%** (worst year −7.85%,
  worst drawdown −15.3%), the sized 840-pair book to **+46.9%** (worst year −20.1%, drawdown
  −46.1%). The earlier "a thousand pairs is the wrong shape" was measured without sizing and
  is too strong — the wide book beats its own benchmark, it is just dominated (§1.5b).
* **Never rebalance a five-name trend book monthly.** `kcs-riskparity`'s version returns +50%
  at Sharpe 0.19 where the fixed-weight basket returns +127% at 0.99 — re-equalising averages
  down into the weakest leg (§1.5, §1.9).
* **Costs decide everything — on the strategy that trades a lot.** The wide 7/7 book halves its
  result for every extra 0.1% per side (§1.6); the winning basket, which holds ten liquid pairs
  ~18% of the time, still returns +58% at Sharpe 0.61 when the fee is **five times** the
  modelled 0.1%, and charging each leg its own estimated spread (`--spread-model
  corwin-schultz`, hourly bars) costs it 0.04 Sharpe (+91.4% → +86.1%, §1.6a). Estimate spreads
  on the finest series available: the same estimator reads BTC at 30 bp on daily bars and
  5.6 bp on hourly ones.
* **Trend following on liquid survivors is the only thing that survived** across markets, and
  its positive mean is a handful of assets — leave-one-out removes most of the multiple
  (§1.7–1.8).
* **Diversifying the same rule across liquid names is where its edge looks least fragile**
  (§1.9), as long as the names are chosen by a rule and not rebalanced away.
* **The filter for dying assets is distance from the pair's own high, not liquidity.** The
  trend gate lifts Sharpe from 0.30 to ~0.50 and refuses 72% of the pairs that later lost;
  a turnover floor makes the same rule worse at every level, up to −87% (§1.3). The
  distance-from-peak gate has **no plateau**, so a fixed threshold is meaningless — but
  re-choosing it on the previous two years only, then applying it to the next year, is stable
  (−50% once, −30% four times), gives five positive years and compounds to **+612%** against
  **+47%** for the same rule without it and **+23%** with a fixed −90% (§1.5c). The value is in
  the re-choosing, and **the placebo confirms it is information rather than churn**: rotating
  the readings between symbols at the same book size (194 vs 207 names, 739 vs 747) compounds
  to **−67.4%** and loses in four of five years, so §1.3's "half of it is just a smaller book"
  was too cautious once sizing is in the mix. What still stands is §4's caveat: no delistings
  in this archive flatters any drawdown gate, and an 840-name book most of all.
* **Take-profit and stop-loss exits cost money and buy nothing — and the intrabar convention
  is what settles it.** `stops-sma` / `stops-breakout` / `voltarget-stops-sma` wrap any signal,
  and `engine.run_backtest(..., exit_prices=...)` fills a level **inside the bar** when the
  strategy reports one through `intrabar_exits`. The stated convention (bars cannot say whether
  the high or the low came first): **the stop wins if both levels are touched**, it fills at the
  level, a gap through it fills at the open, a gap in your favour does not. Read that way:
  on BTC daily a 10% stop on SMA200 turns +221% into +195% and the drawdown from −36% to −41%;
  a 15% trail on the same rule gives +56% at Sharpe 0.26; the daily **breakout reverses** —
  the stop that *helped* under the soft close-based trigger (+41.95% → +54.12%, Sharpe 0.22 →
  0.28) now **hurts** (+33.04%, 0.18, −56%), because a wick is noise while a close beyond the
  level is information (§1.11 vs §1.11b). Inside the recommended basket a 15% stop moves three
  tenths of a point, a 30% stop does nothing, a 20% trail buys 0.02 Sharpe for 2.7 points of
  return, and a 50% take-profit costs 20 points of return for 1.5 of drawdown. **An exit that is
  never touched is dead code; an exit that is touched costs money.** Use them for your own risk
  tolerance, not to improve the strategy (§1.11b).
* **Rejected by measurement, in one list**: cross-sectional ranking, shorting, grid trading,
  martingale, value averaging, RSI mean reversion, inverting an SMA, reading a slow signal
  every bar on hourly data, leverage, and take-profit or stop-loss overlays on a trend rule
  (§2).
* **Corrections that changed published numbers**: the portfolio ran one rebalance late (fixed;
  every portfolio figure was re-measured), the entry commission cancelled itself out against
  the curve's normalising base, and the quote-filter result reversed once the timing was
  right (§3).
* **Size is modelled too, and at retail it is noise.** `--capital 60000` charges each leg
  `coefficient * per-bar volatility * sqrt(order / turnover_per_bar)` (the square-root impact
  law). On the winning basket: **+83.90% at Sharpe 0.81 against +86.05% at 0.83** with spreads
  only — 2.2 points — while $1M costs 8 points, $10M costs 24, and even $1M at the harsh
  coefficient 0.5 leaves +51.6%. **Capacity is set by the thinnest leg** (0.5 bp of impact on
  BTC at $60k against 29 bp on MOVR), because every leg is held at `1/N`; a ten-name basket is
  as large as its least liquid name. Still unmodelled: partial fills and the queue, delistings
  (the archive has none), the true path *inside* a bar (the convention in §1.11b is stated, not
  measured), and funding — read §4 before trusting any number.
* **A liquidity rule costs more than the illiquidity it avoids — that gap is the survivorship
  bias, measured.** Holding a leg only while its rolling median turnover clears a floor
  (`--min-turnover-now`) *always* costs return: $100k → +78.2%, $300k → +69.0%, $1M → +63.8%
  against +86.1% for keeping everything, while the impact it avoids is worth 2.2 points at
  $60k (§1.6b, §1.6c). The reason is structural: this archive has **no delistings**, so the thin
  pairs in it are the ones that survived and multiplied, and the thin pairs that died are
  missing. The cost of the rule is therefore a **lower bound on the bias** — at least 17 points
  over five years — and it is the honest answer to "how much of this is survival": the +86%
  describes the pairs that lived.
* **`tsmom`'s defaults are hourly; on daily bars they make it a spectator.** `lookback` and
  `rebalance` count **bars** (720 and 168 = a month and a week on 1h, two years and five and a
  half months on 1d), which is why it sat out SEI and WLD entirely and lost 67% on ADA while
  "holding". Shortening it is a real out-of-sample improvement, not a fit: with parameters
  re-chosen on the past only, `tsmom` lookback 30-360 × rebalance 1/7 gives ADA **+2,485%
  against +388% for holding** (Sharpe 0.73 vs 0.26), and `tsmom-blend` with a base of 7-60 bars
  is the best return measured anywhere in this repository on BTC (**+3,097% at Sharpe 1.13**,
  drawdown −38.8% against holding's −76.9%). The in-sample "best" lookback differs per asset
  (BTC 180, SEI 360, WLD 30, ADA 30), so do not read it off one series; drawdowns stay −39…−83%,
  and `voltarget-sma` still has the best Sharpe on BTC (1.19 at −23%). **It also does not
  transfer everywhere**: on SUI-USDT the plain short-lookback TSMOM *lost* 21.66% out of sample
  where `sma` made +86.33% and `voltarget-sma` +20.78% at the best Sharpe of the group (0.54,
  drawdown −17.0%), through a period in which holding lost 60.34%. The narrow, transferable
  claim is therefore "hourly defaults on daily bars are dead code" — not "short lookbacks win".
  The module docstring says all of this, and `tsmom-blend` remains the way to avoid choosing a
  lookback (§1.12).
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

1. **The measurement programme is complete for what this archive can answer.** Every dial has
   been walked forward (§1.5a basket, §1.5b gates and sizing, §1.5c the rolling drawdown
   threshold), costs are bounded from every side that can be measured from OHLCV — fees, the
   per-pair spread (§1.6a), and your own size (§1.6b, 2.2 points at $60k, 8 at $1M, 24 at
   $10M) — and the drawdown gate has passed its own placebo under sizing (−67.4% against
   +612%, same book size). The one thread the data cannot settle is an archive **with
   delistings**: every number here is survivorship-biased (§4), and the wide book most of all.
   The "filter the universe by liquidity" thread is **answered and rejected**: every turnover
   floor made the sign rule worse (≥1e6 → −87%).
2. **Simulating the delistings the archive lacks.** Every measurable cost is now bounded (§1.6,
   §1.6a, §1.6b) and the liquidity rule that would dodge the illiquid names is measured too —
   it costs 17 points, which is the *lower bound* on what survivorship bias is worth (§1.6c).
   What is still missing is the other half of that bound: a death process. Forcing a fraction of
   the thin names to −100% each period, at a rate taken from KuCoin's real delisting history,
   would turn "at least 17 points" into a range — and it needs no new market data, only a
   number for the death rate.
3. Leave-one-out across the twelve long symbols: how broad is the TSMOM edge really?
4. **The intrabar fill model exists now** (`exit_prices`), so what is left here is the
   *convention*, not the machinery: the current rule is stop-first with level fills, and an
   ATR-trailing or break-even variant would be a variation on `intrabar_exits` rather than new
   engine work. Any such variant inherits the assumption, which is worth remembering before
   believing a small improvement from one.
