#!/usr/bin/env node
/**
 * Run the Lightweight Charts dashboard headlessly and check what it does.
 *
 * `render_check.mjs` covers the hand-written canvas page, because node has a canvas
 * stand-in and a browser does not exist in this environment. The main page cannot be
 * executed at all without a DOM, which is how the sidebar kept shipping bugs nobody could
 * see from here — a percentage that followed the wrong window, a sort that could not put
 * the losers on top.
 *
 * So this script fakes the three things the page needs — a DOM built from its own markup,
 * a `LightweightCharts` whose calls are recorded, and a `fetch` that serves synthetic
 * series — boots the page, and then asserts on the behaviour: the order of the sidebar,
 * what the labels say, which series receive data, which price-scale modes are applied.
 *
 *     node analysis/experiments/dashboard_check.mjs
 *
 * If a control is added to the page's markup, the DOM stub picks it up from the HTML: the
 * static buttons, selects and inputs are discovered by parsing the file, so the stub does
 * not have to be edited when the page grows.
 */

import fs from "node:fs";

const PAGE = new URL("../src/analysis/dashboard.html", import.meta.url);
const html = fs.readFileSync(PAGE, "utf8");
const markup = html.slice(0, html.indexOf("<script"));
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
const pageScript = scripts[scripts.length - 1];

const errors = [];
const log = (...parts) => console.log(...parts);
const check = (condition, message) => { if (!condition) errors.push(message); };

/* ------------------------------------------------------- a DOM from the markup */
const nodes = new Map();
function element(tag, id = "") {
  const node = {
    tagName: tag.toUpperCase(), id, children: [], parent: null,
    textContent: "", value: "", title: "", className: "",
    dataset: {}, style: {}, attrs: {},
    classList: {
      _set: new Set(),
      add(name) { this._set.add(name); }, remove(name) { this._set.delete(name); },
      toggle(name, on) { on === undefined ? (this._set.has(name) ? this._set.delete(name) : this._set.add(name)) : (on ? this._set.add(name) : this._set.delete(name)); },
      contains(name) { return this._set.has(name); },
    },
    append(child) { child.parent = this; this.children.push(child); },
    appendChild(child) { this.append(child); },
    addEventListener(type, handler) { (this._listeners ||= {})[type] = handler; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 700 }),
    getContext: () => null,
    clientWidth: 1200, clientHeight: 700,
  };
  node.classList = { ...node.classList };
  node.classList._set = new Set();
  // `innerHTML = ""` is how the page clears a list before refilling it, so the stub has
  // to drop the children as well as the string: otherwise rows accumulate and every
  // order assertion sees the previous render still attached.
  let markupText = "";
  Object.defineProperty(node, "innerHTML", {
    get: () => markupText,
    set: (value) => { markupText = value; if (!value) node.children = []; },
  });
  nodes.set(id, node);
  return node;
}
const byId = (id) => {
  if (!nodes.has(id)) nodes.set(id, element("div", id));
  return nodes.get(id);
};

// Every `id="…"` in the markup exists, and every `<button>` inside a `<div id="…">` is
// placed under it, so `querySelectorAll("#tools button[data-sma]")` finds real children.
for (const match of markup.matchAll(/id="([\w-]+)"/g)) byId(match[1]);
let currentParent = null;
for (const match of markup.matchAll(/<div id="([\w-]+)"|<button([^>]*)>([^<]*)</g)) {
  if (match[1]) { currentParent = byId(match[1]); continue; }
  const attrs = match[2] || "";
  const button = element("button");
  button.textContent = match[3].trim();
  for (const attr of attrs.matchAll(/([\w-]+)="([^"]*)"/g)) {
    if (attr[1] === "id") { button.id = attr[2]; nodes.set(attr[2], button); }
    else if (attr[1].startsWith("data-")) button.dataset[attr[1].slice(5)] = attr[2];
    else button.attrs[attr[1]] = attr[2];
  }
  if (currentParent) currentParent.append(button);
}
// Selects and inputs behave like elements with a value.
for (const match of markup.matchAll(/<(select|input)[^>]*id="([\w-]+)"[^>]*>/g)) {
  const node = byId(match[2]);
  node.tagName = match[1].toUpperCase();
}
// A select's options exist so the page can read and set them.
for (const match of markup.matchAll(/<option value="([^"]*)"/g)) {
  if (!byId("sort").options) byId("sort").options = [];
  byId("sort").options.push({ value: match[1] });
}

