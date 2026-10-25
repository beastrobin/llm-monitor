'use strict';

const HISTORY_LIMIT = 900;
const THEME_KEY = 'llm-monitor-theme';
const HIST_TAB_KEY = 'llm-moni…tab';

// T() wraps strings that never enter the DOM (window.confirm) -- /i18n.js
// supplies it before this file runs in index.html. The identity fallback
// keeps confirm alive when i18n.js is absent, e.g. render_check's hand-built
// window or a blocked script load; the language itself still shows Chinese.
const T = (window.I18N && window.I18N.t) || function (s) { return s; };
const PEAK_WINDOW_SEC = 1800; // 滚动峰值窗口：30分钟

// The connection lamp reports the *transport*, not the watched endpoints --
// those already have their own error text on their cards.
const CONN_TITLES = {
  live: '实时推送中',
  poll: '推送不可用，已降级为定时轮询',
  down: '连接断开，正在重连…',
};
// One column per ~2 CSS pixels is the sweet spot: enough resolution to keep the
// shape, few enough points that the stroke stays crisp.
const BUCKET_PX = 2.2;
const FALLBACK_MONO = 'ui-monospace, "Cascadia Mono", Consolas, monospace';

const S = {
  snapshot: null,
  history: { timestamps: [], series: {} },
  config: null,
};

// ---------------------------------------------------------------- formatting

function num(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits === undefined ? 1 : digits);
}

// First argument that is actually a number.  The driver reports several power
// columns as [N/A] -- which arrives here as null -- and one of them standing in
// for the other is the whole reason this exists: falling back must not treat a
// missing reading as a zero.
function firstNumber() {
  for (let i = 0; i < arguments.length; i += 1) {
    const value = arguments[i];
    if (value !== null && value !== undefined && !Number.isNaN(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

function pct(v, digits) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return (Number(v) * 100).toFixed(digits === undefined ? 1 : digits) + '%';
}

function gb(mb, digits) {
  if (mb === null || mb === undefined || Number.isNaN(mb)) return '—';
  return (Number(mb) / 1024).toFixed(digits === undefined ? 1 : digits);
}

function esc(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function shortPath(p) {
  if (!p) return '—';
  const parts = String(p).split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function countText(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toLocaleString('zh-CN');
}

// ------------------------------------------------------------------ sparkline

function cleanSeries(values) {
  const out = [];
  const source = values || [];
  for (let i = 0; i < source.length; i++) {
    const v = source[i];
    if (v !== null && v !== undefined && !Number.isNaN(v)) out.push(v);
  }
  return out;
}

// 900 samples crammed into a ~200 px card alias into a hairy scribble if every
// sample keeps its own point.  Bucketing holds roughly one column per two
// pixels while keeping each bucket's extremes, so a spike survives the
// downsampling instead of being smoothed away by an average.
function bucketise(values, columns) {
  const source = values || [];
  const total = source.length;
  const out = [];
  if (!total || columns < 1) return out;

  const collapsed = total > columns;
  const size = collapsed ? total / columns : 1;
  const count = collapsed ? columns : total;

  for (let c = 0; c < count; c++) {
    const start = collapsed ? Math.floor(c * size) : c;
    const end = collapsed ? Math.max(start + 1, Math.floor((c + 1) * size)) : c + 1;
    let lo = null;
    let hi = null;
    let sum = 0;
    let hits = 0;
    for (let i = start; i < end && i < total; i++) {
      const v = source[i];
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      if (lo === null || v < lo) lo = v;
      if (hi === null || v > hi) hi = v;
      sum += v;
      hits += 1;
    }
    out.push({ lo: lo, hi: hi, avg: hits ? sum / hits : null, empty: hits === 0 });
  }
  return out;
}

function niceCeil(value) {
  if (!(value > 0)) return 1;
  const exp = Math.floor(Math.log10(value));
  const base = Math.pow(10, exp);
  const n = value / base;
  const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return step * base;
}

// A ceiling recomputed from the data on every tick rescales the whole curve
// whenever a new peak lands, so the chart visibly twitches every couple of
// seconds.  The ceiling jumps up at once and only decays slowly.
function stableCeiling(canvas, observed) {
  const previous = canvas._ceiling;
  if (previous === undefined || observed > previous) {
    canvas._ceiling = niceCeil(observed * 1.08);
  } else if (observed < previous * 0.5) {
    canvas._ceiling = Math.max(niceCeil(observed * 1.08), previous * 0.88);
  }
  return canvas._ceiling || 1;
}

function defaultFormat(value, decimals) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const abs = Math.abs(value);
  if (abs >= 1e6) return (value / 1e6).toFixed(1) + 'M';
  if (abs >= 1e4) return (value / 1e3).toFixed(0) + 'k';
  return value.toFixed(decimals === undefined ? 0 : decimals);
}

// A vertical gradient for the area under a curve: the colour the card's line
// already uses, stronger near the curve and fading toward the baseline.  The
// CSS custom properties are hex strings, so the alpha has to be built by hand
// (a gradient stop cannot take `color` plus an alpha the way a fill can).
// A colour the parser cannot read falls back to the solid colour rather than
// an empty string, which would fill nothing at all.
function gradientUnder(g, color, yTop, yBase) {
  if (!g.createLinearGradient) return color;
  const rgb = hexToRgb(color);
  const grad = g.createLinearGradient(0, yTop, 0, yBase);
  if (!rgb) {
    grad.addColorStop(0, color);
    grad.addColorStop(1, color);
    return grad;
  }
  grad.addColorStop(0, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',0.30)');
  grad.addColorStop(1, 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',0.06)');
  return grad;
}

function hexToRgb(value) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(value || '').trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath();
  g.moveTo(x + r, y);
  g.lineTo(x + w - r, y);
  g.quadraticCurveTo(x + w, y, x + w, y + r);
  g.lineTo(x + w, y + h - r);
  g.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  g.lineTo(x + r, y + h);
  g.quadraticCurveTo(x, y + h, x, y + h - r);
  g.lineTo(x, y + r);
  g.quadraticCurveTo(x, y, x + r, y);
  g.closePath();
}

function drawSpark(canvas, data, options) {
  const opts = options || {};
  const values = data || [];
  // Keep the payload on the element so the hover handler can repaint without
  // waiting for the next sample to arrive.
  canvas._spark = { values: values, opts: opts };
  if (canvas._hover === undefined) canvas._hover = null;

  const mono = cssVar('--font-mono') || FALLBACK_MONO;
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(64, canvas.clientWidth || 160);
  const height = Math.max(30, canvas.clientHeight || 46);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, width, height);

  const color = opts.color || cssVar('--chart-a');
  const gridColor = cssVar('--grid');
  const trackColor = cssVar('--track');
  const axisColor = cssVar('--axis');
  const surface = cssVar('--panel');
  const edge = cssVar('--border');
  const ink = cssVar('--text');
  const fmt = opts.format || function (v) { return defaultFormat(v, opts.decimals); };

  const clean = cleanSeries(values);
  let min = (opts.min === undefined || opts.min === null) ? null : opts.min;
  let max = (opts.max === undefined || opts.max === null) ? null : opts.max;

  if (max === null) {
    max = stableCeiling(canvas, clean.length ? Math.max.apply(null, clean) : 0);
  } else {
    canvas._ceiling = max;
  }
  if (min === null) {
    const floor = clean.length ? Math.min.apply(null, clean) : 0;
    min = floor < 0 ? floor * 1.15 : 0;
  }
  // EKG mode: a fixed range (0–100%) squashes a real swing into a sliver, so
  // when the caller opts in we zoom to the data's own span.  The big number and
  // the bar below still show the absolute level, and the axis labels print the
  // exact window on display, so the zoom never misleads.  A minimum-span floor
  // keeps a genuinely flat series from turning its own noise into a fake EKG;
  // too little data falls back to the fixed range for the same reason.
  if (opts.autoscale && clean.length >= 8) {
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < clean.length; i++) {
      if (clean[i] < lo) lo = clean[i];
      if (clean[i] > hi) hi = clean[i];
    }
    // Outlier filter: one completion burst an order of magnitude above the
    // rest (a stepped counter landing whole in one scrape, a cache-hit flash)
    // used to own the y-axis and squash every real reading into a flat line.
    // The ceiling comes from the 98th percentile with headroom instead; the
    // spike still draws, pinned to the top edge, and the hover tooltip still
    // reports its true value -- filtered scale, not hidden data.  A series
    // whose p98 is zero (mostly idle) is left alone: there the high points
    // are the signal, not the noise.
    if (hi > 0) {
      const sorted = clean.slice().sort(function (a, b) { return a - b; });
      const p98 = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.98))];
      if (p98 > 0 && hi > p98 * 3) hi = p98 * 1.25;
    }
    const refSpan = Math.max(max - min, 1e-6);
    let span = hi - lo;
    if (span < refSpan * 0.12) {
      // Nearly flat: show a bounded window centred on the data rather than
      // stretching micro-noise across the whole box.
      span = refSpan * 0.12;
      const mid = (lo + hi) / 2;
      lo = mid - span / 2;
      hi = mid + span / 2;
    }
    const pad = span * 0.1;
    min = Math.max(0, lo - pad);
    max = hi + pad;
  }
  if (max - min < 1e-9) max = min + 1;

  const showAxis = opts.axis !== false;
  const x0 = showAxis ? 32 : 2;
  const x1 = width - 3;
  const yTop = showAxis ? 11 : 5;
  const yBase = height - 3;
  const plotW = Math.max(8, x1 - x0);
  const plotH = Math.max(8, yBase - yTop);
  const yOf = function (v) {
    const raw = yBase - ((v - min) / (max - min)) * plotH;
    return Math.max(yTop - 2, Math.min(yBase + 2, raw));
  };

  g.lineWidth = 1;
  g.strokeStyle = gridColor;
  g.beginPath();
  g.moveTo(x0, Math.round(yOf(min + (max - min) / 2)) + 0.5);
  g.lineTo(x1, Math.round(yOf(min + (max - min) / 2)) + 0.5);
  g.stroke();

  g.strokeStyle = trackColor;
  g.beginPath();
  g.moveTo(x0, yBase + 0.5);
  g.lineTo(x1, yBase + 0.5);
  g.stroke();

  // An empty series would otherwise render as a bare axis with a stray "1" on
  // it, which reads like a broken chart.
  if (!clean.length) {
    canvas._geom = { x0: x0, plotW: plotW, count: 0 };
    canvas._hover = null;
    g.font = '11px ' + mono;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillStyle = axisColor;
    g.fillText(T('暂无数据'), (x0 + x1) / 2, (yTop + yBase) / 2);
    return;
  }

  const columns = Math.max(2, Math.floor(plotW / BUCKET_PX));
  const pts = bucketise(values, columns);
  const dense = pts.length < values.length;
  const count = pts.length;
  canvas._geom = { x0: x0, plotW: plotW, count: count };

  const pointAt = function (k) {
    const p = pts[k];
    if (!p || p.empty) return null;
    const x = count === 1 ? (x0 + x1) / 2 : x0 + (k / (count - 1)) * plotW;
    const value = dense ? p.avg : p.lo;
    return { x: x, y: yOf(value), lo: yOf(p.lo), hi: yOf(p.hi), value: value };
  };

  // Empty samples (engine idle, no rate reported) are skipped without
  // breaking the line: the last reading before a gap connects straight to the
  // first one after it, the way Strata's own sparklines read.  Treating them
  // as breaks shattered the speed curves into confetti every time a request
  // ended and the engine went quiet between calls.
  const runs = [];
  let run = null;
  for (let k = 0; k < count; k++) {
    const point = pointAt(k);
    if (!point) continue;
    if (!run) { run = []; runs.push(run); }
    run.push(point);
  }

  if (runs.length) {
    // Colour UNDER the curve, not just a band hugging it.  The old fill was the
    // per-column high/low envelope, which on a nearly flat series is a few
    // pixels thick -- so most cards looked like a lonely line with nothing
    // beneath it.  The area now fills from the curve down to the baseline with
    // a vertical gradient (stronger at the curve, fading toward the floor),
    // which is what makes the chart read as "how much of the window this
    // reading occupies".  The noise envelope survives as a thin stroke instead
    // of a fill, so a spiky series still shows its spikes.
    const area = gradientUnder(g, color, yTop, yBase);
    runs.forEach(function (r) {
      if (r.length < 1) return;
      g.beginPath();
      g.moveTo(r[0].x, r[0].y);
      for (let i = 1; i < r.length; i++) g.lineTo(r[i].x, r[i].y);
      g.lineTo(r[r.length - 1].x, yBase);
      g.lineTo(r[0].x, yBase);
      g.closePath();
      g.fillStyle = area;
      g.fill();
      canvas._areaFill = true;
    });

    if (dense) {
      g.globalAlpha = 0.4;
      g.strokeStyle = color;
      g.lineWidth = 1;
      runs.forEach(function (r) {
        if (r.length < 2) return;
        g.beginPath();
        g.moveTo(r[0].x, r[0].hi);
        for (let i = 1; i < r.length; i++) g.lineTo(r[i].x, r[i].hi);
        g.stroke();
        g.beginPath();
        g.moveTo(r[0].x, r[0].lo);
        for (let i = 1; i < r.length; i++) g.lineTo(r[i].x, r[i].lo);
        g.stroke();
      });
      g.globalAlpha = 1;
    }

    // One stroke per contiguous run: joining across a gap would draw a line
    // through samples that do not exist.
    g.strokeStyle = color;
    g.lineWidth = 1.5;
    g.lineJoin = 'round';
    g.lineCap = 'round';
    runs.forEach(function (r) {
      if (r.length < 2) return;
      g.beginPath();
      g.moveTo(r[0].x, r[0].y);
      for (let i = 1; i < r.length; i++) g.lineTo(r[i].x, r[i].y);
      g.stroke();
    });

    const tail = runs[runs.length - 1];
    const head = tail[tail.length - 1];
    g.beginPath();
    g.arc(head.x, head.y, 3.4, 0, Math.PI * 2);
    g.fillStyle = surface;
    g.fill();
    g.beginPath();
    g.arc(head.x, head.y, 2.1, 0, Math.PI * 2);
    g.fillStyle = color;
    g.fill();

    const hovered = canvas._hover;
    if (hovered !== null && hovered !== undefined && hovered < count) {
      const point = pointAt(hovered);
      if (point) {
        g.strokeStyle = edge;
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(Math.round(point.x) + 0.5, yTop - 3);
        g.lineTo(Math.round(point.x) + 0.5, yBase);
        g.stroke();

        g.beginPath();
        g.arc(point.x, point.y, 3.2, 0, Math.PI * 2);
        g.fillStyle = surface;
        g.fill();
        g.strokeStyle = color;
        g.lineWidth = 1.5;
        g.stroke();

        const text = fmt(point.value);
        g.font = '11px ' + mono;
        const boxW = g.measureText(text).width + 12;
        const boxH = 18;
        const boxX = Math.max(x0, Math.min(x1 - boxW, point.x - boxW / 2));
        const boxY = Math.max(yTop - 9, point.y - boxH - 7);
        roundRect(g, boxX, boxY, boxW, boxH, 5);
        g.fillStyle = surface;
        g.fill();
        g.strokeStyle = edge;
        g.lineWidth = 1;
        g.stroke();
        g.fillStyle = ink;
        g.textAlign = 'left';
        g.textBaseline = 'middle';
        g.fillText(text, boxX + 6, boxY + boxH / 2 + 0.5);
      }
    }
  }

  if (showAxis) {
    g.font = '10px ' + mono;
    g.textAlign = 'right';
    g.textBaseline = 'middle';
    g.fillStyle = axisColor;
    g.fillText(fmt(max), x0 - 6, Math.max(6, Math.min(yBase - 2, yOf(max))));
    // A zero floor needs no label: the baseline line already says it.
    if (min > 0) {
      g.fillText(fmt(min), x0 - 6, Math.max(6, Math.min(yBase - 2, yOf(min))));
    }
  }
}

// A crosshair read-out on hover turns a pretty squiggle into something you can
// actually take a number off, which is the point of the chart.
let hoveredSpark = null;

