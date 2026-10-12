"""End-to-end check of the idle-but-busy explanation on the real page.

Drives headless Chrome over CDP against http://127.0.0.1:8090 while traffic is
put through Strata:

  * one lone completion  -> every slot row says "idle", and the fold has to say
    why (engine handling N requests with nothing booked per slot);
  * two concurrent ones   -> rows switch to reading/decoding and that sentence
    has to disappear again.

Usage: python verify_slot_note.py [chrome_cdp_port]
"""
import asyncio
import json
import sys
import threading
import time
import urllib.request
from typing import Any

import websockets

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

EXPR = r"""(() => {
  const t = el => (el && el.textContent || '').replace(/\s+/g,' ').trim();
  const c = document.querySelector('.slot-fold [data-role="count"]');
  return {
    count: c ? t(c) : null,
    noteShown: !!(c && c.title),
    states: [...document.querySelectorAll('.slot-row .slot-state')].map(t),
    speeds: [...document.querySelectorAll('.slot-row [data-card="tok_s"] .card-value')].map(t),
    prefill: [...document.querySelectorAll('.slot-row [data-card="prefill"] .card-value')].map(t),
    decoded: [...document.querySelectorAll('.slot-row [data-card="decoded"] .card-value')].map(t),
  };
})()"""


def get(url):
    return json.loads(OP.open(url, timeout=20).read().decode())


def engine_model_id():
    """Ask the panel which model the Strata endpoint is actually serving."""
    snap = get('http://127.0.0.1:8090/api/snapshot')['snapshot']
    for e in snap.get('endpoints') or []:
        if 'strata' not in str(e.get('id')).lower() and 'strata' not in str(e.get('base_url')):
            continue
        for m in e.get('models') or []:
            if m.get('id'):
                return m['id']
    raise SystemExit('no loaded Strata model on the panel -- nothing to drive')


MODEL = None


def chat(prompt, max_tokens, sink):
    body = json.dumps({'model': MODEL,
                       'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': max_tokens, 'temperature': 0}).encode()
    req = urllib.request.Request('http://127.0.0.1:8200/v1/chat/completions',
                                 data=body, method='POST',
                                 headers={'Content-Type': 'application/json'})
    try:
        out = json.loads(OP.open(req, timeout=180).read().decode())
        sink.append(len(out['choices'][0]['message']['content']))
    except Exception as exc:                       # noqa: BLE001 - probe only
        sink.append(repr(exc)[:80])


class CDP:
    def __init__(self, ws_url):
        self.sock: Any = None      # filled in by the caller's connect()
        self.ws_url = ws_url
        self.n = 0

    async def cmd(self, method, **params):
        self.n += 1
        i = self.n
        await self.sock.send(json.dumps({'id': i, 'method': method, 'params': params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.sock.recv(), timeout=30))
            if msg.get('id') == i:
                if 'error' in msg:
                    raise RuntimeError('%s -> %s' % (method, msg['error']))
                return msg.get('result', {})


async def drive(port):
    pages = [p for p in get('http://127.0.0.1:%d/json' % port) if p.get('type') == 'page']
    cdp = CDP(pages[0]['webSocketDebuggerUrl'])
    async with websockets.connect(cdp.ws_url, max_size=64 * 1024 * 1024) as sock:
        cdp.sock = sock
        await cdp.cmd('Runtime.enable')

        async def peek():
            res = await cdp.cmd('Runtime.evaluate', expression=EXPR, returnByValue=True)
            return res.get('result', {}).get('value')

        # Navigate fresh so the page carries the just-restarted collector fields.
        await cdp.cmd('Page.navigate', url='http://127.0.0.1:8090/')
        await asyncio.sleep(8)

        print('-- idle baseline --')
        print(json.dumps(await peek(), ensure_ascii=False))

        one, two = [], []
        th = threading.Thread(target=chat, args=('从1数到150，一行一个数字，无解释。', 500, one), daemon=True)
        th.start()
        await asyncio.sleep(2.2)
        print('\n-- ONE request in flight (solo path expected) --')
        for _ in range(3):
            print(json.dumps(await peek(), ensure_ascii=False))
            await asyncio.sleep(0.9)
        th.join(timeout=60)
        print('completion:', one)

        a, b = [], []
        t1 = threading.Thread(target=chat, args=('从1数到140，一行一个数字，无解释。', 500, a), daemon=True)
        t2 = threading.Thread(target=chat, args=('从1000数到1140，一行一个数字，无解释。', 500, b), daemon=True)
        t1.start(); t2.start()
        await asyncio.sleep(2.6)
        print('\n-- TWO concurrent (per-slot rows expected) --')
        for _ in range(3):
            print(json.dumps(await peek(), ensure_ascii=False))
            await asyncio.sleep(0.9)
        t1.join(timeout=60); t2.join(timeout=60)
        print('completions:', a, b)

        print('\n-- back to quiet --')
        await asyncio.sleep(2)
        print(json.dumps(await peek(), ensure_ascii=False))


if __name__ == '__main__':
    MODEL = engine_model_id()
    print('driving model:', MODEL)
    asyncio.run(drive(int(sys.argv[1]) if len(sys.argv) > 1 else 9345))
