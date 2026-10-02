# vendor

Third-party code that ships with the dashboard, kept here rather than fetched from a CDN:
the archive is local, and so is the dashboard, so opening a chart must not need the
network. The Python tests check that the file is present, carries its licence banner, and
exports every API the page uses.

## `lightweight-charts.standalone.production.js`

| | |
|---|---|
| what | [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/) — the charting library behind the dashboard |
| version | **5.2.1** |
| licence | Apache-2.0 (banner kept at the top of the file; upstream text: <https://www.apache.org/licenses/LICENSE-2.0>) |
| source | <https://unpkg.com/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js> |
| size | ~194 KB, minified, no dependencies, no separate CSS |

The page loads it from `/vendor/lightweight-charts.js` (see `dashboard.py`), and
`kcs-dashboard --export` inlines it into the saved HTML so a single file works offline.

To update:

```bash
curl -sL -o analysis/src/analysis/vendor/lightweight-charts.standalone.production.js \
  https://unpkg.com/lightweight-charts@<version>/dist/lightweight-charts.standalone.production.js
uv run pytest analysis/src/analysis/tests/test_dashboard.py   # license, size, API contract
```

The API-contract test is the one that matters: it reads the export map at the end of the
bundle and fails if the page uses a name this build does not export, which is the failure
mode that would otherwise show up as a blank chart in the browser.