function queryAll(selector) {
  const match = selector.match(/^#([\w-]+)\s+button(?:\[data-([\w-]+)\])?$/);
  if (!match) return [];
  const parent = byId(match[1]);
  const attribute = match[2];
  return parent.children.filter(
    (child) => child.tagName === "BUTTON" && (!attribute || child.dataset[attribute] != null),
  );
}

global.document = {
  getElementById: byId,
  createElement: (tag) => element(tag),
  querySelectorAll: queryAll,
  querySelector: (selector) => queryAll(selector)[0] || null,
};
const windowListeners = {};
global.window = {
  devicePixelRatio: 1,
  addEventListener: (type, handler) => { windowListeners[type] = handler; },
  LightweightCharts: null,          // filled in below
  __DATA__: null,
};
global.history = { replaceState() {} };
global.location = { hash: "" };
const store = new Map();
global.localStorage = {
  getItem: (key) => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
};

/* --------------------------------------------------- a library that records calls */
const calls = { series: [], applyOptions: [], modes: [], data: new Map(), visible: [] };
function seriesStub(kind, options, paneIndex) {
  const record = { kind, options: { ...options }, paneIndex: paneIndex ?? 0, visible: options.visible !== false };
  const stub = {
    kind,
    setData: (data) => { calls.data.set(record, data); return stub; },
    applyOptions: (next) => {
      Object.assign(record.options, next);
      if (next.visible !== undefined) record.visible = next.visible;
      calls.visible.push({ kind, visible: record.visible });
      return stub;
    },
    setPreserveEmptyPane: () => stub,
    paneIndex: () => record.paneIndex,
    priceScale: () => ({ applyOptions: () => ({}) }),
    _record: record,
  };
  calls.series.push(record);
  return stub;
}
const chartStub = {
  addSeries: (definition, options, paneIndex) => seriesStub(definition, options, paneIndex),
  applyOptions: () => chartStub,
  priceScale: (id) => ({
    applyOptions: (options) => { calls.modes.push({ id, ...options }); return {}; },
  }),
  timeScale: () => ({
    setVisibleRange: () => {}, setVisibleLogicalRange: () => {}, fitContent: () => {},
    getVisibleLogicalRange: () => ({ from: 0, to: 100 }),
    subscribeVisibleLogicalRangeChange: () => {},
  }),
  subscribeCrosshairMove: () => {},
  panes: () => [{ setHeight: () => {} }],
  takeScreenshot: () => ({ toDataURL: () => "data:image/png;base64," }),
  remove: () => {},
};
global.window.LightweightCharts = {
  createChart: () => chartStub,
  CandlestickSeries: "CandlestickSeries",
  BarSeries: "BarSeries",
  LineSeries: "LineSeries",
  HistogramSeries: "HistogramSeries",
  PriceScaleMode: { Normal: 0, Logarithmic: 1, Percentage: 2 },
  CrosshairMode: { Normal: 0 },
};

/* ------------------------------------------------------------- synthetic series */
// `TEL-BTC` is a cross pair: the archive holds them, and the page has to label the quote
// from the symbol rather than assuming USDT.
const DRIFTS = { "AAA-USDT": 0.004, "TEL-BTC": 0.003, "BBB-USDT": 0.002, "CCC-USDT": 0.0005,
                 "DDD-USDT": -0.001, "EEE-USDT": -0.003, "FFF-USDT": -0.005 };
const TURNOVER = { "AAA-USDT": 5e6, "BBB-USDT": 4e6, "CCC-USDT": 3e6, "DDD-USDT": 2e6,
                   "EEE-USDT": 1e6, "FFF-USDT": 5e5, "TEL-BTC": 2.5e5 };
const START = 1_507_161_600, DAY = 86400, COUNT = 400;

function series(symbol) {
  const bars = { t: [], o: [], h: [], l: [], c: [], v: [] };
  let close = 100;
  for (let i = 0; i < COUNT; i++) {
    const open = close;
    close = open * (1 + DRIFTS[symbol]);
    bars.t.push(START + i * DAY);
    bars.o.push(open); bars.h.push(Math.max(open, close) * 1.001);
    bars.l.push(Math.min(open, close) * 0.999); bars.c.push(close);
    bars.v.push(1000 + i);
  }
  return {
    symbol, timeframe: "1d", count: COUNT, first: bars.t[0], last: bars.t[COUNT - 1],
    more: false, series_bars: COUNT, ...bars,
  };
}
const SERIES = Object.fromEntries(Object.keys(DRIFTS).map((symbol) => [symbol, series(symbol)]));

function indexRow(symbol) {
  const bars = SERIES[symbol];
  // A change over `bars` bars is the last close against the close that many bars back —
  // the first version of this fixture returned a one-bar move shifted into the past, which
  // made the 30-day column look like the daily one for every window.
  const change = (back) => bars.c[COUNT - 1] / bars.c[COUNT - 1 - back] - 1;
  return {
    symbol, last: bars.c[bars.count - 1],
    change: change(1),
    changes: { "1": change(1), "24h": change(1), "7d": change(7), "30d": change(30), "1y": change(365) },
    turnover: TURNOVER[symbol], recent: bars.count, last_time: bars.last,
  };
}

global.fetch = async (url) => {
  const parsed = new URL(url, "http://localhost");
  const payload = (() => {
    if (parsed.pathname === "/api/index") {
      return { timeframe: "1d", change_bars: { "1": 1, "24h": 1, "7d": 7, "30d": 30, "1y": 365 },
               rows: Object.keys(DRIFTS).map(indexRow) };
    }
    if (parsed.pathname === "/api/bars") {
      return SERIES[parsed.searchParams.get("symbol")] || { count: 0, t: [], o: [], h: [], l: [], c: [], v: [] };
    }
    if (parsed.pathname === "/api/stats") {
      return { symbol: parsed.searchParams.get("symbol"), timeframe: "1d", bars: COUNT, first: START,
               last: START + (COUNT - 1) * DAY, years: 1.1, close: 100, change: 0.001,
               change_bars: 1, median_abs_return: 0.002, median_return_bars: 120,
               spread_per_side: 0.00056, spread_source: "1h", spread_bars: 720,
               turnover: 1e6, stale_days: 0.2 };
    }
    throw new Error("unexpected fetch " + url);
  })();
  return { ok: true, json: async () => payload };
};

/* ------------------------------------------------------------------- run it */
process.on("unhandledRejection", (error) => errors.push("unhandled rejection: " + error));
try { eval(pageScript); } catch (error) { errors.push("boot threw: " + error.message); }
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
await settle();

const cellText = (row, pattern) => (row.innerHTML.match(pattern)?.[1] || "").trim();
const symbolOrder = () => byId("list").children.map((row) => cellText(row, /class="sym">([^<]+)</));
// Defensive on purpose: `check()` records a failure without stopping, so a missing row must
// come back as a named assertion rather than as a crash three lines further down.
const quoteOf = (index) => {
  const row = byId("list").children[index];
  return row ? cellText(row, /class="sub">\s*\/([^<]+)</) : "<no such row>";
};
const percentOf = (index) => {
  const match = byId("list").children[index].innerHTML.match(/class="chg [^"]*">([^<]+)</);
  return match ? match[1] : "";
};

log("boot:", symbolOrder().join(" "), "| head:", byId("sort-label").textContent);
check(symbolOrder().length === 7, `expected 7 rows, got ${symbolOrder().length}`);
// Default order: turnover, largest first.
check(symbolOrder().join(" ") === "AAA BBB CCC DDD EEE FFF TEL", `default order is ${symbolOrder().join(" ")}`);
check(byId("sort-label").textContent.includes("largest first"), `sort label reads ${byId("sort-label").textContent}`);
// One bar of the selected timeframe: the drift, not a 30-bar move.
check(percentOf(0) === "+0.40%", `AAA one-bar change shows ${percentOf(0)}, expected +0.40%`);

/* the direction toggle puts the losers on top */
byId("sort-direction").onclick();
log("after ↓ -> ↑:", symbolOrder().join(" "));
check(symbolOrder().join(" ") === "TEL FFF EEE DDD CCC BBB AAA", `reversed order is ${symbolOrder().join(" ")}`);
check(byId("sort-label").textContent.includes("smallest first"), `sort label reads ${byId("sort-label").textContent}`);
byId("sort-direction").onclick();

/* sorting by Δ, both ways */
byId("sort").value = "change";
byId("sort").onchange();
log("by Δ, largest first:", symbolOrder().join(" "));
check(symbolOrder().join(" ") === "AAA TEL BBB CCC DDD EEE FFF", `Δ order is ${symbolOrder().join(" ")}`);
byId("sort-direction").onclick();
log("by Δ, smallest first (losers on top):", symbolOrder().join(" "));
check(symbolOrder().join(" ") === "FFF EEE DDD CCC BBB TEL AAA", `Δ reversed is ${symbolOrder().join(" ")}`);
check(byId("sort-label").textContent.startsWith("Δ one bar"), `sort label reads ${byId("sort-label").textContent}`);

/* the change window switches what the column measures */
byId("change-window").value = "30d";
byId("change-window").onchange();
// A constant drift makes the expected numbers exact: FFF falls 0.5% a bar, so its 30-day
// change is 0.995^30 - 1 = -13.96%, and its one-bar change is -0.50%. The column has to
// switch between the two, and the label has to say which is on screen.
const thirtyDay = parseFloat(percentOf(0));
const expected30 = (Math.pow(1 + DRIFTS["FFF-USDT"], 30) - 1) * 100;
log(`Δ30d for FFF: ${thirtyDay.toFixed(2)}% (expected ${expected30.toFixed(2)}%) | head: ${byId("change-label").textContent}`);
check(Math.abs(thirtyDay - expected30) < 0.05, `the 30-day column shows ${thirtyDay}%, expected ${expected30.toFixed(2)}%`);
check(byId("change-label").textContent.includes("30d"), `window label reads ${byId("change-label").textContent}`);
check(percentOf(0) !== "-0.50%", "the 30-day column is still showing the one-bar move");
byId("change-window").value = "1";
byId("change-window").onchange();

/* the turnover floor hides rows */
byId("min-turnover").value = "2000000";
byId("min-turnover").oninput();
log("min turnover 2M:", symbolOrder().join(" "));
check(symbolOrder().length === 4, `expected 4 rows above 2M, got ${symbolOrder().length}`);

byId("min-turnover").value = "";
byId("min-turnover").oninput();

/* the quote label comes from the symbol, not from an assumption: the archive holds cross
   pairs like TEL-BTC, which the first version labelled "TEL-BTC /USDT" */
const crossIndex = symbolOrder().indexOf("TEL");
const crossRow = crossIndex >= 0 ? byId("list").children[crossIndex] : null;
check(crossIndex >= 0, `the cross pair is not listed as TEL (order: ${symbolOrder().join(" ")})`);
check(quoteOf(crossIndex) === "BTC", `TEL-BTC is labelled with the quote "${quoteOf(crossIndex)}"`);
check(Boolean(crossRow) && !crossRow.innerHTML.includes("/USDT"), "a cross pair is labelled /USDT");
check(quoteOf(symbolOrder().indexOf("AAA")) === "USDT", "a USDT pair lost its quote label");

/* search understands the quote, the way the list writes it */
const searchRows = (text) => {
  byId("search").value = text;
  byId("search").oninput();
  return symbolOrder();
};
// The list keeps whatever order the sort controls asked for, so these compare sets.
const same = (actual, expected) =>
  [...actual].sort().join(" ") === [...expected].sort().join(" ");
log("/usdt:", searchRows("/usdt").join(" "));
check(same(searchRows("/usdt"), ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]),
      `"/usdt" returned ${searchRows("/usdt").join(" ")}`);
