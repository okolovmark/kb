// The notices block's server side: `kb --json notices show` feeds it, `kb notices dismiss <id>`
// closes one. runKb is the server's own, passed in so a test can stand a stub in for kb.

export function parseNotices(stdout) {
  let raw;
  try {
    raw = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.notices)) return null;
  const text = (value) => (typeof value === 'string' ? value : '');
  return raw.notices
    .filter((n) => n && typeof n === 'object' && typeof n.id === 'number')
    .map((n) => ({ id: n.id, scope: text(n.scope), title: text(n.title), body: text(n.body) }));
}

// `show` only prints them, so a refresh never closes one; [] when kb cannot answer: no block
export async function loadNotices(runKb, cwd) {
  const res = await runKb(['--json', 'notices', 'show'], cwd);
  return (res.exitCode === 0 && parseNotices(res.stdout)) || [];
}

export async function dismissNotice(runKb, cwd, id) {
  const num = Number(id);
  if (!Number.isInteger(num) || num <= 0) {
    return { ok: false, message: `kb notices dismiss: not a record id: ${String(id)}` };
  }
  const res = await runKb(['notices', 'dismiss', String(num)], cwd);
  return {
    ok: res.exitCode === 0,
    message:
      res.exitCode === 0
        ? `kb notices dismiss ${num}`
        : `kb notices dismiss [${num}] failed: ${(res.stderr || res.stdout).trim().split('\n')[0]}`,
  };
}
