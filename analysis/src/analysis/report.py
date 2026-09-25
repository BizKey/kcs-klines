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

#: Curve colours, in the order the curves are given to `write_curves`.
CURVE_COLORS = ("#1a73e8", "#9aa0a6", "#188038", "#d93025", "#f9ab00", "#9334e6")

#: Vertical marker colours: into the market, out of it, and through zero.
MARKER_ENTRY = "#137333"
MARKER_EXIT = "#c5221f"
MARKER_FLIP = "#8430ce"

#: A marker is `(index into times, colour, tooltip text)`.
Marker = tuple[int, str, str]

__all__ = [
    "render",
    "write_trades",
    "write_equity",
    "write_chart",
    "write_curves",
    "write_metrics",
    "position_markers",
    "MARKER_ENTRY",
    "MARKER_EXIT",
    "MARKER_FLIP",
]


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
    """Equity vs benchmark on a log scale, as a dependency-free SVG.

    Every change of position is marked with a vertical line, so the curve can be
    read against what the strategy did: a run that spends most of its return on a
    handful of entries is visible at a glance, and so is a year of churn. The
    final level of each curve gets a dashed horizontal line and its multiple,
    which is the number the report prints.
    """
    strategy_label = f"{result.label} (net)"
    benchmark_label = "buy & hold (net)"
    write_curves(
        path,
        [bar.time for bar in bars],
        {strategy_label: result.equity, benchmark_label: result.benchmark.equity},
        title,
        colors={strategy_label: "#1a73e8", benchmark_label: "#9aa0a6"},
        markers=position_markers([bar.time for bar in bars], result.positions),
        levels={
            strategy_label: result.performance.final_equity,
            benchmark_label: result.benchmark.performance.final_equity,
        },
    )


def position_markers(times: list[int], positions: list[float]) -> list[Marker]:
    """One marker per bar where the *position* changed, not per resize.

    Entering, leaving and flipping are different events and get different colours.
    A strategy that only resizes — volatility targeting adjusts the exposure on
    nearly every bar — would otherwise turn the chart into a solid block of lines,
    which is why a change of size within the same position is not marked.
    """
    if len(times) != len(positions):
        raise ValueError(f"got {len(positions)} positions for {len(times)} times")
    markers: list[Marker] = []
    for i in range(1, len(positions)):
        previous, current = positions[i - 1], positions[i]
        if current == previous:
            continue
        if previous == 0:
            colour, what = MARKER_ENTRY, "into the market"
        elif current == 0:
            colour, what = MARKER_EXIT, "out of the market"
        elif (current > 0) != (previous > 0):
            colour, what = MARKER_FLIP, "flipped"
        else:
            continue  # the same position, held at a different size
        markers.append((i, colour, f"{iso(times[i])} UTC — {what} ({previous:g} → {current:g})"))
    return markers