function repaintSpark(canvas) {
  if (!canvas || !canvas._spark) return;
  drawSpark(canvas, canvas._spark.values, canvas._spark.opts);
}

function onPointerMove(event) {
  const target = event.target;
  const canvas = (target && target.tagName === 'CANVAS' && target._spark) ? target : null;

  if (hoveredSpark && hoveredSpark !== canvas) {
    hoveredSpark._hover = null;
    repaintSpark(hoveredSpark);
    hoveredSpark = null;
  }
  if (!canvas) return;

  hoveredSpark = canvas;
  const geom = canvas._geom;
  if (!geom || geom.count < 2) return;
  const rect = canvas.getBoundingClientRect();
  const ratio = (event.clientX - rect.left - geom.x0) / geom.plotW;
  const index = Math.round(Math.max(0, Math.min(1, ratio)) * (geom.count - 1));
  if (canvas._hover === index) return;
  canvas._hover = index;
  repaintSpark(canvas);
}

// -------------------------------------------------------------------- history

function seriesOf(key) {
  if (!S.history.series[key]) S.history.series[key] = [];
  return S.history.series[key];
}

function appendPoint(point) {
  if (!point) return;
  const stamps = S.history.timestamps;
  const last = stamps.length ? stamps[stamps.length - 1] : 0;
  if (point.ts <= last) return;

  stamps.push(point.ts);
  if (stamps.length > HISTORY_LIMIT) stamps.shift();

  const keys = Object.keys(point.values);
  keys.forEach(function (key) {
    const series = seriesOf(key);
    while (series.length < stamps.length - 1) series.push(null);
    series.push(point.values[key]);
    if (series.length > stamps.length) series.shift();
  });

  const length = stamps.length;
  Object.keys(S.history.series).forEach(function (key) {
    const series = S.history.series[key];
    while (series.length < length) series.unshift(null);
    if (series.length > length) series.shift();
  });
}

function loadHistory(payload) {
  if (!payload) return;
  S.history.timestamps = payload.timestamps || [];
  S.history.series = {};
  const source = payload.series || {};
  Object.keys(source).forEach(function (key) { S.history.series[key] = source[key].slice(); });
}

function peakOf(series) {
  let best = null;
  (series || []).forEach(function (value) {
    if (value === null || value === undefined || Number.isNaN(value)) return;
    if (best === null || value > best) best = value;
  });
  return best;
}

// Rolling peak: only consider values within the last PEAK_WINDOW_SEC seconds.
// Old peaks naturally decay as new data ages them out of the window.
function rollingPeakOf(series, timestamps) {
  if (!series || !timestamps || !timestamps.length) return null;
  const cutoff = timestamps[timestamps.length - 1] - PEAK_WINDOW_SEC;
  let best = null;
  for (let i = 0; i < series.length; i++) {
    if ((timestamps[i] || 0) < cutoff) continue;
    const v = series[i];
    if (v === null || v === undefined || Number.isNaN(v)) continue;
    if (best === null || v > best) best = v;
  }
  return best;
}

function maxOf(list, field) {
  let best = null;
  (list || []).forEach(function (item) {
    const value = item[field];
    if (value === null || value === undefined) return;
    if (best === null || value > best) best = value;
  });
  return best;
}

// ----------------------------------------------------------------------- cards

function cardSpec(key, label, options) {
  const opts = options || {};
  return {
    key: key,
    label: label,
    tip: opts.tip || '',
    bar: !!opts.bar,
    // Rail orientation: 'v' = the classic left-edge rail filling bottom-up,
    // 'h' = a full-width track under the value filling left-to-right.
    barDir: opts.barDir === 'h' ? 'h' : 'v',
    spark: opts.spark !== false,
    // A card that carries a per-slot identity tag (slot id + engine state)
    // INLINE in its label row — used by the 已生成 card so a slot row needs
    // no separate title line and no reserved placeholder (2026-10-19).
    tag: !!opts.tag,
  };
}

// Why every slot row can read "idle" while the engine is plainly working.
// Under --batch Strata only books a request against a numbered slot once two or
// more requests are concurrent (BGEN per slot); **a lone request goes back to
// the single-request path** -- GEN on the control lines, which is the fastest
// stream and carries no slot number (serve/server.py's `_may_go_solo`).
// Measured on this box: one chat completion at a time leaves live.slots as
// `[{"slot":0,"state":"idle","held_tokens":371}, ...]` for every frame of the
// generation while live.state/phase say "reading"/"thinking"; fire two at once
// and the rows turn reading/decoding with prompt_tokens/generated/tok_s.  So an
// all-idle breakdown plus a moving headline curve is normal for a single
// conversation, and saying so beats six cards of em-dashes that look broken.
const SOLO_PATH_NOTE =
  '并发到 2 个以上时，引擎才把请求装进编号槽并逐槽记账（BGEN）。' +
  '只有你一个请求在跑时它走单请求通道（控制行上的 GEN），不落在任何槽上 —— ' +
  '所以每一行的状态都是 idle、速率列空着，这不是采集坏了。' +
  '此时上下文列显示的是该槽为下一轮留着的那段前缀。';

// Every card renders the same slots and hides the unused ones with visibility,
// so cards that carry no bar keep the exact width of the ones that do and a
// row of cards lines up.  The bar is a vertical rail on the card's left edge;
// everything else lives in .card-body so the rail can stretch full height.
// The info icon sits OUTSIDE .card-label on purpose:
// the label's overflow:hidden (used for ellipsis) would clip the tooltip.
function buildCards(container, specs) {
  container.innerHTML = specs.map(function (spec) {
    // A card with spark: false carries no canvas at all (the slot cards asked
    // for no curves on 2026-10-19); the old hidden-canvas placeholder only
    // existed so mixed spark/no-spark rows lined up, and no row does that.
    // The horizontal-bar (slot) cards are a TWO-LINE card (2026-10-19):
    // row 1 = slot tag + label, row 2 = value with the health bar running
    // out to the card edge on the same line.  The rail therefore lives
    // INSIDE the body next to the value instead of under everything, and no
    // card reserves an empty tag placeholder any more.
    if (spec.barDir === 'h') {
      return '<div class="card' + (spec.bar ? '' : ' no-bar') + ' bar-h" data-card="' + esc(spec.key) + '">' +
        '<div class="card-body">' +
        '<div class="card-rail-row">' +
          (spec.tag ? '<span class="card-tag" data-role="tag"></span>' : '') +
          '<div class="bar" data-role="bar"><i data-role="fill"></i></div>' +
        '</div>' +
        '<div class="card-meta-row">' +
          '<span class="card-label">' + esc(spec.label) + '</span>' +
          (spec.tip ? '<span class="tip" tabindex="0" data-tip="' + esc(spec.tip) + '">i</span>' : '') +
          '<span class="card-value" data-role="value">—</span>' +
        '</div>' +
        '<div class="card-foot" data-role="foot"></div>' +
        '</div></div>';
    }
    return '<div class="card' + (spec.bar ? '' : ' no-bar') + '" data-card="' + esc(spec.key) + '">' +
      '<div class="bar" data-role="bar"><i data-role="fill"></i></div>' +
      '<div class="card-body">' +
        '<div class="card-head">' +
          '<span class="card-label">' + esc(spec.label) + '</span>' +
          (spec.tip ? '<span class="tip" tabindex="0" data-tip="' + esc(spec.tip) + '">i</span>' : '') +
          '<span class="card-value" data-role="value">—</span>' +
        '</div>' +
        (spec.spark ? '<div class="card-visual">' +
          '<canvas class="spark" data-role="spark"></canvas>' +
          '</div>' : '') +
        '<div class="card-foot" data-role="foot"></div>' +
      '</div>' +
      '</div>';
  }).join('');

  const refs = new Map();
  specs.forEach(function (spec) {
    const root = container.querySelector('[data-card="' + spec.key + '"]');
    if (!root) return;
    refs.set(spec.key, {
      root: root,
      value: root.querySelector('[data-role="value"]'),
      spark: root.querySelector('[data-role="spark"]'),
      bar: root.querySelector('[data-role="bar"]'),
      fill: root.querySelector('[data-role="fill"]'),
      horizontal: spec.barDir === 'h',
      tag: root.querySelector('[data-role="tag"]'),
      foot: root.querySelector('[data-role="foot"]'),
      // The info icon next to the label; its tooltip now also carries the live
      // foot text, so hovering one icon explains the metric AND shows the
      // current breakdown instead of a permanent line under the spark.
      tip: root.querySelector('.card-head .tip'),
      tipBase: spec.tip || '',
    });
  });
  return refs;
}

function ensureCards(container, specs) {
  const signature = specs.map(function (s) { return s.key; }).join('|');
  if (container.dataset.signature !== signature) {
    container.dataset.signature = signature;
    container._refs = buildCards(container, specs);
  }
  return container._refs;
}

function paintCards(refs, values) {
  refs.forEach(function (ref, key) {
    const value = values[key] || {};
    if (ref.value) {
      ref.value.innerHTML = esc(value.text) +
        (value.unit ? '<span class="card-unit">' + esc(value.unit) + '</span>' : '');
    }
    // The foot line is hidden; its text now rides on the info icon's tooltip so
    // the card keeps its full height for the spark.  The static metric note
    // (if any) leads, the live breakdown follows.
    if (ref.tip) {
      const base = ref.tipBase;
      const foot = value.foot || '';
      ref.tip.setAttribute('data-tip', base ? (base + ' ' + foot).trim() : foot);
    }
    if (ref.fill) {
      const ratio = (value.bar === undefined || value.bar === null) ? null : value.bar;
      // The rail fills along its own axis: the vertical rail from the bottom
      // up (a height), the horizontal one left to right (a width). railHeight
      // keeps a small but real reading visible either way: a 0.2% context
      // share is a tenth of a pixel and reads as an empty card.
      if (ref.horizontal) ref.fill.style.width = railHeight(ratio);
      else ref.fill.style.height = railHeight(ratio);
      ref.bar.className = 'bar' + (value.barClass ? ' ' + value.barClass : '');
    }
    if (ref.spark && value.series) {
      drawSpark(ref.spark, value.series, value.sparkOptions || {});
    }
  });
}

function kvChips(items) {
  return (items || []).map(function (item) {
    // Raw passthrough: the item is already-built markup, for chips that keep
    // the hero line's own look (the spec tags) instead of the label/value shape.
    if (item.raw) {
      return item.html || '';
    }
    // Expandable chip: a <details> whose summary is the chip itself and whose
    // body holds the child rows.  Used for the long MTP per-position list so
    // it does not sprawl across the detail row by default.
    if (item.expand) {
      const children = (item.children || []).map(function (c) {
        return '<span class="kv-item">' +
          '<em>' + esc(c.label) + '</em><b>' + esc(c.value) + '</b></span>';
      }).join('');
      return '<details class="kv-item kv-expand">' +
        '<summary><em>' + esc(item.label) + '</em><b>' + esc(item.value) + '</b></summary>' +
        '<div class="kv-children">' + children + '</div>' +
        '</details>';
    }
    return '<span class="kv-item' + (item.wide ? ' wide' : '') + (item.alert ? ' alert' : '') + '"' +
      (item.title ? ' title="' + esc(item.title) + '"' : '') + '>' +
      '<em>' + esc(item.label) + '</em>' +
      '<b>' + esc(item.value) + '</b>' +
      (item.unit ? '<span class="kv-unit">' + esc(item.unit) + '</span>' : '') +
      '</span>';
  }).join('');
}

// --------------------------------------------------------------------- render

function modelSignature(endpoint) {
  return (endpoint.models || []).map(function (m) {
    return (m.id || 'model') + (m.slots ? ':' + m.slots.length : '');
  }).join(',') || 'none';
}

// Newer builds report speculative.types as a comma list like
// "none,draft-mtp,ngram-simple".  Show only the active types, joined.
function cleanSpecTypes(value) {
  const parts = String(value || '').split(',').map(function (s) {
    return s.trim();
  }).filter(function (s) { return s && s.toLowerCase() !== 'none'; });
  return parts.join('+');
}

// Strata's Monitor tab leads with a model-state panel: five lamps over its
// state machine plus one line about what the request is doing.  The lamps are
// the engine's own vocabulary (serve/web/app.js picks them the same way:
// queued wins over the state when requests are waiting); the compact shape --
// lamps and one line squeezed onto the hero row's right -- is ours.
const STATE_LAMPS = [
  ['idle', '空闲'], ['reading', '读prompt'], ['generating', '生成中'],
  ['queued', '排队'], ['error', '出错'],
];
// The five lamps stay on the hero line, right-aligned, and never move: they
// are the fixed-width part of the state gauge.  The note that used to sit
// beside them does NOT -- its length changes every tick ("读 1.2k/4.4k tok",
// "生成中 · 812 tok @ 205.7 t/s", "上次 386 tok @ 127.3 t/s"), and with the
// row pushed to the far right by margin-left:auto that growth shoved the
// lamps left and back on every sample.  So the note moved to the detail row
// below, left-aligned and free to change width, exactly like the value chips
// already there.  The lamps keep the identity; the note keeps the detail.
function heroLampsHtml(model) {
  const live = model && model.live_state;
  if (!live || !live.state) return '';
  const on = live.queued > 0 ? 'queued' : live.state;
  const lamps = STATE_LAMPS.map(function (l) {
    const lit = l[0] === on || l[0] === live.state;
    return '<span class="state-lamp' + (lit ? ' on s-' + l[0] : '') + '">' +
      '<i></i>' + l[1] + '</span>';
  }).join('');
  return '<span class="hero-lamps" title="引擎自报状态（Strata /metrics live 块）">' +
    lamps + '</span>';
}

// The live note for one model: what the engine is doing right now, and what it
// did last when it is idle.  Rendered into the detail row, where its width is
// nobody else's problem.
function stateNoteText(model) {
  const live = model && model.live_state;
  if (!live || !live.state) return '';
  if (live.state === 'reading') {
    // Engine vocabulary stays English on purpose (thinking/decoding/read):
    // these notes sit right beside the engine's own chips (2026-10-19).
    return live.prompt_total
      ? 'read ' + countText(live.prompt_read) + '/' + countText(live.prompt_total) + ' tok'
      : 'read prompt ' + countText(live.prompt_tokens) + ' tok';
  }
  if (live.state === 'generating') {
    return (live.phase || '生成中') + ' · ' + countText(live.generated) + ' tok' +
      (live.tok_s != null ? ' @ ' + num(live.tok_s, 1) + ' t/s' : '');
  }
  const last = model.last_request;
  if (last && last.output_tokens != null) {
    return '上次 ' + countText(last.output_tokens) + ' tok' +
      (last.decode_tok_s != null ? ' @ ' + num(last.decode_tok_s, 1) + ' t/s' : '');
  }
  return '等待请求';
}

// The spec tags (quant / context / slots / MTP).  They belong on the hero
// line: they describe what the engine IS, and the hero line is the row that
// names the engine.  Keeping them next to the name means the right-hand end
// of that row holds a fixed set of chips -- the state lamps -- so the row no
// longer changes width as the numbers elsewhere do.
function modelTags(model) {
  const mtpTag = model.mtp_enabled
    ? '<span class="tag on">MTP ' + esc(cleanSpecTypes(model.mtp_type) || '启用') + '</span>'
    : '<span class="tag off">MTP 关闭</span>';
  return [
    model.model_ftype ? '<span class="tag">' + esc(model.model_ftype) + '</span>' : '',
    model.n_ctx ? '<span class="tag">' + esc((model.n_ctx / 1024).toFixed(0)) + 'K 上下文</span>' : '',
    model.total_slots ? '<span class="tag">' + esc(model.total_slots) + ' slots</span>' : '',
    mtpTag,
  ].join('');
}

// Some builds report the full model path as the model id; show only the
// file name without the .gguf suffix so the card reads "which model", not a
// filesystem breadcrumb.
function shortModelName(id) {
  const seg = String(id || 'model').split(/[\\/]/).pop();
  return seg.replace(/\.gguf$/i, '');
}

