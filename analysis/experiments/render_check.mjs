#!/usr/bin/env node
/**
 * Render the dashboard page without a browser.
 *
 * The dashboard's fallback page is drawn on a canvas, so a typo in the drawing code is
 * invisible to the Python test suite and there was no browser in the environment this was
 * written in. (The main page is TradingView's Lightweight Charts; its contract with the
 * vendored bundle is checked in `tests/test_dashboard.py` instead.)
 * This script stubs just enough of the DOM, feeds the page real bars, drives every
 * interaction the page wires up (range buttons, zoom, pan, crosshair, SMA overlays,
 * the log scale, reset) and fails if anything throws or if the starting window
 * collapses back to a handful of bars.
 *
 *     uv run python - <<'EOF'
 *     import json, sys; sys.path.insert(0, "analysis/src")
 *     from analysis import dashboard, data
 *     archive = dashboard.Archive(data.DEFAULT_DATA_DIR)
 *     bars = archive.bars("BTC-USDT", "1d")
 *     payload = dashboard.bars_payload(bars, bars_wanted=5000)
 *     payload.update(symbol="BTC-USDT", timeframe="1d", series_bars=len(bars))
 *     json.dump({"bars": payload, "rows": archive.index("1d")[:50]}, open("/tmp/payload.json", "w"))
 *     EOF
 *     node analysis/experiments/render_check.mjs /tmp/payload.json
 */

import fs from "node:fs";

const payloadPath = process.argv[2] || "/tmp/payload.json";
const payload = JSON.parse(fs.readFileSync(payloadPath, "utf8"));
// The page this script can execute is the hand-written canvas one: the Lightweight Charts
// page needs a real DOM, a canvas and a browser layout engine, none of which node has here.
const html = fs.readFileSync(new URL("../src/analysis/dashboard-canvas.html", import.meta.url), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);

let js =
  "window.__DATA__ = " +
  JSON.stringify({
    static: true,
    index: { timeframe: payload.bars.timeframe, rows: payload.rows },
    bars: { [payload.bars.symbol + "|" + payload.bars.timeframe]: payload.bars },
  }) +
  ";\n" +
  scripts[scripts.length - 1];

/* ---------------------------------------------------------------- DOM stubs */
const calls = {};
const rects = [];                      // geometry of every fillRect since the last reset
const texts = [];                      // every fillText since the last reset
const CHAR = 6.6;                      // 11px monospace, near enough to catch overflow
const count = (name) => (..._args) => { calls[name] = (calls[name] || 0) + 1; };
function context() {
  return new Proxy(
    {
      fillRect: (...args) => { calls.fillRect = (calls.fillRect || 0) + 1; rects.push(args); },
      fillText: (text, x, y) => {
        calls.fillText = (calls.fillText || 0) + 1;
        texts.push({ text: String(text), x, y, width: String(text).length * CHAR });
      }, strokeRect: count("strokeRect"), clearRect: count("clearRect"),
      beginPath: count("beginPath"), moveTo: count("moveTo"),
      lineTo: count("lineTo"), stroke: count("stroke"), fill: count("fill"), save: count("save"),
      restore: count("restore"), setLineDash: count("setLineDash"), setTransform: count("setTransform"),
      measureText: (text) => ({ width: String(text).length * CHAR }),
    },
    { get: (target, key) => (key in target ? target[key] : undefined), set: (target, key, value) => { target[key] = value; return true; } },
  );
}
const listeners = new Map();
function element(id) {
  return {
    id, style: {}, dataset: {}, className: "", textContent: "", innerHTML: "", value: "",
    width: 0, height: 0, clientWidth: 1200, clientHeight: 700,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener: (type, handler) => listeners.set(id + ":" + type, handler),
    append() {}, appendChild() {}, getContext: () => context(),
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 700 }),
    dispatchEvent() {},
  };
}
const nodes = new Map();
const byId = (id) => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
const smaButtons = ["20", "50", "200"].map((period) => { const node = element("sma" + period); node.dataset.sma = period; return node; });
const rangeButtons = ["1M", "3M", "1Y", "3Y", "All"].map((label) => { const node = element("range" + label); node.dataset.range = label; node.textContent = label; return node; });
const styleButtons = ["auto", "candles", "bars", "line"].map((style) => { const node = element("style" + style); node.dataset.style = style; node.textContent = style; return node; });

