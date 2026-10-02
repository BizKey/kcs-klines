# analysis — testing trading strategies on the collected klines

A small, dependency-light toolkit that answers one question over and over:
**what would this strategy have done on this data, once you pay for trading?**

```bash
uv sync                          # create/refresh the environment from uv.lock
uv run kcs-backtest              # SMA 200 on BTC-USDT 1h, 0.1% per side
uv run kcs-backtest --symbol ETH-USDT --timeframe 4h --sweep 50,100,200
uv run kcs-backtest --fee 0      # same signals, no commission
uv run kcs-backtest --list       # strategies, their parameters, stored series
uv run kcs-backtest --strategy sma-ls --param window=100
uv run kcs-backtest --journal --note "why I ran this"
uv run kcs-backtest --strategy tsmom --fee 0.001 --symbol ETH-USDT
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT
uv run kcs-backtest --symbol BTC-USDT --last 1y   # only the last year, warm history
uv run kcs-backtest --strategy tsmom --param lookback=30 --param rebalance=7 \
    --dd-scale 10,40,25                           # cut size while the account is in drawdown
uv run kcs-riskparity --top 5 --min-history 3y --vol-budget 0.4   # a de-risked book
uv run kcs-journal verify        # re-check what was recorded
uv run pytest                    # 423 tests
```