// "source / short name" for the ledger tables.  The source prefix carries a
// per-endpoint colour (strata blue, hermes green, loader amber) so a glance
// at a column of names says which server produced the tokens without reading
// each prefix.  Unknown sources stay neutral.
// Ledger tables carry the engine build that produced each row's numbers: a
// speed or hit-rate only means something next to the version that achieved it
// (2026-10-19 request).  renderEndpoints refreshes this map from every online
// endpoint's /props banner; names stay clean when a source reports nothing.
var S_VERSIONS = {};
function rowVerHtml(row) {
  const key = String(row.endpoint || '').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
  const v = S_VERSIONS[key];
  return v ? '<span class="row-ver">' + esc(v) + '</span>' : '';
}

function sourceName(row) {
  if (!row.endpoint) return esc(shortModelName(row.model));
  const key = String(row.endpoint).toLowerCase().replace(/[^a-z0-9_-]/g, '-');
  return '<span class="src-tag src-' + esc(key) + '">' + esc(row.endpoint) + '/</span>' +
    esc(shortModelName(row.model));
}

// Power-tier chip class for the detail table: one colour per ceiling, cool
// for the eco tiers, hot for the full-power one.  The watts value is baked
// into the class so the CSS -- not the row builder -- owns the palette.
function tierChipClass(watts) {
  if (!watts) return ' unknown';
  return ' t-' + Math.round(Number(watts));
}

function portOf(url) {
  const m = /:(\d+)(?:[/?]|$)/.exec(String(url || ''));
  return m ? m[1] : '';
}

// One compact chip per source: which llamacpp is up and on what port.  The
// preset inventory used to live here; the user only needs "which server,
// which model", so the rest of this section is a status line.
function endpointChip(endpoint) {
  const label = endpoint.live ? 'spawn 子 server' : endpoint.label;
  const port = portOf(endpoint.base_url);
  return '<span class="src-chip' + (endpoint.up ? '' : ' down') + '">' +
    '<span class="src-name">' + esc(label) + '</span>' +
    (port ? '<span class="src-port">:' + esc(port) + '</span>' : '') +
    '<span class="src-state">' + (endpoint.up ? '在线' : '离线') + '</span>' +
    '</span>';
}

// Why the live probe found nothing, said in the panel's own words.
//
// The empty model card is the most confusing state this dashboard has: it looks
// identical whether no model is loaded, the router log moved, or the path in
// config.json never existed at all.  Only the collector knows which, so its
// reason code and the path it actually opened both go on screen -- otherwise
// the answer lives in run.log and the card just looks broken.
const LIVE_REASON = {
  'log missing': '日志文件不存在',
  'log empty': '日志是空的',
  'log unreadable': '日志读不出来',
  'no proxy line in tail': '日志尾部没有模型请求记录',
  'router reports no loaded model': 'router 上没有任何预设处于加载状态',
  'router unreachable': 'router 没有应答',
};

// The reasons that came from asking the router rather than from reading a file.
// These are the ones where the address matters and the log path does not.
const ROUTER_REASON = {
  'router unreachable': true,
  'router reports no loaded model': true,
};

function liveNote(endpoint) {
  if (!endpoint || !endpoint.error) return '';
  if (endpoint.covered_by) {
    // Silence, rather than a referral line.  A covered address means another
    // card is already polling it and has put its model on screen -- usually
    // with the "running" tag, because the hub reports that owner as the
    // current endpoint.  So "当前模型由 strata 卡代为显示" only told the reader
    // what the card above it already said, and it did so in a card of its
    // own, which read as a stray empty panel (user request 2026-10-23).
    // Not falling through either: this card's own "no active model" is the
    // half-truth the referral existed to replace, so printing it here would
    // be worse than printing nothing.
    return '';
  }
  if (endpoint.error === 'no active model') {
    const why = LIVE_REASON[endpoint.log_reason] || endpoint.log_reason || '原因未知';
    // Show where we looked, and show the right thing: a log-shaped answer is
    // about a file, so it names the path; an answer that came from the router
    // names the address that was asked.  Printing the log path beside a router
    // verdict would send the reader to a file that is not the problem -- and a
    // stale address is exactly what "router 没有应答" almost always means.
    let where = '';
    if (ROUTER_REASON[endpoint.log_reason]) {
      if (endpoint.router_url) {
        where = '；已询问 <code title="' + esc(endpoint.router_url) + '">' +
          esc(endpoint.router_url) + '</code>';
      }
    } else if (endpoint.log_path) {
      where = '；查找位置 <code title="' + esc(endpoint.log_path) + '">' +
        esc(shortPath(endpoint.log_path)) + '</code>';
    }
    return '<div class="err">未找到当前模型：' + esc(why) + where + '</div>';
  }
  // A model is named as loaded, but nothing answers on its port yet -- the
  // normal state for the seconds a child takes to come up, and otherwise
  // indistinguishable from a broken panel.
  if (endpoint.target_model) {
    return '<div class="err">router 已把 <code>' + esc(endpoint.target_model) +
      '</code>（:' + esc(endpoint.target_port) + '）标记为已加载，但该端口还没应答 —— ' +
      '正在加载中，通常几秒后这里会出现实时指标。</div>';
  }
  return '<div class="err">' + esc(endpoint.error) + '</div>';
}

const MODEL_SPECS = [
  cardSpec('decode', '生成速度', {
    bar: true,
    tip: 'Δ已生成 token ÷ Δ解码耗时，分母取引擎自己计时的耗时而不是墙钟，' +
      '否则空闲秒数会被摊进窗口、读数偏低。引擎空闲时不产生新耗时，' +
      '所以数字会退回累计均值并带 ≈ 前缀。' +
      '血条是当前速度 ÷ 本会话峰值速度；空闲时为 0。',
  }),
  cardSpec('prefill', 'Prefill 速度', {
    bar: true,
    tip: 'Δ提示 token ÷ Δ预填充耗时。只在读入提示词时才明显上升；' +
      '纯输出阶段它不动是正常的，不是采集失败。' +
      '血条是当前速度 ÷ 本会话峰值速度。',
  }),
  cardSpec('mtp', 'MTP 命中率', {
    bar: true,
    tip: '投机解码接受率 = 被接受的草稿 token ÷ 草稿 token 总数。' +
      '越高说明 MTP 猜得越准、加速越明显。低于 50% 进度条转黄。' +
      '注意：是否启用 MTP 看的是启动参数里的 --spec-type，' +
      '因为 /props 的 speculative.types 在 MTP 实际运行时也可能报 none。',
  }),
  cardSpec('kv', 'KV 缓存命中', {
    bar: true,
    tip: '前缀缓存命中率 = 命中缓存的 token ÷ 全部提示 token。' +
      '多轮对话里重复前缀越多这个值越高，命中的部分不需要重新 prefill。',
  }),
  cardSpec('ctx', '上下文占比', {
    bar: true,
    tip: '各 slot 里已用上下文的最大值（提示词 + 已生成）÷ 模型上下文上限。' +
      '超过 85% 变红，再涨就该清理对话了。' +
      'slot 空闲时服务端会清掉提示词计数，此时数字只剩上次请求的已生成残留，' +
      '脚注会标出"空闲残留"。',
  }),
  cardSpec('ttft', 'TTFT', {
    bar: true,
    tip: '首 token 延迟 = 引擎实测：从请求进到吐出第一个 token 用了几秒，含排队和读提示词。' +
      '数值是最近一个已完成请求，脚注给引擎留存样本的平均值与个数。' +
      'Strata 的 /metrics 请求历史里有这一项；llama.cpp 没有，显示 —。',
  }),
];

function applyModel(root, endpoint, model) {
  const metrics = model.metrics || {};
  const counters = metrics.counters || {};
  const slots = model.slots || [];
  const modelId = model.id || 'model';
  const base = endpoint.id + '/' + modelId;
  const cards = root.querySelector('[data-role="cards"]');
  if (!cards) return;
  const refs = ensureCards(cards, MODEL_SPECS);
  if (!refs) return;

  const decodeSeries = seriesOf(base + '.decode');
  const prefillSeries = seriesOf(base + '.prefill');
  const ctxSeries = seriesOf(base + '.ctx');

  // An idle engine reports no new tokens, so the instantaneous rate is 0.
  // Showing that as the headline reads like a stall; fall back to the
  // cumulative average and mark it with "≈" so the number is not taken for a
  // live reading.
  const decodeNow = metrics.decode_tps_inst
    ? num(metrics.decode_tps_inst, 1)
    : '≈' + num(metrics.decode_tps_avg, 1);
  const prefillNow = metrics.prefill_tps_inst
    ? num(metrics.prefill_tps_inst, 0)
    : '≈' + num(metrics.prefill_tps_avg, 0);

  const ctxRatio = maxOf(slots, 'ctx_ratio');
  const mtpRatio = metrics.mtp_accept_ratio;
  const kvRatio = metrics.kv_hit_ratio;
  const stepValue = metrics.mtp_tokens_per_step;
  const ctxTight = (ctxRatio || 0) > 0.85;
  const mtpEnabled = !!model.mtp_enabled;
  const drafts = counters.draft_tokens;
  const draftSeen = drafts !== null && drafts !== undefined && drafts > 0;
  // The slot drops its prompt counters once a request finishes, so an idle
  // engine reports a partially cleared context.  Say so rather than passing the
  // residue off as a live watermark.
  const firstSlot = slots[0] || {};
  const slotLive = !!firstSlot.is_processing ||
    (firstSlot.n_prompt_tokens !== null && firstSlot.n_prompt_tokens !== undefined);
  const decodePeak = rollingPeakOf(decodeSeries, S.history.timestamps);
  const prefillPeak = rollingPeakOf(prefillSeries, S.history.timestamps);
  const ttftLast = metrics.ttft_s_last;
  const ttftSeries = seriesOf(base + '.ttft');
  const ttftPeak = rollingPeakOf(ttftSeries, S.history.timestamps);

  // A rate has no natural 0–100% range, so the bar measures "how close to
  // this session's best": current rate ÷ session peak.  An idle engine
  // (inst = 0) reads empty on purpose — the ≈ headline already says it is
  // not a live number.
  const decodeBar = (metrics.decode_tps_inst === null || metrics.decode_tps_inst === undefined || !decodePeak)
    ? null : Math.max(0, Math.min(1, metrics.decode_tps_inst / decodePeak));
  const prefillBar = (metrics.prefill_tps_inst === null || metrics.prefill_tps_inst === undefined || !prefillPeak)
    ? null : Math.max(0, Math.min(1, metrics.prefill_tps_inst / prefillPeak));

  const rateFoot = function (average, peak, digits) {
    return '均值 ' + num(average, digits) + ' t/s' +
      (peak === null ? ' · 暂无曲线' : ' · 峰值 ' + num(peak, digits));
  };

  paintCards(refs, {
    decode: {
      text: decodeNow, unit: 't/s', bar: decodeBar,
      barClass: 't-a',
      foot: rateFoot(metrics.decode_tps_avg, decodePeak, 1),
      series: decodeSeries,
      sparkOptions: {
        min: 0, color: cssVar('--chart-a'), autoscale: true,
        format: function (v) { return num(v, 0); },
      },
    },
    prefill: {
      text: prefillNow, unit: 't/s', bar: prefillBar,
      barClass: 't-b',
      foot: rateFoot(metrics.prefill_tps_avg, prefillPeak, 0),
      series: prefillSeries,
      sparkOptions: {
        min: 0, color: cssVar('--chart-b'), autoscale: true,
        format: function (v) { return num(v, 0); },
      },
    },
    mtp: {
      text: pct(mtpRatio), bar: mtpRatio,
      barClass: (mtpRatio === null || mtpRatio === undefined) ? '' : (mtpRatio >= 0.5 ? 'good' : 'warn'),
      foot: draftSeen && stepValue !== null && stepValue !== undefined
        ? '每步接受 ' + num(stepValue, 2) + ' tok'
        : (mtpEnabled ? '已启用 ' + (model.mtp_type || '') + ' · 暂无每步数据' : '未启用 MTP'),
      series: seriesOf(base + '.mtp'),
      sparkOptions: {
        min: 0, max: 1, color: cssVar('--chart-d'), autoscale: true,
        format: function (v) { return num(v * 100, 0) + '%'; },
      },
    },
    kv: {
      text: pct(kvRatio), bar: kvRatio, barClass: 't-c',
      foot: '累计命中 ' + countText(counters.prompt_cached) + ' tok',
      series: seriesOf(base + '.kv'),
      sparkOptions: {
        min: 0, max: 1, color: cssVar('--chart-c'), autoscale: true,
        format: function (v) { return num(v * 100, 0) + '%'; },
      },
    },
    ctx: {
      text: pct(ctxRatio), bar: ctxRatio,
      barClass: ctxTight ? 'bad' : 't-g',
      foot: '上限 ' + countText(model.n_ctx) + ' tok' +
        (ctxTight ? ' · 接近溢出' : '') + (slotLive ? '' : ' · 空闲残留'),
      series: ctxSeries,
      sparkOptions: {
        min: 0, max: 1, color: ctxTight ? cssVar('--bad') : cssVar('--chart-g'), autoscale: true,
        format: function (v) { return num(v * 100, 0) + '%'; },
      },
    },
    ttft: {
      text: num(ttftLast, 1),
      // A dash is not a reading, so it carries no unit (the slot cards already
      // follow this rule).
      unit: (ttftLast === null || ttftLast === undefined) ? '' : 's',
      // For a latency the bar means "how close to this session's worst", so a
      // long fill warns instead of flattering -- the opposite direction from
      // the rate cards, which measure distance to the best.
      bar: (ttftLast === null || ttftLast === undefined || !ttftPeak)
        ? null : Math.max(0, Math.min(1, ttftLast / ttftPeak)),
      barClass: 't-h',
      foot: metrics.ttft_n
        ? '均值 ' + num(metrics.ttft_s_mean, 1) + ' s · ' + metrics.ttft_n + ' 个请求'
        : '暂无已完成请求',
      series: ttftSeries,
      sparkOptions: {
        min: 0, color: cssVar('--chart-h'), autoscale: true,
        format: function (v) { return num(v, 1); },
      },
    },
  });

  const detail = root.querySelector('[data-role="detail"]');
  if (detail) {
    // The row is rebuilt on every tick; carry the user's expand choice over so
    // opening the per-position list does not snap shut two seconds later.
    const openDetail = detail.querySelector('details');
    const wasOpen = !!(openDetail && openDetail.open);
    // The spec tags live on the hero line (see modelTags).  This row used to
    // carry the engine's whole live block; the state note and the
    // 累计均值 / prefill 均值 / 处理中 chips moved to the slot 明细 title
    // line on 2026-10-19, and what is left (sequence peak, batch slots, MTP
    // per-position, collection errors) only appears when the engine actually
    // reported it -- nothing here draws a bare "—".
    const items = [];
    if (typeof metrics.n_tokens_max === 'number') {
      items.push({ label: '序列峰值', value: countText(metrics.n_tokens_max), unit: 'tok' });
    }
    if (typeof metrics.busy_slots_per_decode === 'number') {
      items.push({ label: '批内 slot', value: num(metrics.busy_slots_per_decode, 2), unit: '个' });
    }
    const positions = metrics.mtp_positions || {};
    const posKeys = Object.keys(positions).sort(function (a, b) {
      return Number(a) - Number(b);
    });
    // The per-position acceptance list is long (ngram drafts report every
    // position), so it folds away by default and only expands on demand.
    if (posKeys.length) {
      items.push({
        label: 'MTP 逐位命中',
        value: posKeys.length + ' 位',
        title: '展开看每一位草稿的接受率',
        expand: true,
        children: posKeys.map(function (p) {
          return { label: '第' + (Number(p) + 1) + '位', value: pct(positions[p]) };
        }),
      });
    }
    // The weights chip is gone: the file name repeats what the hero name and
    // the engine already say, and the full path only added a breadcrumb.
    if (model.metrics_error) {
      items.push({ label: '指标采集失败', value: model.metrics_error, wide: true, alert: true });
    }
    if (model.slots_error) {
      items.push({ label: 'slot 采集失败', value: model.slots_error, wide: true, alert: true });
    }
    detail.innerHTML = kvChips(items);
    renderSlotDetail(root.querySelector('[data-role="slots"]'), model, base);
  }
}

// ---------------------------------------------------------------- slot detail

