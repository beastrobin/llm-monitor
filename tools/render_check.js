/* Render the dashboard in jsdom against a live /api/snapshot payload.
 *
 * There is no browser here, so this harness exists to catch the failure mode
 * that static checks cannot: a runtime exception halfway through render, which
 * leaves a blank page.  Canvas is stubbed (jsdom has no 2D context) and the CSS
 * custom properties are parsed out of style.css so cssVar() returns real values
 * instead of empty strings.
 *
 *   node tools/render_check.js [http://127.0.0.1:8090]
 */

const fs = require('fs');
const http = require('http');
const path = require('path');
const { JSDOM } = require('jsdom');

const BASE = process.argv[2] || 'http://127.0.0.1:8090';
const ROOT = path.resolve(__dirname, '..');
const FRONTEND = path.join(ROOT, 'frontend');

function get(url) {
  return new Promise(function (resolve, reject) {
    http.get(url, function (res) {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', function (chunk) { body += chunk; });
      res.on('end', function () { resolve(body); });
    }).on('error', reject);
  });
}

function cssVariables(text) {
  const out = {};
  const start = text.indexOf(':root');
  const end = text.indexOf('}', start);
  const block = start >= 0 ? text.slice(start, end) : '';
  block.replace(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g, function (_, name, value) {
    out[name] = value.trim();
    return '';
  });
  return out;
}

/* Read the first frames off the real SSE endpoint and parse them.
 *
 * The cards read from `snapshot`, so the server has to put a snapshot in every
 * frame -- not just the opening one.  A stream of point-only frames leaves the
 * numbers frozen while the charts keep moving, which is precisely the bug this
 * whole file failed to catch once.  So check the contract, not just the DOM.
 */
function readFrames(base, count, timeoutMs) {
  return new Promise(function (resolve) {
    let buffer = '';
    let settled = false;
    const finish = function () {
      if (settled) return;
      settled = true;
      try { request.destroy(); } catch (error) { /* already gone */ }
      const frames = buffer.split('\n\n').map(function (part) {
        const line = part.split('\n').find(function (row) {
          return row.indexOf('data: ') === 0;
        });
        if (!line) return null;
        try { return JSON.parse(line.slice(6)); } catch (error) { return null; }
      }).filter(Boolean);
      resolve(frames);
    };
    const request = http.get(base + '/api/stream', function (res) {
      res.setEncoding('utf8');
      res.on('data', function (chunk) {
        buffer += chunk;
        if (buffer.split('\n\n').length >= count + 1) finish();
      });
      res.on('end', finish);
      res.on('error', finish);
    });
    request.on('error', function () { resolve([]); });
    setTimeout(finish, timeoutMs);
  });
}