check(!searchRows("/usdt").includes("TEL"), `"/usdt" returned the BTC pair: ${searchRows("/usdt").join(" ")}`);
check(byId("status").textContent.startsWith('6 of 7 symbols match "/usdt"'),
      `status reads "${byId("status").textContent}"`);
log("/btc:", searchRows("/btc").join(" "));
check(same(searchRows("/btc"), ["TEL"]), `"/btc" returned ${searchRows("/btc").join(" ")}`);
for (const text of ["tel/btc", "tel-btc", "TEL", "tel", "TEL/USDT"]) {
  // `TEL/USDT` is not a pair here, but a search must not crash or match everything.
  const found = searchRows(text);
  log(`  "${text}":`, found.join(" ") || "(nothing)");
  if (text !== "TEL/USDT") check(same(found, ["TEL"]), `"${text}" returned ${found.join(" ")}`);
}
log("usdc btc (two terms):", searchRows("usdc btc").join(" "));
check(searchRows("usdc btc").length === 0, `"usdc btc" should match nothing here, got ${searchRows("usdc btc").join(" ")}`);
searchRows("");
check(symbolOrder().length === 7, "clearing the search did not restore the whole list");

/* the scale buttons reach the price scale */
byId("scales").children.find((button) => button.dataset.scale === "percent").onclick();
const percentage = calls.modes.filter((call) => call.mode === 2).length;
log("price-scale mode calls:", calls.modes.filter((c) => c.mode !== undefined).map((c) => c.mode).join(","));
check(percentage >= 1, "the % button did not set the percentage price scale");