// The per-slot breakdown: one collapsible section under the model card, one
// row per batch slot, six small cards each.  The engine has always reported
// this (llama.cpp's /slots, Strata's /metrics live.slots) and the collector
// always forwards it -- until now the panel only read the largest
// ctx_ratio out of the array, so two of three slots were rendered and thrown
// away.  With parallel requests running, which slot holds the long context
// and what each one is actually generating at is the interesting question,
// and "max of three" cannot answer it.
//
// Folded OPEN by default: the user asked to see the per-slot breakdown
// without a click, and each row is one line of six small cards.  The cards
// are value + horizontal health bar only -- no curve (2026-10-19 request).
// The right-hand end of the slot 明细 title line (2026-10-19 request): three
// live chips (累计均值 / prefill 均值 / 处理中) and then the small state note
// ("writing a tool call: thinking · 8198 tok @ 40 t/s") that used to sit on
// the model card's own row.  Repainted every tick -- these numbers change
// that fast, and the old count sentence freezing on the building frame is
// exactly the bug this shape avoids.
function slotLiveHtml(model) {
  const metrics = model.metrics || {};
  const chips = [
    { label: '累计均值', value: num(metrics.decode_tps_avg, 1), unit: 't/s' },
    { label: 'prefill 均值', value: num(metrics.prefill_tps_avg, 0), unit: 't/s' },
  ];
  if (typeof metrics.requests_processing === 'number') {
    chips.push({ label: '处理中', value: num(metrics.requests_processing, 0), unit: '请求' });
  }
  const note = stateNoteText(model);
  return kvChips(chips) + (note ? '<span class="kv-note">' + esc(note) + '</span>' : '');
}

function renderSlotDetail(host, model, base) {
  if (!host) return;
  const slots = model.slots || [];
  if (slots.length < 1) {
    // Blank here is what made this section look broken: a loaded model whose
    // engine simply does not export per-slot rows (older build, single mode) or
    // whose slot dump failed to fetch left nothing at all -- no heading, no
    // reason, and no way to tell "not collected" from "collector crashed".
    // Say which one it is whenever the panel knows.
    const why = [];
    if (model.slots_error) why.push('采集失败：' + model.slots_error);
    if (typeof model.total_slots === 'number') {
      why.push('配置里有 ' + model.total_slots + ' 个槽，但引擎这一轮没有上报逐槽数据');
    } else {
      why.push('引擎这一轮没有上报逐槽数据');
    }
    host.innerHTML = '<div class="slot-fold is-empty">' +
      '<div class="empty">' + esc('slot 明细 · ' + why.join('；')) + '</div>' +
      '<span class="slot-live">' + slotLiveHtml(model) + '</span>' +
      '</div>';
    host.dataset.sig = '';
    return;
  }

  // Rebuild only when the shape changes: the row count and each slot's id.
  // The values inside are repainted every tick without touching the DOM, so
  // the fold state and the canvases survive.
  const signature = 'slots:' + slots.length + ':' +
    slots.map(function (s) { return s.id; }).join(',');
  if (host.dataset.sig !== signature) {
    host.dataset.sig = signature;
    host.innerHTML =
      '<details class="collapsible slot-fold" data-tone="h" open>' +
      '<summary><h3>slot 明细</h3><span class="slot-live" data-role="live"></span></summary>' +
      '<div class="slot-rows">' +
      slots.map(function (slot) {
        // No title line: the slot's identity (id + state) rides inside the
        // leading 已生成 card instead, one line per row (2026-10-19).
        return '<div class="slot-row" data-slot="' + esc(String(slot.id)) + '">' +
          '<div class="slot-cards grid compact" data-role="cards"></div>' +
          '</div>';
      }).join('') +
      '</div></details>';
  }

  // Same job as applyModel, one row at a time: the slot cards are built by the
  // shared buildCards/paintCards pair.  Each card is label + value + a
  // horizontal health bar, and the leading 已生成 card carries the slot's id
  // and engine state inline in its label row -- the row has no title of its
  // own anymore (2026-10-19).
  //
  // The "N 个槽 · M 个在跑" count sentence is gone (2026-10-19 request): the
  // title line carries the engine's live readings instead (see slotLiveHtml),
  // repainted every tick the same way, and the solo-path explanation survives
  // only on the state chip's tooltip.
  const liveState = model.live_state || {};
  const soloPath = !slots.some(function (s) { return s.is_processing; }) &&
    Number(liveState.running) > 0;
  const liveEl = host.querySelector('[data-role="live"]');
  if (liveEl) liveEl.innerHTML = slotLiveHtml(model);

  host.querySelectorAll('.slot-row').forEach(function (row) {
    const id = row.getAttribute('data-slot');
    const slot = slots.filter(function (s) { return String(s.id) === id; })[0];
    if (!slot) return;
    const cardsHost = row.querySelector('[data-role="cards"]');
    const refs = ensureCards(cardsHost, SLOT_SPECS);
    if (!refs) return;
    paintCards(refs, slotCardValues(slot, base, id));
    // The row's identity: slot id + engine state, inline in the label row of
    // the leading 已生成 card (title line and placeholder both retired).
    const tagRef = refs.get('decoded');
    const tagEl = tagRef && tagRef.tag;
    if (tagEl) {
      const label = slot.is_processing ? (slot.state || 'processing') : 'idle';
      tagEl.innerHTML = '<span class="slot-id">slot ' + esc(String(slot.id)) + '</span>' +
        '<span class="slot-state' + (slot.is_processing ? ' on' : '') + '">' +
        esc(label) + '</span>';
      // "idle" here is the engine's word, and under a lone request it is not
      // the whole story -- say so where the user is already looking.
      tagEl.lastChild.title = soloPath ? SOLO_PATH_NOTE : '';
    }
  });
}

// One slot's six readings, in the shape paintCards expects: headline, rail
// ratio, and a tooltip that carries the live breakdown.  No trend series: the
// slot cards dropped their curves on 2026-10-19, so only the rate cards still
// read history (for their peak-relative rail).
//
// Order since 2026-10-19: 已生成 first (it carries the slot tag), then
// 生成速度 / Prefill 速度 / 上下文 / 上下文占比 / KV 缓存命中.  已生成 stands
// in for the headline's MTP 每步接受 -- MTP counters are engine-global
// (Strata's C++ sums drafts across slots and never says which slot a draft
// belonged to), so a per-slot MTP card would be the same number three times
// over.  已生成 is the one reading that is genuinely per slot and has no
// headline equivalent.
//
// Every field is optional.  An idle Strata slot reports held_tokens and nothing
// else (server.py's slots_view idle branch has a single length to give), so the
// row shows what it has and "—" for the rest rather than zeros that read like
// real measurements -- and the trend skips those samples instead of diving to
// the floor, because a null sample is dropped rather than drawn as a zero.
const SLOT_SPECS = [
  // 已生成 leads (2026-10-19): it is the card that carries the slot's
  // identity tag (slot id + engine state), so the row's own title line was
  // dropped and every slot row is one line shorter.
  cardSpec('decoded', '已生成', {
    barDir: 'h', spark: false, tag: true,
    tip: '这个 slot 已经生成的 token 数。',
  }),
  cardSpec('tok_s', '生成速度', {
    bar: true, barDir: 'h', spark: false,
    tip: '该 slot 自己的生成速度 = 已生成 ÷ 首个 token 之后的秒数，' +
      '就是社区 tg 那个公式，但**按槽**而非跨槽求和 —— 并发时仍能与单请求基准比较。' +
      'Strata 只在槽忙时上报；llama.cpp 的 /slots 没有这一项，显示 — 而不是 0。' +
      '血条是该槽当前速度 ÷ 该槽本会话峰值。',
  }),
  cardSpec('prefill', 'Prefill 速度', {
    bar: true, barDir: 'h', spark: false,
    tip: '该 slot 读入提示词的速率。引擎只报一个全局 prefill 速率，' +
      '但它一次只读一个提示词（控制行是信号量），所以正在 reading 的那个槽就是这条速率的主人。' +
      '没有槽在读、或同时有几个在读时不猜，显示 —。',
  }),
  cardSpec('ctx', '上下文', {
    bar: true, barDir: 'h', spark: false,
    tip: '这个 slot 手上留着的 token 数：在跑时是「提示词 + 已生成」，' +
      '空闲时是服务端为下一轮保留的那段前缀。' +
      '血条是它占模型上下文上限的比例（下一张卡是同一个数的百分比读法）。',
  }),
  cardSpec('ratio', '上下文占比', {
    bar: true, barDir: 'h', spark: false,
    tip: '该 slot 留着的 token ÷ 模型上下文上限。超过 85% 转红。' +
      '注意这是**单个** slot 的占比，主卡的「上下文占比」取的是各 slot 的最大值。' +
      '低于 2% 时血条按 2% 画，否则横条上的 0.2% 根本看不见——数字本身没被改。',
  }),
  cardSpec('kv', 'KV 缓存命中', {
    bar: true, barDir: 'h', spark: false,
    tip: '这个 slot 本轮提示词里，前缀缓存直接给回、不需要重新 prefill 的比例。' +
      'llama.cpp 的 /slots 直接报这个拆分；Strata 按槽不报，这里取的是「该槽上一轮留下的前缀 ÷ 本轮提示词」——' +
      '正是引擎 pick_slot 复用的那一段。主卡的 KV 缓存命中是全局累计口径，两者算法不同，不必相等。',
  }),
];

// The smallest rail a nonzero reading can draw.  The bar is 6px wide and fills
// from the bottom, so a 0.2% context share is 0.1px of height: invisible, which
// reads as "no data" on a card that has data.  The floor is a visibility floor
// on the drawing only -- the headline number and the curve stay exact.
const BAR_VISIBLE_MIN = 0.02;

function railHeight(ratio) {
  if (ratio === null || ratio === undefined) return '0%';
  const clamped = Math.max(0, Math.min(1, ratio));
  if (clamped === 0) return '0%';
  return (Math.max(clamped, BAR_VISIBLE_MIN) * 100) + '%';
}

function slotCardValues(slot, base, slotId) {
  // Only the two rate cards read history, and only for their peak-relative
  // rail (see the comments below); prefix keys those series.
  const prefix = base + '.slot' + slotId;
  const out = {};
  const used = slot.ctx_used;
  const busy = !!slot.is_processing;
  const ceiling = slot.n_ctx;

  out.ctx = {
    text: used === null || used === undefined ? '—' : countText(used),
    unit: used === null || used === undefined ? '' : 'tok',
    foot: ceiling ? '上限 ' + countText(ceiling) + ' tok' : '',
    bar: (used === null || used === undefined || !ceiling) ? null : used / ceiling,
    barClass: (ceiling && used / ceiling > 0.85) ? 'bad' : 't-c',
  };

  const ratio = slot.ctx_ratio;
  out.ratio = {
    text: ratio === null || ratio === undefined ? '—' : pct(ratio),
    bar: ratio,
    barClass: (ratio !== null && ratio !== undefined && ratio > 0.85) ? 'bad' : 't-c',
    foot: busy ? '本槽占比' : '空闲残留',
  };

  out.decoded = {
    text: slot.n_decoded === null || slot.n_decoded === undefined
      ? '—' : countText(slot.n_decoded),
    unit: slot.n_decoded === null || slot.n_decoded === undefined ? '' : 'tok',
    foot: busy ? '本轮已出字' : '空闲无请求',
  };

  // The slot's own rate, when the engine reports one.  The rail is "how close
  // to this slot's own best", the same reading the headline rate card gives --
  // a rate has no natural 0-100% range, so the peak is the only honest full
  // scale.  Idle slots report no rate at all, and neither does llama.cpp's
  // /slots, so both show an empty rail and "—" rather than 0.
  const rate = slot.tok_s;
  const rateSeries = seriesOf(prefix + '.tok_s');
  const ratePeak = rollingPeakOf(rateSeries, S.history.timestamps);
  out.tok_s = {
    text: typeof rate === 'number' ? num(rate, 1) : '—',
    unit: typeof rate === 'number' ? 't/s' : '',
    bar: (typeof rate !== 'number' || !ratePeak) ? null
      : Math.max(0, Math.min(1, rate / ratePeak)),
    barClass: 't-a',
    foot: typeof rate !== 'number'
      ? '引擎未上报'
      : ('峰值 ' + num(ratePeak, 1) + ' t/s'),
  };

  // Prefill is attributed by the collector (see strata.py's _slots): the
  // engine reads one prompt at a time, so the global rate belongs to the slot
  // that is reading.  Same rail rule as the decode card -- this slot's own
  // session peak is the full scale.
  const prefill = slot.prefill_tok_s;
  const prefillSeries = seriesOf(prefix + '.prefill');
  const prefillPeak = rollingPeakOf(prefillSeries, S.history.timestamps);
  out.prefill = {
    text: typeof prefill === 'number' ? num(prefill, 0) : '—',
    unit: typeof prefill === 'number' ? 't/s' : '',
    bar: (typeof prefill !== 'number' || !prefillPeak) ? null
      : Math.max(0, Math.min(1, prefill / prefillPeak)),
    barClass: 't-b',
    foot: typeof prefill !== 'number'
      ? (busy ? '不在读提示词' : '空闲无请求')
      : ('峰值 ' + num(prefillPeak, 0) + ' t/s'),
  };

  const kv = slot.kv_hit;
  out.kv = {
    text: typeof kv === 'number' ? pct(kv) : '—',
    bar: typeof kv === 'number' ? kv : null,
    barClass: typeof kv === 'number' ? 't-d' : '',
    foot: typeof kv === 'number' ? '' : (busy ? '引擎未上报' : '空闲无请求'),
  };
  return out;
}

function renderEndpointModels(container, endpoint) {
  const models = endpoint.models || [];
  if (!models.length) {
    container.innerHTML = '';
    return;
  }

  // The model represented by the hero line above already has its name, port
  // and spec tags on that row, so it renders without a repeated title.
  const heroId = container.dataset.heroModel || '';

  container.innerHTML = models.map(function (model) {
    // /props reports speculative.types as "none" even while a draft-mtp preset
    // runs, so the tag follows the launch argument instead.
    const title = model.id === heroId
      ? ''
      : '<div class="model-title"><span class="id">' + esc(shortModelName(model.id)) + '</span>' + modelTags(model) + '</div>';
    return '<div class="model" data-model="' + esc(model.id || 'model') + '" data-fade>' +
      title +
      '<div class="grid" data-role="cards"></div>' +
      '<div class="kv" data-role="detail"></div>' +
      '<div data-role="slots"></div>' +
      '</div>';
  }).join('');

  // Trigger fadeIn animation on new/updated models
  container.querySelectorAll('[data-fade]').forEach(function (m) {
    m.classList.add('fade-in');
  });

  models.forEach(function (model) {
    const root = container.querySelector('[data-model="' + CSS.escape(model.id || 'model') + '"]');
    if (root) applyModel(root, endpoint, model);
  });
}

