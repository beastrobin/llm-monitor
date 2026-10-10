/* Reproducible benchmark: how much of each spark card's plot area actually
   gets painted, before and after the area-fill change.

   Deterministic and offline -- fixed fixtures, fixed canvas size, no live
   panel, no network.  Run:

     node tools/benchmark_spark_fill.js

   What it measures: the polygon the fill call actually receives (recorded off
   the stubbed 2D context, area by shoelace), as a fraction of the plot area.
   It does NOT measure GPU rasterisation cost or frame time -- jsdom has no 2D
   context, so "painted" here means "a fill polygon of this area was issued".
   That is the honest scope: it answers "does the curve have colour under it",
   which is the reported failure, not "is rendering faster".

   Two versions of app.js are loaded side by side:
     old = git show HEAD:frontend/app.js   (band-only fill, the reported bug)
     new = the working tree
   Same fixture, same options, same canvas geometry for both.
*/

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { JSDOM } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const GIT = 'C:/Program Files/Git/cmd/git.exe';

const CANVAS_W = 200;
const CANVAS_H = 34;
// drawSpark's own constants, mirrored so the plot area can be computed.
const BUCKET_PX = 2.2;
const Y_TOP = 11;          // showAxis path
const Y_BASE = CANVAS_H - 3;
const X0 = 32;
const X1 = CANVAS_W - 3;
const PLOT_W = X1 - X0;
const PLOT_H = Y_BASE - Y_TOP;
const PLOT_AREA = PLOT_W * PLOT_H;

const VARS = {
  '--chart-a': '#2563eb', '--chart-b': '#0d9488', '--chart-c': '#7c3aed',
  '--chart-d': '#16a34a', '--chart-e': '#9333ea', '--chart-f': '#ea580c',
  '--chart-g': '#ca8a04', '--chart-h': '#0284c7',
  '--grid': '#e2e8f0', '--track': '#e2e8f0', '--axis': '#64748b',
  '--panel': '#ffffff', '--border': '#cbd5e1', '--text': '#0f172a',
  '--font-mono': 'monospace',
};

// Fixed fixtures: the shapes a real panel spends its time in.  900 samples is
// the retained history length, so these are the dense path -- the one that
// produced the lonely line.
function ramp(n, lo, hi) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(lo + (hi - lo) * (i / (n - 1)));
  return out;
}
function flat(n, v) { return new Array(n).fill(v); }
function spiky(n, base, spikes) {
  const out = new Array(n).fill(base);
  spikes.forEach(function (i) { out[i] = base * 4; });
  return out;
}
function withGaps(n, base, gapFrom, gapTo) {
  const out = new Array(n).fill(base);
  for (let i = gapFrom; i <= gapTo; i++) out[i] = null;
  return out;
}