/* indicator toggles hand data to their own panes */
const before = calls.data.size;
byId("indicators").children.find((button) => button.dataset.indicator === "macd").onclick();
const macdSeries = calls.series.filter((record) => record.paneIndex === 2);
log("MACD series in pane 2:", macdSeries.length, "| datasets written:", calls.data.size - before);
check(macdSeries.length === 3, `expected three MACD series, found ${macdSeries.length}`);
check(calls.data.size > before, "toggling MACD wrote no data");
check(macdSeries.some((record) => record.visible), "no MACD series was made visible");

/* the stats line shows the archive's own numbers */
check(byId("stats").innerHTML.includes("spread"), `stats line reads "${byId("stats").innerHTML}"`);
check(byId("stats").innerHTML.includes("5.60 bp"), `stats line reads "${byId("stats").innerHTML}"`);

/* live mode arms a timer without throwing */
byId("live").onclick();
check(byId("live").classList.contains("on"), "the live button did not turn on");
byId("live").onclick();
check(!byId("live").classList.contains("on"), "the live button did not turn off");

/* keyboard shortcuts: ↑/↓ walk the list, s cycles the style */
const styleBefore = byId("styles").children.find((b) => b.classList.contains("on"))?.dataset.style;
windowListeners.keydown({ key: "s", target: { tagName: "BODY" } });
const styleAfter = byId("styles").children.find((b) => b.classList.contains("on"))?.dataset.style;
log("style:", styleBefore, "->", styleAfter);
check(styleBefore !== styleAfter, "`s` did not change the display style");

log("\ntotals:", JSON.stringify({
  series: calls.series.length,
  datasets: calls.data.size,
  priceScaleCalls: calls.modes.length,
}));
if (errors.length) {
  console.log("FAILED:\n - " + errors.join("\n - "));
  process.exit(1);
}
console.log("no exceptions; the page behaved as documented");
