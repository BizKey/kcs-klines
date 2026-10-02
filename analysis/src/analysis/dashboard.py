"""A local dashboard over the kline archive.

The archive is 1.4 GB across a thousand symbols, so the chart cannot be a file
generated up front: the dashboard is a small local HTTP server that reads the
Parquet on demand, plus a single page that draws candlesticks and volume on a
canvas. Nothing is installed beyond `pyarrow`, which the package already needs, and
the page has no build step and no CDN.

    uv run kcs-dashboard                 # http://127.0.0.1:8765
    uv run kcs-dashboard --port 9000 --open
    uv run kcs-dashboard --export BTC-USDT,1h --out btc.html   # one self-contained file

Two endpoints carry the data and both are cheap enough to call from the page:

* `GET /api/index?timeframe=1d` — one row per symbol: last price, change over the
  recent window, median turnover per bar, bar count. Built from the newest partition
  of each series, which is the part that fits in memory, and cached for a minute so
  a collector run does not need a restart.
* `GET /api/bars?symbol=BTC-USDT&timeframe=1h&bars=5000&end=<unix>` — the bars
  themselves, as parallel arrays rather than objects (a third of the bytes), newest
  last. `end` walks backwards so the page can extend the history as you pan left.

Anything else is `404` with a JSON body, and a bad symbol is a `400` — the symbol is
looked up in the archive listing rather than joined into a path, so a request cannot
reach outside the data directory.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import data, spreads

#: The dashboard itself: a thin layer over TradingView's Lightweight Charts.
PAGE = Path(__file__).with_name("dashboard.html")

#: The hand-written canvas renderer this dashboard started as. It is kept as a fallback
#: (served at `/canvas`) for the day the vendored library cannot be used, and it is the
#: page `analysis/experiments/render_check.mjs` can actually execute headlessly — the
#: library page needs a real DOM, a browser and a canvas, none of which the test
#: environment has.
CANVAS_PAGE = Path(__file__).with_name("dashboard-canvas.html")

#: Vendored, not fetched from a CDN: the archive is local and so is the dashboard.
VENDOR = Path(__file__).with_name("vendor") / "lightweight-charts.standalone.production.js"

#: The page's script tag, replaced by the library's source when a chart is exported, so a
#: saved file carries its own renderer and works with no server and no network.
VENDOR_TAG = '<script src="/vendor/lightweight-charts.js"></script>'


class Asset:
    """A file the server hands out, re-read whenever it changes on disk.

    The page used to be read once when the server started, which meant a running dashboard
    kept serving the version it booted with: every edit needed a restart, and a `git pull`
    needed one too. Re-reading is a `stat` per request and a read only when the timestamp
    moved, so the loop is cheap and the browser always gets the current file.
    """

    def __init__(self, path: Path):
        self.path = path
        self._cached: tuple[float, int, str] | None = None

    def text(self) -> str:
        stat = self.path.stat()
        key = (stat.st_mtime, stat.st_size)
        if self._cached is None or self._cached[:2] != key:
            self._cached = (*key, self.path.read_text())
        return self._cached[2]


def vendored_library() -> str:
    if not VENDOR.is_file():
        raise FileNotFoundError(
            f"{VENDOR} is missing: the dashboard needs the vendored Lightweight Charts build "
            "(see analysis/src/analysis/vendor/README.md)"
        )
    return VENDOR.read_text()

#: Timeframes the collector stores, in the order the page shows them.
TIMEFRAMES = ("1h", "4h", "1d", "1w", "1mon")

#: How long an index row (or a loaded series) is trusted before it is rebuilt.
#: The archive is alive: a collector run during the day appends bars, and a minute
#: is short enough that the page notices without re-reading a thousand files.
CACHE_SECONDS = 60.0

#: Bars per request when the page does not say, and the ceiling a request may ask for.
DEFAULT_BARS = 5_000
MAX_BARS = 200_000

#: The change windows the sidebar can show. `"1"` is **one bar of the selected
#: timeframe** — the honest answer to "how did this move on the timeframe I am looking
#: at" — and the others are fixed stretches, labelled as such in the page. The first
#: version showed a single unlabelled "recent" window (24 bars on hourly, 30 on daily,
#: 4 on weekly), which read as a daily move on a daily chart and looked wrong.
CHANGE_WINDOWS: tuple[tuple[str, int | None], ...] = (
    ("1", None),
    ("24h", 86_400),
    ("7d", 7 * 86_400),
    ("30d", 30 * 86_400),
    ("1y", 365 * 86_400),
)


def change_bars(timeframe: str) -> dict[str, int]:
    """How many bars of a timeframe each change window covers, at least one."""
    per_year = data.bars_per_year(timeframe)
    return {
        label: max(1, round(seconds * per_year / (365 * 86_400))) if seconds else 1
        for label, seconds in CHANGE_WINDOWS
    }


def _change(closes: list[float], bars: int) -> float | None:
    """The change over that many bars, or `None` when the series is too short for it."""
    if bars <= 0 or len(closes) <= bars:
        return None
    return closes[-1] / closes[-bars - 1] - 1.0


@dataclass
class Cached:
    """A value with the moment it was built."""

    value: object
    built: float

    def fresh(self, seconds: float = CACHE_SECONDS) -> bool:
        return (time.monotonic() - self.built) < seconds


class Archive:
    """Reads the archive on demand and remembers what it just read.

    The lock matters: `ThreadingHTTPServer` answers each request in its own thread, and
    two panning gestures at once would otherwise both walk the directory and both build
    the same index.
    """

    def __init__(self, data_dir: Path | str, *, cache_seconds: float = CACHE_SECONDS):
        self.data_dir = Path(data_dir)
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._series: dict[tuple[str, str], Cached] = {}
        self._index: dict[str, Cached] = {}
        self._stats: dict[tuple[str, str], Cached] = {}
        self._symbols: Cached | None = None

    # --- the archive's own shape ------------------------------------------
    def series(self) -> list[tuple[str, str]]:
        """Every `(symbol, timeframe)` on disk, cached for the cache window."""
        with self._lock:
            if self._symbols is not None and self._symbols.fresh(self.cache_seconds):
                return self._symbols.value  # type: ignore[return-value]
            listed = data.available_series(self.data_dir)
            self._symbols = Cached(listed, time.monotonic())
            return listed

    def symbols(self, timeframe: str) -> list[str]:
        return sorted(symbol for symbol, tf in self.series() if tf == timeframe)

    def require(self, symbol: str, timeframe: str) -> None:
        """Refuse a symbol the archive does not have. Keeps paths out of requests."""
        if timeframe not in TIMEFRAMES:
            raise KeyError(f"unknown timeframe {timeframe!r}")
        if symbol not in set(self.symbols(timeframe)):
            raise KeyError(f"no {timeframe} series for {symbol}")

    # --- bars --------------------------------------------------------------
    def bars(self, symbol: str, timeframe: str) -> list[data.Bar]:
        """One series, reloaded when the files under it change."""
        self.require(symbol, timeframe)
        key = (symbol, timeframe)
        stamp = max(
            (path.stat().st_mtime for path in data.series_files(self.data_dir, symbol, timeframe)),
            default=0.0,
        )
        with self._lock:
            hit = self._series.get(key)
            if hit is not None and hit.fresh(self.cache_seconds) and hit.value[0] == stamp:
                return hit.value[1]  # type: ignore[return-value]
        loaded = data.load_series(self.data_dir, symbol, timeframe)
        with self._lock:
            self._series[key] = Cached((stamp, loaded), time.monotonic())
        return loaded

    # --- the sidebar -------------------------------------------------------
    def index(self, timeframe: str) -> list[dict]:
        """One summary row per symbol, from the newest partition of each series."""
        if timeframe not in TIMEFRAMES:
            raise KeyError(f"unknown timeframe {timeframe!r}")
        with self._lock:
            hit = self._index.get(timeframe)
            if hit is not None and hit.fresh(self.cache_seconds):
                return hit.value  # type: ignore[return-value]
        rows = [row for symbol in self.symbols(timeframe) if (row := self._row(symbol, timeframe))]
        rows.sort(key=lambda row: row["turnover"], reverse=True)
        with self._lock:
            self._index[timeframe] = Cached(rows, time.monotonic())
        return rows

    def stats(self, symbol: str, timeframe: str, *, window: int = 120) -> dict:
        """The numbers that decide whether a pair is worth trading, not just looking at.

        `spread_per_side` comes from the same Corwin-Schultz estimator the backtests charge
        costs with, and it is measured on the **hourly** series whenever one exists: the
        estimator is calibrated on the finest series available (it reads BTC at 30 bp on
        daily bars and 5.6 bp on hourly ones), so quoting the daily number while the chart
        shows days would overstate what trading the pair costs.
        """
        self.require(symbol, timeframe)
        bars = self.bars(symbol, timeframe)
        closes = [bar.close for bar in bars if bar.close > 0]
        if len(closes) < 2:
            raise ValueError(f"{symbol} has too few bars to say anything about")
        lookback = change_bars(timeframe)["1"]
        recent = closes[-lookback - 1:]
        returns = [
            abs(recent[i] / recent[i - 1] - 1.0) for i in range(1, len(recent)) if recent[i - 1] > 0
        ]
        tail = [bar for bar in bars[-(window + 1):]]
        tail_returns = [
            abs(tail[i].close / tail[i - 1].close - 1.0)
            for i in range(1, len(tail))
            if tail[i - 1].close > 0
        ]
        source = "1h" if (symbol, "1h") in set(self.series()) else timeframe
        spread_bars = bars if source == timeframe else self.bars(symbol, source)
        spread = spreads.corwin_schultz(spread_bars, window=720)
        row = self._row(symbol, timeframe) or {}
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": len(bars),
            "first": bars[0].time,
            "last": bars[-1].time,
            "years": round((bars[-1].time - bars[0].time) / (365 * 86400), 2),
            "close": closes[-1],
            "change": (closes[-1] / recent[0] - 1.0) if len(recent) > 1 else None,
            "change_bars": lookback,
            "median_abs_return": statistics.median(tail_returns) if tail_returns else None,
            "median_return_bars": len(tail_returns),
            "spread_per_side": spread,
            "spread_source": source,
            "spread_bars": 720,
            "turnover": row.get("turnover"),
            "stale_days": round((row["last_time"] - bars[-1].time) / 86400, 1) if row.get("last_time") else None,
        }

    def _row(self, symbol: str, timeframe: str) -> dict | None:
        """Read only the newest partition: the tail is all the sidebar needs.

        Reading the whole series for a thousand symbols takes minutes; the last file
        holds the recent bars, and the change/turnover columns are medians over what
        is in it. The row says how many bars it saw (`recent`), so nothing pretends
        the median is over the whole history.
        """
        import pyarrow.parquet as pq

        files = data.series_files(self.data_dir, symbol, timeframe)
        if not files:
            return None
        # The newest partition is usually enough, but a daily series' last file holds about
        # a year and the `1y` window needs one bar more than that. When the change windows
        # reach further back, the *previous* partition is read for `time` and `close` only:
        # the change needs those two columns, and the turnover median is deliberately of the
        # newest partition, because "how much does this trade now" is the question it answers.
        wanted = ["time", "close", "turnover", "volume"]
        newest = pq.read_table(files[-1], columns=wanted).to_pydict()
        older_closes: list[float] = []
        needed = max(change_bars(timeframe).values()) + 1
        if len(newest["close"]) < needed and len(files) > 1:
            try:
                older_closes = pq.read_table(files[-2], columns=["close"]).to_pydict()["close"]
            except (OSError, ValueError):
                older_closes = []
        columns = newest
        closes = [value for value in older_closes + columns["close"] if value and value > 0]
        if not closes:
            return None
        turnovers = [value for value in columns["turnover"] if value and value > 0]
        windows = change_bars(timeframe)
        changes = {label: _change(closes, bars) for label, bars in windows.items()}
        return {
            "symbol": symbol,
            "last": closes[-1],
            #: `change` is the one-bar move: the column follows the timeframe by default.
            "change": changes.get("1"),
            "changes": changes,
            "turnover": statistics.median(turnovers) if turnovers else 0.0,
            "recent": len(closes),
            "last_time": max(columns["time"]) if columns["time"] else None,
        }


def bars_payload(bars: list[data.Bar], *, bars_wanted: int, end: int | None = None) -> dict:
    """The bars a request asked for, newest last, as parallel arrays.

    Parallel arrays instead of objects: the same data costs roughly a third of the
    bytes, and the page indexes them by position anyway.
    """
    window = bars
    if end is not None:
        window = [bar for bar in window if bar.time <= end]
    if bars_wanted < len(window):
        window = window[-bars_wanted:]
    return {
        "symbol": None,  # filled by the caller, which knows it
        "count": len(window),
        "first": window[0].time if window else None,
        "last": window[-1].time if window else None,
        "more": bool(window) and window[0].time > (bars[0].time if bars else 0),
        "t": [bar.time for bar in window],
        "o": [bar.open for bar in window],
        "h": [bar.high for bar in window],
        "l": [bar.low for bar in window],
        "c": [bar.close for bar in window],
        "v": [bar.volume for bar in window],
    }


class Handler(BaseHTTPRequestHandler):
    """Four routes and a JSON error body. Nothing is served from the filesystem."""

    archive: Archive  # set on the subclass the server builds
    #: Literal overrides, used by tests and by `make_server(page=...)`.
    page: str = ""
    canvas_page: str = ""
    vendor_script: str = ""
    #: Live files, when the server was built from disk rather than from strings.
    page_asset: Asset | None = None
    canvas_asset: Asset | None = None
    vendor_asset: Asset | None = None
    static_payload: dict | None = None

    def current_page(self) -> str:
        return self.page_asset.text() if self.page_asset else self.page

    def current_canvas(self) -> str:
        return self.canvas_asset.text() if self.canvas_asset else self.canvas_page

    def current_vendor(self) -> str:
        return self.vendor_asset.text() if self.vendor_asset else self.vendor_script

    protocol_version = "HTTP/1.1"
    server_version = "kcs-dashboard"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # A hand-built server (a test, an embedder) has no `verbose` flag; silence by default.
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                self._html(self.current_page())
            elif parsed.path in ("/canvas", "/canvas.html"):
                self._html(self.current_canvas())
            elif parsed.path == "/vendor/lightweight-charts.js":
                self._script(self.current_vendor())
            elif parsed.path == "/api/series":
                self._json({"timeframes": list(TIMEFRAMES), "default": "1d"})
            elif parsed.path == "/api/index":
                timeframe = self._timeframe(query)
                self._json({
                    "timeframe": timeframe,
                    "change_bars": change_bars(timeframe),
                    "rows": self.archive.index(timeframe),
                })
            elif parsed.path == "/api/bars":
                self._bars(query)
            elif parsed.path == "/api/stats":
                self._stats_route(query)
            elif parsed.path == "/api/export":
                self._export(query)
            else:
                self._json({"error": f"no route {parsed.path}"}, status=404)
        except KeyError as error:
            self._json({"error": str(error).strip("'")}, status=400)
        except ValueError as error:
            self._json({"error": str(error)}, status=400)
        except BrokenPipeError:  # the page was closed mid-download
            pass

    # --- routes ------------------------------------------------------------
    def _bars(self, query: dict[str, list[str]]) -> None:
        symbol = self._symbol(query)
        timeframe = self._timeframe(query)
        wanted = min(int(query.get("bars", [DEFAULT_BARS])[0]), MAX_BARS)
        if wanted < 1:
            raise ValueError("bars must be at least 1")
        end = query.get("end")
        bars = self.archive.bars(symbol, timeframe)
        payload = bars_payload(bars, bars_wanted=wanted, end=int(end[0]) if end else None)
        payload["symbol"] = symbol
        payload["timeframe"] = timeframe
        payload["series_bars"] = len(bars)
        self._json(payload)

    def _stats_route(self, query: dict[str, list[str]]) -> None:
        symbol = self._symbol(query)
        timeframe = self._timeframe(query)
        key = (symbol, timeframe)
        with self.archive._lock:
            hit = self.archive._stats.get(key)
            if hit is not None and hit.fresh(self.archive.cache_seconds):
                self._json(hit.value)  # type: ignore[arg-type]
                return
        payload = self.archive.stats(symbol, timeframe)
        with self.archive._lock:
            self.archive._stats[key] = Cached(payload, time.monotonic())
        self._json(payload)

    def _export(self, query: dict[str, list[str]]) -> None:
        """The same page with its data embedded, for keeping as one file."""
        symbol = self._symbol(query)
        timeframe = self._timeframe(query)
        wanted = min(int(query.get("bars", [DEFAULT_BARS])[0]), MAX_BARS)
        bars = self.archive.bars(symbol, timeframe)
        payload = bars_payload(bars, bars_wanted=wanted)
        payload["symbol"] = symbol
        payload["timeframe"] = timeframe
        embedded = {
            "static": True,
            "index": {"timeframe": timeframe, "rows": self.archive.index(timeframe)},
            "bars": {f"{symbol}|{timeframe}": payload},
        }
        page = embed(self.current_page(), embedded, vendor=self.current_vendor())
        filename = f"{symbol}_{timeframe}_dashboard.html".replace("/", "-")
        self._html(page, download_as=filename)

    # --- plumbing ----------------------------------------------------------
    def _symbol(self, query: dict[str, list[str]]) -> str:
        if "symbol" not in query:
            raise ValueError("symbol is required")
        return query["symbol"][0].upper()

    def _timeframe(self, query: dict[str, list[str]]) -> str:
        timeframe = query.get("timeframe", ["1d"])[0]
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        return timeframe

    def _body(self, payload: bytes, content_type: str, *, download_as: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if download_as:
            self.send_header("Content-Disposition", f'attachment; filename="{download_as}"')
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, page: str, *, download_as: str | None = None) -> None:
        self._body(page.encode("utf-8"), "text/html; charset=utf-8", download_as=download_as)

    def _script(self, source: str) -> None:
        # Versioned by the filename in the vendor README, so caching it is safe.
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(source.encode("utf-8"))))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(source.encode("utf-8"))

    def _json(self, payload: dict, *, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        if status != 200:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._body(body, "application/json")


def embed(page: str, payload: dict, *, vendor: str) -> str:
    """A page that carries its own renderer and its own data: no server, no network.

    Both replacements are markers in the page rather than string surgery on a live copy,
    so exporting cannot half-apply: either both are found or the export fails loudly.
    """
    if VENDOR_TAG not in page:
        raise ValueError(f"the page has no {VENDOR_TAG!r} marker to inline the library into")
    if "<script>window.__DATA__ = null;</script>" not in page:
        raise ValueError("the page has no `window.__DATA__` marker to inline the bars into")
    return page.replace(VENDOR_TAG, f"<script>{vendor}</script>").replace(
        "<script>window.__DATA__ = null;</script>",
        f"<script>window.__DATA__ = {json.dumps(payload, separators=(',', ':'))};</script>",
    )


def make_server(
    archive: Archive,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    page: str | None = None,
    verbose: bool = False,
) -> ThreadingHTTPServer:
    """A ready-to-serve HTTP server, with the archive and the page already bound.

    `serve()` and the tests both build the server this way, so the tests exercise the
    routes the CLI actually runs rather than a re-implementation of them. Port 0 asks
    the operating system for a free one, which is what the tests use.
    """
    if page is None:
        # Read from disk on every request: editing the page (or pulling a new one) shows up
        # on the next reload, with no restart.
        bound = {
            "archive": archive,
            "page_asset": Asset(PAGE),
            "canvas_asset": Asset(CANVAS_PAGE),
            "vendor_asset": Asset(VENDOR),
        }
    else:
        bound = {
            "archive": archive,
            "page": page,
            "canvas_page": CANVAS_PAGE.read_text(),
            "vendor_script": vendored_library(),
        }
    handler = type("BoundHandler", (Handler,), bound)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def serve(args: argparse.Namespace) -> int:
    """Run until interrupted. Returns the exit code."""
    archive = Archive(args.data_dir)
    server = make_server(archive, host=args.host, port=args.port, verbose=args.verbose)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"kcs-dashboard reading {archive.data_dir}")
    print(f"  {len(archive.symbols('1d'))} symbols with a 1d series; index builds on first request")
    print(f"  {url}")
    print("  Ctrl-C to stop", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


def export(args: argparse.Namespace) -> int:
    """Write one self-contained HTML file for a symbol and timeframe."""
    archive = Archive(args.data_dir)
    symbol, _, timeframe = args.export.partition(",")
    symbol, timeframe = symbol.strip().upper(), (timeframe.strip() or "1d")
    archive.require(symbol, timeframe)
    bars = archive.bars(symbol, timeframe)
    payload = bars_payload(bars, bars_wanted=args.bars)
    payload["symbol"] = symbol
    payload["timeframe"] = timeframe
    embedded = {
        "static": True,
        "index": {"timeframe": timeframe, "rows": archive.index(timeframe)},
        "bars": {f"{symbol}|{timeframe}": payload},
    }
    page = embed(PAGE.read_text(), embedded, vendor=vendored_library())
    out = Path(args.out) if args.out else Path(f"{symbol}_{timeframe}_dashboard.html")
    out.write_text(page)
    print(
        f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {payload['count']:,} bars of {symbol} {timeframe})",
        flush=True,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kcs-dashboard",
        description="Interactive candlestick and volume dashboard over the kline archive",
        epilog="Serve it and open the printed URL, or --export a single file to keep.",
    )
    parser.add_argument("--data-dir", default=str(data.DEFAULT_DATA_DIR), help="archive root")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: %(default)s)")
    parser.add_argument("--bars", type=int, default=DEFAULT_BARS, help="bars per request (default: %(default)s)")
    parser.add_argument("--open", action="store_true", help="open the browser at the printed URL")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    parser.add_argument(
        "--export",
        metavar="SYMBOL,TIMEFRAME",
        help="write one self-contained HTML file instead of serving (e.g. BTC-USDT,1h)",
    )
    parser.add_argument("--out", help="where --export writes (default: SYMBOL_TIMEFRAME_dashboard.html)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return export(args) if args.export else serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
