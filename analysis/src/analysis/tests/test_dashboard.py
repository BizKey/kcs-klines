"""The dashboard: the payloads it builds and the routes it serves them on."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from http.server import ThreadingHTTPServer

from .. import dashboard, data
from .conftest import START, make_bars, write_archive

DAY = 86400


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    """Three daily series with different turnover, plus an hourly one."""
    root = tmp_path / "spot"
    for symbol, series, turnover in (
        ("BIG-USDT", [100.0 + i for i in range(120)], 5_000_000.0),
        ("MID-USDT", [50.0 + 0.5 * i for i in range(120)], 500_000.0),
        ("SMALL-USDT", [10.0 - 0.01 * i for i in range(120)], 5_000.0),
    ):
        write_archive(
            root, symbol, "1d", make_bars(series, start=START, step=DAY),
            turnovers=[turnover] * len(series),
        )
    write_archive(root, "BIG-USDT", "1h", make_bars([100.0] * 48, start=START, step=3600))
    return root


@pytest.fixture
def server(archive_dir: Path):
    """A real server on a free port, torn down afterwards."""
    instance = dashboard.make_server(dashboard.Archive(archive_dir), page="<html>page</html>")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{instance.server_address[1]}"
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


def fetch(url: str) -> tuple[int, dict | str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode()
            if response.headers.get_content_type() == "application/json":
                return response.status, json.loads(body)
            return response.status, body
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


# --- the payloads -----------------------------------------------------------
def test_bars_payload_keeps_the_newest_bars_and_says_what_is_missing():
    bars = make_bars([100.0 + i for i in range(50)], start=START, step=DAY)
    payload = dashboard.bars_payload(bars, bars_wanted=10)
    assert payload["count"] == 10
    assert payload["last"] == bars[-1].time
    assert payload["first"] == bars[-10].time
    assert payload["more"] is True, "there are older bars the page has not loaded"
    assert payload["c"] == [bar.close for bar in bars[-10:]]
    assert len(payload["t"]) == len(payload["o"]) == len(payload["h"]) == len(payload["l"]) == 10
    assert len(payload["v"]) == 10


def test_bars_payload_can_walk_backwards_from_a_timestamp():
    bars = make_bars([100.0 + i for i in range(50)], start=START, step=DAY)
    cutoff = bars[20].time
    payload = dashboard.bars_payload(bars, bars_wanted=5, end=cutoff)
    assert payload["last"] == cutoff, "the window ends where the caller asked"
    assert payload["count"] == 5


def test_bars_payload_of_an_empty_series_does_not_pretend():
    payload = dashboard.bars_payload([], bars_wanted=10)
    assert payload["count"] == 0 and payload["first"] is None and payload["more"] is False


def test_the_index_is_one_row_per_symbol_sorted_by_turnover(archive_dir: Path):
    rows = dashboard.Archive(archive_dir).index("1d")
    assert [row["symbol"] for row in rows] == ["BIG-USDT", "MID-USDT", "SMALL-USDT"]
    assert rows[0]["turnover"] == pytest.approx(5_000_000.0)
    assert rows[0]["last"] == pytest.approx(219.0)
    assert rows[0]["recent"] == 120
    # 30 bars back is `closes[-31]`, not `closes[-30]`: the change compares the last
    # bar with the bar thirty steps before it.
    # `change` is one bar of the timeframe — the column follows the timeframe — and the
    # other windows are carried alongside it, labelled.
    assert rows[0]["change"] == pytest.approx(219.0 / 218.0 - 1.0)
    assert rows[0]["changes"]["30d"] == pytest.approx(219.0 / 189.0 - 1.0)
    assert rows[0]["changes"]["1"] == rows[0]["change"]


def test_the_change_windows_are_bars_of_the_timeframe():
    """`1` is one bar; the rest are fixed stretches, spelled in bars of that timeframe."""
    assert dashboard.change_bars("1h") == {"1": 1, "24h": 24, "7d": 168, "30d": 720, "1y": 8760}
    assert dashboard.change_bars("1d") == {"1": 1, "24h": 1, "7d": 7, "30d": 30, "1y": 365}
    # A weekly bar is longer than a day, so the 24h window is one bar, not zero.
    assert dashboard.change_bars("1w")["24h"] == 1
    assert dashboard.change_bars("1mon") == {"1": 1, "24h": 1, "7d": 1, "30d": 1, "1y": 12}


def test_a_window_longer_than_the_series_is_none_not_a_shorter_window():
    assert dashboard._change([100.0, 110.0], 5) is None
    assert dashboard._change([100.0, 110.0], 1) == pytest.approx(0.1)
    assert dashboard._change([100.0, 110.0], 0) is None


def test_the_index_route_carries_the_bar_counts(server: str):
    status, body = fetch(server + "/api/index?timeframe=1d")
    assert status == 200
    assert body["change_bars"] == {"1": 1, "24h": 1, "7d": 7, "30d": 30, "1y": 365}


def test_a_symbol_the_archive_does_not_have_is_refused(archive_dir: Path):
    """The symbol is looked up in the listing, never joined into a path."""
    archive = dashboard.Archive(archive_dir)
    with pytest.raises(KeyError, match="no 1d series"):
        archive.bars("../../etc/passwd", "1d")
    with pytest.raises(KeyError, match="no 1d series"):
        archive.bars("NOPE-USDT", "1d")
    with pytest.raises(KeyError, match="unknown timeframe"):
        archive.bars("BIG-USDT", "5m")


def test_bars_are_cached_and_reloaded_when_a_file_changes(archive_dir: Path):
    archive = dashboard.Archive(archive_dir, cache_seconds=60.0)
    first = archive.bars("BIG-USDT", "1d")
    assert archive.bars("BIG-USDT", "1d") is first, "a second read is served from memory"
    # Rewrite the series with one more bar, as a collector run would.
    longer = make_bars([100.0 + i for i in range(121)], start=START, step=DAY)
    write_archive(archive_dir, "BIG-USDT", "1d", longer)
    assert len(archive.bars("BIG-USDT", "1d")) == 121, "a changed file invalidates the cache"


# --- the routes -------------------------------------------------------------
def test_the_page_is_served_at_the_root(server: str):
    status, body = fetch(server + "/")
    assert status == 200 and body == "<html>page</html>"


def test_the_series_route_lists_the_timeframes(server: str):
    status, body = fetch(server + "/api/series")
    assert status == 200
    assert body["timeframes"] == list(dashboard.TIMEFRAMES)
    assert body["default"] in body["timeframes"]


def test_the_index_route_answers_the_sidebar(server: str):
    status, body = fetch(server + "/api/index?timeframe=1d")
    assert status == 200
    assert body["timeframe"] == "1d"
    assert [row["symbol"] for row in body["rows"]] == ["BIG-USDT", "MID-USDT", "SMALL-USDT"]


def test_the_bars_route_returns_parallel_arrays(server: str):
    status, body = fetch(server + "/api/bars?symbol=BIG-USDT&timeframe=1d&bars=7")
    assert status == 200
    assert body["symbol"] == "BIG-USDT" and body["timeframe"] == "1d"
    assert body["count"] == 7 and body["series_bars"] == 120
    assert body["t"] == sorted(body["t"]), "oldest first, so the page can append"
    # The window's first bar has close 213 and open 212: `make_bars` opens each bar
    # where the previous one closed.
    assert body["c"][0] == pytest.approx(213.0)
    assert body["o"][0] == pytest.approx(212.0)


def test_the_bars_route_can_walk_backwards(server: str):
    status, first = fetch(server + "/api/bars?symbol=BIG-USDT&timeframe=1d&bars=10")
    assert status == 200
    status, older = fetch(
        server + f"/api/bars?symbol=BIG-USDT&timeframe=1d&bars=10&end={first['first'] - 1}"
    )
    assert status == 200
    assert older["last"] < first["first"], "the second page ends before the first begins"
    assert not set(older["t"]) & set(first["t"]), "and the pages do not overlap"


def test_a_request_for_an_unknown_symbol_is_a_400_not_a_traceback(server: str):
    status, body = fetch(server + "/api/bars?symbol=NOPE-USDT&timeframe=1d")
    assert status == 400 and "no 1d series" in body["error"]


def test_a_request_without_a_symbol_says_so(server: str):
    status, body = fetch(server + "/api/bars?timeframe=1d")
    assert status == 400 and body["error"] == "symbol is required"


def test_an_unknown_timeframe_is_refused(server: str):
    status, body = fetch(server + "/api/bars?symbol=BIG-USDT&timeframe=5m")
    assert status == 400 and "timeframe must be one of" in body["error"]


def test_an_unknown_route_is_a_404(server: str):
    status, body = fetch(server + "/api/nope")
    assert status == 404 and "no route" in body["error"]


def test_the_export_route_embeds_the_data_in_the_page(archive_dir: Path):
    """`--export` writes one file with the bars inlined and no fetch left to make."""
    # An exporting server needs a page with both markers: the data one and the one the
    # library is inlined into.
    stub = (
        "<html>" + dashboard.VENDOR_TAG + "<script>window.__DATA__ = null;</script></html>"
    )
    instance = dashboard.make_server(dashboard.Archive(archive_dir), page=stub)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{instance.server_address[1]}/api/export?symbol=BIG-USDT&timeframe=1d&bars=20"
        with urllib.request.urlopen(url, timeout=10) as response:
            page = response.read().decode()
            assert "attachment" in response.headers.get("Content-Disposition", "")
        assert "window.__DATA__ = null" not in page
        assert '"BIG-USDT|1d"' in page and '"count":20' in page
        # and its own renderer: a saved chart must work with no server and no network
        assert dashboard.VENDOR_TAG not in page
        assert "window.LightweightCharts" in page
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_the_export_writes_a_file_that_can_be_opened_anywhere(archive_dir: Path, tmp_path: Path):
    out = tmp_path / "one.html"
    code = dashboard.main([
        "--data-dir", str(archive_dir), "--export", "BIG-USDT,1d", "--out", str(out), "--bars", "20",
    ])
    assert code == 0
    page = out.read_text()
    assert page.startswith("<!DOCTYPE html>")
    assert '"count":20' in page and '"BIG-USDT|1d"' in page
    assert dashboard.VENDOR_TAG not in page, "the exported file inlines the library"
    assert data.iso(START) in page or "window.__DATA__ = null" not in page


def test_exporting_a_symbol_that_is_not_there_fails_loudly(archive_dir: Path, tmp_path: Path):
    with pytest.raises(KeyError, match="no 1d series"):
        dashboard.main([
            "--data-dir", str(archive_dir), "--export", "NOPE-USDT,1d", "--out", str(tmp_path / "x.html"),
        ])


def test_an_asset_is_re_read_when_the_file_changes(tmp_path: Path):
    """Editing the page must not need a server restart (the first version cached it forever)."""
    import os

    path = tmp_path / "page.html"
    path.write_text("one")
    asset = dashboard.Asset(path)
    assert asset.text() == "one"
    assert asset.text() == "one"                       # cached while the file is unchanged
    path.write_text("two")
    os.utime(path, (path.stat().st_atime + 10, path.stat().st_mtime + 10))
    assert asset.text() == "two"


def test_the_route_serves_the_current_file_without_a_restart(tmp_path: Path, archive_dir: Path):
    """The same thing through the route the browser actually hits."""
    import os
    import threading

    page = tmp_path / "page.html"
    page.write_text("<html>first build</html>")
    handler = type("Bound", (dashboard.Handler,), {
        "archive": dashboard.Archive(archive_dir),
        "page_asset": dashboard.Asset(page),
        "canvas_asset": dashboard.Asset(dashboard.CANVAS_PAGE),
        "vendor_asset": dashboard.Asset(dashboard.VENDOR),
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        status, body = fetch(base + "/")
        assert (status, body) == (200, "<html>first build</html>")
        page.write_text("<html>second build</html>")
        os.utime(page, (page.stat().st_atime + 10, page.stat().st_mtime + 10))
        status, body = fetch(base + "/")
        assert (status, body) == (200, "<html>second build</html>"), "the server served a stale page"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_page_ships_with_the_package():
    """The HTML is read from beside the module, so a wheel has to carry it."""
    assert dashboard.PAGE.is_file()
    page = dashboard.PAGE.read_text()
    assert "window.__DATA__ = null" in page, "the export marker the server replaces"
    assert dashboard.VENDOR_TAG in page, "the marker the exporter inlines the library into"
    assert "createChart" in page and "fetch(" in page


def test_the_canvas_fallback_page_ships_too():
    """The hand-written renderer is kept: it is the page the headless check can run."""
    assert dashboard.CANVAS_PAGE.is_file()
    page = dashboard.CANVAS_PAGE.read_text()
    assert "<canvas" in page and "window.__DATA__ = null" in page


def test_the_vendored_library_is_present_and_carries_its_license():
    assert dashboard.VENDOR.is_file()
    source = dashboard.vendored_library()
    assert len(source) > 100_000, "a truncated bundle would fail in the browser, not here"
    assert "Apache License 2.0" in source[:400], "the license banner has to travel with it"
    assert "window.LightweightCharts" in source


def test_the_page_only_uses_library_apis_the_bundle_exports():
    """The one runtime failure this environment can catch without a browser.

    The page talks to the library through `LWC.<name>`. If a name is misspelled, or the
    vendored build is older than the code, the chart simply does not appear — and nothing
    else here would notice. The bundle ends with its export map, so that region is the
    contract to check against.
    """
    page = dashboard.PAGE.read_text()
    used = set(re.findall(r"\bLWC\.([A-Za-z_][A-Za-z0-9_]*)", page))
    assert used, "the page should be using the library"
    exports = dashboard.vendored_library()[-6000:]
    missing = sorted(name for name in used if not re.search(rf"\b{name}\s*[:(]", exports))
    assert not missing, f"the page uses {missing}, which this bundle does not export"


#: The library methods this page drives. They cannot be executed here — there is no
#: browser and no DOM — so the contract is checked by name instead, against the bundle.
LIBRARY_METHODS = (
    "addSeries", "applyOptions", "setData", "takeScreenshot", "panes", "paneIndex",
    "setPreserveEmptyPane", "setHeight", "priceScale", "timeScale", "setVisibleRange",
    "setVisibleLogicalRange", "getVisibleLogicalRange", "fitContent",
    "subscribeCrosshairMove", "subscribeVisibleLogicalRangeChange",
)


def test_every_library_method_the_page_calls_exists_in_the_bundle():
    """A missing method is a blank chart in the browser and nothing here would notice."""
    page = dashboard.PAGE.read_text()
    bundle = dashboard.vendored_library()
    used = [name for name in LIBRARY_METHODS if f".{name}(" in page]
    assert len(used) >= 10, "the page should be driving the library, not a couple of calls"
    missing = [name for name in used if not re.search(rf"\b{name}\s*[:(]", bundle)]
    assert not missing, f"the page calls {missing}, which this bundle does not provide"


def test_the_stats_route_describes_a_pair(server: str):
    status, body = fetch(server + "/api/stats?symbol=BIG-USDT&timeframe=1d")
    assert status == 200
    assert body["symbol"] == "BIG-USDT" and body["timeframe"] == "1d"
    assert body["bars"] == 120 and body["years"] >= 0
    assert body["close"] == pytest.approx(219.0)
    assert body["change"] == pytest.approx(219.0 / 218.0 - 1.0)
    assert body["median_abs_return"] is not None and body["change_bars"] == 1
    # The spread needs high/low bars; three-close fixtures still have some.
    assert body["spread_source"] in ("1d", "1h")
    assert body["turnover"] == pytest.approx(5_000_000.0)


def test_the_stats_route_refuses_an_unknown_symbol(server: str):
    status, body = fetch(server + "/api/stats?symbol=NOPE-USDT&timeframe=1d")
    assert status == 400 and "no 1d series" in body["error"]


def test_the_stats_route_answers_from_its_cache(archive_dir: Path, monkeypatch):
    """A second request inside the cache window must not rebuild the numbers."""
    archive = dashboard.Archive(archive_dir, cache_seconds=60.0)
    calls = []
    original = dashboard.Archive.stats

    def counted(self, symbol, timeframe, **kwargs):
        calls.append((symbol, timeframe))
        return original(self, symbol, timeframe, **kwargs)

    monkeypatch.setattr(dashboard.Archive, "stats", counted)
    instance = dashboard.make_server(archive)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{instance.server_address[1]}"
        for _ in range(3):
            status, body = fetch(base + "/api/stats?symbol=BIG-USDT&timeframe=1d")
            assert status == 200 and body["symbol"] == "BIG-USDT"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)
    assert len(calls) == 1, f"the archive was asked to rebuild {len(calls)} times"


def test_the_vendor_route_serves_the_library(server: str):
    with urllib.request.urlopen(server + "/vendor/lightweight-charts.js", timeout=10) as response:
        body = response.read().decode()
        assert response.status == 200
        assert response.headers.get_content_type() == "text/javascript"
        assert "LightweightCharts" in body and len(body) > 100_000


def test_the_canvas_route_serves_the_fallback(server: str):
    status, body = fetch(server + "/canvas")
    assert status == 200
    assert "<canvas" in body