function renderEndpoints(snapshot) {
  const container = document.getElementById('endpoints');
  const endpoints = snapshot.endpoints || [];
  // The section answers one question: which llamacpp is serving which model
  // right now.  Every source with a loaded model gets its own card -- more
  // than one server can serve at once (hermes and Strata do) -- and every
  // source (router / loader / spawned child) is also reduced to one status
  // chip in the section head.
  const live = endpoints.find(function (e) { return e.live; }) || null;
  // Endpoints that share an id (hermes router + the child the router spawned)
  // fold into one source: the live endpoint carries real metrics, the router
  // carries preset metadata, and together they make one combined model list.
  //
  // The two are not equal partners, though.  The spawned child exists only
  // while a model is loaded, so its "connection refused" is the idle state
  // rather than a failure -- reading it as the source's own status made a
  // perfectly healthy router show up as 离线, named after a process that had
  // already exited.  So the source is up when *any* of its endpoints answers,
  // and its identity (label, port) follows the one with something to show.
  const byId = {};
  endpoints.forEach(function (e) {
    (byId[e.id] = byId[e.id] || []).push(e);
  });
  const deduped = Object.keys(byId).map(function (id) {
    const list = byId[id];
    const livePart = list.find(function (e) { return e.live; }) || null;
    // A live child leads only while it has a model to show; a model loaded
    // elsewhere in the meantime would otherwise be described by a dead port.
    const primary = (livePart && (livePart.models || []).length ? livePart : null) ||
      list.find(function (e) { return !e.live; }) || livePart || list[0];
    const out = {
      id: id,
      label: primary.label,
      base_url: primary.base_url,
      up: list.some(function (e) { return e.up; }),
      live: false,  // internal distinction, never a label of its own
      models: [],
      error: null,
      backing_off: list.some(function (e) { return e.backing_off; }),
      polled_at: primary.polled_at,
      mode: primary.mode,
    };
    list.forEach(function (e) {
      if (e.error && !out.error) out.error = e.error;
    });
    // The engine build banner rides the merged record too: any member may be
    // the one whose /props answered (the router probes itself), so the first
    // non-empty one wins — dropping it here silenced every version annotation
    // while the raw payload still carried it (caught by render_check).
    list.forEach(function (e) {
      if (e.engine_version && !out.engine_version) out.engine_version = e.engine_version;
    });
    list.forEach(function (e) {
      (e.models || []).forEach(function (m) {
        // Avoid duplicate models by id across endpoints
        const already = out.models.some(function (mm) { return mm.id === m.id; });
        if (already) {
          // The router's model record carries preset metadata (e.g. mtp_type
          // from launch args) that a live child cannot report — backfill it
          // into the merged record so tags match the running server.
          out.models.forEach(function (mm) {
            if (mm.id !== m.id) return;
            if (m.mtp_enabled) mm.mtp_enabled = true;
            if (m.mtp_type && (!mm.mtp_type || mm.mtp_type === 'none')) mm.mtp_type = m.mtp_type;
          });
          return;
        }
        out.models.push(m);
      });
    });
    return out;
  });
  // Every server with a loaded model gets its own card.  Taking only the
  // first one dropped the others entirely: with hermes and strata both up,
  // strata's chip said 在线 while its model, rates and context sat on no
  // card at all -- the section described one server on a machine running two.
  const serving = deduped.filter(function (e) { return (e.models || []).length; });
  // The "当前模型" hero belongs to the source hermes is actually using -- the
  // one the live tracker follows -- not to whichever endpoint happens to come
  // first in config.json.  When the live card yields to a direct server (one
  // address, one card), that server becomes the current one.
  const current = snapshot.current_endpoint_id
    ? serving.find(function (e) { return e.id === snapshot.current_endpoint_id; }) || null
    : (live && serving.find(function (e) { return e.id === live.id; })) || serving[0] || null;
  // The reason line belongs in the signature too: a wrong log path changes
  // nothing else on screen, so without it the card would keep its first
  // explanation for the rest of the run.
  const signature = deduped.map(function (e) {
    return e.id + '@' + e.up + '#' + modelSignature(e) + '!' + (e.error || '');
  }).join('|') + '|current=' + (current ? current.id : '');

  // The source chips live in the section head (right-aligned), so they are
  // refreshed on their own tick -- up/down state changes without rebuilding
  // the model card below.  Online servers only (user request 2026-10-19):
  // offline chips were pure noise -- the card area below already says when
  // nothing is serving.
  // Engine-build map for the ledger tables (see rowVerHtml). Endpoints that
  // share an id are already deduped here; last online writer wins, which is
  // the same rule the merged card uses.
  S_VERSIONS = {};
  deduped.forEach(function (e) {
    if (e.up && e.engine_version) S_VERSIONS[String(e.id).toLowerCase()] = e.engine_version;
  });

  const sourcesBox = document.getElementById('endpoint-sources');
  if (sourcesBox) {
    const online = deduped.filter(function (e) { return e.up; });
    const srcSig = online.map(function (e) { return e.id + '@' + e.up; }).join('|');
    if (sourcesBox.dataset.sig !== srcSig) {
      sourcesBox.dataset.sig = srcSig;
      sourcesBox.innerHTML = online.map(endpointChip).join('');
    }
  }

  if (container.dataset.signature !== signature) {
    container.dataset.signature = signature;
    // The hero line carries the identity only: label + name + port + state.
    // The spec tags moved down to the detail row (see modelTags), so the state
    // pill sits right next to the name instead of being pushed off the row.
    const note = liveNote(live);
    const blocks = serving.map(function (e) {
      const heroModel = (e.models || [])[0] || null;
      const hero = heroModel
        ? '<div class="hero" data-role="hero">' +
          '<span class="hero-label">' + esc(e === current ? 'running' : e.label) + '</span>' +
          '<span class="hero-name">' + esc(shortModelName(heroModel.id)) + '</span>' +
          '<span class="hero-port">' + esc(portOf(e.base_url) || '?') + '</span>' +
          modelTags(heroModel) +
          (e.engine_version
            ? '<span class="engine-ver" title="当前服务这个模型的引擎构建版本">' + esc(e.engine_version) + '</span>'
            : '') +
          '<span data-role="hero-lamps"></span>' +
          '</div>'
        : '';
      // The live tracker's failure note belongs on its own source's card:
      // printing it on another server's card would blame the wrong machine.
      const own = (live && e.id === live.id) ? note : '';
      return '<div class="endpoint' + (e === current ? ' is-live' : '') + '"' +
        ' data-endpoint="' + esc(e.id) + '">' +
        hero + own +
        '<div data-role="models" data-hero-model="' + esc(heroModel ? heroModel.id : '') + '"></div>' +
        '</div>';
    }).join('');
    // A live tracker that found nothing has no card of its own -- and its
    // explanation is the whole point of that state (the blank card must say
    // why, or it reads exactly like "the server is down").  Give the note a
    // home of its own rather than dropping it or parking it on another
    // server's card.
    const stranded = blocks && note &&
      !serving.some(function (e) { return e.id === live.id; })
      ? '<div class="endpoint">' + note + '</div>'
      : '';
    container.innerHTML = blocks + stranded ||
      '<div class="endpoint">' +
      '<div class="empty">当前没有已加载的模型 —— 服务在线时指标会自动出现在这里。</div>' +
      note + '</div>';
  }

  serving.forEach(function (e) {
    const root = container.querySelector('[data-endpoint="' + CSS.escape(e.id) + '"]');
    if (!root) return;
    const before = root.dataset.modelsSig || '';
    const after = modelSignature(e);
    if (before !== after) {
      root.dataset.modelsSig = after;
      renderEndpointModels(root.querySelector('[data-role="models"]'), e);
    } else {
      // Values refresh on every tick and the DOM is reused, so the canvases
      // (and their accumulated scale) survive.
      (e.models || []).forEach(function (model) {
        const modelRoot = root.querySelector(
          '[data-model="' + CSS.escape(model.id || 'model') + '"]');
        if (modelRoot) applyModel(modelRoot, e, model);
      });
    }
    // The engine's state lamps change faster than the block signature (idle
    // and generating share one card), so they get their own per-tick slot.
    const lampBox = root.querySelector('[data-role="hero-lamps"]');
    if (lampBox) {
      const html = heroLampsHtml((e.models || [])[0]);
      if (lampBox._sig !== html) {
        lampBox._sig = html;
        lampBox.innerHTML = html;
      }
    }
  });
}

const SYS_SPECS = [
  cardSpec('util', '显卡利用率', {
    bar: true,
    tip: 'nvidia-smi 的 utilization.gpu，采样区间内 SM 处于工作状态的时间占比。' +
      '批量推理时它常常很低，因为瓶颈在显存带宽而不是算力。',
  }),
  cardSpec('mem', '显存占用', {
    bar: true,
    tip: '已用显存 ÷ 总显存。脚注里的上限就是这张卡的全部显存。' +
      '注意 WDDM 驱动下 nvidia-smi 不给每进程显存。',
  }),
  cardSpec('temp', '核心温度', {
    bar: true,
    tip: 'GPU 核心温度。进度条按 30–90°C 换算占用比例，' +
      '75°C 转黄、83°C 转红并在脚注直接写明温度偏高。',
  }),
  cardSpec('power', '功耗', {
    bar: true,
    tip: '当前功耗 ÷ 功耗上限。上限优先取软件功耗墙（nvidia-smi -pl 写进去的那个）；' +
      '笔记本 GPU 根本不上报软件墙，那里显示的是驱动实际生效的上限，' +
      '由 Dynamic Boost 在最大 TGP 之内动态调整，所以会在一个区间里浮动。' +
      '接近上限说明这张卡已经在全力跑。档位切换在顶栏更新时间左边。',
  }),
  cardSpec('cpu', 'CPU 负载', {
    bar: true,
    tip: 'GetSystemTimes 两次采样差分算出的整机 CPU 占用。' +
      'Windows 的 kernel time 已经包含 idle，所以忙碌比是 (kernel + user − idle) ÷ (kernel + user)。' +
      '这是整机总负载，不是单个核心。',
  }),
  cardSpec('commit', '提交内存', {
    bar: true,
    tip: 'commit charge = 物理内存 + 页面文件已经"承诺"出去的量，' +
      '比物理占用更接近真实压力。超过 90% 变红，那时再申请内存就会开始失败。',
  }),
];

// One merged block at the top of the page: the card's main vitals (GPU util /
// VRAM / temp / power) plus the host's (CPU load / commit memory), with the
// power-limit switch right underneath.  The per-section GPU and system grids
// used to sit two blocks down and took a full screen; these six are what a
// glance actually needs, so they lead and everything else is one scroll away.
// The power-limit SWITCH moved to the top bar on 2026-10-19 (left of the
// update time); the 功耗 card itself stayed here -- watts belong to the grid,
// the tier buttons are the thing worth keeping one click away at all times.
function renderSystem(snapshot) {
  const container = document.getElementById('sys');
  if (!container) return;

  const gpuState = snapshot.gpu || {};
  const host = snapshot.system || {};
  const gpus = gpuState.gpus || [];
  const gpu = gpus.length ? gpus[0] : null;
  const cpu = host.cpu || {};
  const memory = host.memory || {};

  const refs = ensureCards(container, SYS_SPECS);
  if (!refs) return;

  const index = gpu && (gpu.index === undefined || gpu.index === null) ? 0 : (gpu ? gpu.index : 0);
  const memRatio = gpu ? gpu.mem_ratio : null;
  // Which ceiling the bar is drawn against.  The software limit is the honest
  // denominator where it exists; where it does not (laptop GPUs, where the
  // driver only reports the ceiling it is enforcing right now) that ceiling is
  // the only one there is, and the label says so.
  const powerCeiling = gpu ? firstNumber(gpu.power_requested_w, gpu.power_enforced_w) : null;
  const powerDynamic = !!gpu && firstNumber(gpu.power_requested_w) === null;
  const powerRatio = (gpu && powerCeiling) ? gpu.power_w / powerCeiling : null;
  const temp = gpu ? gpu.temp_c : null;
  const tempRatio = (temp === null || temp === undefined)
    ? null : Math.max(0, Math.min(1, (temp - 30) / 60));
  const hot = temp !== null && temp !== undefined && temp >= 83;
  const warm = temp !== null && temp !== undefined && temp >= 75;
  const memTight = gpu && (memRatio || 0) > 0.9;
  const cpuLoad = host.available ? cpu.load_pct : null;
  const commitRatio = host.available ? memory.commit_ratio : null;
  const commitTight = (commitRatio || 0) > 0.9;

  paintCards(refs, {
    util: {
      text: gpu ? num(gpu.util_pct, 0) : '—', unit: gpu ? '%' : '',
      bar: gpu ? (gpu.util_pct || 0) / 100 : null,
      barClass: 't-d',
      foot: gpu
        ? 'SM ' + countText(gpu.clock_sm_mhz) + ' MHz · 显存 ' + countText(gpu.clock_mem_mhz) + ' MHz'
        : '显卡不可用',
      series: seriesOf('gpu' + index + '.util'),
      sparkOptions: {
        min: 0, max: 100, color: cssVar('--chart-d'), autoscale: true,
        format: function (v) { return num(v, 0) + '%'; },
      },
    },
    mem: {
      text: gpu ? gb(gpu.mem_used_mb, 1) : '—', unit: gpu ? 'GB' : '', bar: memRatio,
      barClass: memTight ? 'bad' : (gpu && (memRatio || 0) > 0.8 ? 'warn' : 't-e'),
      foot: gpu ? '上限 ' + gb(gpu.mem_total_mb, 1) + ' GB' + (memTight ? ' · 接近上限' : '') : '显卡不可用',
      series: seriesOf('gpu' + index + '.mem'),
      sparkOptions: {
        min: 0, max: 1, color: cssVar('--chart-e'), autoscale: true,
        format: function (v) { return num(v * 100, 0) + '%'; },
      },
    },
    temp: {
      text: gpu ? num(temp, 0) : '—', unit: gpu ? '°C' : '', bar: tempRatio,
      barClass: hot ? 'bad' : (warm ? 'warn' : 't-f'),
      foot: gpu
        ? (hot ? '温度偏高，注意散热' : '风扇 ' + num(gpu.fan_pct, 0) + '%')
        : '显卡不可用',
      series: seriesOf('gpu' + index + '.temp'),
      sparkOptions: {
        min: 30, max: 95, color: hot ? cssVar('--bad') : cssVar('--chart-f'), autoscale: true,
        format: function (v) { return num(v, 0) + '°'; },
      },
    },
    power: {
      text: gpu ? num(gpu.power_w, 0) : '—', unit: gpu ? 'W' : '', bar: powerRatio,
      barClass: (powerRatio || 0) > 0.95 ? 'warn' : 't-g',
      foot: gpu
        ? (powerCeiling !== null
            ? '上限 ' + num(powerCeiling, 0) + ' W' + (powerDynamic ? ' · 动态' : '')
            : '上限 — W')
        : '显卡不可用',
      series: seriesOf('gpu' + index + '.power'),
      sparkOptions: {
        min: 0, color: cssVar('--chart-g'), autoscale: true,
        format: function (v) { return num(v, 0); },
      },
    },
    cpu: {
      text: host.available ? num(cpuLoad, 1) : '—', unit: host.available ? '%' : '',
      bar: host.available ? (cpuLoad || 0) / 100 : null,
      barClass: host.available
        ? ((cpuLoad || 0) > 92 ? 'bad' : ((cpuLoad || 0) > 80 ? 'warn' : 't-h'))
        : '',
      foot: host.available
        ? '基准 ' + countText(cpu.max_mhz) + ' MHz' + ((cpuLoad || 0) > 92 ? ' · 负载饱和' : '')
        : '系统采集不可用',
      series: seriesOf('host.cpu'),
      sparkOptions: {
        min: 0, max: 100, color: cssVar('--chart-h'), autoscale: true,
        format: function (v) { return num(v, 0) + '%'; },
      },
    },
    commit: {
      text: host.available ? gb(memory.commit_used_mb, 1) : '—', unit: host.available ? 'GB' : '',
      bar: commitRatio,
      barClass: commitTight ? 'bad' : (host.available && (commitRatio || 0) > 0.8 ? 'warn' : 't-b'),
      foot: host.available
        ? '上限 ' + gb(memory.commit_limit_mb, 1) + ' GB' + (commitTight ? ' · 压力偏高' : '')
        : '系统采集不可用',
      series: seriesOf('host.commit'),
      sparkOptions: {
        min: 0, max: 1, color: cssVar('--chart-b'), autoscale: true,
        format: function (v) { return num(v * 100, 0) + '%'; },
      },
    },
  });
}

// ---------------------------------------------------------------- power limit

// The wording lives here rather than in the server so the Python side can stay
// free of non-ASCII text; the API speaks in preset ids and error codes.
//
// The ids are *roles*, not wattages.  The menu is derived from the installed
// card -- 40/60/80/100% of its own maximum -- so the same four buttons have to
// read sensibly on a 175 W laptop and on a 600 W workstation.  The notes
// therefore describe the intent; the measured numbers come from the 150-600 W
// decode sweep, belong to the card it was run on, and are appended only for the
// presets the backend marks `tuned` (i.e. the ones that sweep actually produced).
const POWER_LABELS = {
  eco: {
    name: '静音',
    note: '最低档：最凉最静、最省电。解码（出字）卡在显存带宽，掉速通常有限，' +
      '预填充（首字延迟）会明显变长，长提示词尤其。',
  },
  quiet: {
    name: '低耗',
    note: '中低档：日常长对话的安静选择，解码速度一般接近满血，代价主要在预填充。',
  },
  balanced: {
    name: '均衡',
    note: '中高档：接近满速、能效最好的区间。解码与满血档通常只差几个百分点，' +
      '功耗和风扇噪声却低不少。',
  },
  full: {
    name: '满血',
    note: '最高档：放开整张卡的功耗上限。预填充最快，代价是功耗、温度和噪声都到顶。',
  },
};