global.document = {
  getElementById: byId,
  createElement: (tag) => element(tag),
  querySelector: (selector) =>
    (selector.includes("style=auto") && styleButtons.find((node) => node.dataset.style === "auto")) || null,
  querySelectorAll: (selector) =>
    selector.includes("data-sma") ? smaButtons
      : selector.includes("#ranges") ? rangeButtons
      : selector.includes("#styles") ? styleButtons : [],
};
global.window = { devicePixelRatio: 2, addEventListener: (t, h) => listeners.set("window:" + t, h), __DATA__: null };
global.history = { replaceState() {} };
global.location = { hash: "" };
global.fetch = async () => { throw new Error("a static export must not fetch"); };
global.WheelEvent = class { constructor(type, options) { Object.assign(this, options, { type }); } };

/* ------------------------------------------------------------------- drive */
const errors = [];

// How many bars are on screen right now: the volume histogram draws exactly one bar per
// visible point and every one of them ends on the same baseline, so the most common
// bottom edge among the last draw's rectangles *is* the visible bar count. Counting
// fillRect totals instead (the first version) mixed several redraws together.
function visibleBars() {
  const bottoms = new Map();
  for (const [, y, , height] of rects) {
    const bottom = Math.round(y + height);
    bottoms.set(bottom, (bottoms.get(bottom) || 0) + 1);
  }
  let most = 0;
  for (const seen of bottoms.values()) most = Math.max(most, seen);
  return most;
}

function measure(trigger) {
  rects.length = 0;
  texts.length = 0;
  trigger();
  return visibleBars();
}

// Nothing the page draws may run off the canvas: the widest price label decides how much
// room the right-hand scale needs, and a fixed margin clipped the larger ones.
function drawThenAudit(trigger) {
  rects.length = 0;
  texts.length = 0;
  trigger();
  const canvasWidth = 1200;
  const overflow = texts.filter((entry) => entry.x + entry.width > canvasWidth + 0.5);
  const scale = texts
    .filter((entry) => entry.x > canvasWidth - 220)
    .map((entry) => entry.text);
  const distinct = new Set(scale).size;
  return {
    fills: rects.length,
    scale,
    bars: visibleBars(),
    overflow: overflow.map((entry) => `${entry.text} @${Math.round(entry.x)} ends ${Math.round(entry.x + entry.width)}`),
    widest: Math.round(Math.max(0, ...texts.map((entry) => entry.width))),
  };
}

// Like `measure`, but also resets the call counters so the *shape* of a draw can be
// compared between display styles: bodies are fillRects, wicks and ticks are lines.
function probe(trigger) {
  for (const key of Object.keys(calls)) calls[key] = 0;
  rects.length = 0;
  trigger();
  return { ...calls, bars: visibleBars() };
}
const fire = (key, argument) => {
  const handler = listeners.get(key);
  if (!handler) throw new Error("no handler for " + key);
  return handler(argument);
};

process.on("unhandledRejection", (error) => errors.push(String(error)));
try { eval(js); } catch (error) { errors.push("boot threw: " + error.message); }
await new Promise((resolve) => setTimeout(resolve, 300));
const audit = drawThenAudit(() => fire("chart:mousemove", { clientX: 700, clientY: 200 }));
const bootWindow = audit.bars;
// The default view is a fixed style, so the wheel must only zoom: if the draw leaves
// candle bodies behind (one fill per bar plus the volume bar), the type was stable.
if (audit.fills < audit.bars * 1.5) {
  errors.push(`the default view drew ${audit.fills} fills for ${audit.bars} bars: it is not candlesticks`);
}
console.log(`right-hand scale: ${audit.scale.join(" ")}`);
console.log(`widest label: ${audit.widest} px`);
if (audit.overflow.length) {
  errors.push(`text runs off the right edge: ${audit.overflow.slice(0, 5).join(", ")}`);
}
// A price axis that prints the same number at every tick is not an axis: this caught
// the cheap pairs, where fixed decimals collapsed the whole range into "0.00000000".
if (payload.bars.c[payload.bars.count - 1] > 0 && audit.scale.length >= 3 && !audit.scale.some((text, i) => text !== audit.scale[0])) {
  errors.push(`the price axis prints one value everywhere: ${audit.scale.join(" ")}`);
}
if (audit.scale.some((text) => text === "0.00000000" || text === "-0")) {
  errors.push(`the price axis shows a collapsed price: ${audit.scale.join(" ")}`);
}
console.log(`bars in the starting window: ${bootWindow} (of ${payload.bars.count} loaded)`);
if (bootWindow < 400) errors.push(`the starting window is too tight: ${bootWindow} bars`);
if (payload.bars.count > 1000 && bootWindow >= payload.bars.count) {
  errors.push("the starting window shows the whole series instead of a readable slice");
}

