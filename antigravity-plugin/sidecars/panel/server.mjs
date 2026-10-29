import { execFile } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync, watchFile, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { SidecarApp } from 'sidecar_sdk';
import { dismissNotice, loadNotices } from './notices.mjs';

const execFileAsync = promisify(execFile);

// a synced update of either module restarts the sidecar (restart_policy always)
for (const url of [import.meta.url, new URL('./notices.mjs', import.meta.url)]) {
  watchFile(fileURLToPath(url), { interval: 1000 }, () => process.exit(1));
}

const HOME = homedir();
const GEMINI_CONFIG_DIR = join(HOME, '.gemini', 'config');
const ANTIGRAVITY_DIR = process.env.ANTIGRAVITY_APP_DATA_DIR || join(HOME, '.gemini', 'antigravity');
const BRAIN_DIR = join(ANTIGRAVITY_DIR, 'brain');
const PLUGIN_DATA_DIR = join(ANTIGRAVITY_DIR, 'plugin_data', 'kb');

const LEVELS = [
  { level: 4, name: 'scream', glyph: '◉', color: '#e879f9', badgeClass: 'badge-purple' },
  { level: 3, name: 'loud', glyph: '●', color: '#f87171', badgeClass: 'badge-red' },
  { level: 2, name: 'normal', glyph: '○', color: '#fbbf24', badgeClass: 'badge-amber' },
  { level: 0, name: 'others', glyph: '◦', color: '#94a3b8', badgeClass: 'badge-muted' },
];

function levelStyle(level) {
  return LEVELS.find((s) => Number(level) >= s.level) || LEVELS[3];
}

function plusDays(isoDate, days) {
  const base = isoDate && /^\d{4}-\d{2}-\d{2}$/.test(isoDate)
    ? new Date(`${isoDate}T00:00:00Z`)
    : new Date();
  base.setUTCDate(base.getUTCDate() + days);
  return base.toISOString().slice(0, 10);
}

function enrichedEnv(extra = {}) {
  const nixPaths = [
    join(HOME, '.nix-profile', 'bin'),
    '/nix/var/nix/profiles/default/bin',
    join(HOME, '.local', 'bin'),
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
  ];
  const currentPath = process.env.PATH || '';
  return {
    ...process.env,
    PATH: [...nixPaths, currentPath].filter(Boolean).join(':'),
    ...extra,
  };
}

async function runKb(args, cwd, extraEnv = {}) {
  try {
    const { stdout, stderr } = await execFileAsync('kb', args, {
      cwd: cwd || process.cwd(),
      env: enrichedEnv(extraEnv),
      timeout: 45000,
      maxBuffer: 10 * 1024 * 1024,
    });
    return { exitCode: 0, stdout: stdout || '', stderr: stderr || '' };
  } catch (err) {
    return {
      exitCode: typeof err.code === 'number' ? err.code : 1,
      stdout: err.stdout || '',
      stderr: err.stderr || err.message || String(err),
    };
  }
}

async function lsrpc(method, body = {}) {
  const lsAddr = process.env.ANTIGRAVITY_LS_ADDRESS;
  const csrf = process.env.ANTIGRAVITY_CSRF_TOKEN;
  if (!lsAddr || !csrf) return null;
  try {
    const res = await fetch(
      `http://${lsAddr}/exa.language_server_pb.LanguageServerService/${method}`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-codeium-csrf-token': csrf,
        },
        body: JSON.stringify(body),
      }
    );
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

function safeReadJson(filePath) {
  try {
    if (!existsSync(filePath)) return null;
    return JSON.parse(readFileSync(filePath, 'utf-8'));
  } catch {
    return null;
  }
}

function uriToPath(uri) {
  if (!uri) return '';
  if (uri.startsWith('file://')) {
    try {
      return fileURLToPath(uri);
    } catch {
      return uri.replace(/^file:\/\//, '');
    }
  }
  return uri;
}

function loadKnownProjects() {
  const projectsDir = join(GEMINI_CONFIG_DIR, 'projects');
  const projects = [];
  if (!existsSync(projectsDir)) return projects;
  for (const file of readdirSync(projectsDir)) {
    if (!file.endsWith('.json')) continue;
    const data = safeReadJson(join(projectsDir, file));
    if (!data) continue;
    const uris = [];
    for (const r of data?.projectResources?.resources || []) {
      const folderUri = r?.gitFolder?.folderUri || r?.folderUri;
      if (folderUri) uris.push(folderUri);
    }
    if (uris.length > 0) {
      projects.push({
        id: data.id || basename(file, '.json'),
        name: data.name || basename(uriToPath(uris[0])),
        workspaceUris: uris,
      });
    }
  }
  return projects;
}

function parseToday(stdout) {
  let raw;
  try {
    raw = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.tasks)) return null;
  const tasks = raw.tasks;
  const counts = {
    loud: tasks.filter((t) => t.level >= 3).length,
    normal: tasks.filter((t) => t.level === 2).length,
    others: tasks.filter((t) => t.level <= 1).length,
    due: tasks.filter((t) => t.due !== null && t.due !== undefined).length,
  };
  return {
    date: raw.date || new Date().toISOString().slice(0, 10),
    scope: raw.scope || '',
    tasks,
    noSummary: Array.isArray(raw.no_summary) ? raw.no_summary.length : 0,
    counts,
  };
}