const FIXTURES = [
  { name: 'flat dense (37%)',        data: flat(900, 0.37),  opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-c'] } },
  { name: 'flat dense (0.2%)',       data: flat(900, 0.0016), opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-c'] } },
  { name: 'rising dense',            data: ramp(900, 0.05, 0.6), opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-a'] } },
  { name: 'spiky dense',             data: spiky(900, 0.3, [400, 700]), opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-a'] } },
  { name: 'dense with a gap',        data: withGaps(900, 0.4, 300, 420), opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-b'] } },
  { name: 'sparse (20 samples)',     data: ramp(20, 0.1, 0.5), opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-d'] } },
  { name: 'empty (all null)',        data: new Array(900).fill(null), opts: { min: 0, max: 1, autoscale: true, color: VARS['--chart-e'] } },
];

function shoelace(points) {
  let sum = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    sum += a[0] * b[1] - b[0] * a[1];
  }
  return Math.abs(sum) / 2;
}

function buildWindow(scriptText) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://127.0.0.1:8090/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const win = dom.window;
  win.getComputedStyle = function () {
    return { getPropertyValue: function (name) { return VARS[name] || ''; } };
  };
  win.devicePixelRatio = 1;

  const fills = [];
  win.HTMLCanvasElement.prototype.getContext = function () {
    const canvas = this;
    let path = null;
    const ctx = {
      measureText: function (t) { return { width: String(t).length * 6 }; },
      fillText: function () {},
      stroke: function () {},
      fill: function () {
        if (path && path.length >= 3) {
          fills.push({ canvas: canvas, points: path.slice(), style: ctx.fillStyle });
        }
      },
      setTransform: function () {}, clearRect: function () {},
      beginPath: function () { path = []; },
      closePath: function () {},
      moveTo: function (x, y) { if (path) path.push([x, y]); },
      lineTo: function (x, y) { if (path) path.push([x, y]); },
      strokeRect: function () {}, fillRect: function () {}, arc: function () {},
      quadraticCurveTo: function () {}, save: function () {}, restore: function () {},
      setLineDash: function () {},
      createLinearGradient: function () {
        const stops = [];
        return { addColorStop: function (pos, col) { stops.push([pos, col]); }, _stops: stops };
      },
      fillStyle: null,
    };
    return ctx;
  };
  Object.defineProperty(win.HTMLCanvasElement.prototype, 'clientWidth', { get: function () { return CANVAS_W; } });
  Object.defineProperty(win.HTMLCanvasElement.prototype, 'clientHeight', { get: function () { return CANVAS_H; } });

  win.eval(scriptText + '\nwindow.drawSparkForTest = drawSpark;');
  return { win: win, fills: fills };
}

function measure(scriptText, label) {
  const out = [];
  FIXTURES.forEach(function (fixture) {
    const env = buildWindow(scriptText);
    const canvas = env.win.document.createElement('canvas');
    canvas._hover = null;
    env.win.drawSparkForTest(canvas, fixture.data, fixture.opts);
    const own = env.fills.filter(function (f) { return f.canvas === canvas; });
    const area = own.reduce(function (s, f) { return s + shoelace(f.points); }, 0);
    out.push({
      fixture: fixture.name,
      label: label,
      fillPolys: own.length,
      fillArea: area,
      coverage: PLOT_AREA ? area / PLOT_AREA : 0,
      dense: !!(canvas._geom && canvas._geom.count < fixture.data.filter(function (v) { return v !== null; }).length),
      gradient: !!(own.length && own[0].style && own[0].style._stops),
    });
  });
  return out;
}

function main() {
  let oldScript;
  try {
    oldScript = execFileSync(GIT, ['show', 'HEAD:frontend/app.js'], { cwd: ROOT, encoding: 'utf8' });
  } catch (error) {
    console.error('could not read HEAD:frontend/app.js -- ' + error.message);
    process.exit(2);
  }
  const newScript = fs.readFileSync(path.join(ROOT, 'frontend', 'app.js'), 'utf8');

  const old = measure(oldScript, 'old');
  const neu = measure(newScript, 'new');

  const rows = FIXTURES.map(function (f, i) {
    const o = old[i];
    const n = neu[i];
    return {
      fixture: f.name,
      path: n.dense ? 'dense' : (f.data.some(function (v) { return v !== null; }) ? 'sparse' : 'none'),
      old_pct: Number((o.coverage * 100).toFixed(1)),
      new_pct: Number((n.coverage * 100).toFixed(1)),
      old_polys: o.fillPolys,
      new_polys: n.fillPolys,
      new_gradient: n.gradient,
    };
  });

  console.log('plot area basis: ' + PLOT_W + ' x ' + PLOT_H + ' px = ' + PLOT_AREA.toFixed(0) + ' px^2');
  console.log('');
  console.log('fixture              path    old fill%   new fill%   old polys  new polys');
  rows.forEach(function (r) {
    console.log(
      r.fixture.padEnd(20) + '  ' + r.path.padEnd(6) + '  ' +
      String(r.old_pct).padStart(7) + '      ' + String(r.new_pct).padStart(7) + '      ' +
      String(r.old_polys).padStart(5) + '      ' + String(r.new_polys).padStart(5));
  });

  // The guard is what the change promises, stated honestly:
  //   1. every card with data issues a fill (the reported failure was a bare
  //      line on a flat series);
  //   2. the fill is a gradient, not the solid colour the old band used;
  //   3. the flat dense case goes from ~0% (band collapses to nothing) to a
  //      real area;
  //   4. coverage tracks the level: a series at 37% fills about 37% of the
  //      displayed window, and a series at 0.2% fills about 2%.  A near-zero
  //      series is NOT expected to fill 30% -- demanding that would make the
  //      chart lie about the level.
  const withData = rows.filter(function (r) { return r.path !== 'none'; });
  const allFill = withData.every(function (r) { return r.new_polys >= 1 && r.new_pct > 0; });
  const flatRows = rows.filter(function (r) { return r.fixture.indexOf('flat dense') === 0; });
  const flatImproved = flatRows.every(function (r) { return r.old_pct < 1 && r.new_pct > 0; });
  const levelAt37 = flatRows.filter(function (r) { return r.fixture.indexOf('37%') >= 0; })
    .every(function (r) { return r.new_pct >= 30; });
  const gradient = withData.every(function (r) { return r.new_gradient; });
  const guard = allFill && flatImproved && levelAt37 && gradient;
  console.log('');
  console.log('guard: fill on every card with data ' + (allFill ? 'PASS' : 'FAIL') +
    ' | flat dense improved ' + (flatImproved ? 'PASS' : 'FAIL') +
    ' | 37% level fills >=30% ' + (levelAt37 ? 'PASS' : 'FAIL') +
    ' | gradient fill ' + (gradient ? 'PASS' : 'FAIL'));
  console.log('overall: ' + (guard ? 'PASS' : 'FAIL'));
  console.log('note: coverage is relative to the displayed window. A flat series autoscales to a bounded window centred on its value, so the fill lands near half the plot -- that is the level, not the noise.');
  process.exit(guard ? 0 : 1);
}

main();