// One wheel tick zooms by about 13%, so the window has to shrink by about that much: the
// handler must start from the window that is on screen. It did not once — it rebuilt a
// hardcoded 260-bar window on its first tick, so the chart jumped to a third of what was
// on screen and looked like it was drifting sideways as you scrolled.
let afterTick = bootWindow;
let tickAudit = audit;
try {
  tickAudit = drawThenAudit(() => fire("chart:wheel", { deltaY: -120, clientX: 700, clientY: 300, preventDefault() {} }));
  afterTick = tickAudit.bars;
} catch (error) {
  errors.push("first wheel tick threw: " + error.message);
}
if (tickAudit.fills < tickAudit.bars * 1.5) {
  errors.push(
    `zooming changed the bar type: ${audit.fills} fills -> ${tickAudit.fills} for ` +
    `${tickAudit.bars} bars`,
  );
}
const ratio = bootWindow > 0 ? afterTick / bootWindow : 1;
console.log(`one wheel tick: ${bootWindow} bars -> ${afterTick} bars (${(ratio * 100).toFixed(0)}%)`);
if (ratio < 0.7) {
  errors.push(
    `the first wheel tick collapsed the window (${bootWindow} -> ${afterTick} bars): ` +
    "the zoom handler is not starting from what is on screen",
  );
}
if (ratio > 1.05) {
  errors.push(`a wheel tick meant to zoom in made the window wider (${bootWindow} -> ${afterTick} bars)`);
}

try {
  rangeButtons.forEach((button) => button.onclick && button.onclick());
  byId("reset").onclick();
  smaButtons.forEach((button) => button.onclick());
  byId("log").onclick();
  fire("chart:mousemove", { clientX: 700, clientY: 200 });
  fire("chart:mousemove", { clientX: 600, clientY: 300 });
  fire("chart:wheel", { deltaY: -120, clientX: 600, clientY: 300, preventDefault() {} });
  for (let i = 0; i < 12; i++) fire("chart:wheel", { deltaY: -120, clientX: 700, clientY: 300, preventDefault() {} });
  for (let i = 0; i < 25; i++) fire("chart:wheel", { deltaY: 120, clientX: 700, clientY: 300, preventDefault() {} });
  fire("chart:mousedown", { clientX: 400 });
  fire("chart:mousemove", { clientX: 300, clientY: 250 });
  fire("window:mouseup", {});
  fire("window:keydown", { key: "ArrowLeft", target: { tagName: "BODY" } });
  fire("window:resize", {});
  fire("chart:dblclick", {});
} catch (error) {
  errors.push("interaction threw: " + error.message);
}

// The four display styles have to be reachable and visibly different: candlesticks fill a
// body per bar, OHLC bars draw wicks and ticks instead, and the line mode draws one path.
const shapes = {};
try {
  for (const style of ["auto", "candles", "bars", "line"]) {
    const button = styleButtons.find((node) => node.dataset.style === style);
    shapes[style] = probe(() => button.onclick());
    const shape = shapes[style];
    console.log(
      `${style.padEnd(8)} bars=${String(shape.bars).padStart(4)} ` +
      `fillRect=${String(shape.fillRect).padStart(5)} moves=${String(shape.moveTo).padStart(5)}`,
    );
  }
} catch (error) {
  errors.push("switching the display style threw: " + error.message);
}

