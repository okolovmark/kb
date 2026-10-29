// The Antigravity panel's notices: node --test tests/antigravity/*.test.mjs
// notices.mjs against a stub kb, and the panel's own client script, run in a vm with a stub DOM
// and a stub window.sidecar, for the block, the summary count and dismiss.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { dismissNotice, loadNotices, parseNotices } from '../../antigravity-plugin/sidecars/panel/notices.mjs';

const SERVER = new URL('../../antigravity-plugin/sidecars/panel/server.mjs', import.meta.url);
const NOTICE_A = { id: 901, scope: 'proj', title: 'kb upgraded', body: 'first line\n\nafter a <b>blank</b>' };
const NOTICE_B = { id: 902, scope: 'global', title: 'backup restored', body: '' };
const TASKS = [
  { id: 207, level: 3, name: 'loud', reason: '30 working days', title: 'part standard', scope: 'proj', sched: 'age', due: null },
  { id: 282, level: 2, name: 'normal', reason: 'decide (T+0)', title: 'payment terms', scope: 'proj', sched: 'soft', due: '2026-10-07' },
];

function stubKb(answer) {
  const calls = [];
  const runKb = async (args, cwd) => {
    calls.push({ args, cwd });
    return answer(args);
  };
  return { calls, runKb };
}

test('parseNotices keeps the four fields of each notice with an id', () => {
  const stdout = JSON.stringify({ date: '2026-10-07', scope: 'proj', notices: [NOTICE_A, { title: 'no id' }, null, NOTICE_B] });
  assert.deepEqual(parseNotices(stdout), [NOTICE_A, NOTICE_B]);
  assert.equal(parseNotices(JSON.stringify({ tasks: [] })), null);
  assert.equal(parseNotices('kb: NO MEMORY'), null);
});

test('loadNotices runs kb notices show, and gives [] when kb cannot answer', async () => {
  const ok = stubKb(() => ({ exitCode: 0, stdout: JSON.stringify({ notices: [NOTICE_A] }), stderr: '' }));
  assert.deepEqual(await loadNotices(ok.runKb, '/p'), [NOTICE_A]);
  assert.deepEqual(ok.calls, [{ args: ['--json', 'notices', 'show'], cwd: '/p' }]);
  const down = stubKb(() => ({ exitCode: 1, stdout: '', stderr: 'kb: NO MEMORY' }));
  assert.deepEqual(await loadNotices(down.runKb, '/p'), []);
});

test('dismissNotice runs kb notices dismiss <id>; a bad id never reaches kb', async () => {
  const ok = stubKb(() => ({ exitCode: 0, stdout: '[901] kb upgraded  · dismissed\n', stderr: '' }));
  assert.deepEqual(await dismissNotice(ok.runKb, '/p', 901), { ok: true, message: 'kb notices dismiss 901' });
  assert.deepEqual(ok.calls, [{ args: ['notices', 'dismiss', '901'], cwd: '/p' }]);

  const refused = stubKb(() => ({ exitCode: 1, stdout: '', stderr: 'Error: [207] is a task on schedule age; dismiss applies to --once notices\n' }));
  assert.deepEqual(await dismissNotice(refused.runKb, '/p', '207'), {
    ok: false,
    message: 'kb notices dismiss [207] failed: Error: [207] is a task on schedule age; dismiss applies to --once notices',
  });

  const none = stubKb(() => assert.fail('kb ran'));
  assert.equal((await dismissNotice(none.runKb, '/p', 'x')).ok, false);
  assert.equal(none.calls.length, 0);
});

// the panel's page: the HTML template literal evaluated as server.mjs does, then its <script>
function clientScript() {
  const src = readFileSync(SERVER, 'utf-8');
  const start = src.indexOf('const HTML = `') + 'const HTML = `'.length;
  const end = src.indexOf('</html>`;', start) + '</html>'.length;
  const html = new Function(`return \`${src.slice(start, end)}\``)();
  return html.slice(html.indexOf('<script>') + '<script>'.length, html.lastIndexOf('</script>'));
}

function element() {
  return { innerHTML: '', textContent: '', style: {}, value: '', onclick: null, addEventListener() {} };
}

async function panel(notices) {
  const elements = new Map();
  const calls = [];
  const sidecar = {
    conversationId: '',
    getWorkspaceUris: async () => ['file:///p'],
    onWorkspaceChange() {},
    agent: { sendMessage: async () => {} },
    fetch: async (path, opts) => {
      const body = JSON.parse(opts?.body || '{}');
      calls.push([path, body]);
      let data = {};
      if (path === '/api/state') {
        data = {
          workspaceName: 'p',
          today: { date: '2026-10-26', scope: 'proj', tasks: TASKS, noSummary: 0, counts: { loud: 1, normal: 1, others: 0, due: 1 } },
          notices: notices.slice(),
          journal: null,
        };
      } else if (path === '/api/dismiss') {
        notices.splice(notices.findIndex((n) => n.id === body.id), 1);
        data = { ok: true, message: `kb notices dismiss ${body.id}` };
      }
      return { json: async () => data };
    },
  };
  const context = vm.createContext({
    window: { sidecar },
    document: {
      getElementById: (id) => elements.get(id) ?? elements.set(id, element()).get(id),
      querySelectorAll: () => [],
    },
    setTimeout: () => 0,
    setInterval: () => 0,
  });
  vm.runInContext(clientScript(), context);
  await new Promise((resolve) => setImmediate(resolve));
  return { calls, context, html: (id) => elements.get(id)?.innerHTML ?? '' };
}

test('the panel draws the notices above the legend, a card each, and counts them in the summary', async () => {
  const view = await panel([{ ...NOTICE_A }, { ...NOTICE_B }]);
  const content = view.html('content');
  assert.ok(content.includes('id="notices-block"'));
  assert.ok(content.indexOf('notices-block') < content.indexOf('class="legend"'));
  assert.ok(content.indexOf('notices-block') < content.indexOf('[207]'));
  assert.ok(content.includes('<strong>kb upgraded</strong>') && content.includes('<strong>backup restored</strong>'));
  // the body whole and escaped, its line breaks left to pre-wrap; no body, no body div
  assert.ok(content.includes('<div class="notice-body">first line\n\nafter a &lt;b&gt;blank&lt;/b&gt;</div>'));
  assert.equal(content.split('class="notice-body"').length - 1, 1);
  assert.ok(content.includes('class="dismiss-btn" data-id="901"') && content.includes('class="dismiss-btn" data-id="902"'));
  const summary = view.html('summary-bar');
  assert.ok(summary.includes('2 notices'));
  assert.ok(summary.indexOf('2 notices') < summary.indexOf('1 loud'));
  assert.ok(summary.includes('1 normal'));
});

test('dismiss calls the server, reloads, and the block leaves with the last notice', async () => {
  const view = await panel([{ ...NOTICE_A }, { ...NOTICE_B }]);
  await view.context.dismissNotice(901);
  assert.deepEqual(view.calls.map(([path, body]) => [path, body.id ?? null]), [
    ['/api/state', null],
    ['/api/dismiss', 901],
    ['/api/state', null],
  ]);
  assert.ok(!view.html('content').includes('data-id="901"'));
  assert.ok(view.html('summary-bar').includes('1 notice<'));
  await view.context.dismissNotice(902);
  assert.ok(!view.html('content').includes('notices-block'));
  assert.ok(!view.html('summary-bar').includes('notice'));
  assert.ok(view.html('content').includes('[207]'));
});