// Measured on the 600 W-class card the sweep was run on.  Keyed by watts on
// purpose: these are properties of that card's curve, not of the role, so they
// only make sense next to the tuned preset that produced them.
const POWER_MEASURED = {
  150: '实测：~634MHz，decode ~19t/s，3K 上下文 prefill 要 ~33s。',
  300: '实测：~1386MHz，decode ~61t/s（长上下文），prefill ~10s。',
  400: '实测：~2350MHz，decode ~67t/s，峰值速度的 95%，能效最高档（曲线在 450W 后变平）。',
  600: '实测：~2777MHz 到时钟顶，decode ~70t/s（长上下文）/159t/s（短）；出厂默认，也就是「复原」。',
};

function presetMeta(preset) {
  return POWER_LABELS[preset.id] || { name: '', note: '' };
}

function presetName(preset) {
  const meta = presetMeta(preset);
  // On the tuned menu the top preset is the card's factory default, which is
  // what "复原" means; a derived menu's top setting is simply full power.
  if (preset.tuned && preset.id === 'full') return '复原';
  return meta.name;
}

function presetTip(preset) {
  const measured = preset.tuned ? POWER_MEASURED[preset.watts] : '';
  return [presetName(preset), presetMeta(preset).note, measured]
    .filter(Boolean).join(' · ');
}

const POWER_ERRORS = {
  not_elevated: '需要管理员权限',
  not_a_preset: '不是允许的档位',
  out_of_range: '超出这张卡允许的范围',
  no_gpu: '读不到显卡的功耗范围',
  unsupported: '这张卡不接受软件功耗墙（驱动拒绝）',
  not_applied: '驱动没有按请求生效',
  smi_failed: '驱动拒绝了这次设置',
  bad_host: '请求来源校验失败',
  bad_origin: '跨站请求已被拒绝',
  missing_header: '缺少必要的请求头',
  bad_body: '请求内容不合法',
  not_found: '接口不存在',
};

const POWER_UI = { pending: null, message: null };

function powerMessage() {
  const message = POWER_UI.message;
  if (!message) return null;
  // render() runs on every sample, so a timestamp is all it takes to expire
  // the note -- no timer to leak.
  return (Date.now() - message.at < 8000) ? message : null;
}

function renderPowerControl(snapshot) {
  const box = document.getElementById('power-control');
  if (!box) return;

  const gpuState = (snapshot && snapshot.gpu) || {};
  const gpus = gpuState.gpus || [];
  const config = S.config || {};
  const presets = config.power_presets || [];
  // False once the driver has refused a software power limit outright.  There
  // is nothing to retry in that case, so the buttons go away and the row
  // reports the live ceiling instead of offering a write that cannot land.
  const settable = config.power_settable !== false;

  if (!gpus.length) {
    // Nothing readable to draw against -- a machine with no NVIDIA card here.
    box.innerHTML = '';
    box.dataset.sig = '';
    return;
  }

  const gpu = gpus[0];
  // Three different numbers, and the row wants all of them: what the card draws
  // right now, the ceiling it is holding, and which preset owns that ceiling.
  // Only the middle one is always readable -- laptop GPUs report no software
  // limit at all -- so the highlight falls back to what this dashboard last
  // set, and drops that memory the moment the driver reports something else.
  const requested = firstNumber(gpu.power_requested_w);
  const enforced = firstNumber(gpu.power_enforced_w);
  const ceiling = firstNumber(requested, enforced);
  const dynamic = requested === null && enforced !== null;
  const live = firstNumber(gpu.power_w);
  const applied = firstNumber(config.power_applied_w);
  const current = firstNumber(requested, applied);
  const elevated = !!config.elevated;
  const pending = POWER_UI.pending;
  const note = powerMessage();
  // The .top variant now lives in the top bar as a one-line switch (it used
  // to sit in the system block's head); the classic full-width row is kept
  // for any future wider placement.
  const compact = box.classList.contains('top');

  if (!presets.length) {
    // The numbers came through but the menu did not: what a page looks like
    // when its boot-time config request lost the race against a restart. A
    // blank row reads as "the power board disappeared", so keep the live
    // wattage visible and name the missing half instead. The next stream
    // connection carries config and this goes away by itself.
    if (box.dataset.sig !== 'pending-menu') {
      box.dataset.sig = 'pending-menu';
      box.innerHTML = '<span class="pw-range">' +
        (live !== null ? num(live, 0) + ' W' : '—') +
        (ceiling !== null ? ' / 上限 ' + num(ceiling, 0) + ' W' : '') +
        '</span><span class="pw-status">档位菜单未到，连接恢复后自动补上</span>';
    }
    return;
  }

  // The readout ticks with every sample, and rebuilding the whole row that
  // often would fight the pointer and flicker the hover state -- so the markup
  // is gated on this signature and the numbers are patched in below.
  const sig = JSON.stringify([current, ceiling, dynamic, elevated, settable,
    pending, note ? note.text + note.tone : '', compact, presets.length]);
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = powerMarkup(presets, {
      gpu: gpu, settable: settable, elevated: elevated, pending: pending,
      note: note, compact: compact, current: current, live: live,
      ceiling: ceiling, dynamic: dynamic,
    });
  }
}

// The structural half of the power row: buttons, labels, status and range.
// Called only when the signature above changes, so it can afford to build the
// whole string at once.
function powerMarkup(presets, state) {
  const compact = state.compact;
  const buttons = presets.map(function (preset) {
    // Highlight from the measured limit, not from what we last asked for: if
    // something else changes the card the buttons follow reality.
    const active = state.current !== null &&
      Math.abs(preset.watts - state.current) < 1;
    const classes = ['pw-btn'];
    if (active) classes.push('on');
    if (state.pending === preset.watts) classes.push('pending');
    const disabled = (state.elevated && state.settable) ? '' : ' disabled';
    if (compact) {
      // One line, watts only: the preset name and its explanation move into
      // the tooltip because the section head has no room for a second line.
      return '<button type="button" class="' + classes.join(' ') +
        '" data-watts="' + preset.watts + '"' + disabled +
        ' title="' + esc(presetTip(preset)) + '">' +
        '<span class="pw-watts">' + preset.watts + 'W</span></button>';
    }
    return '<button type="button" class="' + classes.join(' ') +
      '" data-watts="' + preset.watts + '"' + disabled +
      ' title="' + esc(presetTip(preset)) + '">' +
      '<span class="pw-watts">' + preset.watts + ' W</span>' +
      '<span class="pw-note">' + esc(presetName(preset)) + '</span></button>';
  }).join('');

  const low = firstNumber(state.gpu.power_min_w);
  const high = firstNumber(state.gpu.power_max_w);
  const factory = firstNumber(state.gpu.power_default_w);
  const rangeParts = [];
  if (low !== null && high !== null) {
    rangeParts.push('可用范围 ' + num(low, 0) + '–' + num(high, 0) + ' W');
  }
  if (factory !== null) rangeParts.push('出厂默认 ' + num(factory, 0) + ' W');
  const range = rangeParts.join(' · ');

  let status = '';
  if (state.note) {
    status = '<span class="pw-status ' + (state.note.tone || '') + '">' +
      esc(state.note.text) + '</span>';
  } else if (!state.settable) {
    // Not an error: the driver simply does not implement the setting.  Say
    // what is in force instead, which is the number the user actually asked to
    // see in the first place.
    status = '<span class="pw-status">此卡不接受软件功耗墙，上限由 Dynamic Boost 动态管理</span>';
  }

  let tipText = '对应 nvidia-smi -pl 的功率墙。' +
    '满载时显卡会在此值附近降频，实际功耗不会超过去。' +
    '需要管理员权限，且重启电脑后恢复默认（可用任务计划程序做开机自动设置）。' +
    '出字（解码）阶段卡在显存带宽，适度下调通常几乎不掉速；' +
    '预填充阶段卡在算力，会变慢一些，但它只占一次请求开头的一小段。';
  if (state.dynamic) {
    tipText += '这张卡不上报软件功耗墙（笔记本 GPU 一般由 Dynamic Boost 接管上限），' +
      '所以读数里的是驱动此刻真正生效的上限，它会随 CPU 负载和温度在最大 TGP 之内浮动，' +
      '这也是「动态」两个字的来历。';
  }
  if (!state.settable) {
    tipText += '驱动已经明确拒绝过软件设限，所以档位按钮不再提供。';
  }
  const tip = '<span class="tip" tabindex="0" data-tip="' + esc(tipText) + '">i</span>';
  // The live "当前 X W · 上限 Y W" readout used to sit here; the power card
  // right above already shows both numbers, so the row is buttons only.

  if (compact) {
    // No room for the admin-rights paragraph here, so it becomes a lock icon
    // with the explanation in its tooltip.
    const lock = state.elevated ? '' :
      '<span class="tip pw-lock" tabindex="0" data-tip="' + esc('当前进程没有管理员权限，改不了功耗上限（nvidia-smi -pl 会被系统拒绝）。请关闭本窗口，改用 start_monitor_admin.vbs 启动（双击，会弹一次 UAC 确认）。') + '">🔒</span>';
    const group = state.settable
      ? '<div class="pw-group">' + buttons + '</div>' : '';
    return '<div class="pw-row">' +
        '<span class="pw-title">显卡功耗' + tip + lock + '</span>' +
        group + status +
      '</div>';
  }

  const group = state.settable
    ? '<div class="pw-group">' + buttons + '</div>' : '';
  return '<div class="pw-row">' +
      '<span class="pw-title">功耗上限' + tip + '</span>' +
      group + status +
      (range ? '<span class="pw-range">' + range + '</span>' : '') +
    '</div>' +
    (state.elevated ? '' : '<div class="pw-hint">当前进程没有管理员权限，改不了功耗上限' +
      '（<code>nvidia-smi -pl</code> 会被系统拒绝）。' +
      '请关闭本窗口，改用 <b>start_monitor_admin.vbs</b> 启动（双击，会弹一次 UAC 确认）。</div>');
}

function applyPowerLimit(watts) {
  const config = S.config || {};
  if (!config.elevated) {
    POWER_UI.message = { text: POWER_ERRORS.not_elevated, tone: 'bad', at: Date.now() };
    renderPowerControl(S.snapshot || {});
    return;
  }
  if (POWER_UI.pending !== null) return;

  const gpus = (S.snapshot && S.snapshot.gpu && S.snapshot.gpu.gpus) || [];
  const active = gpus.length
    ? firstNumber(gpus[0].power_requested_w, config.power_applied_w) : null;
  if (active !== null && Math.abs(active - watts) < 1) {
    POWER_UI.message = { text: '当前已经是该档位', tone: '', at: Date.now() };
    renderPowerControl(S.snapshot || {});
    return;
  }

  POWER_UI.pending = watts;
  POWER_UI.message = { text: '正在设置…', tone: '', at: Date.now() };
  renderPowerControl(S.snapshot || {});

  fetch('/api/power-limit', {
    method: 'POST',
    // The custom header is the check that matters: it forces a CORS preflight,
    // and this server never answers OPTIONS, so another site cannot get the
    // request out at all.  See Handler._write_refusal.
    headers: { 'Content-Type': 'application/json', 'X-LLM-Monitor': '1' },
    body: JSON.stringify({ watts: watts }),
  }).then(function (response) {
    return response.json().then(function (body) { return body || {}; });
  }).then(function (body) {
    POWER_UI.pending = null;
    if (body.ok) {
      // Move the highlight on this frame; the next sample overwrites it with
      // what the driver actually reports.
      const applied = firstNumber(body.applied);
      if (gpus.length && applied !== null) gpus[0].power_requested_w = applied;
      // The server remembers what it set for exactly this case: a card that
      // accepts the write but will not report it back has no other basis for
      // the highlight, and the memory is dropped the moment the driver starts
      // reporting a value of its own.
      config.power_applied_w = applied !== null ? applied : watts;
      const previous = firstNumber(body.previous);
      const target = applied !== null ? applied : watts;
      POWER_UI.message = {
        text: '已设为 ' + num(target, 0) + ' W' +
          (applied === null ? '（驱动不回读软件功耗墙，按请求值显示）' : '') +
          (previous !== null && Math.abs(previous - target) >= 1
            ? '（原 ' + num(previous, 0) + ' W）' : ''),
        tone: 'ok', at: Date.now(),
      };
    } else {
      const code = body.error || 'unknown';
      if (code === 'unsupported') {
        // Verified refusal: the driver does not implement this setting at all,
        // so stop offering it now rather than on the next sample.
        config.power_settable = false;
      }
      POWER_UI.message = {
        text: (POWER_ERRORS[code] || '设置失败（' + code + '）') +
          (body.detail ? '：' + body.detail : ''),
        tone: 'bad', at: Date.now(),
      };
    }
    renderPowerControl(S.snapshot || {});
  }).catch(function (error) {
    POWER_UI.pending = null;
    POWER_UI.message = { text: '请求失败：' + error.message, tone: 'bad', at: Date.now() };
    renderPowerControl(S.snapshot || {});
  });
}

// ---------------------------------------------------------------- token ledger

