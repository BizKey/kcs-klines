"""Reading and auditing the kline series collected by `kcs-klines backfill`.

A *series* is one `symbol` at one `timeframe`, stored as one Parquet file per
partition, e.g. `data/kucoin/spot/BTC-USDT/1h/2017.parquet`. Everything here is
read-only and offline.
"""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

def repo_root() -> Path:
    """The checkout this package lives in, or the working directory as a fallback.

    Used for the two locations that belong to the repository rather than to
    wherever the command happens to be run from: the kline archive and the
    trade journal.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return Path.cwd()


#: Kline archive written by `kcs-klines backfill`.
DEFAULT_DATA_DIR = repo_root() / "data" / "kucoin" / "spot"

#: Fixed-length timeframes, in seconds. `1mon` is deliberately absent: its bars
#: are 28-31 days long, so it is handled as a calendar step instead.
FIXED_TIMEFRAMES: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}
CALENDAR_TIMEFRAMES = frozenset({"1mon"})

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV kline. `time` is the bar's open time in unix seconds (UTC)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class QualityReport:
    """What the data itself looks like, independent of any strategy."""

    bars: int
    first: int
    last: int
    gaps: int
    missing_bars: int
    largest_gap_slots: int
    bars_violating_ohlc: int
    nonpositive_prices: int

    def as_dict(self) -> dict:
        return asdict(self)


def iso(ts: int) -> str:
    """`1507107600` -> `2017-10-04 09:00` (UTC, no seconds)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


#: How long each suffix means, in seconds. `m` is a minute and `mon` a month —
#: the trading convention, because "1m" on a chart is a one-minute bar.
DURATIONS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "second": 1,
    "m": 60,
    "min": 60,
    "minute": 60,
    "h": 3600,
    "hour": 3600,
    "d": 86400,
    "day": 86400,
    "w": 7 * 86400,
    "week": 7 * 86400,
    "mon": 30 * 86400,
    "month": 30 * 86400,
    "y": 365 * 86400,
    "year": 365 * 86400,
}


def parse_duration(text: str) -> int:
    """`"1y"` -> 31536000, `"30d"` -> 2592000, `"6 mon"` -> 15552000.

    Used by `--last`, so the flag reads the way a person writes it. Months and
    years are fixed lengths (30 and 365 days) on purpose: a calendar month would
    make `--last 1mon` mean something different on every run. A capital `M` is a
    month and a small `m` a minute, because that is what they mean on every chart
    — and a silent ten-thousand-fold error is worth one case-sensitive branch.
    """
    cleaned = text.strip()
    digits = ""
    for character in cleaned:
        if character.isdigit() or character == ".":
            digits += character
        else:
            break
    suffix = cleaned[len(digits) :].strip()
    key = "mon" if suffix == "M" else suffix.lower()
    if not digits or key not in DURATIONS:
        known = ", ".join(sorted({"1s", "1m", "1h", "1d", "1w", "1mon", "1y"}))
        raise ValueError(f"cannot read {text!r} as a duration; try one of {known} (e.g. 30d)")
    return int(float(digits) * DURATIONS[key])


def is_known_timeframe(timeframe: str) -> bool:
    return timeframe in FIXED_TIMEFRAMES or timeframe in CALENDAR_TIMEFRAMES


def interval_seconds(timeframe: str) -> int:
    """Length of one bar, in seconds. Calendar months are rejected on purpose."""
    try:
        return FIXED_TIMEFRAMES[timeframe]
    except KeyError:
        if timeframe in CALENDAR_TIMEFRAMES:
            raise ValueError(
                f"{timeframe!r} is a calendar timeframe: its bars are 28-31 days long, "
                "so it has no fixed interval. Use `bars_per_year` or `next_label` instead."
            ) from None
        raise ValueError(
            f"unknown timeframe {timeframe!r}; known: "
            f"{', '.join(sorted(set(FIXED_TIMEFRAMES) | CALENDAR_TIMEFRAMES))}"
        ) from None


def bars_per_year(timeframe: str) -> float:
    """How many bars of this timeframe a 365-day year holds (annualisation factor)."""
    if timeframe in CALENDAR_TIMEFRAMES:
        return 12.0  # exactly 12 monthly bars in a year
    return SECONDS_PER_YEAR / interval_seconds(timeframe)