def write_curves(
    path: Path,
    times: list[int],
    curves: dict[str, list[float]],
    title: str,
    *,
    colors: dict[str, str] | None = None,
    markers: list[Marker] | None = None,
    levels: dict[str, float] | None = None,
) -> None:
    """Several curves on one log-scale chart, as a dependency-free SVG.

    Every curve needs exactly one value per entry in `times`; the first is drawn
    thicker, because it is the one being asked about. `markers` draws vertical
    lines behind the curves at the given indices, and `levels` draws a dashed
    horizontal line with its multiple at the end value of the named curve — the
    headline figure, which is what a reader wants off the chart without doing
    arithmetic on a log axis. A curve that reaches zero — an account wiped out by
    a geared position — is drawn on the axis floor rather than crashing the log
    scale, and labels are XML-escaped so an `&` in a symbol or a strategy name
    cannot corrupt the file.
    """
    if not times or not curves:
        raise ValueError("nothing to plot")
    for label, values in curves.items():
        if len(values) != len(times):
            raise ValueError(f"curve {label!r} has {len(values)} points for {len(times)} times")
    marks = list(markers or [])
    for index, _, _ in marks:
        if not 0 <= index < len(times):
            raise ValueError(f"marker index {index} is outside 0..{len(times) - 1}")
    for label, level in (levels or {}).items():
        if label not in curves:
            raise ValueError(f"level given for {label!r}, which is not one of the curves")
        if level <= 0:
            raise ValueError(f"level for {label!r} must be positive to sit on a log scale")
    positive = [value for values in curves.values() for value in values if value > 0]
    if not positive:
        raise ValueError("every curve is at or below zero: nothing to draw on a log scale")

    path.parent.mkdir(parents=True, exist_ok=True)
    pad_l, pad_r, pad_t, pad_b = 70, 24, 40, 40
    # A wiped-out curve sits just under the lowest real value instead of at -inf.
    floor = min(positive) / 2
    lo_exp = math.floor(math.log10(min(positive)) * 4) / 4
    hi_exp = math.ceil(math.log10(max(positive)) * 4) / 4
    span = (hi_exp - lo_exp) or 1.0
    n = len(times)
    # ~1 point per pixel is plenty at this width and keeps the file small.
    step = max(1, n // (WIDTH - pad_l - pad_r))
    idx = sorted(set(list(range(0, n, step)) + [n - 1]))

    def x(i: int) -> float:
        return pad_l + (WIDTH - pad_l - pad_r) * i / (n - 1) if n > 1 else pad_l

    def y(value: float) -> float:
        return pad_t + (HEIGHT - pad_t - pad_b) * (1 - (math.log10(max(value, floor)) - lo_exp) / span)

    def poly(values: list[float]) -> str:
        return " ".join(f"{x(i):.2f},{y(values[i]):.2f}" for i in idx)

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
        i = round((n - 1) * k / 5)
        axis.append(
            f'<text x="{x(i):.2f}" y="{HEIGHT - pad_b + 20}" font-size="12" fill="#666" '
            f'text-anchor="middle">{day(times[i])}</text>'
        )

    legend = []
    lines = []
    level_lines = []
    for position, (label, values) in enumerate(curves.items()):
        colour = (colors or {}).get(label) or CURVE_COLORS[position % len(CURVE_COLORS)]
        lines.append(
            f'<polyline fill="none" stroke="{colour}" stroke-width="{1.8 if position == 0 else 1.6}" '
            f'points="{poly(values)}"/>'
        )
        legend.append(
            f'<text x="{WIDTH - pad_r}" y="{pad_t + 12 + 16 * position}" font-size="12" '
            f'font-family="sans-serif" fill="{colour}" text-anchor="end">{_xml(label)}</text>'
        )
        if levels and label in levels:
            level = levels[label]
            # The multiple sits at the left, just above its line: the right-hand
            # side belongs to the legend, and a curve that ends high would collide
            # with it there.
            level_lines.append(
                f'<g class="final-level"><title>{_xml(f"{label} ends at {level:,.2f}x")}</title>'
                f'<line x1="{pad_l}" y1="{y(level):.2f}" x2="{WIDTH - pad_r}" y2="{y(level):.2f}" '
                f'stroke="{colour}" stroke-width="1" stroke-dasharray="6 4" stroke-opacity="0.8"/></g>'
                f'<text x="{pad_l + 4}" y="{y(level) - 4:.2f}" font-size="12" font-weight="bold" '
                f'font-family="sans-serif" fill="{colour}">{level:,.2f}x</text>'
            )

    # Position changes go behind the curves; the more of them there are, the
    # fainter each one is, so an active rule cannot blacken its own chart.
    changes = []
    if marks:
        opacity = max(0.15, min(0.9, 40.0 / len(marks)))
        for index, colour, label in marks:
            changes.append(
                f'<g class="position-change"><title>{_xml(label)}</title>'
                f'<line x1="{x(index):.2f}" y1="{pad_t}" x2="{x(index):.2f}" y2="{HEIGHT - pad_b}" '
                f'stroke="{colour}" stroke-width="1" stroke-opacity="{opacity:.2f}"/></g>'
            )
        counted = [
            (MARKER_ENTRY, "in", "green"),
            (MARKER_EXIT, "out", "red"),
            (MARKER_FLIP, "flip", "purple"),
        ]
        tally = ", ".join(
            f"{word} = {name} ({sum(1 for _, colour, _ in marks if colour == target)})"
            for target, word, name in counted
            if any(colour == target for _, colour, _ in marks)
        )
        key_text = f"position changes: {tally}"
    else:
        key_text = ""

    body = "\n".join(lines)
    key = "\n".join(legend)
    mark_svg = "\n".join(changes)
    level_svg = "\n".join(level_lines)
    footer = (
        f'<text x="{pad_l}" y="{HEIGHT - 6}" font-size="11" font-family="sans-serif" '
        f'fill="#666">{_xml(key_text)}</text>'
        if key_text
        else ""
    )
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>
<text x="{pad_l}" y="24" font-size="15" font-family="sans-serif" fill="#111">{_xml(title)}</text>
{''.join(grid)}{''.join(axis)}
{mark_svg}
{body}
{level_svg}
{key}
{footer}
<line x1="{pad_l}" y1="{HEIGHT - pad_b}" x2="{WIDTH - pad_r}" y2="{HEIGHT - pad_b}" stroke="#bbb"/>
</svg>
"""
    )


def _xml(text: str) -> str:
    """`&` in a symbol or a strategy name must not break the SVG."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