function tokenText(value) {
  // These totals reach into the millions and the exact digit count never
  // changes a decision, so the headline stays short and the precise value
  // lives in the tooltip.
  const n = Number(value);
  if (!isFinite(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}

function tokenExact(value) {
  const n = Number(value);
  return isFinite(n) ? Math.round(n).toLocaleString('zh-CN') + ' tok' : T('暂无数据');
}

// A recorded power limit is just a number; the preset name is a frontend
// concern (the backend deliberately carries no Chinese).  A limit that is not
// one of the presets -- someone can set any value with nvidia-smi directly, and
// a card with no software limit records the ceiling it happened to be holding.
// The tilde that used to mark "measured, not chosen" is gone at the user's
// request; the preset name still tells the two cases apart.
function wattsLabel(watts) {
  const value = Number(watts);
  if (!isFinite(value) || value <= 0) return '档位未知';
  const presets = (S.config && S.config.power_presets) || [];
  const hit = presets.filter(function (preset) {
    return Math.abs(Number(preset.watts) - value) < 1;
  })[0];
  const meta = hit ? POWER_LABELS[hit.id] : null;
  const name = hit ? presetName(hit) : '';
  return Math.round(value) + ' W' + (name ? ' ' + name : '');
}

function rateText(value) {
  const n = Number(value);
  if (!isFinite(n) || n <= 0) return '—';
  return n >= 100 ? String(Math.round(n)) : n.toFixed(1);
}

function currentPowerLimit(snapshot) {
  const gpu = ((snapshot && snapshot.gpu) || {}).gpus || [];
  // The tier the ledger booked this sample under, not the raw ceiling: on a
  // card that will not report a software limit the two differ (the server
  // rounds the dynamic ceiling onto the 5 W grid), and the row this is used to
  // mark has to be the row the tokens actually landed in.
  const value = gpu.length
    ? firstNumber(gpu[0].power_tier_w, gpu[0].power_requested_w) : null;
  return value === null ? null : Number(value);
}

function renderTokenStats(snapshot) {
  const grid = document.getElementById('token-stats');
  const note = document.getElementById('token-note');
  const days = document.getElementById('token-days');
  // The counter cards now sit in the 模型信息 head; when the ledger is
  // down .ledger-off hides the whole row instead of leaving a dead
  // 未启用 tile on the title line.
  const head = document.getElementById('model-info');
  const tiers = document.getElementById('token-tiers');
  const tierNote = document.getElementById('token-tier-note');
  const tierSummary = document.getElementById('token-tier-summary');
  const combos = document.getElementById('token-combos');
  const rankOutput = document.getElementById('token-rank-output');
  const rankSpeed = document.getElementById('token-rank-speed');
  if (!grid || !note || !days) return;
  if (head) head.classList.remove('ledger-off');

  const data = snapshot.stats;
  if (!data || data.available === false) {
    const reason = (data && data.error === 'disabled')
      ? 'config.json 里 stats.enabled 设成了 false'
      : '本次采样没有上报账本';
    if (head) head.classList.add('ledger-off');
    note.className = 'stat-note';
    note.textContent = '账本未启用：' + reason + '。';
    days.innerHTML = '';
    if (tiers) tiers.innerHTML = '';
    if (combos) combos.innerHTML = '';
    if (rankOutput) rankOutput.innerHTML = '';
    if (rankSpeed) rankSpeed.innerHTML = '';
    if (tierNote) tierNote.textContent = '';
    if (tierSummary) tierSummary.textContent = '';
    return;
  }

  const today = data.today_totals || {};
  const total = data.totals || {};
  const cells = [
    {
      label: '今日生成', value: today.predicted_tokens, unit: 'tok',
      foot: 'decode 调用 ' + tokenText(today.decode_steps) + ' 次',
    },
    {
      label: '今日预填充', value: today.prompt_tokens, unit: 'tok',
      foot: 'KV 复用 ' + tokenText(today.cached_tokens) + ' tok',
    },
    {
      label: '累计生成', value: total.predicted_tokens, unit: 'tok',
      foot: data.since ? '自 ' + data.since + ' 起' : '尚无记录',
    },
    {
      label: '累计预填充', value: total.prompt_tokens, unit: 'tok',
      foot: '覆盖 ' + (data.days || 0) + ' 天',
    },
  ];
  grid.innerHTML = cells.map(function (cell) {
    return '<div class="stat">' +
      '<span class="stat-label">' + esc(cell.label) + '</span>' +
      '<span class="stat-value" title="' + esc(tokenExact(cell.value)) + '">' +
        tokenText(cell.value) +
        '<span class="stat-unit">' + esc(cell.unit) + '</span></span>' +
      '<span class="stat-foot">' + esc(cell.foot) + '</span>' +
      '</div>';
  }).join('');

  // Persistence is the entire point of the ledger, so a failure to write has
  // to be visible on the page rather than tucked into a hover.  Everything
  // else this line used to carry -- reset count, ledger path, coverage -- was
  // ambient chatter, now removed at the user's request; it still lives in
  // run.log and in stats.json itself.
  if (!data.persisted) {
    note.className = 'stat-note warn';
    note.textContent = '账本未落盘（' + (data.error || '未知原因') +
      '）：当前只记在内存里，工作台重启后会清零。';
  } else {
    note.className = 'stat-note';
    note.textContent = '';
  }

  // TOP-3 rankings straight off the per-model ledger: one table by output
  // volume, one by engine-measured decode speed.  The model name uses the same
  // "source / short name" format as the rest of the panel so a loader path id
  // reads like every other entry.
  const rankRows = (data.model_rows || []).slice();
  // raw() is the number used for filtering and sorting; display() is what the
  // cell shows.  Keeping them apart matters because a formatted total like
  // "1.23M" cannot be subtracted to order rows.
  const rankTable = function (title, metricLabel, raw, display) {
    const rows = rankRows.filter(function (row) { return raw(row) !== null; })
      .sort(function (a, b) { return raw(b) - raw(a); })
      .slice(0, 3);
    if (!rows.length) return '<p class="muted">暂无记录。</p>';
    return '<table><thead><tr><th>#</th><th>模型</th><th class="num">' +
      title + '</th></tr></thead><tbody>' +
      rows.map(function (row, i) {
        return '<tr><td class="num rank-no">' + (i + 1) + '</td>' +
          '<td>' + sourceName(row) + rowVerHtml(row) + '</td>' +
          '<td class="num" title="' + esc(metricLabel) + '">' + display(row) + '</td></tr>';
      }).join('') + '</tbody></table>';
  };
  if (rankOutput) {
    rankOutput.innerHTML = rankTable('输出 tok', '累计生成的 token 总数',
      function (row) {
        const n = Number(row.predicted_tokens || 0);
        return n > 0 ? n : null;
      },
      function (row) { return tokenText(row.predicted_tokens); });
  }
  if (rankSpeed) {
    rankSpeed.innerHTML = rankTable('生成 t/s', '引擎自计时口径的平均生成速率',
      function (row) {
        const n = Number(row.predicted_tps);
        return isFinite(n) && n > 0 ? n : null;
      },
      function (row) { return rateText(row.predicted_tps); });
  }

  // Power-tier attribution.  A token delta carries the limit that was in force
  // when it was booked, so this is the one place the panel can answer "what did
  // I run at 300 W".  A zero tier is a real state -- the limit could not be
  // read -- so it gets its own row instead of being hidden or merged.
  const limit = currentPowerLimit(snapshot);
  const tierRows = (data.tiers || []).slice();
  if (tiers) {
    const tiered = tierRows.reduce(function (acc, row) {
      return acc + Number(row.predicted_tokens || 0);
    }, 0);
    tiers.innerHTML = tierRows.map(function (row) {
      const isNow = limit !== null && Math.abs(limit - row.watts) < 1;
      const share = tiered > 0 ? Number(row.predicted_tokens || 0) / tiered : 0;
      const metric = function (label, value, unit, title) {
        return '<span class="tier-item"' +
          (title ? ' title="' + esc(title) + '"' : '') + '><em>' + esc(label) +
          '</em><b>' + value + '</b><span class="tier-unit">' + esc(unit) +
          '</span></span>';
      };
      return '<div class="tier' + (isNow ? ' now' : '') +
        (row.watts ? '' : ' unknown') + '">' +
        '<div class="tier-head"><b>' + esc(wattsLabel(row.watts)) + '</b>' +
          (isNow ? '<span class="tier-now">当前</span>' : '') + '</div>' +
        '<div class="tier-metrics">' +
          metric('生成', tokenText(row.predicted_tokens), 'tok',
                 tokenExact(row.predicted_tokens)) +
          metric('预填充', tokenText(row.prompt_tokens), 'tok',
                 tokenExact(row.prompt_tokens)) +
          // Shown next to the prefill rate on purpose: cached tokens are
          // near-free to re-read, so a high prefill t/s usually means a high
          // cache hit rate rather than raw compute.
          metric('KV 复用', tokenText(row.cached_tokens), 'tok',
                 tokenExact(row.cached_tokens)) +
          metric('生成速度', rateText(row.predicted_tps), 't/s', '') +
          metric('预填充速度', rateText(row.prompt_tps), 't/s', '') +
        '</div>' +
        '<div class="tier-bar" title="占已归集生成量的 ' +
          (share * 100).toFixed(1) + '%"><i style="width:' +
          (share * 100).toFixed(1) + '%"></i></div>' +
        '</div>';
    }).join('');
  }

  if (tierSummary) {
    tierSummary.textContent = tierRows.length
      ? tierRows.length + ' 个档位'
      : '';
  }
  if (tierNote) {
    // Only the warning stays on the page: a tier whose ceiling could not be
    // read is a conclusion, not a detail -- part of the total cannot be
    // attributed at all.  The "collecting since" and "nothing yet" lines were
    // ambient chatter and are gone.
    const unknown = tierRows.filter(function (row) { return !row.watts; }).length;
    if (unknown) {
      tierNote.className = 'stat-note warn';
      tierNote.textContent = '有 ' + unknown + ' 组记录读不到功耗上限' +
        '（显卡采集被关闭或驱动查询失败），已单独计为『档位未知』。';
    } else {
      tierNote.className = 'stat-note subtle';
      tierNote.textContent = '';
    }
  }

  const comboRows = (data.combos || []).slice();
  if (combos) {
    combos.innerHTML = comboRows.length ? '<table><thead><tr>' +
      '<th>日期</th><th>功率档</th><th>模型</th>' +
      '<th class="num">生成</th><th class="num">预填充</th>' +
      '<th class="num">KV 复用</th>' +
      '<th class="num">生成 t/s</th><th class="num">预填充 t/s</th>' +
      '</tr></thead><tbody>' +
      comboRows.map(function (row) {
        const isNow = limit !== null && Math.abs(limit - row.watts) < 1;
        return '<tr>' +
          '<td>' + esc(row.day) + '</td>' +
          '<td><span class="tier-chip' + tierChipClass(row.watts) +
            (isNow ? ' now' : '') + '">' + esc(wattsLabel(row.watts)) +
            '</span></td>' +
          '<td>' + sourceName(row) + rowVerHtml(row) + '</td>' +
          '<td class="num" title="' + esc(tokenExact(row.predicted_tokens)) + '">' +
            tokenText(row.predicted_tokens) + '</td>' +
          '<td class="num" title="' + esc(tokenExact(row.prompt_tokens)) + '">' +
            tokenText(row.prompt_tokens) + '</td>' +
          '<td class="num" title="' + esc(tokenExact(row.cached_tokens)) + '">' +
            tokenText(row.cached_tokens) + '</td>' +
          '<td class="num">' + rateText(row.predicted_tps) + '</td>' +
          '<td class="num">' + rateText(row.prompt_tps) + '</td>' +
          '</tr>';
      }).join('') + '</tbody></table>'
      : '<p class="muted">尚无明细记录。</p>';
  }

  const recent = (data.recent || []).slice();
  days.innerHTML = recent.length ? '<table><thead><tr>' +
    '<th>日期</th><th class="num">生成</th><th class="num">预填充</th>' +
    '<th class="num">合计</th></tr></thead><tbody>' +
    recent.map(function (row) {
      const sum = Number(row.predicted_tokens || 0) +
        Number(row.prompt_tokens || 0);
      return '<tr><td>' + esc(row.day) + '</td>' +
        '<td class="num" title="' + esc(tokenExact(row.predicted_tokens)) + '">' +
        tokenText(row.predicted_tokens) + '</td>' +
        '<td class="num" title="' + esc(tokenExact(row.prompt_tokens)) + '">' +
          tokenText(row.prompt_tokens) + '</td>' +
        '<td class="num" title="' + esc(tokenExact(sum)) + '">' +
          tokenText(sum) + '</td></tr>';
    }).join('') + '</tbody></table>' : '';
}

// ------------------------------------------------------------------ energy
// The 能耗面板 tab (2026-10-24): Netdata Power Dashboard's content shape,
// fed by this machine's own books.  One number on this pane is a measurement
// -- the GPU's NVML draw -- and everything machine-level is an estimate whose
// model lives in collectors/stats.py and arrives with the payload as
// energy_model, so the live cards here and the integrator there can never
// drift apart.  Estimates say 估算 in their own label row, not in a footnote.

function whText(value) {
  if (value === null || value === undefined || !isFinite(value)) return '—';
  if (value >= 1000) return num(value / 1000, 2) + ' kWh';
  return num(value, 0) + ' Wh';
}

function drawEnergyBars(canvas, rows) {
  // Ledger rows arrive newest-first; bars read left to right oldest to newest.
  const days = (rows || []).slice().reverse();
  const mono = cssVar('--font-mono') || FALLBACK_MONO;
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(64, canvas.clientWidth || 320);
  const height = Math.max(60, canvas.clientHeight || 150);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, width, height);

  if (!days.length) {
    g.fillStyle = cssVar('--muted');
    g.font = '12px ' + mono;
    g.fillText('暂无能耗记录', 8, height / 2);
    return;
  }

  let peak = 0;
  days.forEach(function (row) {
    peak = Math.max(peak, row.machine_wh || 0, row.gpu_wh || 0);
  });
  // Nice ceiling (1/2/5 x 10^n) so the Wh gridlines read as round numbers.
  let max = peak > 0 ? peak : 1;
  const mag = Math.pow(10, Math.floor(Math.log10(max)));
  const norm = max / mag;
  max = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;

  const axisColor = cssVar('--axis');
  const gridColor = cssVar('--grid');
  const gpuColor = cssVar('--chart-b');
  // The machine bar sits behind the GPU bar as a translucent shell, so the
  // eye reads "measured" as the filled core of "estimated total" -- the
  // relationship the numbers actually have.
  const machineColor = 'rgba(124, 148, 190, 0.32)';

  const padL = 40, padR = 8, padT = 10, padB = 18;
  const plotW = Math.max(10, width - padL - padR);
  const plotH = Math.max(10, height - padT - padB);

  g.strokeStyle = gridColor;
  g.fillStyle = axisColor;
  g.font = '10px ' + mono;
  g.lineWidth = 1;
  for (let i = 0; i <= 2; i++) {
    const value = max * i / 2;
    const y = padT + plotH - plotH * i / 2;
    g.beginPath();
    g.moveTo(padL, y);
    g.lineTo(width - padR, y);
    g.stroke();
    g.fillText(whText(value), 2, y + 3);
  }

  const slot = plotW / days.length;
  const barW = Math.max(6, Math.min(26, slot * 0.34));
  days.forEach(function (row, i) {
    const cx = padL + slot * (i + 0.5);
    const mh = Math.max(0, plotH * (row.machine_wh || 0) / max);
    const gh = Math.max(0, plotH * (row.gpu_wh || 0) / max);
    g.fillStyle = machineColor;
    g.fillRect(cx - barW - 1, padT + plotH - mh, barW, mh);
    g.fillStyle = gpuColor;
    g.fillRect(cx + 1, padT + plotH - gh, barW, gh);
    g.fillStyle = axisColor;
    g.textAlign = 'center';
    g.fillText((row.day || '').slice(5), cx, height - 5);
    g.textAlign = 'left';
  });
}

function renderEnergyPanel(snapshot) {
  const cards = document.getElementById('energy-cards');
  const note = document.getElementById('energy-note');
  const table = document.getElementById('energy-days');
  const line = document.getElementById('energy-power-line');
  const bars = document.getElementById('energy-daily-bars');
  if (!cards || !note || !table || !line || !bars) return;

  const data = snapshot.stats;
  const model = (data && data.energy_model) ||
    { platform_w: 130, cpu_base_w: 15, cpu_slope_w: 65, psu_efficiency: 0.9 };
  const price = (data && typeof data.energy_price_cny_per_kwh === 'number')
    ? data.energy_price_cny_per_kwh : 0.6;

  // Live draw: GPU measured (summed over cards that reported), machine/wall
  // from the ledger's own constants -- the same arithmetic observe_power()
  // integrates, evaluated at "now" instead of over an interval.
  const gpus = (snapshot.gpu && snapshot.gpu.gpus) || [];
  let watts = 0;
  let hasWatts = false;
  gpus.forEach(function (card) {
    if (typeof card.power_w === 'number' && isFinite(card.power_w)) {
      watts += card.power_w;
      hasWatts = true;
    }
  });
  const sys = snapshot.system || {};
  const cpuPct = (sys.available && sys.cpu) ? sys.cpu.load_pct : null;
  const cpuW = (typeof cpuPct === 'number' && isFinite(cpuPct))
    ? model.cpu_base_w + model.cpu_slope_w * Math.min(100, Math.max(0, cpuPct)) / 100
    : null;
  const machineW = (hasWatts ? watts : 0) +
    (cpuW === null ? model.cpu_base_w : cpuW) + model.platform_w;
  const wallW = machineW / (model.psu_efficiency || 0.9);

  const rows = (data && data.energy_rows) || [];
  const todayRow = (data && rows.length && rows[0].day === data.today) ? rows[0] : null;
  // Cost comes from the ledger's own bill (hourly peak/valley when the books
  // have an hourly split; flat otherwise) -- the UI never re-prices energy.
  const pricing = (data && data.energy_pricing) || { mode: 'flat' };
  const costYuan = todayRow ? todayRow.cost_yuan : null;
  const flatToday = !!(pricing.mode === 'tou' && todayRow &&
    todayRow.cost_basis !== 'tou');

  const ledgerDown = !data || data.available === false;
  if (ledgerDown) {
    note.className = 'stat-note subtle';
    note.textContent = '账本未启用：没有能耗积分。';
    cards.innerHTML = '';
    table.innerHTML = '';
    drawSpark(line, [], { min: 0, color: cssVar('--chart-b'), format: function (v) { return num(v, 0) + ' W'; } });
    drawEnergyBars(bars, []);
    return;
  }

  const cells = [
    {
      label: '显卡功率', est: false, value: hasWatts ? watts : null,
      unit: 'W', digits: 0,
      foot: todayRow ? '实测 · 今日 ' + whText(todayRow.gpu_wh) : '实测 · 今日暂无',
      tip: gpus.length > 1 ? '全部显卡实时功耗之和，nvidia-smi 读数'
        : 'nvidia-smi 实时读数',
    },
    {
      label: '整机功率', est: true, value: machineW, unit: 'W', digits: 0,
      foot: 'CPU 估算 + 平台 ' + num(model.platform_w, 0) + ' W',
    },
    {
      label: '墙插功率', est: true, value: wallW, unit: 'W', digits: 0,
      foot: '按电源效率 ' + num(model.psu_efficiency * 100, 0) + '%',
    },
    {
      label: '今日电费', est: true, value: costYuan, unit: '', digits: null,
      foot: pricing.mode === 'tou' && pricing.tou
        ? '峰 ' + num(pricing.tou.peak_price_cny_per_kwh, 3) + ' / 谷 ' +
          num(pricing.tou.valley_price_cny_per_kwh, 3) + ' 元/kWh · 按小时'
        : '电价假设 ' + num(price, 2) + ' 元/kWh',
      tip: flatToday ? '本日含无小时数据的电量，按一口价补算' : '',
    },
  ];
  cards.innerHTML = cells.map(function (cell) {
    let value;
    if (cell.value === null || cell.value === undefined || !isFinite(cell.value)) {
      value = '—';
    } else if (cell.digits === null) {
      value = '¥' + num(cell.value, 2);
    } else {
      value = num(cell.value, cell.digits) +
        '<span class="stat-unit">' + esc(cell.unit) + '</span>';
    }
    return '<div class="stat">' +
      '<span class="stat-label">' + esc(cell.label) +
        (cell.est ? ' <i class="est-flag">估算</i>' : '') + '</span>' +
      '<span class="stat-value"' + (cell.tip ? ' title="' + esc(cell.tip) + '"' : '') + '>' +
        value + '</span>' +
      '<span class="stat-foot">' + esc(cell.foot) + '</span>' +
      '</div>';
  }).join('');

  // Coverage belongs in the open: a day the dashboard never ran shows no
  // energy, so the note says when the books actually started.  Days booked
  // before the hourly split get a flat-price bill, and the note says that
  // too rather than letting ＊ sit there unexplained.
  const flatDays = pricing.mode === 'tou' &&
    rows.some(function (row) { return row.cost_basis !== 'tou'; });
  note.className = 'stat-note subtle';
  note.textContent = (data.since
    ? '能耗自 ' + data.since + ' 起按采样积分；采样覆盖的时长见明细表『采样』列。'
    : '开始记账后每轮采样自动积分，暂无数据。') +
    (flatDays ? '带＊的日期含无小时数据的电量，该部分按一口价 ' + num(price, 2) + ' 元/kWh 补算。' : '');

  drawSpark(line, seriesOf('gpu0.power'), {
    min: 0, color: cssVar('--chart-b'), autoscale: true,
    format: function (v) { return num(v, 0) + ' W'; },
  });
  drawEnergyBars(bars, rows);

  table.innerHTML = rows.length ? '<table><thead><tr><th>日期</th>' +
    '<th class="num">GPU 实测</th><th class="num">整机估算</th>' +
    '<th class="num">墙插估算</th><th class="num">电费估算</th>' +
    '<th class="num">采样</th></tr></thead><tbody>' +
    rows.map(function (row) {
      return '<tr><td>' + esc(row.day) + '</td>' +
        '<td class="num">' + whText(row.gpu_wh) + '</td>' +
        '<td class="num">' + whText(row.machine_wh) + '</td>' +
        '<td class="num">' + whText(row.wall_wh) + '</td>' +
        '<td class="num"' + (row.cost_basis !== 'tou' && pricing.mode === 'tou'
          ? ' title="本日含无小时数据的电量，按一口价补算"' : '') + '>¥' +
          num(row.cost_yuan, 2) + (row.cost_basis !== 'tou' && pricing.mode === 'tou' ? '＊' : '') + '</td>' +
        '<td class="num">' + num(row.hours, 1) + ' h / ' + tokenText(row.samples) + ' 次</td></tr>';
    }).join('') + '</tbody></table>' : '';
}

// The release is named by the server, not by this file, so a browser holding a
// cached app.js still shows the version it is actually talking to.  Empty until
// the first snapshot lands, and hidden by CSS while it is.
function renderVersion() {
  const badge = document.getElementById('version');
  if (!badge) return;
  const version = (S.config && S.config.version) || '';
  const text = version ? 'v' + version : '';
  if (badge.textContent === text) return;
  badge.textContent = text;
  badge.title = version ? 'llm-monitor v' + version : '';
}

function render() {
  const snapshot = S.snapshot;
  if (!snapshot) return;
  renderVersion();
  renderEndpoints(snapshot);
  renderSystem(snapshot);
  renderPowerControl(snapshot);
  renderTokenStats(snapshot);
  renderEnergyPanel(snapshot);

  const meta = document.getElementById('meta');
  const when = new Date(snapshot.ts * 1000);
  const timings = snapshot.timings || {};
  const parts = [];
  if (timings.gpu !== undefined) parts.push('显卡 ' + timings.gpu + ' ms');
  if (timings.system !== undefined) parts.push('系统 ' + timings.system + ' ms');
  if (timings.endpoints && timings.endpoints.length) {
    parts.push('推理 ' + timings.endpoints.join('/') + ' ms');
  }
  meta.title = '各阶段采样耗时：' + (parts.join(' · ') || '未上报');
  meta.textContent = '更新于 ' + when.toLocaleTimeString('zh-CN', { hour12: false }) +
    ' · 采样 ' + num(snapshot.sample_ms, 0) + ' ms';

  // The connection lamp is owned by markConn() in the stream watchdog.  It
  // used to be set here from endpoint state too, so the two overwrote each
  // other on every sample and the lamp said nothing dependable.

  // The footer line (model count, history depth, peak-window age) was
  // ambient chatter and is gone; the numbers it showed are visible in the
  // charts themselves.
}

// ----------------------------------------------------------------------- theme

function initTheme() {
  const button = document.getElementById('theme-btn');
  const sync = function () {
    const dark = document.documentElement.dataset.theme === 'dark';
    button.textContent = dark ? '浅色' : '深色';
    button.title = dark ? '切换到浅色主题' : '切换到深色主题';
  };
  sync();
  button.addEventListener('click', function () {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(THEME_KEY, next); } catch (error) { /* private mode */ }
    sync();
    render();
  });
}