function parseNode(stdout) {
  let r;
  try {
    r = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (!r || typeof r !== 'object' || typeof r.id !== 'number') return null;
  const urgency = r.urgency;
  const links = Array.isArray(r.links) ? r.links : [];
  const events = Array.isArray(r.events) ? r.events : [];
  return {
    id: r.id,
    kind: typeof r.kind === 'string' ? r.kind : '',
    scope: typeof r.scope === 'string' ? r.scope : '',
    title: typeof r.title === 'string' ? r.title : '',
    body: typeof r.body === 'string' ? r.body : '',
    tags: Array.isArray(r.tags) ? r.tags.map(String) : [],
    sched: typeof r.sched === 'string' ? r.sched : '',
    due: typeof r.due === 'string' ? r.due : null,
    isDone: typeof r.done_at === 'string',
    urgency:
      urgency && typeof urgency.level === 'number'
        ? {
            level: urgency.level,
            name: urgency.name || '',
            reason: urgency.reason || '',
          }
        : null,
    links: links
      .filter((l) => typeof l.id === 'number')
      .map((l) => ({
        type: String(l.type || ''),
        direction: String(l.direction || ''),
        id: l.id,
        title: String(l.title || ''),
      })),
    events: events.slice(0, 10).map((ev) => ({
      kind: String(ev.kind || ''),
      at: String(ev.at || '').slice(0, 16).replace('T', ' '),
    })),
  };
}

const KB_WRITE_RE =
  /(?:^|[;&|(]\s*)(?:[A-Z_][A-Z0-9_]*=\S+\s+)*kb\s+(add|append|done|edit|link|promote|archive|snooze|skip|session\s+note)\b([^\n;&|]*)/g;
const SUMMARY_WRITE_RE =
  /(?:^|[;&|(]\s*)(?:[A-Z_][A-Z0-9_]*=\S+\s+)*kb\s+session\s+close\b[^\n;&|]*--(?:body|body-file|title)\b/m;
const COMMIT_LINE_RE = /^\[([^\]\s]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.+)$/gm;
const PR_URL_RE = /https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+/g;

function addUnique(list, items) {
  for (const item of items) {
    if (item && !list.includes(item)) list.push(item);
  }
}

function relativeTo(pathStr, root) {
  if (!pathStr) return '';
  const prefix = `${root.replace(/\/+$/, '')}/`;
  return pathStr.startsWith(prefix) ? pathStr.slice(prefix.length) : pathStr;
}

function unwrapArg(val) {
  if (typeof val !== 'string') return val;
  if (val.startsWith('"') && val.endsWith('"')) {
    try {
      return JSON.parse(val);
    } catch {
      return val.slice(1, -1);
    }
  }
  return val;
}

async function resolveActiveConversationId(requestedConvId, activeUris) {
  if (requestedConvId) return requestedConvId;
  const lsTrajRes = await lsrpc('GetAllCascadeTrajectories', {});
  const summariesObj = lsTrajRes?.trajectorySummaries || {};
  const allTrajs = Object.entries(summariesObj).map(([cascadeId, t]) => {
    const wsUris = [
      ...(t.workspaces || []).map((w) => w.workspaceFolderAbsoluteUri).filter(Boolean),
      ...(t.trajectoryMetadata?.workspaceUris || []),
    ];
    return {
      cascadeId,
      lastModifiedTime: t.lastModifiedTime || t.annotations?.lastUserViewTime || '',
      wsUris,
    };
  });
  const matching = allTrajs.filter(
    (t) => activeUris.length === 0 || t.wsUris.some((u) => activeUris.includes(u))
  );
  const pool = matching.length > 0 ? matching : allTrajs;
  pool.sort((a, b) => new Date(b.lastModifiedTime || 0) - new Date(a.lastModifiedTime || 0));
  return pool[0]?.cascadeId || process.env.ANTIGRAVITY_CONVERSATION_ID || '';
}

function scanSessionJournal(convId, wsRoot) {
  const journal = {
    conversationId: convId || '',
    files: [],
    commits: [],
    prs: [],
    kbWrites: [],
    summarized: false,
    mark: 0,
    sinceSummary: 0,
  };
  if (!convId) return journal;

  const transcriptPath = join(BRAIN_DIR, convId, '.system_generated', 'logs', 'transcript.jsonl');
  if (existsSync(transcriptPath)) {
    try {
      const lines = readFileSync(transcriptPath, 'utf-8').split('\n');
      for (const line of lines) {
        if (!line.trim()) continue;
        let step;
        try {
          step = JSON.parse(line);
        } catch {
          continue;
        }
        for (const tc of step.tool_calls || []) {
          const name = tc.name || '';
          const args = tc.args || {};
          if (
            name === 'write_file' ||
            name === 'edit_file' ||
            name === 'write_to_file' ||
            name === 'replace_file_content' ||
            name === 'multi_replace_file_content'
          ) {
            const rawPath = unwrapArg(args.Path || args.TargetFile || '');
            if (rawPath && !rawPath.includes('/.gemini/antigravity/brain/')) {
              addUnique(journal.files, [relativeTo(rawPath, wsRoot)]);
            }
          } else if (name === 'run_command') {
            const cmd = unwrapArg(args.CommandLine || '') || '';
            const kbMatches = [...cmd.matchAll(KB_WRITE_RE)].map((m) =>
              `kb ${m[1].replace(/\s+/g, ' ')}${m[2]}`.trim().slice(0, 120)
            );
            addUnique(journal.kbWrites, kbMatches);
            if (SUMMARY_WRITE_RE.test(cmd)) {
              journal.summarized = true;
              journal.mark =
                journal.files.length +
                journal.commits.length +
                journal.prs.length +
                journal.kbWrites.length;
            }
          }
        }
        if (step.type === 'GENERIC' && typeof step.content === 'string') {
          const commits = [...step.content.matchAll(COMMIT_LINE_RE)].map(
            (m) => `${m[2]} ${m[3]} (${m[1]})`
          );
          const prs = [...new Set(step.content.match(PR_URL_RE) || [])];
          addUnique(journal.commits, commits);
          addUnique(journal.prs, prs);
        }
      }
    } catch {}
  }

  // Merge stored journal facts if any
  const storedAll = safeReadJson(join(PLUGIN_DATA_DIR, 'journals.json')) || {};
  const stored = storedAll[convId];
  if (stored) {
    addUnique(journal.files, stored.files || []);
    addUnique(journal.commits, stored.commits || []);
    addUnique(journal.prs, stored.prs || []);
    addUnique(journal.kbWrites, stored.kbWrites || []);
    if (stored.summarized && !journal.summarized) {
      journal.summarized = true;
      journal.mark = stored.mark || 0;
    }
  }

  const totalFacts =
    journal.files.length + journal.commits.length + journal.prs.length + journal.kbWrites.length;
  journal.sinceSummary = journal.summarized ? Math.max(0, totalFacts - journal.mark) : 0;
  return journal;
}

function buildClosePrompt(scope, sessionId, journal) {
  const scopeFlag = scope ? ` --scope ${scope}` : '';
  const listed = (items) => (items.length === 0 ? '  - none' : items.map((x) => `  - ${x}`).join('\n'));
  return [
    'We are closing this Antigravity session. Do the full session close, in order:',
    `1. Open threads. Every unfinished piece of work, follow-up, bug noticed in passing or "we should also…" from this conversation goes into kb (${'`'}kb add "<title>"${scopeFlag}${'`'} with the right --due/--plan/--window, and ${'`'}kb link${'`'} to related records), or an append to the existing record. Nothing stays only in the chat. A decision only I can make is not a task you file on my behalf: it is a question for step 5.`,
    '2. Lessons. What this session taught that a later session needs (a gotcha, a rule I gave, a fact about this environment) goes into kb as a note, feedback or reference; update an existing record rather than add a duplicate.',
    '3. Repositories. Check every repository this session changed: git status, unpushed commits, open PRs. Commit and push only what I already asked to ship; list anything left uncommitted or unpushed and do not act on it.',
    `4. Journal. Close the kb session record with ${'`'}kb session close --id ${sessionId} --title "<title>" --body-file <file>${'`'} with Intent, Tags (include #antigravity), Files touched, Done, Decisions, Open threads. Recorded facts from this session:\n- files:\n${listed(journal.files)}\n- commits:\n${listed(journal.commits)}\n- pull requests:\n${listed(journal.prs)}\n- kb writes:\n${listed(journal.kbWrites)}`,
    '5. Report to me, in my language: what went where (record ids), what is left open, and every question you still have for me.',
  ].join('\n');
}

const HTML = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>kb</title>
  <script src="/preload.js"></script>
  <style>
    :root {
      color-scheme: dark;
      --wi-bg: #13161f;
      --wi-fg: #f8fafc;
      --wi-text-sec: #cbd5e1;
      --wi-text-dim: #94a3b8;
      --wi-card: #1c2130;
      --wi-card-hover: #252b3d;
      --wi-border: rgba(148, 163, 184, 0.22);
      --wi-accent: #60a5fa;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0 !important;
      padding: 14px !important;
      background: var(--wi-bg) !important;
      color: var(--wi-fg) !important;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 13px;
      line-height: 1.45;
    }
    .header {
      display: flex;
      flex-direction: column;
      gap: 9px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--wi-border);
      margin-bottom: 12px;
    }
    .title-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .title {
      font-size: 15px;
      font-weight: 600;
      color: var(--wi-fg);
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .bar-row {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    input[type="search"], input[type="text"] {
      background-color: var(--wi-card);
      color: var(--wi-fg);
      border: 1px solid var(--wi-border);
      border-radius: 6px;
      padding: 6px 9px;
      font-size: 12px;
      outline: none;
      flex: 1;
      min-width: 140px;
    }
    input::placeholder { color: var(--wi-text-dim); }
    input:focus { border-color: var(--wi-accent); }
    button {
      background: var(--wi-card);
      color: var(--wi-fg);
      border: 1px solid var(--wi-border);
      border-radius: 6px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }
    button:hover {
      background: var(--wi-card-hover);
      border-color: rgba(148, 163, 184, 0.45);
    }
    button.btn-primary {
      background: rgba(96, 165, 250, 0.2);
      border-color: rgba(96, 165, 250, 0.45);
      color: #93c5fd;
    }
    button.btn-danger {
      background: rgba(248, 113, 113, 0.2);
      border-color: rgba(248, 113, 113, 0.45);
      color: #fca5a5;
    }
    .card {
      background: var(--wi-card);
      border: 1px solid var(--wi-border);
      border-radius: 8px;
      padding: 10px 12px;
      margin-bottom: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      border: 1px solid transparent;
    }
    .badge-ws {
      background: rgba(96, 165, 250, 0.18);
      color: #93c5fd;
      border-color: rgba(96, 165, 250, 0.38);
    }
    .badge-ok {
      background: rgba(52, 211, 153, 0.18);
      color: #6ee7b7;
      border-color: rgba(52, 211, 153, 0.38);
    }
    .badge-amber {
      background: rgba(251, 191, 36, 0.18);
      color: #fde68a;
      border-color: rgba(251, 191, 36, 0.38);
    }
    .badge-red {
      background: rgba(248, 113, 113, 0.18);
      color: #fca5a5;
      border-color: rgba(248, 113, 113, 0.38);
    }
    .badge-purple {
      background: rgba(192, 132, 252, 0.18);
      color: #d8b4fe;
      border-color: rgba(192, 132, 252, 0.38);
    }
    .badge-muted {
      background: rgba(148, 163, 184, 0.16);
      color: #e2e8f0;
      border-color: rgba(148, 163, 184, 0.32);
    }
    .badge-notice {
      background: rgba(45, 212, 191, 0.16);
      color: #5eead4;
      border-color: rgba(45, 212, 191, 0.38);
    }
    .notice-row-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .notice-body {
      white-space: pre-wrap;
      word-break: break-word;
      color: var(--wi-text-sec);
      margin-top: 6px;
    }
    .group-header {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 12px 0 6px 0;
      font-weight: 700;
      font-size: 13px;
    }
    .task-row-top {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      font-weight: 500;
    }
    .task-id {
      color: var(--wi-text-dim);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      flex-shrink: 0;
    }
    .task-row-bottom {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-top: 7px;
      flex-wrap: wrap;
    }
    .task-reason {
      font-size: 11.5px;
      color: var(--wi-text-dim);
    }
    .task-reason.due {
      color: #fbbf24;
      font-weight: 500;
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 5px;
      flex-wrap: wrap;
    }
    .legend {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 11.5px;
      color: var(--wi-text-dim);
      margin-bottom: 10px;
      padding: 7px 10px;
      background: rgba(28, 33, 48, 0.55);
      border-radius: 6px;
      border: 1px solid var(--wi-border);
    }
    .md-body {
      font-size: 13px;
      line-height: 1.55;
      color: var(--wi-fg);
      word-break: break-word;
    }
    .md-body h1, .md-body h2, .md-body h3 {
      margin: 10px 0 6px 0;
      font-size: 14px;
      color: #93c5fd;
    }
    .md-body p { margin: 6px 0; }
    .md-body ul, .md-body ol { margin: 6px 0; padding-left: 20px; }
    .md-body code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11.5px;
      background: rgba(15, 23, 42, 0.7);
      padding: 1px 5px;
      border-radius: 4px;
      color: #6ee7b7;
    }
    .md-body pre {
      background: rgba(15, 23, 42, 0.8);
      padding: 8px 10px;
      border-radius: 6px;
      overflow-x: auto;
      border: 1px solid var(--wi-border);
    }
    .md-body pre code {
      background: none;
      padding: 0;
      color: var(--wi-fg);
    }
    .toast {
      position: fixed;
      bottom: 12px;
      right: 12px;
      background: #1e293b;
      color: #f8fafc;
      border: 1px solid var(--wi-accent);
      border-radius: 6px;
      padding: 7px 12px;
      font-size: 12px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.4);
      z-index: 100;
      display: none;
    }
    details {
      margin-top: 6px;
      border-top: 1px solid var(--wi-border);
      padding-top: 6px;
    }
    summary {
      cursor: pointer;
      font-size: 11.5px;
      color: #93c5fd;
      user-select: none;
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 11px;
      color: var(--wi-text-sec);
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="title-row">
      <div class="title">
        <span>kb</span>
        <span id="scope-badge" class="badge badge-ws">loading...</span>
        <span id="date-badge" class="badge badge-muted"></span>
      </div>
      <div class="bar-row">
        <button id="today-btn" style="display:none;">← today</button>
        <button id="refresh-btn">Refresh</button>
      </div>
    </div>
    <div class="bar-row">
      <input type="search" id="search-input" placeholder="Filter tasks, or enter record ID (e.g. 881) / search query..." />
      <button id="open-id-btn" title="Open record by ID or search kb">Search / Open</button>
    </div>
  </div>

  <div id="session-bar"></div>
  <div id="summary-bar" class="bar-row" style="margin-bottom:10px;"></div>
  <div id="content">Loading kb standup...</div>
  <div id="toast" class="toast"></div>

  <script>
    const LEVELS = [
      { level: 4, name: 'scream', glyph: '◉', color: '#e879f9' },
      { level: 3, name: 'loud', glyph: '●', color: '#f87171' },
      { level: 2, name: 'normal', glyph: '○', color: '#fbbf24' },
      { level: 0, name: 'others', glyph: '◦', color: '#94a3b8' },
    ];
    function levelStyle(level) {
      return LEVELS.find((s) => Number(level) >= s.level) || LEVELS[3];
    }
    // the notices block's header and the summary's count of it
    const NOTICE = { glyph: '✉', color: '#5eead4' };

    let state = null;
    let viewingNode = null;
    let searchResults = null;
    let showOthers = false;
    let armedTaskId = null;
    let busyTaskId = null;
    let isClosingSession = false;
    let filterText = '';
    let workspaceUris = [];

    function showToast(msg) {
      const el = document.getElementById('toast');
      el.textContent = msg;
      el.style.display = 'block';
      setTimeout(() => { el.style.display = 'none'; }, 3500);
    }

    function escapeHtml(s) {
      return String(s || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function renderMarkdown(md) {
      if (!md) return '<em style="color:var(--wi-text-dim);">_no body_</em>';
      let html = escapeHtml(md);
      html = html.replace(/\`\`\`([\\s\\S]*?)\`\`\`/g, (_, code) => '<pre><code>' + code.trim() + '</code></pre>');
      html = html.replace(/\`([^\`\\n]+)\`/g, '<code>$1</code>');
      html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
      html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
      html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
      html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
      html = html.replace(/\\[(\\d+)\\]/g, '<a href="#" class="kb-ref-link" data-id="$1" style="color:#60a5fa;text-decoration:none;font-weight:600;">[$1]</a>');
      html = html.replace(/\\n\\n+/g, '</p><p>');
      html = '<p>' + html.replace(/\\n/g, '<br/>') + '</p>';
      return html;
    }

    async function loadStandup() {
      const res = await window.sidecar.fetch('/api/state', {
        method: 'POST',
        body: JSON.stringify({
          workspaceUris,
          conversationId: window.sidecar.conversationId || '',
        }),
      });
      state = await res.json();
      render();
    }

    async function openNode(id) {
      const numId = Number(id);
      if (!Number.isInteger(numId) || numId <= 0) return;
      document.getElementById('content').innerHTML = \`<div class="card">Loading [\${numId}]...</div>\`;
      const res = await window.sidecar.fetch('/api/node', {
        method: 'POST',
        body: JSON.stringify({ id: numId, workspaceUris }),
      });
      const data = await res.json();
      if (!data.node) {
        showToast(data.error || \`kb show \${numId} failed\`);
        render();
        return;
      }
      viewingNode = data.node;
      searchResults = null;
      armedTaskId = null;
      render();
    }

    async function actOnTask(task, verb) {
      busyTaskId = task.id;
      armedTaskId = null;
      render();
      try {
        const res = await window.sidecar.fetch('/api/act', {
          method: 'POST',
          body: JSON.stringify({
            id: task.id,
            verb,
            date: state?.today?.date || '',
            workspaceUris,
          }),
        });
        const out = await res.json();
        showToast(out.message || \`kb \${verb} [\${task.id}]\`);
        await loadStandup();
        if (viewingNode && viewingNode.id === task.id) {
          await openNode(task.id);
        }
      } finally {
        busyTaskId = null;
        render();
      }
    }

    async function dismissNotice(id) {
      busyTaskId = id;
      render();
      try {
        const res = await window.sidecar.fetch('/api/dismiss', {
          method: 'POST',
          body: JSON.stringify({ id, workspaceUris }),
        });
        const out = await res.json();
        showToast(out.message || \`kb notices dismiss \${id}\`);
        await loadStandup();
      } finally {
        busyTaskId = null;
        render();
      }
    }

    async function runSearchOrOpen() {
      const q = document.getElementById('search-input').value.trim();
      if (!q) {
        searchResults = null;
        viewingNode = null;
        render();
        return;
      }
      if (/^\\[?\\d+\\]?$/.test(q)) {
        const id = Number(q.replace(/\\[|\\]/g, ''));
        await openNode(id);
        return;
      }
      const res = await window.sidecar.fetch('/api/search', {
        method: 'POST',
        body: JSON.stringify({ query: q, workspaceUris }),
      });
      const data = await res.json();
      viewingNode = null;
      searchResults = data.results || [];
      render();
    }

    function renderSessionBar() {
      const el = document.getElementById('session-bar');
      const j = state?.journal;
      if (!el || !j) {
        if (el) el.innerHTML = '';
        return;
      }
      const statusBadge = !j.summarized
        ? '<span class="badge badge-amber">not closed</span>'
        : j.sinceSummary > 0
          ? \`<span class="badge badge-ok">closed</span> <span class="badge badge-amber">\${j.sinceSummary} new since closing</span>\`
          : '<span class="badge badge-ok">✓ closed</span>';

      const canClose = !j.summarized || j.sinceSummary > 0;
      const closeBtn = canClose
        ? isClosingSession
          ? '<span class="badge badge-muted">closing…</span>'
          : '<button id="close-session-btn" class="btn-primary">close session</button>'
        : '';

      const hasFacts =
        (j.files?.length || 0) +
          (j.commits?.length || 0) +
          (j.prs?.length || 0) +
          (j.kbWrites?.length || 0) >
        0;

      el.innerHTML = \`
        <div class="card">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;">
            <div class="bar-row">
              <strong style="font-size:12px;text-transform:uppercase;color:var(--wi-text-sec);letter-spacing:0.04em;">Session</strong>
              \${statusBadge}
              <span style="font-size:12px;color:var(--wi-text-dim);">
                \${j.files?.length || 0} files · \${j.commits?.length || 0} commits · \${j.prs?.length || 0} PRs · \${j.kbWrites?.length || 0} kb writes
              </span>
            </div>
            <div>\${closeBtn}</div>
          </div>
          \${hasFacts ? \`
            <details>
              <summary>Show recorded session facts (\${escapeHtml((j.conversationId || '').slice(0, 8))})</summary>
              <div class="mono" style="margin-top:6px;display:flex;flex-direction:column;gap:4px;">
                \${(j.files || []).map((f) => \`<div>📄 \${escapeHtml(f)}</div>\`).join('')}
                \${(j.commits || []).map((c) => \`<div>🔨 \${escapeHtml(c)}</div>\`).join('')}
                \${(j.prs || []).map((p) => \`<div>🔗 \${escapeHtml(p)}</div>\`).join('')}
                \${(j.kbWrites || []).map((k) => \`<div>🧠 \${escapeHtml(k)}</div>\`).join('')}
              </div>
            </details>
          \` : ''}
        </div>\`;

      const closeEl = document.getElementById('close-session-btn');
      if (closeEl) {
        closeEl.onclick = async () => {
          isClosingSession = true;
          renderSessionBar();
          try {
            const res = await window.sidecar.fetch('/api/close-prompt', {
              method: 'POST',
              body: JSON.stringify({
                workspaceUris,
                conversationId: j.conversationId || window.sidecar.conversationId || '',
              }),
            });
            const data = await res.json();
            if (data.prompt) {
              await window.sidecar.agent.sendMessage(
                data.prompt,
                j.conversationId || window.sidecar.conversationId || undefined
              );
              showToast('Session close prompt sent to agent');
            }
          } catch (err) {
            showToast('Failed to send close prompt: ' + err.message);
            isClosingSession = false;
            renderSessionBar();
          }
        };
      }
    }

    function renderTaskControls(task) {
      if (busyTaskId === task.id) {
        return '<span style="color:var(--wi-text-dim);font-size:12px;">working…</span>';
      }
      if (armedTaskId === task.id) {
        return \`
          <div class="controls">
            <span style="color:#fbbf24;font-size:12px;font-weight:600;">close [\${task.id}]?</span>
            <button class="btn-danger act-btn" data-id="\${task.id}" data-verb="done">yes, close</button>
            <button class="cancel-arm-btn">no</button>
          </div>\`;
      }
      return \`
        <div class="controls">
          <button class="open-node-btn" data-id="\${task.id}">open</button>
          <button class="arm-done-btn" data-id="\${task.id}">done</button>
          <button class="act-btn" data-id="\${task.id}" data-verb="snooze">snooze</button>
          <button class="act-btn" data-id="\${task.id}" data-verb="week">+7d</button>
          \${task.sched === 'window' ? \`<button class="act-btn" data-id="\${task.id}" data-verb="skip">skip</button>\` : ''}
        </div>\`;
    }

    // a card per waiting notice: title, the whole body (pre-wrap keeps its line breaks), dismiss
    function renderNotices(notices) {
      if (!Array.isArray(notices) || notices.length === 0) return '';
      return \`
        <div id="notices-block">
          <div class="group-header">
            <span style="color:\${NOTICE.color};">\${NOTICE.glyph} notices</span>
            <span style="color:var(--wi-text-dim);font-weight:400;">\${notices.length}</span>
          </div>
          \${notices.map((n) => \`
            <div class="card">
              <div class="notice-row-top">
                <div class="task-row-top">
                  <span class="task-id">[\${n.id}]</span>
                  <strong>\${escapeHtml(n.title)}</strong>
                </div>
                \${busyTaskId === n.id
                  ? '<span style="color:var(--wi-text-dim);font-size:12px;">working…</span>'
                  : \`<button class="dismiss-btn" data-id="\${n.id}">dismiss</button>\`}
              </div>
              \${n.body ? \`<div class="notice-body">\${escapeHtml(n.body)}</div>\` : ''}
            </div>
          \`).join('')}
        </div>\`;
    }

    function bindControlEvents() {
      document.querySelectorAll('.dismiss-btn').forEach((btn) => {
        btn.onclick = () => dismissNotice(Number(btn.dataset.id));
      });
      document.querySelectorAll('.open-node-btn').forEach((btn) => {
        btn.onclick = () => openNode(Number(btn.dataset.id));
      });
      document.querySelectorAll('.kb-ref-link').forEach((a) => {
        a.onclick = (e) => {
          e.preventDefault();
          openNode(Number(a.dataset.id));
        };
      });
      document.querySelectorAll('.arm-done-btn').forEach((btn) => {
        btn.onclick = () => {
          armedTaskId = Number(btn.dataset.id);
          render();
        };
      });
      document.querySelectorAll('.cancel-arm-btn').forEach((btn) => {
        btn.onclick = () => {
          armedTaskId = null;
          render();
        };
      });
      document.querySelectorAll('.act-btn').forEach((btn) => {
        btn.onclick = () => {
          const id = Number(btn.dataset.id);
          const verb = btn.dataset.verb;
          const task =
            viewingNode && viewingNode.id === id
              ? viewingNode
              : (state?.today?.tasks || []).find((t) => t.id === id) || { id, sched: '' };
          actOnTask(task, verb);
        };
      });
    }

    function render() {
      const today = state?.today;
      document.getElementById('scope-badge').textContent =
        today?.scope || state?.workspaceName || 'kaertech-odoo-16';
      document.getElementById('date-badge').textContent = today
        ? \`today \${today.date} · \${today.tasks.length - today.counts.others} standup · \${today.counts.others} others\`
        : '';

      const todayBtn = document.getElementById('today-btn');
      todayBtn.style.display = viewingNode || searchResults ? 'inline-flex' : 'none';

      renderSessionBar();

      const sumEl = document.getElementById('summary-bar');
      const notices = Array.isArray(state?.notices) ? state.notices : [];
      if (today) {
        // the level counts come from kb next --all, which leaves notices out; they get their own count
        const c = today.counts;
        sumEl.innerHTML = \`
          \${notices.length > 0 ? \`<span class="badge badge-notice">\${notices.length === 1 ? '1 notice' : notices.length + ' notices'}</span>\` : ''}
          \${c.loud > 0 ? \`<span class="badge badge-red">\${c.loud} loud</span>\` : ''}
          <span class="badge badge-amber">\${c.normal} normal</span>
          \${c.due > 0 ? \`<span class="badge badge-ws">\${c.due} due</span>\` : ''}
          <span class="badge badge-muted">\${c.others} others</span>
          \${today.noSummary > 0 ? \`<span class="badge badge-amber">\${today.noSummary} without summary</span>\` : ''}
        \`;
      } else {
        sumEl.innerHTML = '';
      }

      const contentEl = document.getElementById('content');

      // 1. Record / Node view
      if (viewingNode) {
        const n = viewingNode;
        const style = n.urgency ? levelStyle(n.urgency.level) : null;
        const isOpenTask = n.kind === 'task' && !n.isDone;
        const meta = [
          n.kind,
          n.scope,
          n.tags.length > 0 ? n.tags.map((t) => '#' + t).join(' ') : '',
          n.sched ? 'sched ' + n.sched : '',
          n.due ? 'due ' + n.due : '',
          n.isDone ? '✓ done' : '',
        ].filter(Boolean);

        contentEl.innerHTML = \`
          <div class="card">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
              <div style="font-size:15px;font-weight:700;display:flex;align-items:center;gap:6px;">
                \${style ? \`<span style="color:\${style.color};">\${style.glyph}</span>\` : ''}
                <span>[\${n.id}] \${escapeHtml(n.title)}</span>
              </div>
              \${isOpenTask ? (
                armedTaskId === n.id
                  ? \`<div class="controls">
                      <span style="color:#fbbf24;font-size:12px;font-weight:600;">close [\${n.id}]?</span>
                      <button class="btn-danger act-btn" data-id="\${n.id}" data-verb="done">yes, close</button>
                      <button class="cancel-arm-btn">no</button>
                    </div>\`
                  : \`<div class="controls">
                      <button class="arm-done-btn" data-id="\${n.id}">done</button>
                      <button class="act-btn" data-id="\${n.id}" data-verb="snooze">snooze</button>
                      <button class="act-btn" data-id="\${n.id}" data-verb="week">+7d</button>
                    </div>\`
              ) : ''}
            </div>
            <div style="font-size:12px;color:var(--wi-text-dim);margin-bottom:4px;">\${escapeHtml(meta.join(' · '))}</div>
            \${n.urgency ? \`<div style="font-size:12px;color:\${style?.color || '#fbbf24'};margin-bottom:8px;font-weight:600;">\${escapeHtml(n.urgency.name)}: \${escapeHtml(n.urgency.reason)}</div>\` : ''}
            <div class="card" style="background:rgba(15,23,42,0.55);margin-top:8px;">
              <div class="md-body">\${renderMarkdown(n.body)}</div>
            </div>
            \${n.links.length > 0 ? \`
              <div style="margin-top:10px;">
                <div style="font-weight:700;font-size:12px;text-transform:uppercase;color:var(--wi-text-sec);margin-bottom:6px;">Links · \${n.links.length}</div>
                \${n.links.map((l) => \`
                  <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
                    <button class="open-node-btn" data-id="\${l.id}">open</button>
                    <span style="font-size:11.5px;color:var(--wi-text-dim);">\${escapeHtml(l.type)} \${escapeHtml(l.direction)}</span>
                    <span>[\${l.id}] \${escapeHtml(l.title)}</span>
                  </div>
                \`).join('')}
              </div>
            \` : ''}
            \${n.events.length > 0 ? \`
              <div style="margin-top:10px;">
                <div style="font-weight:700;font-size:12px;text-transform:uppercase;color:var(--wi-text-sec);margin-bottom:4px;">Events</div>
                <div class="mono">
                  \${n.events.map((ev) => \`<div>\${escapeHtml(ev.at)}  \${escapeHtml(ev.kind)}</div>\`).join('')}
                </div>
              </div>
            \` : ''}
          </div>\`;
        bindControlEvents();
        return;
      }

      // 2. Search results view
      if (searchResults) {
        contentEl.innerHTML = \`
          <div style="margin-bottom:8px;font-weight:600;color:var(--wi-text-sec);">Search results (\${searchResults.length})</div>
          \${searchResults.length === 0 ? '<div class="card">No matching records in kb.</div>' : ''}
          \${searchResults.map((r) => \`
            <div class="card">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
                <div>
                  <span class="task-id">[\${r.id}]</span>
                  <span class="badge badge-muted">\${escapeHtml(r.kind || '')}</span>
                  <strong>\${escapeHtml(r.title || '')}</strong>
                </div>
                <button class="open-node-btn" data-id="\${r.id}">open</button>
              </div>
              \${r.summary ? \`<div style="font-size:12px;color:var(--wi-text-sec);margin-top:4px;">\${escapeHtml(r.summary)}</div>\` : ''}
            </div>
          \`).join('')}
        \`;
        bindControlEvents();
        return;
      }

      // 3. Today Standup View
      if (!today) {
        contentEl.innerHTML = \`<div class="card">kb: no standup (\${escapeHtml(state?.error || 'kb on PATH? neo4j-kb.service up?')})</div>\`;
        return;
      }

      let html = renderNotices(notices) + \`
        <div class="legend">
          \${LEVELS.map((s) => \`<span style="color:\${s.color};font-weight:600;">\${s.glyph} \${s.name}</span>\`).join('')}
          <span>· others = every open task below normal, folded below</span>
          <span style="color:#fbbf24;">· yellow = has due date</span>
        </div>\`;

      const q = filterText.toLowerCase();
      for (const style of LEVELS) {
        const groupTasks = today.tasks.filter((t) => {
          if (levelStyle(t.level) !== style) return false;
          if (!q) return true;
          return (
            String(t.id).includes(q) ||
            String(t.title || '').toLowerCase().includes(q) ||
            String(t.reason || '').toLowerCase().includes(q)
          );
        });
        if (groupTasks.length === 0) continue;

        const isVisible = style.level > 0 || showOthers || Boolean(q);
        html += \`
          <div class="group-header">
            <span style="color:\${style.color};">\${style.glyph} \${style.name}</span>
            <span style="color:var(--wi-text-dim);font-weight:400;">\${groupTasks.length}</span>
            \${style.level === 0 ? \`<button id="toggle-others-btn">\${showOthers ? 'hide' : 'show'}</button>\` : ''}
          </div>\`;

        if (isVisible) {
          for (const task of groupTasks) {
            html += \`
              <div class="card">
                <div class="task-row-top">
                  <span class="task-id">[\${task.id}]</span>
                  <span>\${escapeHtml(task.title)}</span>
                </div>
                <div class="task-row-bottom">
                  <span class="task-reason \${task.due !== null ? 'due' : ''}">\${escapeHtml(task.reason)}</span>
                  \${renderTaskControls(task)}
                </div>
              </div>\`;
          }
        }
      }

      contentEl.innerHTML = html;
      const toggleOthersBtn = document.getElementById('toggle-others-btn');
      if (toggleOthersBtn) {
        toggleOthersBtn.onclick = () => {
          showOthers = !showOthers;
          render();
        };
      }
      bindControlEvents();
    }

    document.getElementById('today-btn').onclick = () => {
      viewingNode = null;
      searchResults = null;
      render();
    };
    document.getElementById('refresh-btn').onclick = () => loadStandup();
    document.getElementById('open-id-btn').onclick = () => runSearchOrOpen();
    document.getElementById('search-input').addEventListener('input', (e) => {
      filterText = e.target.value.trim();
      if (!viewingNode && !searchResults) render();
    });
    document.getElementById('search-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') runSearchOrOpen();
    });

    window.sidecar.onWorkspaceChange((uris) => {
      if (Array.isArray(uris) && uris.length > 0) {
        workspaceUris = uris;
        loadStandup();
      }
    });

    (async () => {
      const uris = await window.sidecar.getWorkspaceUris();
      if (Array.isArray(uris) && uris.length > 0) workspaceUris = uris;
      await loadStandup();
      setInterval(() => {
        if (!viewingNode && !searchResults && !armedTaskId && !busyTaskId) loadStandup();
      }, 30000);
    })();
  </script>
</body>
</html>`;

function resolveWorkspaceRoot(requestedUris = []) {
  const projects = loadKnownProjects();
  let activeUris = Array.isArray(requestedUris) && requestedUris.length > 0 ? requestedUris : [];
  if (activeUris.length === 0 && projects.length > 0) {
    activeUris = projects[0].workspaceUris;
  }
  const wsPaths = activeUris.map(uriToPath).filter(Boolean);
  return {
    wsRoot: wsPaths[0] || process.cwd(),
    activeUris,
  };
}

const app = new SidecarApp();

app.page('/', () => HTML);

app.api('/api/state', async (data) => {
  const { wsRoot, activeUris } = resolveWorkspaceRoot(data?.workspaceUris);
  const convId = await resolveActiveConversationId(data?.conversationId, activeUris);
  const [kbRes, notices, journal] = await Promise.all([
    runKb(['--json', 'next', '--all'], wsRoot),
    loadNotices(runKb, wsRoot),
    Promise.resolve(scanSessionJournal(convId, wsRoot)),
  ]);
  const today = kbRes.exitCode === 0 ? parseToday(kbRes.stdout) : null;
  return {
    workspaceName: basename(wsRoot),
    today,
    notices,
    journal,
    error: today ? null : (kbRes.stderr || kbRes.stdout || '').trim().split('\n')[0],
  };
});

app.api('/api/node', async (data) => {
  const { wsRoot } = resolveWorkspaceRoot(data?.workspaceUris);
  const id = Number(data?.id);
  const res = await runKb(['--json', 'show', String(id)], wsRoot);
  const node = res.exitCode === 0 ? parseNode(res.stdout) : null;
  return {
    node,
    error: node ? null : (res.stderr || res.stdout || '').trim().split('\n')[0],
  };
});

app.api('/api/act', async (data) => {
  const { wsRoot } = resolveWorkspaceRoot(data?.workspaceUris);
  const id = String(Number(data?.id));
  const verb = String(data?.verb || '');
  const date = String(data?.date || '');
  const args =
    verb === 'done'
      ? ['done', id]
      : verb === 'skip'
        ? ['skip', id]
        : verb === 'week'
          ? ['snooze', id, plusDays(date, 7)]
          : ['snooze', id];
  const res = await runKb(args, wsRoot);
  return {
    ok: res.exitCode === 0,
    message:
      res.exitCode === 0
        ? `kb ${args.join(' ')}`
        : `kb ${args[0]} [${id}] failed: ${(res.stderr || res.stdout).trim().split('\n')[0]}`,
  };
});

app.api('/api/dismiss', async (data) => {
  const { wsRoot } = resolveWorkspaceRoot(data?.workspaceUris);
  return dismissNotice(runKb, wsRoot, data?.id);
});

app.api('/api/search', async (data) => {
  const { wsRoot } = resolveWorkspaceRoot(data?.workspaceUris);
  const query = String(data?.query || '').trim();
  if (!query) return { results: [] };
  const res = await runKb(['--json', 'search', query], wsRoot);
  let results = [];
  if (res.exitCode === 0) {
    try {
      const parsed = JSON.parse(res.stdout);
      results = Array.isArray(parsed) ? parsed : Array.isArray(parsed?.results) ? parsed.results : [];
    } catch {}
  }
  return { results };
});

app.api('/api/close-prompt', async (data) => {
  const { wsRoot, activeUris } = resolveWorkspaceRoot(data?.workspaceUris);
  const convId = await resolveActiveConversationId(data?.conversationId, activeUris);
  const journal = scanSessionJournal(convId, wsRoot);
  const kbRes = await runKb(['--json', 'next', '--all'], wsRoot);
  const today = kbRes.exitCode === 0 ? parseToday(kbRes.stdout) : null;
  const prompt = buildClosePrompt(today?.scope || basename(wsRoot), convId, journal);
  return { prompt, conversationId: convId };
});

await app.run();
