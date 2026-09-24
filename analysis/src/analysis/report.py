"""Human-readable report and the artifact writers (CSV, JSON, SVG).

No plotting dependency is used: the equity chart is an SVG polyline written by
hand, on a log scale, downsampled to roughly one point per pixel.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from .data import Bar, QualityReport, day, iso
from .engine import BacktestResult
from .metrics import pct

WIDTH = 1100
HEIGHT = 420

__all__ = ["render", "write_trades", "write_equity", "write_chart", "write_metrics"]


def render(
    symbol: str,
    timeframe: str,
    quality: QualityReport,
    result: BacktestResult,
) -> str:
    """The console report: data audit, strategy vs benchmark, execution checks."""
    perf = result.performance
    benchmark = result.benchmark.performance
    lines: list[str] = []
    add = lines.append

    add("=" * 78)
    add(f"{result.label} — {symbol} {timeframe}, {result.costs}")
    add("=" * 78)
    add(
        f"data        : {quality.bars:,} bars, {iso(quality.first)} .. {iso(quality.last)} UTC "
        f"({perf.years:.2f} years)"
    )
    add(
        f"continuity  : {quality.gaps} gaps, {quality.missing_bars:,} missing bars, "
        f"largest gap {quality.largest_gap_slots} slots"
    )
    add(
        f"bar quality : {quality.bars_violating_ohlc} bars violating OHLC, "
        f"{quality.nonpositive_prices} non-positive prices"
    )
    add("")
    add("execution   : signal on close of t, filled at open of t+1, equity marked at opens")
    add("")
    add(f"{'metric':<26}{result.label:>18}{'buy & hold':>18}")
    add("-" * 78)

    def row(label: str, left: str, right: str) -> None:
        add(f"{label:<26}{left:>18}{right:>18}")

    row("total return (net)", pct(perf.total_return), pct(benchmark.total_return))
    row("total return (gross)", pct(result.gross_equity - 1.0), pct(result.benchmark.gross_equity - 1.0))
    row("CAGR", pct(perf.cagr), pct(benchmark.cagr))
    row("annualised vol", pct(perf.ann_vol), pct(benchmark.ann_vol))
    row("Sharpe (rf=0)", f"{perf.sharpe:.2f}", f"{benchmark.sharpe:.2f}")
    row("max drawdown", pct(perf.max_dd), pct(benchmark.max_dd))
    add("")
    row("time in market", pct(result.exposure), "100.00%")
    row("closed trades", f"{len(result.closed_trades)}", "1")
    row("win rate", pct(result.win_rate) if result.closed_trades else "—", "—")
    row("avg bars held", f"{result.avg_bars_held:.0f}" if result.closed_trades else "—", "—")
    row("avg / median trade", f"{pct(result.avg_trade)} / {pct(result.median_trade)}", "—")
    row("geometric avg trade", pct(result.geo_trade) if result.closed_trades else "—", "—")
    row("best / worst trade", f"{pct(result.best_trade)} / {pct(result.worst_trade)}", "—")
    row("total fees paid", pct(result.fees_paid), f"{1 - (1 - result.costs.rate) ** 2:.2%}")
    row("final equity", f"{perf.final_equity:.4f}x", f"{benchmark.final_equity:.4f}x")
    add("")
    add(
        f"drawdown span {result.label:<12}: {iso(perf.max_dd_start)} .. {iso(perf.max_dd_end)} UTC"
    )
    add(
        f"drawdown span {'buy & hold':<12}: {iso(benchmark.max_dd_start)} .. {iso(benchmark.max_dd_end)} UTC"
    )
    if result.open_position:
        add("note        : the last position is still open and is marked to market")
    add(
        f"bookkeeping : compounding all {len(result.trades)} trades gives "
        f"{result.bookkeeping:.4f}x vs equity curve {perf.final_equity:.4f}x "
        f"(error {result.bookkeeping_error:.1e})"
    )
    add(
        f"fill timing : filling at the signal bar's own close instead would give "
        f"{result.close_fill_equity:.4f}x vs {perf.final_equity:.4f}x here"
    )
    for warning in result.warnings:
        add(f"WARNING     : {warning}")
    return "\n".join(lines)


def write_trades(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "side",
                "entry_time_utc",
                "entry_price",
                "exit_time_utc",
                "exit_price",
                "bars_held",
                "gross_return",
                "net_return",
                "equity_at_entry",
                "equity_at_exit",
                "open_at_end",
            ]
        )
        for t in result.trades:
            writer.writerow(
                [
                    t.side,
                    iso(t.entry_time),
                    f"{t.entry_price:.8f}",
                    iso(t.exit_time or 0),
                    f"{t.exit_price or 0.0:.8f}",
                    t.bars_held if t.bars_held is not None else "",
                    f"{t.gross_return:.6f}" if t.gross_return is not None else "",
                    f"{t.net_return:.6f}" if t.net_return is not None else "",
                    f"{t.equity_at_entry:.8f}",
                    f"{t.equity_at_exit:.8f}" if t.equity_at_exit is not None else "",
                    "yes" if t.open_at_end else "",
                ]
            )


def write_equity(path: Path, bars: list[Bar], result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time_utc", "close", "equity", "buy_hold", "position"])
        for i, bar in enumerate(bars):
            writer.writerow(
                [
                    iso(bar.time),
                    f"{bar.close:.8f}",
                    f"{result.equity[i]:.8f}",
                    f"{result.benchmark.equity[i]:.8f}",
                    result.positions[i],
                ]
            )


def write_metrics(path: Path, result: BacktestResult, quality: QualityReport, symbol: str, timeframe: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.as_dict()
    payload["symbol"] = symbol
    payload["timeframe"] = timeframe
    payload["quality"] = quality.as_dict()
    path.write_text(json.dumps(payload, indent=2, default=str))


def write_chart(path: Path, bars: list[Bar], result: BacktestResult, title: str) -> None:
    """Equity vs benchmark on a log scale, as a dependency-free SVG."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pad_l, pad_r, pad_t, pad_b = 70, 24, 40, 40
    equity = result.equity
    benchmark = result.benchmark.equity
    # ~1 point per pixel is plenty at this width and keeps the file small.
    step = max(1, len(bars) // (WIDTH - pad_l - pad_r))
    idx = sorted(set(list(range(0, len(bars), step)) + [len(bars) - 1]))
    values = [equity[i] for i in idx] + [benchmark[i] for i in idx]
    lo_exp = math.floor(math.log10(min(values)) * 4) / 4
    hi_exp = math.ceil(math.log10(max(values)) * 4) / 4
    span = (hi_exp - lo_exp) or 1.0

    def x(i: int) -> float:
        return pad_l + (WIDTH - pad_l - pad_r) * i / (len(bars) - 1)

    def y(v: float) -> float:
        return pad_t + (HEIGHT - pad_t - pad_b) * (1 - (math.log10(v) - lo_exp) / span)

    def poly(series: list[float]) -> str:
        return " ".join(f"{x(i):.2f},{y(series[i]):.2f}" for i in idx)

    grid = []
    for decade in range(int(math.floor(lo_exp)), int(math.ceil(hi_exp)) + 1):
        for mult in (1, 2, 5):
            value = mult * 10.0**decade
            if not 10.0**lo_exp * 0.999 <= value <= 10.0**hi_exp * 1.001:
                continue
            grid.append(
                f'<line x1="{pad_l}" y1="{y(value):.2f}" x2="{WIDTH - pad_r}" y2="{y(value):.2f}" '
                f'stroke="#ececec" stroke-width="1"/>'
                f'<text x="{pad_l - 10}" y="{y(value) + 4:.2f}" font-size="11" fill="#666" '
                f'text-anchor="end">{value:g}x</text>'
            )
    axis = []
    for k in range(6):
        i = round((len(bars) - 1) * k / 5)
        axis.append(
            f'<text x="{x(i):.2f}" y="{HEIGHT - pad_b + 20}" font-size="12" fill="#666" '
            f'text-anchor="middle">{day(bars[i].time)}</text>'
        )

    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>
<text x="{pad_l}" y="24" font-size="15" font-family="sans-serif" fill="#111">{title}</text>
{''.join(grid)}{''.join(axis)}
<polyline fill="none" stroke="#9aa0a6" stroke-width="1.6" points="{poly(benchmark)}"/>
<polyline fill="none" stroke="#1a73e8" stroke-width="1.8" points="{poly(equity)}"/>
<text x="{WIDTH - pad_r}" y="{pad_t + 12}" font-size="12" font-family="sans-serif" fill="#1a73e8" text-anchor="end">{result.label} (net)</text>
<text x="{WIDTH - pad_r}" y="{pad_t + 28}" font-size="12" font-family="sans-serif" fill="#9aa0a6" text-anchor="end">buy &amp; hold (net)</text>
<line x1="{pad_l}" y1="{HEIGHT - pad_b}" x2="{WIDTH - pad_r}" y2="{HEIGHT - pad_b}" stroke="#bbb"/>
</svg>
"""
    )