// ---------------------------------------------------------------------- restart

// The launcher refuses to start a second copy while the dashboard answers,
// and collector code only loads at startup -- so applying a code change used
// to mean hunting the pythonw pid down by hand.  The button POSTs
// /api/restart: the server spawns a detached replacement (which retries the
// bind until this process lets go of the port) and exits.  The page waits for
// a dashboard to answer again and reloads itself.
function initRestart() {
  const button = document.getElementById('restart-btn');
  if (!button) return;
  button.addEventListener('click', function () {
    if (button.disabled) return;
    if (!window.confirm(T('重启 monitor 进程？内存里的曲线历史会被清空，页面随后自动刷新。'))) return;
    button.disabled = true;
    button.textContent = '重启中…';
    fetch('/api/restart', { method: 'POST', headers: { 'X-LLM-Monitor': '1' } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j.ok) {
          resetRestartButton('重启被拒绝：' + (j.error || '未知原因'));
        }
      })
      .catch(function () { /* the connection dying *is* the restart working */ });
    const deadline = Date.now() + 30000;
    const probe = function () {
      fetch('/api/snapshot', { cache: 'no-store' })
        .then(function (r) { if (r.ok) window.location.reload(); })
        .catch(function () {});
      if (Date.now() < deadline) {
        setTimeout(probe, 1000);
      } else {
        resetRestartButton('重启超时：新进程没有应答');
      }
    };
    setTimeout(probe, 800);
  });
}

function resetRestartButton(message) {
  const button = document.getElementById('restart-btn');
  if (button) { button.disabled = false; button.textContent = '重启'; }
  const meta = document.getElementById('meta');
  if (meta && message) meta.textContent = message;
}

// The token ledger outlives every server and dashboard restart, so the only
// way to start the totals over is to ask the panel itself.  Erasing months of
// history is unrecoverable, so the button asks twice: the first confirm says
// exactly what goes, the second says there is no undo.  Cancelling either one
// sends nothing.  The server clears only the buckets and keeps the raw-counter
// baselines (TokenLedger.reset), so the next frame starts from where the
// engines stand now instead of re-booking their whole lifetime counters.
function initStatsReset() {
  const button = document.getElementById('stats-reset-btn');
  if (!button) return;
  button.addEventListener('click', function () {
    if (button.disabled) return;
    if (!window.confirm(T('清空历史信息？今日与累计 token、模型排行、功率档归集、明细表和近 7 天汇总都会归零，并当场写回 stats.json。'))) return;
    if (!window.confirm(T('确认清空？历史数据会永久丢失，无法恢复。'))) return;
    button.disabled = true;
    button.textContent = '已清空';
    fetch('/api/stats-reset', { method: 'POST', headers: { 'X-LLM-Monitor': '1' } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.ok) {
          const meta = document.getElementById('meta');
          if (meta) meta.textContent = '历史信息已清空';
          // Back in reach after a beat: clearing an empty ledger again is
          // legal, just pointless for a moment.
          setTimeout(function () { resetStatsButton(''); }, 4000);
        } else {
          resetStatsButton('清空被拒绝：' + (j.error === 'disabled' ? '账本未启用' : (j.error || '未知原因')));
        }
      })
      .catch(function (error) { resetStatsButton('清空失败：' + error.message); });
  });
}

function resetStatsButton(message) {
  const button = document.getElementById('stats-reset-btn');
  if (button) { button.disabled = false; button.textContent = '清空数据'; }
  const meta = document.getElementById('meta');
  if (meta && message) meta.textContent = message;
}

// ------------------------------------------------------------------------ boot

// Canvas labels are painted, so the CN/EN layer cannot translate them the way
// it translates text nodes. i18n.js announces every language change; repaint
// from the snapshot already in hand. Without this an empty card keeps saying
// "暂无数据" in Chinese until its next rebuild happens to touch that canvas --
// and the same listener is what puts the Chinese back when you switch home.
function initLanguage() {
  window.addEventListener('i18n-change', function () { render(); });
}

// The four ledger views live in tabs on the section head (2026-10-23) where
// they used to be four stacked folds: one view gets the full row width and
// the reader picks. The chosen tab persists like the theme and the language;
// an unknown stored value falls back to the first tab, never to a blank.
function initHistTabs() {
  const tabs = Array.prototype.slice.call(
    document.querySelectorAll('#hist-tabs button[data-pane]'));
  const panes = Array.prototype.slice.call(
    document.querySelectorAll('.hist-pane[data-pane]'));
  if (!tabs.length || !panes.length) return;
  const show = function (name) {
    tabs.forEach(function (b) {
      const on = b.dataset.pane === name;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    panes.forEach(function (p) { p.hidden = p.dataset.pane !== name; });
    try { localStorage.setItem(HIST_TAB_KEY, name); } catch (e) {}
    // Canvases inside a hidden pane measured 0 wide while hidden, and the
    // charts only repaint when the next sample lands -- up to 2 s of an empty
    // chart the moment you switch to a tab. Painting on the switch itself
    // costs one extra render pass and kills the blank.
    if (typeof render === 'function') render();
  };
  tabs.forEach(function (b) {
    b.addEventListener('click', function () { show(b.dataset.pane); });
  });
  let saved = '';
  try { saved = localStorage.getItem(HIST_TAB_KEY) || ''; } catch (e) {}
  const known = panes.some(function (p) { return p.dataset.pane === saved; });
  show(known ? saved : tabs[0].dataset.pane);
}

async function boot() {
  initTheme();
  initLanguage();
  initHistTabs();
  initRestart();
  initStatsReset();
  document.addEventListener('pointermove', onPointerMove, { passive: true });

  // Delegated once: the control row rebuilds itself, so per-button listeners
  // would have to be rebound on every render.
  const powerBox = document.getElementById('power-control');
  if (powerBox) {
    powerBox.addEventListener('click', function (event) {
      const button = event.target.closest('button[data-watts]');
      if (!button || button.disabled) return;
      applyPowerLimit(Number(button.dataset.watts));
    });
  }

  const manualOnly = new URLSearchParams(window.location.search).has('no-stream');
  try {
    const response = await fetch('/api/snapshot');
    const payload = await response.json();
    S.config = payload.config;
    S.snapshot = payload.snapshot;
    loadHistory(payload.history);
    render();
  } catch (error) {
    document.getElementById('meta').textContent = '初次加载失败：' + error.message;
  }

  // Keyboard shortcuts
  document.addEventListener('keydown', function (e) {
    // T: toggle theme
    if (e.key === 't' && !e.ctrlKey && !e.altKey && !e.metaKey) {
      const btn = document.getElementById('theme-btn');
      if (btn) btn.click();
    }
    // R: force refresh
    if (e.key === 'r' && !e.ctrlKey && !e.altKey && !e.metaKey) {
      if (S.snapshot) render();
    }
    // 0-9: toggle collapsible sections
    if (e.key >= '0' && e.key <= '9') {
      const idx = parseInt(e.key) - 1;
      const details = document.querySelectorAll('details.collapsible');
      if (details[idx]) details[idx].toggle();
    }
  });

  if (manualOnly) return;

  // Two independent paths keep the panel live:
  //   * SSE for the normal case -- immediate delivery, no repeating request;
  //   * a watchdog that falls back to polling once no frame has arrived for
  //     WATCHDOG_MS.  Some embedded browsers and preview panes block
  //     EventSource outright, and "refresh it by hand" is not an answer.
  // Streaming always wins: polling only runs while the stream is silent, so a
  // recovered stream stops the polling by itself.
  const WATCHDOG_MS = 8000;
  const POLL_MS = 4000;
  let lastStreamAt = Date.now();
  let lastPollAt = 0;

  function markConn(state) {
    const dot = document.getElementById('conn-dot');
    if (!dot) return;
    dot.className = 'dot ' + state;
    dot.title = CONN_TITLES[state] || '';
  }

  function applyFrame(message) {
    // Every frame carries both a snapshot and the flattened point, so the
    // numbers and the charts advance together.  Consuming only one of them is
    // exactly how the cards end up frozen while the curves keep moving.
    let touched = false;
    // Config rides on every frame rather than only on a hello: a page that
    // booted into a restart handover can miss its one-shot config request, or
    // reconnect before the replacement process has sampled once, and either way
    // the row that needs a menu -- the power control -- would otherwise stay
    // menu-less until someone refreshed by hand.
    if (message.config) { S.config = message.config; touched = true; }
    if (message.snapshot) { S.snapshot = message.snapshot; touched = true; }
    if (message.point) { appendPoint(message.point); touched = true; }
    if (touched) render();
    return touched;
  }

  function pollOnce() {
    lastPollAt = Date.now();
    fetch('/api/snapshot')
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        if (payload.config) S.config = payload.config;
        if (payload.snapshot) S.snapshot = payload.snapshot;
        loadHistory(payload.history);
        render();
        markConn('poll');
      })
      .catch(function () { markConn('down'); });
  }

  const stream = new EventSource('/api/stream');
  stream.onopen = function () { markConn('live'); };
  stream.onmessage = function (event) {
    let message;
    try { message = JSON.parse(event.data); } catch (error) { return; }
    lastStreamAt = Date.now();
    if (applyFrame(message)) markConn('live');
  };
  stream.onerror = function () {
    // EventSource retries by itself; do not close it here, that would cancel
    // the retry.  The watchdog covers the case where retrying cannot help.
    markConn('down');
  };

  window.setInterval(function () {
    if (Date.now() - lastStreamAt < WATCHDOG_MS) return;
    if (Date.now() - lastPollAt < POLL_MS) return;
    pollOnce();
  }, 2000);
}

window.addEventListener('resize', function () {
  clearTimeout(window.__resizeTimer);
  window.__resizeTimer = setTimeout(render, 160);
});

boot();