def next_label(ts: int, timeframe: str) -> int:
    """The open time the *next* bar must have, for gap detection.

    Fixed timeframes add their interval; `1mon` advances one calendar month, so
    a missing month counts as one missing bar rather than thirty.
    """
    if timeframe in CALENDAR_TIMEFRAMES:
        moment = datetime.fromtimestamp(ts, tz=timezone.utc)
        month = moment.month + 1
        year = moment.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day_of_month = min(moment.day, calendar.monthrange(year, month)[1])
        return int(moment.replace(year=year, month=month, day=day_of_month).timestamp())
    return ts + interval_seconds(timeframe)


def series_dir(data_dir: Path | str, symbol: str, timeframe: str) -> Path:
    return Path(data_dir) / symbol / timeframe


def series_files(data_dir: Path | str, symbol: str, timeframe: str) -> list[Path]:
    """Every Parquet file of one series, in filename order (one per partition)."""
    return sorted(series_dir(data_dir, symbol, timeframe).glob("*.parquet"))


def load_series(data_dir: Path | str, symbol: str, timeframe: str) -> list[Bar]:
    """Load one series: every partition, de-duplicated by time, ascending.

    Duplicates across partition boundaries are resolved in favour of the file
    that sorts last, matching how the collector merges overlapping periods.
    """
    files = series_files(data_dir, symbol, timeframe)
    if not files:
        raise FileNotFoundError(
            f"no parquet files in {series_dir(data_dir, symbol, timeframe)}; "
            "run `kcs-klines backfill` first"
        )

    by_time: dict[int, Bar] = {}
    for path in files:
        table = pq.read_table(path, columns=["time", "open", "high", "low", "close", "volume"])
        cols = table.to_pydict()
        for i, ts in enumerate(cols["time"]):
            by_time[ts] = Bar(
                time=ts,
                open=cols["open"][i],
                high=cols["high"][i],
                low=cols["low"][i],
                close=cols["close"][i],
                volume=cols["volume"][i],
            )

    bars = [by_time[ts] for ts in sorted(by_time)]
    if not bars:
        raise ValueError(f"{series_dir(data_dir, symbol, timeframe)} holds no bars")
    return bars


def available_series(data_dir: Path | str = DEFAULT_DATA_DIR) -> list[tuple[str, str]]:
    """Every `(symbol, timeframe)` pair present on disk."""
    root = Path(data_dir)
    if not root.is_dir():
        return []
    found = []
    for symbol_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for tf_dir in sorted(p for p in symbol_dir.iterdir() if p.is_dir()):
            if any(tf_dir.glob("*.parquet")):
                found.append((symbol_dir.name, tf_dir.name))
    return found


def window_of(bars: list[Bar], first: int, last: int) -> list[Bar]:
    """The part of a series inside `[first, last]`, both inclusive.

    This is what keeps a recorded backtest checkable: the archive grows as the
    collector appends bars, but the bars between two timestamps do not move.
    """
    return [bar for bar in bars if first <= bar.time <= last]


def data_quality(bars: list[Bar], timeframe: str) -> QualityReport:
    """Continuity and sanity checks over the stored bars.

    Missing bars are counted against the timeframe's own calendar, and bars that
    violate OHLC (`high < max(open, close)`) are counted rather than dropped:
    KuCoin really does serve such bars, and the archive keeps them verbatim.
    """
    if not bars:
        raise ValueError("cannot audit an empty series")

    gaps = 0
    missing = 0
    largest = 1
    for prev, cur in zip(bars, bars[1:]):
        expected = next_label(prev.time, timeframe)
        if cur.time != expected:
            gaps += 1
            slots = 1
            probe = expected
            while probe < cur.time and slots < 100_000:
                probe = next_label(probe, timeframe)
                slots += 1
            missing += slots - 1
            largest = max(largest, slots)

    bad_ohlc = sum(
        1
        for b in bars
        if b.high < max(b.open, b.close) or b.low > min(b.open, b.close) or b.high < b.low
    )
    nonpositive = sum(1 for b in bars if b.open <= 0 or b.close <= 0)

    return QualityReport(
        bars=len(bars),
        first=bars[0].time,
        last=bars[-1].time,
        gaps=gaps,
        missing_bars=missing,
        largest_gap_slots=largest,
        bars_violating_ohlc=bad_ohlc,
        nonpositive_prices=nonpositive,
    )