(async function main() {
  const payload = JSON.parse(await get(BASE + '/api/snapshot'));

  // The stream contract: every frame carries a snapshot, not only the opening
  // one.  Frames 2..n are the ones that keep the panel live, so a point-only
  // steady-state frame means frozen cards even though nothing throws.
  const frames = await readFrames(BASE, 2, 9000);
  const steady = frames[1] || null;
  const contract = {
    frames: frames.length,
    hasSnapshot: !!(steady && steady.snapshot),
    hasPoint: !!(steady && steady.point),
  };
  const html = fs.readFileSync(path.join(FRONTEND, 'index.html'), 'utf8');
  const script = fs.readFileSync(path.join(FRONTEND, 'app.js'), 'utf8');
  const vars = cssVariables(fs.readFileSync(path.join(FRONTEND, 'style.css'), 'utf8'));

  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const win = dom.window;
  const doc = win.document;

  const errors = [];
  win.addEventListener('error', function (event) {
    errors.push(String(event.message || event.error));
  });

  // --- canvas stub, recording what the chart engine actually drew
  const paint = { strokes: 0, fills: 0, texts: [] };
  win.HTMLCanvasElement.prototype.getContext = function () {
    const ctx = {
      measureText: function (t) { return { width: String(t).length * 6 }; },
      fillText: function (t) { paint.texts.push(String(t)); },
      stroke: function () { paint.strokes += 1; },
      fill: function () { paint.fills += 1; },
      setTransform: function () {}, clearRect: function () {}, beginPath: function () {},
      closePath: function () {}, moveTo: function () {}, lineTo: function () {},
      strokeRect: function () {}, fillRect: function () {}, arc: function () {},
      quadraticCurveTo: function () {}, save: function () {}, restore: function () {},
      setLineDash: function () {},
    };
    return ctx;
  };

  const realComputed = win.getComputedStyle.bind(win);
  win.getComputedStyle = function (element) {
    if (element === doc.documentElement) {
      return { getPropertyValue: function (name) { return vars[name] || ''; } };
    }
    return realComputed(element);
  };

  if (!win.CSS) win.CSS = {};
  if (!win.CSS.escape) {
    win.CSS.escape = function (value) {
      return String(value).replace(/[^a-zA-Z0-9_-]/g, function (c) { return '\\' + c; });
    };
  }

  let streamUrl = null;
  let stream = null;
  win.EventSource = function (url) {
    streamUrl = url;
    this.onmessage = null;
    this.onerror = null;
    this.onopen = null;
    stream = this;
  };

  win.fetch = function () {
    return Promise.resolve({ json: function () { return Promise.resolve(payload); } });
  };

  win.eval(script + '\nwindow.renderEndpointsForTest = renderEndpoints;' +
    '\nwindow.appendPointForTest = appendPoint;');
  await new Promise(function (resolve) { setTimeout(resolve, 60); });

  // ------------------------------------------------------------------ report

  const lines = [];
  function say(text) { lines.push(text); }
  const text = function (el) { return (el && el.textContent || '').replace(/\s+/g, ' ').trim(); };

  // Switching between two already-loaded providers changes only the current
  // source marker. It must invalidate the DOM even when model data is identical.
  const routingFixture = {
    current_endpoint_id: 'second',
    endpoints: ['first', 'second'].map(function (id, i) {
      return { id: id, label: id, up: true, base_url: 'http://localhost:' + (9000 + i),
        models: [{ id: 'model-' + id, loaded: true, slots: [] }] };
    }),
  };
  win.renderEndpointsForTest(routingFixture);
  const selected = function () {
    const card = doc.querySelector('#endpoints .is-live');
    return card && card.dataset.endpoint;
  };
  const selectedSecond = selected() === 'second';
  routingFixture.current_endpoint_id = 'first';
  win.renderEndpointsForTest(routingFixture);
  const switchedFirst = selected() === 'first';
  routingFixture.current_endpoint_id = 'unavailable';
  win.renderEndpointsForTest(routingFixture);
  const noFalseCurrent = selected() === null;
  const routingOk = selectedSecond && switchedFirst && noFalseCurrent;
  win.renderEndpointsForTest(payload.snapshot);

  // --- slot detail: the one section the live snapshot may leave empty, so it
  // gets a fixture of its own.  Both slot shapes Strata reports have to render
  // without throwing: a busy slot (state/tok_s/elapsed_s present) and an idle
  // one (held_tokens only, every other field null).  A fixture that only ever
  // carries busy slots would let the idle path rot unnoticed -- which is
  // exactly the state the panel spends most of its time in.
  const slotFixture = {
    current_endpoint_id: 'strata',
    endpoints: [{
      id: 'strata', label: 'Strata', up: true, base_url: 'http://127.0.0.1:8200',
      models: [{
        id: 'slot-fixture', loaded: true, n_ctx: 524288,
        metrics: { counters: {} },
        slots: [
          { id: 0, n_ctx: 524288, is_processing: true, state: 'decoding',
            n_prompt_tokens: 36287, n_decoded: 1582, ctx_used: 37869,
            ctx_ratio: 0.0722, tok_s: 1498.3, elapsed_s: 1.4,
            prefill_tok_s: null, kv_hit: 0.9312 },
          { id: 1, n_ctx: 524288, is_processing: true, state: 'reading',
            n_prompt_tokens: 67489, n_decoded: 0, ctx_used: 67489,
            ctx_ratio: 0.1287, tok_s: null, elapsed_s: 1.1,
            prefill_tok_s: 1191.8, kv_hit: 0.8871 },
          // A near-empty slot: 833 of 524288 tokens is 0.2 %, which is the
          // case that used to draw an invisible rail and read as "no data".
          { id: 2, n_ctx: 524288, is_processing: false, state: 'idle',
            n_prompt_tokens: null, n_decoded: null, ctx_used: 833,
            ctx_ratio: 0.0016, tok_s: null, elapsed_s: null,
            prefill_tok_s: null, kv_hit: null },
        ],
      }],
    }],
  };
  const slotErrorsBefore = errors.length;
  win.renderEndpointsForTest(slotFixture);
  // Re-render so the slot series have more than one sample: a canvas that has
  // never been drawn reports count 0 and would pass a check meant to prove the
  // trend is actually wired up.  All six keys go in, because the panel only
  // pushes what the hub flattened -- a fixture that seeds one key would leave
  // the other five curves empty and prove nothing about them.
  slotFixture.endpoints[0].models[0].slots[0].n_decoded = 1900;
  slotFixture.endpoints[0].models[0].slots[0].tok_s = 1510.5;
  win.appendPointForTest({ ts: Math.floor(Date.now() / 1000) + 5, values: {
    'strata/slot-fixture.slot0.ctx': 37869,
    'strata/slot-fixture.slot0.ratio': 0.0722,
    'strata/slot-fixture.slot0.decoded': 1900,
    'strata/slot-fixture.slot0.tok_s': 1510.5,
    'strata/slot-fixture.slot0.prefill': 1191.8,
    'strata/slot-fixture.slot0.kv': 0.9312,
  } });
  win.renderEndpointsForTest(slotFixture);

  const SLOT_KEYS = ['tok_s', 'prefill', 'ctx', 'ratio', 'kv', 'decoded'];
  const slotFold = doc.querySelector('.slot-fold');
  const slotRows = doc.querySelectorAll('.slot-row');
  const busyRow = slotRows[0];
  const readRow = slotRows[1];
  const idleRow = slotRows[2];
  const slotCard = function (row, key) {
    return row && row.querySelector('[data-card="' + key + '"]');
  };
  const slotValue = function (row, key) {
    const card = slotCard(row, key);
    return card ? text(card.querySelector('.card-value')) : null;
  };
  const railOf = function (row, key) {
    const fill = slotCard(row, key) && slotCard(row, key).querySelector('.bar > i');
    return fill ? fill.style.height : null;
  };
  const sparkDrawn = function (row, key) {
    const canvas = slotCard(row, key) && slotCard(row, key).querySelector('canvas.spark');
    return !!(canvas && canvas._geom && canvas._geom.count > 0);
  };
  // The fold leads open: the per-slot breakdown is what the user comes to the
  // panel for, and asking for a click before it appears is a click too many.
  const foldOpen = !!(slotFold && slotFold.open);
  // The six cards carry the headline cards' names wherever the engine can give
  // a per-slot value, and the row is addressed as "slot N".  These labels were
  // an explicit request, so they are asserted rather than assumed to stick.
  const EXPECTED_LABELS = ['生成速度', 'Prefill 速度', '上下文',
    '上下文占比', 'KV 缓存命中', '已生成'];
  const labelsOf = function (row) {
    return SLOT_KEYS.map(function (k) {
      const card = slotCard(row, k);
      return card ? text(card.querySelector('.card-label')) : null;
    });
  };
  const labelsOk = busyRow && labelsOf(busyRow).join('|') === EXPECTED_LABELS.join('|');
  const slotIdOk = busyRow && text(busyRow.querySelector('.slot-id')) === 'slot 0';
  // Every slot card must carry the same anatomy as the six headline cards:
  // a rail, a label, a value and a trend canvas.  A missing rail means paintCards
  // never ran for this row.
  const anatomyOk = busyRow && SLOT_KEYS.every(function (key) {
    const card = slotCard(busyRow, key);
    return card && card.querySelector('.bar') && card.querySelector('.card-label') &&
      card.querySelector('.card-value') && card.querySelector('canvas.spark');
  });
  const busyOk = busyRow &&
    slotValue(busyRow, 'ctx') === '37,869tok' &&
    slotValue(busyRow, 'decoded') === '1,900tok' &&
    slotValue(busyRow, 'tok_s') === '1510.5t/s' &&
    slotValue(busyRow, 'kv') === '93.1%' &&
    text(busyRow.querySelector('.slot-state')) === 'decoding' &&
    busyRow.querySelector('.slot-state').className.indexOf('on') >= 0;
  // The reading slot owns the engine's one-at-a-time prefill rate.
  const readOk = readRow && slotValue(readRow, 'prefill') === '1192t/s';
  // An idle slot has held_tokens and nothing else: it must say so with "—"
  // rather than zeros, and its rail must not claim a reading.
  const idleOk = idleRow &&
    slotValue(idleRow, 'ctx') === '833tok' &&
    slotValue(idleRow, 'decoded') === '—' &&
    slotValue(idleRow, 'tok_s') === '—' &&
    slotValue(idleRow, 'prefill') === '—' &&
    slotValue(idleRow, 'kv') === '—' &&
    text(idleRow.querySelector('.slot-state')) === 'idle';
  const idleRailEmpty = idleRow && railOf(idleRow, 'tok_s') === '0%';
  // The visibility floor: this slot holds 833 of 524288 tokens -- 0.16%, which
  // is a tenth of a pixel of rail and used to read as an empty card.  The rail
  // must show, and the headline number must stay exact.
  const tinyRailVisible = idleRow &&
    parseFloat(railOf(idleRow, 'ratio')) >= 2 &&
    slotValue(idleRow, 'ratio') === '0.2%';
  // A busy slot well under the limit must not paint a full bar.
  const ratioRail = busyRow && railOf(busyRow, 'ratio');
  const ratioRailSane = !!ratioRail && parseFloat(ratioRail) > 2 && parseFloat(ratioRail) < 100;
  const sparkOk = SLOT_KEYS.filter(function (k) { return k !== 'prefill'; })
    .every(function (key) { return sparkDrawn(busyRow, key); });
  // The failure report carries the per-card point counts: a curve that draws
  // nothing usually means the series key the frontend asks for does not match
  // the one the hub flattened (the slot cards once keyed themselves
  // ".slotundefined.*" and every curve sat empty while the numbers worked).
  const sparkCounts = SLOT_KEYS.map(function (k) {
    const canvas = slotCard(busyRow, k) && slotCard(busyRow, k).querySelector('canvas.spark');
    return k + '=' + (canvas && canvas._geom ? canvas._geom.count : 'none') +
      '/' + (canvas && canvas._spark ? (canvas._spark.values || []).length : '?');
  }).join(' ');
  const slotOk = foldOpen && labelsOk && slotIdOk && anatomyOk && busyOk && readOk &&
    idleOk && idleRailEmpty && tinyRailVisible && ratioRailSane && sparkOk &&
    errors.length === slotErrorsBefore;
  say('slot 明细: ' + (slotOk ? '通过' : '失败') +
    ' | 行 ' + slotRows.length +
    ' | 默认展开 ' + (foldOpen ? '✓' : '✗') +
    ' | 标签 ' + (labelsOk ? '✓' : '✗ ' + labelsOf(busyRow).join('/')) +
    ' | slot id ' + (slotIdOk ? '✓' : '✗') +
    ' | 结构 ' + (anatomyOk ? '✓' : '✗') +
    ' | 忙碌槽 ' + (busyOk ? '✓' : '✗') +
    ' | 读入槽 prefill ' + (readOk ? '✓' : '✗') +
    ' | 空闲槽 "—" ' + (idleOk ? '✓' : '✗') +
    ' | 空闲血条空 ' + (idleRailEmpty ? '✓' : '✗') +
    ' | 0.2% 血条可见 ' + (tinyRailVisible ? '✓' : '✗') +
    ' | 曲线已绘制 ' + (sparkOk ? '✓' : '✗ ' + sparkCounts));
  if (busyRow) {
    say('   忙碌槽 0: ' + SLOT_KEYS
      .map(function (k) { return text(slotCard(busyRow, k).querySelector('.card-label')) +
        '=' + slotValue(busyRow, k); }).join(' | ') +
      ' | 上下文占比血条 ' + ratioRail);
  }
  if (idleRow) {
    say('   空闲槽 2: ' + SLOT_KEYS
      .map(function (k) { return text(slotCard(idleRow, k).querySelector('.card-label')) +
        '=' + slotValue(idleRow, k) + '(' + railOf(idleRow, k) + ')'; }).join(' | '));
  }
  win.renderEndpointsForTest(payload.snapshot);

  say('当前模型来源切换: ' + (routingOk ? '通过' : '失败'));

  say('错误: ' + (errors.length ? errors.join(' | ') : '无'));
  say('SSE 已连接: ' + (streamUrl || '未连接'));
  say('流帧契约: ' + contract.frames + ' 帧' +
    (contract.frames >= 2
      ? '，第 2 帧 ' + (contract.hasSnapshot ? '含 snapshot ✓' : '缺 snapshot ✗ 卡片会冻结') +
        ' / ' + (contract.hasPoint ? '含 point ✓' : '缺 point ✗ 曲线不会动')
      : '（帧数不足，无法判断）'));
  say('主题: ' + doc.documentElement.dataset.theme);
  // The header badge is the only place the running release is visible to a
  // user, and it is filled from the server rather than from the page.  Checking
  // it here means a version that never arrives -- or a badge wired to a stale
  // hard-coded string -- shows up as a failed smoke test instead of a shrug.
  const versionBadge = doc.getElementById('version');
  const badgeText = text(versionBadge);
  const serverVersion = ((payload.config || {}).version) || '';
  say('版本徽章: ' + (badgeText || '（空）') +
    (serverVersion && badgeText === 'v' + serverVersion
      ? ' ✓ 与服务端 config.version 一致'
      : ' ✗ 服务端下发 "' + serverVersion + '"，徽章却是 "' + badgeText + '"'));
  say('页面 meta: ' + text(doc.getElementById('meta')));

  // --------------------------------------------------------- live update check
  // A pushed frame has to move the *numbers*, not only the charts.  This
  // harness used to stub EventSource and never push anything, so a stream that
  // carried only the flattened point -- leaving every card frozen while the
  // curves kept moving -- went unnoticed for a whole round.  Push one frame
  // and diff the DOM.
  // The ledger is a read-only panel too, so it answers to the same frame
  // contract as the cards: a pushed frame has to move its numbers, not only
  // the charts.  Every container is in the diff, so a tier card or detail row
  // that freezes is caught the same way a frozen stat card is.
  const ledgerText = function () {
    return ['token-stats', 'token-tiers', 'token-combos', 'token-days']
      .map(function (id) {
        const el = doc.getElementById(id);
        return el ? text(el) : '';
      }).join(' | ');
  };

  const live = { pushed: false, cards: 'n/a', history: 'n/a' };
  if (stream) {
    const cardText = function () {
      return Array.prototype.map.call(
        doc.querySelectorAll('#sys .card, .endpoint .grid .card'),
        function (card) {
          return text(card.querySelector('.card-label')) + '=' +
            text(card.querySelector('.card-value'));
        }).join(' | ');
    };
    const pointsOf = function () {
      // The footer line that used to print "曲线历史 N / 900 点" is gone, so
      // read the retained length straight off a system card's spark payload:
      // drawSpark parks the raw series on the canvas on every tick, and every
      // series stays aligned with the shared timestamps.
      const canvas = doc.querySelector('#sys canvas');
      if (!canvas || !canvas._spark) return null;
      return (canvas._spark.values || []).length || null;
    };

    const before = cardText();
    const pointsBefore = pointsOf();
    const ledgerBefore = ledgerText();

    const frame = JSON.parse(JSON.stringify(payload.snapshot));
    if (frame.gpu && frame.gpu.gpus && frame.gpu.gpus.length) {
      frame.gpu.gpus[0].util_pct = 42;
      frame.gpu.gpus[0].temp_c = 63;
      frame.gpu.gpus[0].power_w = 512;
    }
    if (frame.system && frame.system.cpu) frame.system.cpu.load_pct = 37.5;
    live.ledgerPatched = !!(frame.stats && frame.stats.totals);
    if (live.ledgerPatched) {
      frame.stats.totals.predicted_tokens =
        (Number(frame.stats.totals.predicted_tokens) || 0) + 1234567;
    }
    // Move one figure in each of the newer containers as well, so the diff
    // covers the tier card and the detail row rather than only the stat grid.
    ['tiers', 'combos'].forEach(function (name) {
      const list = frame.stats && frame.stats[name];
      if (Array.isArray(list) && list.length) {
        list[0].predicted_tokens =
          (Number(list[0].predicted_tokens) || 0) + 890123;
      }
    });
    const stamps = (payload.history && payload.history.timestamps) || [];
    const newest = Math.max.apply(null, stamps.concat([payload.snapshot.ts || 0]));
    frame.ts = newest + 2;

    stream.onmessage({ data: JSON.stringify({
      snapshot: frame,
      point: { ts: frame.ts, values: {} },
    }) });
    live.pushed = true;

    const after = cardText();
    const pointsAfter = pointsOf();
    live.changed = before !== after;
    live.ledgerChanged = ledgerBefore !== ledgerText();
    live.ledger = !live.ledgerPatched ? '本次快照无 stats，跳过'
      : (live.ledgerChanged ? '已跟随更新' : '未变化（数字被冻结）');
    live.before = before;
    live.after = after;
    live.cards = live.changed ? '已跟随更新' : '未变化（数字被冻结）';
    live.history = (pointsBefore !== null && pointsAfter !== null)
      ? (pointsAfter === pointsBefore + 1
          ? '曲线点数 +1'
          : '未推进（' + pointsBefore + ' → ' + pointsAfter + '）')
      : '无法判断';
  }

  say('');
  say('—— 推帧更新（SSE 路径）——');
  say('  推帧: ' + (live.pushed ? '已推送一帧（GPU util=42 temp=63 power=512）' : '未创建 EventSource'));
  say('  卡片数字: ' + live.cards);
  if (live.after) {
    say('    前: ' + String(live.before).slice(0, 150));
    say('    后: ' + String(live.after).slice(0, 150));
  }
  say('  历史曲线: ' + live.history);
  say('  账本数字: ' + (live.ledger || 'n/a'));

  say('');
  say('—— 卡片 ——');
  const grids = [
    ['系统', '#sys'],
    // The per-slot breakdown reuses .grid.compact for its own six-card rows, so
    // scope this to the model's headline grid (direct child of .model), which
    // is the only one carrying a spark canvas.
    ['推理', '.endpoint .model > .grid'],
  ];
  grids.forEach(function (entry) {
    const cards = doc.querySelectorAll(entry[1] + ' .card');
    say(entry[0] + ' 组：' + cards.length + ' 张卡');
    cards.forEach(function (card) {
      const label = text(card.querySelector('.card-label'));
      const value = text(card.querySelector('.card-value'));
      const foot = text(card.querySelector('.card-foot'));
      const hidden = card.querySelector('.card-visual').className;
      const canvas = card.querySelector('canvas.spark');
      const geom = canvas && canvas._geom;
      say('   ' + label + ' = ' + value + ' | ' + foot +
        ' | 列数 ' + (geom ? geom.count : 0) + ' | ' + hidden);
    });
  });

  say('');
  say('—— 模型 ——');
  // The live model's name/port/tags now lead the section as a single hero line,
  // so report that first; any additional models keep their own title row.
  const hero = doc.querySelector('.hero');
  if (hero) {
    say('  running 模型: ' + text(hero.querySelector('.hero-name')) +
      ' :' + text(hero.querySelector('.hero-port')) +
      ' | 标签: ' + Array.prototype.map.call(hero.querySelectorAll('.tag'), text).join(' / '));
  }
  doc.querySelectorAll('.model').forEach(function (model) {
    const id = model.querySelector('.id');
    if (!id) return; // the hero model renders without a repeated title row
    say('  ' + text(id) + ' 标签: ' +
      Array.prototype.map.call(model.querySelectorAll('.tag'), text).join(' / '));
    const chips = model.querySelectorAll('.kv-item');
    say('  明细 ' + chips.length + ' 项: ' +
      Array.prototype.slice.call(chips, 0, 6).map(text).join(' · '));
    const alerts = model.querySelectorAll('.kv-item.alert');
    if (alerts.length) say('  告警: ' + Array.prototype.map.call(alerts, text).join(' | '));
  });
  const errBox = doc.querySelector('.err');
  if (errBox) say('  端点提示: [' + (errBox.className) + '] ' + text(errBox));

  say('');
  say('—— 功耗控制 ——');
  const powerBox = doc.getElementById('power-control');
  if (powerBox && powerBox.innerHTML) {
    const buttons = powerBox.querySelectorAll('button[data-watts]');
    const active = powerBox.querySelector('.pw-btn.on');
    if (buttons.length) {
      say('  档位按钮: ' + buttons.length + ' 个' +
        (active ? '，当前生效 ' + text(active) : '，无高亮'));
    } else {
      say('  档位按钮: 未提供（驱动不接受软件功耗墙）');
    }
    say('  状态区: ' + text(powerBox.querySelector('.pw-status')));
    say('  范围: ' + text(powerBox.querySelector('.pw-range')));
    say('  权限: ' + (powerBox.querySelector('.pw-hint') ? '只读' : '可写'));
    const hint = powerBox.querySelector('.pw-hint');
    if (hint) say('  只读提示: ' + text(hint));
  } else {
    say('  控制条未渲染（未提权或显卡不可用）');
  }

  say('');
  say('—— 历史信息 ——');
  const statsGrid = doc.getElementById('token-stats');
  if (statsGrid && statsGrid.innerHTML) {
    statsGrid.querySelectorAll('.stat').forEach(function (cell) {
      say('  ' + text(cell.querySelector('.stat-label')) + ' = ' +
        text(cell.querySelector('.stat-value')) + ' | ' +
        text(cell.querySelector('.stat-foot')));
    });
    const noteBox = doc.getElementById('token-note');
    say('  说明: [' + (noteBox ? noteBox.className : '') + '] ' + text(noteBox));
    const tierCards = doc.querySelectorAll('#token-tiers .tier');
    const tierLine = function (card) {
      return text(card.querySelector('.tier-head')) + ' — ' +
        Array.prototype.map.call(card.querySelectorAll('.tier-item'), text)
          .join(' · ');
    };
    say('  档位归集: ' + (tierCards.length ? tierCards.length + ' 档' : '无'));
    Array.prototype.forEach.call(tierCards, function (card) {
      say('    ' + tierLine(card) + (card.className.indexOf('now') >= 0 ? ' [当前]' : ''));
    });
    const tierNote = doc.getElementById('token-tier-note');
    say('  档位说明: [' + (tierNote ? tierNote.className : '') + '] ' + text(tierNote));
    const comboRows = doc.querySelectorAll('#token-combos tbody tr');
    const comboHead = doc.querySelectorAll('#token-combos thead th');
    say('  明细表: ' + comboRows.length + ' 行 / ' +
      Array.prototype.map.call(comboHead, text).join(' | '));
    Array.prototype.slice.call(comboRows, 0, 3).forEach(function (row) {
      say('    ' + text(row));
    });
    const rows = doc.querySelectorAll('#token-days tbody tr');
    say('  近 7 天表: ' + rows.length + ' 行' +
      (rows.length ? '（最新 ' + text(rows[0]) + '）' : ''));
  } else {
    say('  未渲染（服务端没有上报 stats）');
  }

  // Degradation path: a frame reporting the ledger as unavailable has to say
  // so, not leave the section blank or keep stale numbers on screen.
  const degrade = { ran: false, ok: true, text: '' };
  if (stream && statsGrid && statsGrid.innerHTML) {
    const offFrame = JSON.parse(JSON.stringify(payload.snapshot));
    offFrame.stats = { available: false, error: 'disabled' };
    offFrame.ts = Number(offFrame.ts || 0) + 4;
    stream.onmessage({ data: JSON.stringify({
      snapshot: offFrame,
      point: { ts: offFrame.ts, values: {} },
    }) });
    degrade.ran = true;
    degrade.text = ledgerText();
    degrade.placeholder = degrade.text.indexOf('未启用') >= 0;
    // Stale tier cards or detail rows left on screen after the ledger reports
    // itself unavailable would be a lie, not just a cosmetic leftover: the
    // numbers would still look like live totals.
    degrade.leaked =
      doc.querySelectorAll('#token-tiers .tier').length +
      doc.querySelectorAll('#token-combos tbody tr').length;
    degrade.ok = degrade.placeholder && !degrade.leaked;
    say('  降级渲染（stats.available=false）: ' +
      (degrade.placeholder ? '显示「未启用」✓' : '未显示占位 ✗') +
      (degrade.leaked ? ' / 残留 ' + degrade.leaked + ' 行 ✗'
                      : ' / 无残留 ✓'));
    // Restore the live figures so nothing after this reads a degraded panel.
    stream.onmessage({ data: JSON.stringify({
      snapshot: payload.snapshot,
      point: { ts: Number(payload.snapshot.ts || 0) + 6, values: {} },
    }) });
  }

  say('');
  say('—— 表与提示 ——');
  say('  ⓘ 提示数量: ' + doc.querySelectorAll('.tip').length);
  say('  canvas 绘制: stroke ' + paint.strokes + ' 次 / fill ' + paint.fills +
    ' 次 / fillText ' + paint.texts.length + ' 次');
  say('  轴标签样例: ' + paint.texts.slice(0, 12).join(' '));

  // None of these throw: the page loads fine and just stops being useful.
  //   * a frozen card means the pushed frame never reached the numbers;
  //   * a frozen ledger is the same failure on the newest panel, and the tier
  //     cards and detail rows are part of that diff;
  //   * a broken contract means the server is not sending what the page reads;
  //   * a failed degradation means an honest empty state turned into a lie --
  //     either no placeholder, or live-looking rows left behind.
  // All four are exactly what the user notices and what nothing else catches.
  const frozenCards = live.pushed && !live.changed;
  const frozenLedger = live.pushed && live.ledgerPatched && !live.ledgerChanged;
  const brokenContract = contract.frames >= 2 &&
    (!contract.hasSnapshot || !contract.hasPoint);
  const code = (errors.length || !routingOk || frozenCards || frozenLedger ||
    brokenContract || !degrade.ok) ? 1 : 0;

  // Exit explicitly.  The page arms a setInterval watchdog, which keeps the
  // jsdom event loop alive forever; leaving the process to die on its own
  // yields a SIGTERM instead of this exit code and hides the real result.
  // The callback form flushes stdout before quitting.
  process.stdout.write(lines.join('\n') + '\n', function () { process.exit(code); });
})().catch(function (error) {
  process.stderr.write('烟测本身失败: ' + error.stack + '\n', function () { process.exit(2); });
});