`analysis` is a [uv](https://docs.astral.sh/uv/) workspace member: the root
project collects data, this one tests strategies against it. `uv run` uses the
environment described by `pyproject.toml` + `uv.lock` — nothing to activate — and
`uv run python -m analysis.run_backtest` is an exact equivalent of
`uv run kcs-backtest`.

Strategy parameters are generic: `--param NAME=VALUE` (repeatable) and
`--sweep [NAME=]V1,V2,…`. Nothing in the CLI knows about any particular
strategy, so a new one needs no plumbing — `--list` shows what each accepts.

Everything is offline and read-only: the data comes from the Rust collector in
the repository root (`kcs-klines backfill`), which writes KuCoin Spot OHLCV as
Parquet under `data/kucoin/spot/<SYMBOL>/<TIMEFRAME>/`.

---

**Evaluating one stretch, which is what a walk-forward needs.** `--from 2024-10-01 --to
2025-10-01` bounds the reported window by date instead of "the last N", so the same rule can be
judged one year at a time with the selection made before each window:

```bash
for pair in "2021-10-01 2022-10-01" "2022-10-01 2023-10-01" "2023-10-01 2024-10-01" \
            "2024-10-01 2025-10-01" "2025-10-01 2026-10-01"; do
  set -- $pair
  uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma \
      --param window=50 --param target_vol=0.30 --from $1 --to $2 --no-artifacts
done
# compounded +75.9%; worst year −7.85%; worst drawdown −15.3%; the same names held: −52.4%
```

**Choosing the legs by a rule.** `--select-turnover N` replaces a hand-typed `--symbols` list
with the N busiest pairs by median quote turnover over `--select-lookback` (default `90d`)
**immediately before the reported window**, so `--last 5y` cannot be selected with hindsight
from inside its own window. `--symbols` then becomes the candidate pool (omit it to use the
whole archive) and `--select-quote` (default `USDT`) filters that pool. The chosen names and
their median turnover are printed. This is what makes the configuration in `CONCLUSIONS.md`
§1.4 reproducible in one command instead of a script:

```bash
uv run kcs-basket --select-turnover 10 --timeframe 1d --strategy voltarget-sma \
    --param window=50 --param target_vol=0.30 --last 5y
# +91.43% at Sharpe 0.87 and a −17.47% drawdown, against −28.19% for holding the same ten
```

## Layout

`analysis` is a uv workspace member with a `src` layout, so the importable
package is `analysis/src/analysis/` and its tests travel with it:

```
analysis/
├── pyproject.toml          # this package: pyarrow, dev pytest, the console scripts
├── README.md
├── out/                    # artifacts (gitignored)
└── src/analysis/
    ├── data.py  metrics.py  engine.py  report.py  journal.py  run_backtest.py  basket.py
    ├── strategies/         # one module per strategy + the registry
    └── tests/              # pytest suite, inside the package on purpose
```

| file | what lives there |
|---|---|
| `data.py` | load one series from Parquet; audit continuity, OHLC sanity, timeframe arithmetic (including calendar months) |
| `metrics.py` | `sma`, `ema`, `macd`, `max_drawdown`, `performance` (vol, Sharpe, CAGR), `pct` |
| `engine.py` | the execution model: costs, positions, trades, equity curve, invariants, buy & hold benchmark |
| `strategies/base.py` | the `Strategy` interface — one method, `targets(bars)` |
| `strategies/sma_trend.py` | `SmaTrend` (long-only, `rebalance=1` reads it every bar, `168` once a week) and `SmaTrendLongShort` |
| `strategies/sma_reversion.py` | `SmaReversion` — buy below the average, sell above (contrarian) |
| `strategies/macd.py` | `MacdTrend` — long while MACD is above its signal line (default 12/26/9) |
| `strategies/tsmom.py` | `Tsmom` — time-series momentum, decided once every N bars (low turnover on purpose); `BlendTsmom` — several horizons averaged into one signal, so there is no lookback to fit |
| `strategies/rsi_reversion.py` | `RsiReversion` — buy oversold RSI, leave on an exit level or a time stop |
| `strategies/scaled.py` | `ScaledStrategy` — wrap any strategy and size it to a volatility target |
| `strategies/breakout.py` | `DonchianBreakout` — entry channel and a *shorter* exit channel, plus `min_hold` |
| `strategies/registry.py` | registry machinery: `register`, `parameters`, `sweep_parameter` |
| `strategies/__init__.py` | the registry: `get_strategy("sma", window=200)`, parameter introspection |
| `report.py` | console report plus CSV / JSON / SVG writers |
| `journal.py` | the trade journal: record a run, re-verify it later (see `journal/README.md`) |
| `walkforward.py` | choose parameters on a train window, judge them on the next unseen one |
| `portfolio.py` | cross-sectional momentum across the whole universe, ranked and rebalanced |
| `basket.py` | a named list of symbols, one strategy on every leg, combined into one curve |
| `riskparity.py` | a rule-picked book: top names by turnover, unequal weights, a volatility budget |
| `run_backtest.py` | the CLI that ties it together |
| `out/` | artifacts (gitignored) |
| `tests/` | pytest suite: engine contracts, registry-wide strategy checks, CLI end-to-end on a temp archive, and a regression test against the real archive |

Tests sit inside the package because they exercise internals directly
(`from ..engine import …`) and belong to the same import tree; a test module is
never imported by the package itself.

### Running it with uv

```bash
uv sync                     # create/refresh .venv from pyproject.toml + uv.lock
uv run kcs-backtest …       # console script declared in analysis/pyproject.toml
uv run kcs-journal …        # the other one
uv run pytest               # tests (config lives in the root pyproject.toml)
uv add --package analysis some-package          # a new runtime dependency
uv add --package analysis --dev some-dev-tool   # a new dev dependency
```

`uv run` re-syncs the environment first, so a fresh clone needs nothing but
`uv sync` (or just the first `uv run`). Without uv, the equivalent is
`python -m analysis.run_backtest` inside an environment that has `pyarrow`
installed — the package itself is pip-installable from `analysis/`.

## Five ways to ask a question

| tool | the question it answers |
|---|---|
| `kcs-backtest` | what would this strategy have done on this series, at these costs? |
| `kcs-basket` | what would it have done on **these** symbols together? |
| `kcs-riskparity` | what would a rule-picked book have done, held in unequal weights and de-risked? |
| `kcs-walkforward` | if I picked parameters on the past, how did that choice do afterwards? |
| `kcs-portfolio` | which of ~1000 symbols should I hold, and what does the ranking cost? |

`kcs-walkforward` exists because a parameter sweep over one stretch of history
answers "what looked best in hindsight". It rolls a train/test pair forward,
picks the parameter on the train window, measures it on the unseen window, and
stitches those windows into one out-of-sample curve compared against buy & hold
over the same spans. On BTC-USDT 1h it separates the two trend rules cleanly:
SMA 200 (3000/1000 split) returns **+10.5%** out of sample against **+729%** for
buy & hold, while TSMOM rebalanced weekly returns **+1086%** against **+875%**
with a better Sharpe (0.66 vs 0.43) and a shallower drawdown (-54% vs -78%).

`kcs-portfolio` slices the whole archive's daily universe by momentum, holds the
chosen names in equal weights, charges commission on realised turnover, and compares
against the same universe held passively. It reads only the `time` and `close`
columns, so ~950 symbols take seconds rather than minutes.

**Two ways to choose the slice**, and they are different bets:

```bash
# rank: the top of the cross-section (the classic cross-sectional momentum)
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --top 0.2

# sign: everything that cleared a bar — "buy what rose, sell what fell"
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --select sign --threshold 0
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --select sign --threshold 5%
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --select sign --last 60
uv run kcs-portfolio --timeframe 1d --lookback 30 --rebalance 30 --select sign --chart out.svg
```

* `--select rank` (the default) takes a slice of the sorted cross-section: `--top`
  is a fraction below 1 or an absolute count at 1 or more. It holds the same number
  of names whatever the market does.
* **The book can be sized to a volatility target** (`--vol-target 25% --vol-window 12
  --vol-cap 1 --vol-floor 0`): the whole book is scaled by `target / realised`, where the
  reading is the **unscaled** book's own completed periods — measuring the account instead
  would divide by a volatility that already contains the multiplier. `--vol-window` is in
  rebalance periods, not bars (the strategy registry's `vol_window` is in bars). It is the
  largest single improvement measured on the wide book: Sharpe 0.49 → 0.73, drawdown
  −86.8% → −53.8%, commission 1,559% of capital → 113%. The report prints the average
  multiplier, so you can see how much of the capital was actually at work;
* **Health gates refuse the names that are about to die**, all read from bars at or before
  the rebalance date: `--trend-gate N` (close at or above its own N-bar mean; **100–200 works
  and the plateau is wide**), `--max-below-peak P` (drop names more than P below their own
  running peak), `--min-turnover X` (median quote turnover per bar) and `--min-volatility P`
  (annualised floor — this is what removes dead-calm stablecoins), with `--gate-window` for
  the bars the last two look at. Measured on the 7/7 sign book: the trend gate takes Sharpe
  from 0.30 to ~0.50 and the drawdown from −93% to −87% while trading only 31–44 names, and a
  placebo with the same thresholds but shuffled readings gives a median of −33%, so the gate
  knows something. A tight `--max-below-peak 30%` looks spectacular (+177,786%, Sharpe 0.90)
  but runs 13 names and has no plateau — treat it as a walk-forward parameter. **A turnover
  floor makes this rule worse at every level** (≥1e6 → −87%): its winners are illiquid early
  names, so liquidity is the wrong filter here even though the ranking needed it.
* **`--quote` decides which pairs are in the universe at all.** The default is
  **`USDT`**: only pairs settled in tether, because a cross pair like `ADA-BTC` is a
  different bet — its price is a ratio of two crypto assets, so the dollar move cancels out
  and holding it silently adds a second exposure (plus a second currency to fund) that the
  rule never asked for. `--quote USDT,USDC` widens it to both dollar stables, `--quote any`
  restores the old "every pair on disk" behaviour, and the report always says what it did
  (`836 symbols quoted in USDT, 156 other pairs skipped`). Measured on the same 7/7 sign
  configuration over nine years, the filter is worth real money — cross pairs were a drag:

  | universe | pairs | total | CAGR | Sharpe | max DD |
  |---|---|---|---|---|---|
  | `--quote USDT` (default) | 836 | +1,141.23% | 32.5% | 0.30 | −92.6% |
  | `--quote any` | 992 | **+3,339.12%** | 48.4% | 0.57 | −81.4% |

  which is the *opposite* of what this table said before the timing fix in §3 of
  `CONCLUSIONS.md` — under the old one-rebalance lag the narrower USDT universe looked
  better (+2,954% against +1,651%), and on the monthly grid too (sign +610.85% with USDT
  only against `--quote any` +1,782.88%; `rank --top 0.2` −45.38% against +238.47%). So
  choose the quote for what the strategy *is* — one settlement currency, one exposure per
  asset — not for what it earned here. What the filter does keep doing is removing the pair
  that cannot move: `USDT-USDC` is quoted in USDC, so the USDT rule drops the busiest row of
  the trade census (241 trades on a stablecoin), and `--quote USDT,USDC` measures within a
  point of `--quote USDT`. Fee totals are in starting-capital units and grow with the curve
  (860.6% on the USDT book at 1.30 turnover per rebalance) — the turnover is what compares;
* `--select sign` takes **every** symbol whose trailing return beats `--threshold`
  (`0` means "it rose", `5%` is stricter, negative values buy what fell). It is a
  filter, not a ranking, so its breadth moves with the market: many names in a bull
  tape, few in a decline, and **nothing at all** when nothing qualifies — the book
  goes to cash and pays the commission to get there. That liquidation is the
  strategy, not an edge case: the report counts those rebalances (`cash`) and the
  result warns how often it happened.
* `--last N` reports only the newest `N` rebalances. Warm-up still uses the history
  before them, so an indicator is never cold at the window's start, and the curve is
  the portfolio's own (nothing is rebased from the longer run).
* the console report lists **which pairs were actually traded**: how many of the universe
  were held at least once, how many were never bought, and a table of the busiest ones
  (rebalances held, entries, exits, first and last date in the book).
  `--pairs-csv PATH` writes the full census — one row per symbol the run *watched*,
  including the ones it never bought (`ever_traded = no`, `rebalances_held = 0`), with
  `symbol, rebalances_held, buys, sells, shorts, covers, first_held_utc, last_held_utc`.
  It is the file to read when asking "what did this rule actually do": on the 7/7 sign run
  over 992 pairs with `--quote any`, **980 were held at least once and 12 never were** — the filter is not
  selective about *which* pairs it trades, it is selective about *when* (breadth swings
  1 … 688), and the busiest rows show the cost of that (`USDT-USDC` 241 trades,
  `LTC-BTC` 235, `LTC-USDT` 234);
* `--chart-trades PATH` writes a **second** chart: one line per asset (its close on the
  rebalance grid, so the marked points *are* the fill prices this module charges commission
  on), a **green dot where the book bought it** and a **red square where it sold it**, with
  the actual price in the tooltip (`BTC-USDT bought 2017-10-26 at 6010`). Assets the rule
  never bought are drawn as a **grey dashed** line — the honest picture of a filter, since
  most of the universe is watched and never held. `--chart-symbols N` caps the drawing
  (default 40, most-held assets first, with a fifth of the budget for the grey ones): a
  weekly rule on a 146-name book makes ~10,000 trades over nine years, and drawing them all
  is a grey rectangle, not a chart;
* `--chart PATH` writes the portfolio against its benchmark as an SVG — the same
  hand-written chart the rest of the toolkit produces, with a dashed line at each curve's
  final multiple and **vertical markers only where the book entered or left cash**
  (green for back in, red for to cash, with the name count and turnover in the tooltip).
  Breadth is deliberately *not* marked: on a wide universe it swings from 1 to 578 names
  and would draw forty lines out of a hundred rebalances, so the report states it as a
  number instead (`breadth: 0 … 24 … 408 names (min / median / max)`). A ranking book
  enters once and never leaves, so its chart has a single marker.

**This rule is not the same strategy as `--strategy tsmom`, even though the signal is
identical** (`Tsmom.threshold` is this `--threshold`). A single-asset TSMOM is out of the
market 46% of the time on BTC and pays ~20% of capital in commission over nine years; the
same 7/7 signal applied to every pair holds 146 names, is never out of the market, turns the
book over ~630 times and pays **1,348%** of capital — which is why the portfolio version ends
at −69% over the last five years where the single-asset version made +86.7%. The wider the
book, the more the exit half of the signal is neutralised, and the more the commission bill
decides the result: at 0.00/0.05/0.10/0.20/0.30% per side the same 7/7 run returns
+3,197/+2,303/+1,651/+829/+392%. A control that holds every pair with the same weekly
re-equalisation and no filter at all returns +350% at 0.11 turnover per rebalance.

Measured over the whole daily archive (2017-11 … 2026-09, 0.1%/side, 30-bar lookback
and rebalance, fees as a share of starting capital) — **before the timing fix** described
in `CONCLUSIONS.md` §3, kept here because the ordering it shows is what the fix preserved:
with correct timing the same grid gives `sign --threshold 0` +1,782.88% at Sharpe 0.43 and
`rank --top 0.2` +238.47% at 0.19 on `--quote any`, and +610.85% against −45.38% on the
USDT-only universe:

| selection | result | equal-weight universe | Sharpe | max DD | names held |
|---|---|---|---|---|---|
| `--select rank --top 0.2` | **−81.18%** | −47.39% | −0.27 | −91.0% | ~64 |
| `--select sign --threshold 0` | **+0.51%** | −47.39% | 0.00 | −88.1% | ~120 |
| `--select sign --threshold 5%` | −13.08% | −47.39% | −0.02 | −92.0% | fewer |
| `--select sign --threshold -5%` (buy the fallen) | +16.51% | −47.39% | 0.02 | −87.2% | ~few |
| any of them, `--mode long-short` | **−100%** | −47.39% | — | −100% | — |

The filter beats the ranking by a wide margin and the short side is wiped out, which
is the same conclusion the rest of this repository reached from the other direction:
**the universe, not the signal, decides**, and on a universe of ~950 pairs including
the dying ones no selection rule rescues the result. The same `sign` rule over the
last five years loses 86% of the capital while the passive book loses 59%.

`kcs-basket` is the one in between: you name the legs and it tells you what the
account looks like.

```bash
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,BNB-USDT
uv run kcs-basket --symbols BTC-USDT,ETH-USDT --strategy sma --param window=200
uv run kcs-basket --symbols BTC-USDT,ETH-USDT,SOL-USDT --timeframe 1d --fee 0
```

Every leg runs its own strategy through the same engine, so it obeys the same
timing rule and pays its own commission. Four decisions are worth knowing, because
they are what the number means:

* **the window** is what every leg has in common — the latest listing starts the
  basket and the earliest delisting ends it. The alternative, holding a new leg's
  share in cash until it lists, flatters whichever leg happens to be new;
* **the timeline** is the bars *every* leg has, so no leg is ever marked on a bar
  it did not trade;
* **the weights** are fixed at `1/N` and are never rebalanced against each other,
  so the combined curve is exactly `1 + Σ w·(ratio − 1)` and no cross-leg turnover
  is charged. The split is exactly even **at the start** — $10,000 becomes $2,000 a
  leg — and drifts afterwards, because nothing pulls it back: a leg that doubled
  owns more than its `1/N` by the end. The report prints both, the fixed target
  (`weight`) and the realised slice (`share`), plus how much of the capital was in
  the assets rather than in cash at all. Each leg's own fees are inside its own
  curve, and the report restates them against the capital the leg had when the
  window opened;
* **the benchmark** is the same names bought once, in equal parts, at the window's
  **second** open and held. The window's first bar is where the newest leg's
  history starts — the series itself when there is one leg — and `targets[0]`
  never trades there, so entering at that open would credit the passive side with
  a move no strategy can take (SUI-USDT's first hourly bar ran 0.10 → 1.28, which
  turned a −21% hold into a "+912%" one). It is the same rule as
  `engine.buy_and_hold`, and it is why `kcs-basket --symbols SUI-USDT` now reports
  exactly what `kcs-backtest --symbol SUI-USDT` does.

It writes `basket_<strategy>_<legs>_<timeframe>_equity.csv` (the basket, the benchmark and
every leg, one row per shared bar), the same curve as an SVG against the
benchmark, and a JSON summary (`--json`, `--json-curves` for the raw curves). The
legs are in the filename, so a one-symbol run cannot silently overwrite a
five-symbol one; a list too long for a filename falls back to `Nlegs-<digest>`.

What it deliberately does **not** do: rank anything, pick the legs for you, or
rebalance legs against each other. If you want selection, that is `kcs-portfolio`.

### A book held in unequal weights, de-risked

`kcs-riskparity` is the "hold a few tokens in different proportions and move money
out of the volatile ones" idea, written down so it can be measured:

```bash
uv run kcs-riskparity --top 5 --min-history 3y --vol-budget 0.4
uv run kcs-riskparity --top 20 --weight invvol --max-weight 0.2 --vol-budget none
uv run kcs-riskparity --top 5 --min-history 3y --last 1y   # only the last year
uv run kcs-riskparity --top 20 --buy-hold SOL-USDT         # against holding SOL instead
uv run kcs-riskparity --top 5 --min-history 3y --trend 200d   # risk parity plus a signal
```

* the **universe** is the top `--top` symbols by trailing turnover, re-selected on a
  grid using only past data, and `--min-history` keeps the fresh listings out. That
  filter does more than anything else: three years instead of one cut the commission
  bill from 145% of capital to 17% and volatility from 111% to 33%;
* the **weights** are `--weight equal` (the default: plain `1/N`, with no volatility
  in it) or `--weight invvol`. Inverse volatility is the literal reading of "hold the
  volatile ones smaller" — a token whose volatility rises gets a smaller target, so a
  faller is cut and a riser is trimmed. It *is* risk parity once correlations are
  ignored, which on a 100-name book is the only honest option: a covariance matrix
  wants more bars than names. Measured on the last book of `--top 100` (1d), `equal`
  puts every name at **1.00%**, while `invvol` spreads **0.11% (LSK, 710% a year) to
  5.33% (BDX, 15%)** — a 46x range about a 0.81% median. It needs two guards. A pair calmer than
  `MIN_VOLATILITY` (10% a year) is a stablecoin rather than a position: unguarded, a
  top-20 inverse-vol book held 91.6% of USDC-USDT. And no single name may exceed
  `--max-weight`, whose default is twice an equal weight, never below a quarter — it
  scales with the book so a two-name book is not left half in cash;
* the chart and the report carry a **passive reference**: `--buy-hold SYMBOL` (default:
  the `--calendar` symbol, `none` to switch it off) plots one price held from the first
  bar. It is a different animal from the "equal-weight hold" benchmark, which re-selects
  and re-equalises the *same universe* as the book on the same grid — the reference
  answers "did any of this beat simply holding BTC?". It is the price ratio less one
  commission side on the way in and no exit (an open position is not liquidated on the
  last bar), marked at the same closes as the book, and a symbol listed after the
  calendar starts sits in cash until it has a price. Inside a `--last` window it is
  *re-bought* at the window's start rather than carried in: a passive hold has no state,
  so "what you held" and "what you would have bought" are the same position;
* the **trend rule** (`--trend 30d`, off by default) is the one thing here that can
  leave the market. A name is held only while its close is above its close that long
  ago — the signal `Tsmom` reads — and a name that loses its trend is sold on the
  **next close**, between rebalances, so the exit is not a month late; it can only come
  back at the next rebalance, which keeps entries on the same slow clock as the rest of
  the book. The passive comparisons are never gated: gating them too would fold the rule
  being measured into the thing it is measured against. Because the book can now be in
  cash for long stretches, the report says how much capital was **at work across bars**
  and on what share of bars it held nothing at all, and the equity CSV carries the
  exposure on *every* row rather than only on the rebalance rows;
* the **risk budget** scales the whole book down when its own trailing volatility is
  above `--vol-budget`, and never up. It is a dial, not a promise: asking for 40% on
  a wide universe delivered 111%;
* a decision is taken on a bar's close and applied on the **next** close, and costs
  are charged on the traded notional measured against the drifted weights — only the
  *difference* is traded, so a position that is already at its target costs nothing
  and one that has drifted is trimmed rather than sold and rebought (the naive
  version costs about 3.2× more commission on the top-5 book);
* `--last` reports only the last stretch (`1y`, `6mon`, `90d`). Durations follow
  `data.parse_duration`, where **`m` is a minute and `mon` a month**, so half a year is
  `6mon` — and asking for a window shorter than one bar of the chosen timeframe is an
  error that says so rather than quietly becoming a one-bar book. Inside the window,
  the same rule as
  everywhere else applies: the ranking, the volatility estimates, the age filter and the risk
  budget's trailing volatility all still see the history before the window, so the
  window measures the same book rather than a version of it that started cold. The
  book is taken as it stood on the window's first bar, and the report says so; the
  window's own commissions are restated against the capital it started with, and a
  windowed run writes its own artifacts (`_last1y` in the name) so it cannot
  overwrite the full run's.

### Reading only the recent stretch

`--last` on `kcs-backtest` and `kcs-basket` reports the last stretch of the data
instead of the whole series: `--last 1y`, `--last 6mon`, `--last 30d`, `--last 12h`.
Durations read the way a person writes them, and a capital `1M` is a month while a
small `1m` is a minute — the trading convention, and worth the one case-sensitive
branch.

What the flag deliberately does **not** do is truncate the inputs. The strategy
still sees every bar before the window, so its signals are the ones it really had;
a series cut to the window would leave its indicators cold and invent a different
history (the test `test_a_window_keeps_the_signals_the_legs_really_had` pins
exactly that difference). Only the *reported* stretch moves:

* every metric is window-relative: the curve starts at 1.0, `CAGR` annualises the
  window's own length, and fees are restated against the capital the window began
  with. On a one-month window `CAGR` is arithmetic rather than information — the
  report prints it anyway, next to the window length that makes it meaningless;
* the benchmark is re-entered at the window's second open, the same rule
  `buy_and_hold` follows on a whole series, so a listing bar cannot leak in;
* a position already open when the window began is carried in as a partial trade,
  which keeps `prod(1 + net_return) == final_equity` true for the window, and the
  report says so in a warning;
* in a basket, equal `1/N` weights are re-established at the window's start rather
  than carrying the drift the legs had before it.

Two guards come from the same rule that a journal entry must be checkable from the
archive alone: `--last` cannot be combined with `--journal` (journal the full run,
then read the window off it), and a window holding fewer than three bars is refused
with the count rather than producing a number.

---

## The one rule that keeps a backtest honest

```
targets[t]   is decided on the CLOSE of bar t
the position changes at the OPEN of bar t+1
equity       is marked at bar opens
```

So the exposure held over `(open[t], open[t+1])` is `targets[t-1]`, and a
strategy may only look at `bars[0..t]` when producing `targets[t]` — never
later. Getting this wrong is not a rounding error: applying a bar's move to the
position decided on *that same bar's close* produced a **1.2 billion x** equity
curve on BTC-USDT 1h where the honest number is **1.89x**. That episode is why
the engine carries two checks:

* **the trade book must reproduce the curve** — `result.bookkeeping_error` is the
  relative gap between the independently computed equity path and the product of
  all trade returns. Healthy runs sit at ~1e-14; a look-ahead bug pushes it to
  order 1 and is surfaced as a warning (`--strict` turns it into an exception);
* **commissions are charged on every change of exposure**, on the full notional,
  from the equity at that moment. A round trip pays two sides, a long→short flip
  pays two as well. `result.gross_equity` is the same signal path with zero
  costs, so the difference is exactly what trading cost.

Each trade's `net_return` spans from just before its own entry commission to
just after its exit commission, which is what makes the book and the curve add
up: `prod(1 + net_return) == final_equity`.

A more optimistic fill convention (trading at the signal bar's own close) is
computable for comparison via `engine.close_fill_final_equity` — on BTC-USDT 1h
it gives 0.78x against 1.89x, which is the cost of acting one bar later.

### The benchmark starts where the strategy can

`buy_and_hold` enters at the **second** bar's open, not the first's. Nothing can
trade on bar 0 (`targets[0]` never takes effect), so `open[1]` is the earliest
price any strategy in this toolkit can be filled at; a benchmark entered at
`open[0]` would be handed the first bar's move for free. On an ordinary bar that
is nothing, and on old series it is nothing at all — BTC-USDT and ETH-USDT both
open their first hourly bar exactly where their second opens. On a **listing
bar** it is the entire result. PYTH-USDT's first hourly bar ran from 0.06 to
0.319 (`close/open` 5.3x), and quoting buy & hold from that first print turned a
series that fell 78.8% from the first tradable price into a "+13.5% buy & hold";
SUI-USDT is the same story (0.10 → 1.28 on the first bar, 10.17x reported against
0.79x measured from `open[1]`). Both curves now start at 1.0 on bar 0 — one in
cash, the other in cash too, until `open[1]` — so they are compared from the same
place.

---

## Adding a strategy

Three things are required — `targets()`, `slug` and `describe()` — plus a
registration. Nothing else: the CLI, the report, the artifacts and the contract
tests pick the strategy up from the registry.

```python
# a trimmed version of analysis/src/analysis/strategies/breakout.py, which ships
# with the toolkit and carries more parameters (entry/exit_window/min_hold/mode)
from ..data import Bar
from .base import Strategy
from .registry import register          # NOT `from . import register`: that is a cycle


class DonchianBreakout(Strategy):
    name = "breakout"
    sweep_param = "lookback"        # what `--sweep 10,20` sweeps by default

    def __init__(self, lookback: int = 20):
        self.lookback = lookback

    @property
    def slug(self) -> str:
        return f"donchian{self.lookback}"

    @property
    def warmup(self) -> int:
        return self.lookback        # bars needed before a signal can exist

    @property
    def params(self) -> dict:
        return {"lookback": self.lookback}

    def targets(self, bars: list[Bar]) -> list[float]:
        out: list[float] = []
        for i, bar in enumerate(bars):
            if i < self.lookback:
                out.append(0.0)                       # warm-up: no signal yet
            else:
                window = [b.high for b in bars[i - self.lookback:i]]   # excludes bar i
                out.append(1.0 if bar.close > max(window) else 0.0)
        return out

    def describe(self) -> str:
        return f"long on a new {self.lookback}-bar high, flat otherwise"


@register("breakout")
def _breakout(lookback: int = 20, **_: object) -> Strategy:
    """Long on a new N-bar high, flat otherwise."""
    return DonchianBreakout(lookback=lookback)
```

Then add one line to `analysis/src/analysis/strategies/__init__.py` so the package imports
your module (or import it yourself before calling the CLI):

```python
from .breakout import DonchianBreakout
```

```bash
uv run kcs-backtest --list
# what the example above would report, if you registered it under its own name:
#   breakout(lookback=20)   [--sweep lookback]
#       Long on a new N-bar high, flat otherwise.
# the strategy that actually ships registers as:
#   breakout(entry=20, exit_window=10, min_hold=0, mode='long-only')   [--sweep entry]
uv run kcs-backtest --strategy breakout --param entry=30
uv run kcs-backtest --strategy breakout --sweep 10,20,40
```

`analysis/src/analysis/strategies/breakout.py` is the shipped version of that example: already
registered, already covered by the tests, and worth reading before writing your own — its two
separate windows are what keeps a breakout system from trading thousands of times.

Two rules keep this from breaking:

* import the decorator from `.registry`, never from the package root
  (`from . import register` deadlocks the import of the package — a test guards
  this);
* keep the module free of cycle-prone imports at load time.

The factory's named parameters *are* the CLI parameters: they come from
`inspect.signature`, so `--param` knows exactly what to accept and rejects a
typo with the list of valid names — before loading any data.

### What you get for free, and what to check

| free | why |
|---|---|
| execution, costs, equity curve, trades, drawdown, Sharpe | `engine.py` owns all of it |
| report line, CSV/JSON/SVG artifacts, `--json` | `report.py` |
| `--param`, `--sweep`, `--list`, unknown-name errors | the registry |
| interface + no-look-ahead tests | the registry is looped over by `src/analysis/tests/test_strategies.py` |
| the invariant "trade book = equity curve" | checked on every run, reported as `bookkeeping_error` |

Two things a strategy must get right on its own:

* **look-ahead.** `targets[t]` may only use `bars[0..t]`. The test
  `test_targets_never_peek_at_later_bars` rewrites one bar at a time and asserts
  that no *earlier* target moved — it runs over every registered strategy, so a
  new one is covered without writing a test.
* **warm-up.** Return `0.0` until the indicator has enough history; the engine
  fills `targets[0]` with whatever you return first, and that bar never trades.

Exposure may be fractional or negative (`-1` shorts); the engine validates it
into `[-1, 1]`. A geared strategy can wipe the account out, in which case the run
stops at zero equity and says so in `result.warnings`.

## The trade journal

A backtest is only worth something if it can be re-checked. `--journal` appends
a record of the run to `journal/` in the repository — not to the gitignored
`analysis/out/` — holding the strategy name and its parameters, the costs, the
exact data window with a SHA-256 digest of its OHLCV values, the full metric set
and your own note:

```bash
uv run kcs-backtest --strategy sma --param window=200 \
    --journal --note "reference run before touching the engine"

uv run kcs-journal report        # table of runs, per-strategy roll-up
uv run kcs-journal verify        # re-run everything and compare
uv run kcs-journal show --id 20260924T191341Z-sma200
```

`verify` reloads the archive, **slices it back to the recorded window**, rebuilds
the strategy from the entry alone, re-runs the engine with the recorded costs and
compares every numeric field at a relative tolerance of 1e-9 — and, when the run
kept its trade table, checks that table row by row as well. It exits non-zero on
failure, so it can sit in CI. The status distinguishes the failure modes:

| status | meaning |
|---|---|
| `verified` | data digest and every metric reproduce |
| `data-changed` | metrics reproduce, but the stored bars are not those recorded |
| `mismatch` | same inputs, different numbers — the strategy, the engine or the entry changed |
| `unknown-strategy` | the registry no longer has this strategy name |

That window slicing is not a detail. The archive is alive: while this README was
being written the collector appended a bar and the reference series grew from
78,264 to 78,265 bars. An entry recorded before that still verifies, because the
run pinned the bars it actually evaluated rather than "whatever is on disk".

Entries are append-only (`runs.jsonl`, one JSON object per line) so a re-run
never rewrites history: run the same strategy again next month and `report` shows
both entries side by side, which is how you watch statistics move as the archive
grows. `--keep-trades` additionally copies the per-trade table into
`journal/trades/<run_id>.csv`.

Format details are in [`journal/README.md`](../journal/README.md).

---

## Using it as a library

```python
from analysis import Costs, data, engine
from analysis.strategies import get_strategy

bars = data.load_series("data/kucoin/spot", "BTC-USDT", "1h")
strategy = get_strategy("sma", window=200)
costs = Costs(fee_per_side=0.001, slippage_per_side=0.0002)

result = engine.run_backtest(bars, strategy.targets(bars), "1h", costs, label=strategy.slug)

print(result.performance.cagr, result.bookkeeping_error, len(result.closed_trades))
print(result.benchmark.performance.final_equity)   # buy & hold, same window, same costs
print(result.as_dict())                            # JSON-friendly summary
```

Exposure may be fractional or negative (`-1` shorts), which is why the engine
validates it into `[-1, 1]`; it may also wipe the account out, in which case the
run stops at zero equity and says so in `result.warnings`.

---

## What the artifacts contain

For `--symbol BTC-USDT --timeframe 1h --strategy sma --param window=200`, `out/` gets:

* `sma200_BTC-USDT_1h_trades.csv` — one row per leg: side, entry/exit time and
  price, bars held, gross and net return, and the equity level at each end;
* `sma200_BTC-USDT_1h_equity.csv` — per bar: close, strategy equity, buy & hold
  equity, position held;
* `sma200_BTC-USDT_1h_equity.svg` — both curves on a log scale (hand-written
  SVG, no plotting dependency), with a **vertical line at every change of
  position**: green for entering the market, red for leaving it, purple for a
  flip, and a caption counting them. Hovering a line gives the bar and the move
  (`2024-02-22 01:00 UTC — into the market (0 → 1)`). A *resize* is not marked —
  volatility targeting adjusts its exposure on nearly every bar, and marking those
  would turn the chart into a solid block; the more lines there are, the fainter
  each one is drawn. The basket chart marks its own swaps the same way, with the
  legs that moved named in the tooltip
  (`2021-09-23 01:00 UTC — BTC-USDT, ETH-USDT, BNB-USDT out; capital in the market 80% → 20%`).
  The `kcs-riskparity` chart uses the same green/red/purple vocabulary for a
  **different event** — its lines are *rebalances*, not entries and exits: green means
  the book put more capital to work, red that it took risk off, grey that the size did
  not change and only the names did, and a purple flip cannot occur because the book
  is long-only. Each tooltip carries the transition it marks
  (`2026-06-11 00:00 UTC — selected 100 names, 100% → 67% invested, turnover 0.62`), and
  the caption under the chart counts each colour, so the two meanings cannot be
  confused;
* `--dd-scale START,FULL,FLOOR` (in percent, `off` by default) is a **drawdown
  overlay**, applied in the engine to every strategy: at each bar the target exposure is
  multiplied by a factor that is 1.0 while the account is within `START` of its own
  high-water mark, falls linearly to `FLOOR` at `FULL`, and comes back on its own as the
  account recovers. It reads the account — the equity the same bar produced — so it
  cannot look ahead, and `--dd-scale 10,40,25` means "full size until −10%, a quarter of
  the target at −40%". The multiplier is kept per bar in the result and the artifacts
  carry it in the name (`tsmom30-7_dd10-25_BTC-USDT_1d_equity.csv`). **A floor of zero is
  a trap**: a flat account's drawdown never shrinks, so the factor stays at zero forever —
  measured at 15,30,0 it turned TSMOM's 24.94x into 1.36x. What the overlay does *not* do
  is improve risk-adjusted return: across TSMOM, SMA 200 and `voltarget-sma` on BTC daily
  it cut return and drawdown by roughly the same proportion and left Sharpe where it was
  (0.80 → 0.78, 0.67 → 0.68, 0.79 → 0.77). It is a dial for surviving your own drawdown,
  not an edge;
* `sma200_BTC-USDT_1h_metrics.json` — the summary, the data audit, and the
  benchmark if `--json` is given (`--json-curves` adds the full curves).

---

## Notes on the data it reads

* A series is one symbol at one timeframe; the loader de-duplicates overlapping
  partitions by timestamp and sorts ascending.
* `data_quality()` reports gaps and missing bars using the timeframe's own
  calendar — a missing `1mon` bar counts as one missing month, not thirty.
* Bars that violate OHLC are counted, never dropped: KuCoin really serves them,
  and the archive stores them verbatim. As of the current BTC-USDT 1h series
  that is 62 gaps / 398 missing bars and 1 impossible bar over 78,264 bars
  covering 2017-10-04 to 2026-09-24.
* `1mon` has no fixed interval (28-31 day bars), so `interval_seconds()` rejects
  it while `bars_per_year()` and `next_label()` handle it as a calendar step.
* The archive ends at the last collected bar, so the final position is marked to
  market rather than closed — the report says when that is the case.
