"""Synthetic data builders and session fixtures shared by the tests.

Real klines are noisy and expensive to reason about; `make_bars` and `ramp`
produce minimal series whose arithmetic can be checked by hand. The two
fixtures locate the collected archive and are skipped when it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ..data import Bar, load_series, repo_root

STEP = 3600  # one hour
START = 1_507_000_000  # an arbitrary hour-aligned timestamp


def make_bars(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    start: int = START,
    step: int = STEP,
) -> list[Bar]:
    """Bars with the given closes; opens default to the previous close.

    With `opens` omitted the series has no gaps at all, so a bar's return is
    `close[t] / close[t-1] - 1` and open-and-close execution coincide.
    """
    if opens is not None and len(opens) != len(closes):
        raise ValueError("opens and closes must be the same length")
    bars: list[Bar] = []
    for i, close in enumerate(closes):
        if opens is not None:
            open_ = opens[i]
        else:
            open_ = closes[i - 1] if i else close
        bars.append(
            Bar(
                time=start + i * step,
                open=open_,
                high=max(open_, close),
                low=min(open_, close),
                close=close,
                volume=1.0,
            )
        )
    return bars


def ramp(count: int, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(count)]


def write_archive(root: Path, symbol: str, timeframe: str, bars: list[Bar], name: str = "all.parquet") -> Path:
    """Write bars as a Parquet partition the loader understands."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    target = root / symbol / timeframe
    target.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "time": pa.array([b.time for b in bars], type=pa.int64()),
            "open": pa.array([b.open for b in bars], type=pa.float64()),
            "high": pa.array([b.high for b in bars], type=pa.float64()),
            "low": pa.array([b.low for b in bars], type=pa.float64()),
            "close": pa.array([b.close for b in bars], type=pa.float64()),
            "volume": pa.array([b.volume for b in bars], type=pa.float64()),
            "symbol": pa.array([symbol] * len(bars), type=pa.string()),
            "timeframe": pa.array([timeframe] * len(bars), type=pa.string()),
        }
    )
    pq.write_table(table, target / name)
    return root


def wavy(count: int = 400, *, amplitude: float = 20.0, period: float = 7.0, drift: float = 0.05) -> list[float]:
    """A trend plus a repeating cycle: enough crossings to make any SMA trade."""
    import math

    return [100.0 + amplitude * math.sin(i / period) + drift * i for i in range(count)]


@pytest.fixture
def toy_archive(tmp_path: Path) -> Path:
    """A tiny two-symbol archive in a temp dir, so CLI tests need no real data."""
    root = tmp_path / "spot"
    write_archive(root, "TOY-USDT", "1h", make_bars(wavy(400)))
    write_archive(root, "FLAT-USDT", "1h", make_bars([100.0] * 50), name="flat.parquet")
    return root


@pytest.fixture(scope="session")
def data_dir() -> Path:
    """Root of the parquet archive, skipping real-data tests when it is absent."""
    root = repo_root() / "data" / "kucoin" / "spot"
    if not (root / "BTC-USDT" / "1h").is_dir():
        pytest.skip(f"no collected data under {root}")
    return root


@pytest.fixture(scope="session")
def btc_hourly(data_dir: Path) -> list[Bar]:
    """BTC-USDT 1h as collected by `kcs-klines backfill` — the regression series."""
    return load_series(data_dir, "BTC-USDT", "1h")