if (shapes.candles && shapes.candles.fillRect < shapes.candles.bars * 1.5) {
  errors.push("candles mode drew no candle bodies");
}
if (shapes.bars && shapes.bars.fillRect > shapes.bars.bars * 1.2) {
  errors.push("bars mode filled candle bodies instead of drawing wicks and ticks");
}
if (shapes.bars && shapes.line && !(shapes.bars.moveTo > shapes.line.moveTo * 2)) {
  errors.push("bars mode does not draw more strokes per bar than line mode");
}
if (shapes.auto && shapes.line) {
  // `auto` picks whichever of the two the geometry calls for: a candle needs about three
  // pixels, and the default window is wide enough that it does not get them.
  const slot = 1124 / bootWindow;
  const expected = slot >= 3 ? "candles" : "line";
  if (shapes.auto.fillRect !== shapes[expected].fillRect) {
    errors.push(`auto mode drew ${shapes.auto.fillRect} fills where ${expected} needs ${shapes[expected].fillRect}`);
  }
  console.log(`auto at ${slot.toFixed(1)} px per bar resolves to ${expected}`);
}

// --- the two axes are controls ---------------------------------------------
// The right-hand scale is the vertical navigator: it has to change the price range and
// leave the time range alone, and the bottom one the other way round. "I cannot zoom out
// with the right navigator" was exactly this gesture missing.
try {
  const base = drawThenAudit(() => fire("chart:mousemove", { clientX: 700, clientY: 200 }));
  const AXIS_X = 1150;                    // right of the plot: the price scale
  const TIME_Y = 690;                     // below the volume panel: the time scale

  const wheeled = drawThenAudit(() =>
    fire("chart:wheel", { deltaY: -120, clientX: AXIS_X, clientY: 250, preventDefault() {} }));
  if (wheeled.bars !== base.bars) errors.push("wheeling the price scale moved the time range");
  if (wheeled.scale.join() === base.scale.join()) {
    errors.push("wheeling the price scale did not change the price range");
  }

  fire("chart:mousedown", { clientX: AXIS_X, clientY: 250 });
  const dragged = drawThenAudit(() => fire("chart:mousemove", { clientX: AXIS_X, clientY: 350 }));
  fire("window:mouseup", {});
  if (dragged.bars !== base.bars) errors.push("dragging the price scale moved the time range");
  if (dragged.scale.join() === base.scale.join()) {
    errors.push("dragging the price scale did not change the price range");
  }

  const restored = drawThenAudit(() => fire("chart:dblclick", { clientX: AXIS_X, clientY: 250 }));
  if (restored.scale.join() !== base.scale.join()) {
    errors.push("double-clicking the price scale did not restore the fitted range");
  }

  fire("chart:mousedown", { clientX: 600, clientY: TIME_Y });
  const timeDragged = drawThenAudit(() => fire("chart:mousemove", { clientX: 700, clientY: TIME_Y }));
  fire("window:mouseup", {});
  if (timeDragged.bars === base.bars) errors.push("dragging the time scale did not change the time range");

  fire("chart:mousemove", { clientX: AXIS_X, clientY: 250 });
  const overAxis = byId("chart").style.cursor;
  if (overAxis !== "ns-resize") errors.push(`the price scale shows the "${overAxis}" cursor instead of ns-resize`);
  fire("chart:mousemove", { clientX: 600, clientY: TIME_Y });
  const overTime = byId("chart").style.cursor;
  if (overTime !== "ew-resize") errors.push(`the time scale shows the "${overTime}" cursor instead of ew-resize`);
  console.log(`price scale: wheel ${wheeled.scale.length} ticks, drag ${dragged.scale.length} ticks, restored ${restored.scale.length}`);
  console.log(`time scale: ${base.bars} bars -> ${timeDragged.bars} bars after a drag`);
} catch (error) {
  errors.push("the axis gestures threw: " + error.message);
}

console.log("canvas calls:", JSON.stringify(calls));
console.log(errors.length ? "FAILED:\n" + errors.join("\n") : "no exceptions; every interaction rendered");
process.exit(errors.length ? 1 : 0);
