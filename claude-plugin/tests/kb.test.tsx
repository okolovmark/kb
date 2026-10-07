import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { MockClock } from 'claude-code/testing'

import {
  EMPTY_JOURNAL,
  commitDirOf,
  commitsIn,
  kbWritesIn,
  parseJournalReply,
  prUrlsIn,
  sinceSummary,
  writesSummary,
} from '../hooks/journal'
import { parseNode } from '../hooks/node'
import { parseToday, plusDays } from '../hooks/today'

const ROOT = '/p'
const SID = 'sid-1'
const TODAY = JSON.stringify({
  date: '2026-10-07',
  scope: 'proj',
  quiet: 3,
  no_summary: [],
  tasks: [
    { id: 207, level: 3, name: 'loud', reason: '30 working days', title: 'part standard', scope: 'proj', sched: 'age', due: null },
    { id: 282, level: 2, name: 'normal', reason: 'decide (T+0)', title: 'payment terms', scope: 'proj', sched: 'soft', due: '2026-10-07' },
    { id: 742, level: 1, name: 'whisper', reason: '9 working days', title: 'estimate seed', scope: 'proj', sched: 'age', due: null },
  ],
})
const USAGE = { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
const BAND = {
  plugin: 'kb',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 120, scroll: { offset: 0, bodyRows: 9 }, view: {} },
} as const
const PANE = {
  plugin: 'kb',
  component: 'Pane',
  requestId: 'kb-today',
  props: { title: 'kb today', isFocused: true, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 30 }, view: {} },
} as const

const SHOW = JSON.stringify({
  id: 207, kind: 'task', scope: 'proj', title: 'part standard', body: 'reviewed **twice**', tags: ['ready_for_review'],
  sched: 'age', due: null, done_at: null, urgency: { level: 3, name: 'loud', reason: '30 working days' },
  links: [{ type: 'TOUCHED', direction: '<-', id: 592, title: 'requests' }],
  events: [{ kind: 'shown', at: '2026-10-07T02:33:32+00:00', note: null }],
})
type Use = { tool_use_id: string; tool: string; input: Record<string, unknown>; text?: string }

type Panes = { list: { id: string; isShown: boolean }[]; calls: string[] }

function engine(on: On, runs: string[][], history: Use[] = [], panes: Panes = { list: [], calls: [] }): MockClock {
  mock.env(on, { TMPDIR: '/t' })
  mock.store(on)
  const clock = mock.clock(on)
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('session.root', () => ({ value: ROOT }))
  on('session.id', () => ({ value: SID }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('tool.register', (_$, e) => ({ value: { tool: `mcp__kb__${e.name}` } }))
  on('ui.open', (_$, e) => {
    panes.calls.push(`open ${e.id}`)
    return { value: { isPlaced: true } }
  })
  on('ui.close', (_$, e) => {
    panes.calls.push(`close ${e.id}`)
    return { value: undefined }
  })
  on('ui.panes', () => ({
    value: panes.list.map(pane => ({ ...pane, title: pane.id, isFocused: false, isPlaced: true })),
  }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('fs.write', () => ({ value: undefined }))
  on('session.messages', () => ({ value: [{ role: 'assistant', text: '', toolUses: history }] }))
  on('model.fork', () => ({
    value: { isAnswered: true, text: 'TITLE: Mods for kb\n\nIntent: x\n\nDone:\n- y', usage: USAGE },
  }))
  on('process.run', (_$, e) => {
    runs.push([...e.argv])
    const stdout =
      e.argv[0] === 'git' ? 'beef123 [FIX] quiet commit\n' : e.argv[1] !== '--json' ? '' : e.argv[2] === 'show' ? SHOW : TODAY
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // a commit with -q prints nothing; any other call answers with a commit line
  on('tool.call', (_$, e) => ({
    result: { ok: true },
    text: String((e as { command?: unknown }).command ?? '').includes(' -q') ? '' : '[16.0 abc1234] [FIX] x: y\n 1 file changed',
  }))
  on('session.cwd', () => ({ value: ROOT }))
  on('ui.render', ($, e) => {
    const { Box } = $.ui.resolve(e)
    return <Box />
  })
  return clock
}

describe('parsers', () => {
  test('commits, PR urls, kb writes and a summary write', async () => {
    expect(commitsIn('[kio-1 0a1b2c3] [FIX] m: x\n 2 files changed')).toEqual(['0a1b2c3 [FIX] m: x (kio-1)'])
    expect(prUrlsIn('https://github.com/o/r/pull/12\nhttps://github.com/o/r/pull/12')).toEqual(['https://github.com/o/r/pull/12'])
    expect(kbWritesIn('kb done 207 "ok" && kb session note "n"')).toEqual(['kb done 207 "ok"', 'kb session note "n"'])
    expect(kbWritesIn('kb show 207')).toEqual([])
    expect(kbWritesIn('KB_SESSION=abc kb session note "n"')).toEqual(['kb session note "n"'])
    expect(writesSummary('kb session close --body-file /tmp/b.md')).toBe(true)
    expect(writesSummary('kb session close --id x')).toBe(false)
    expect(writesSummary('KB_SESSION=x kb session close --id x --body-file /t/b.md')).toBe(true)
    expect(writesSummary("python3 - <<'EOF'\nexpect(writesSummary('kb session close --body-file /tmp/b.md'))\nEOF")).toBe(false)
    expect(commitDirOf('git -C /r/kb commit -q -m x', '/p')).toBe('/r/kb')
    expect(commitDirOf('git add -A && git commit -m x', '/p')).toBe('/p')
    expect(commitDirOf('git -C "$PROJ" commit -m x', '/p')).toBe(null)
    expect(commitDirOf('K=/r/kb; git -C $K add -A && git -C $K commit -q -F - <<EOF', '/p')).toBe('/r/kb')
    expect(commitDirOf('git status', '/p')).toBe(null)
    const done = { ...EMPTY_JOURNAL, files: ['a', 'b'], summarized: true, mark: 2 }
    expect(sinceSummary(done)).toBe(0)
    expect(sinceSummary({ ...done, files: ['a', 'b', 'c'] })).toBe(1)
    expect(sinceSummary({ ...EMPTY_JOURNAL, files: ['a'] })).toBe(0)
  })

  test('today, dates and the reply', async () => {
    expect(parseToday(TODAY)?.tasks.length).toBe(3)
    expect(parseToday('kb: NO MEMORY')).toBe(null)
    expect(plusDays('2026-10-28', 7)).toBe('2026-11-04')
    expect(parseJournalReply('TITLE: t\n\nIntent: i')).toEqual({ title: 't', body: 'Intent: i' })
    expect(parseNode(SHOW)?.links[0]?.id).toBe(592)
    expect(parseNode('{"error": "no such record"}')).toBe(null)
  })
})

test('the band counts the standup; done asks before it closes', async ($, on) => {
  const runs: string[][] = []
  const clock = engine(on, runs)
  await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const band = await $.ui.mount({ ...BAND, surface })
    expect(await band.find({ text: '1 loud' })).toBeDefined()
    expect(await band.find({ text: '1 due' })).toBeDefined()
    expect(await band.find({ text: '1 quiet' })).toBeDefined()
    expect(await band.find({ key: 'tip-kb-standup' })).toBeDefined()
    await band.unmount()
  }

  const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(runs).toContainEqual(['kb', '--json', 'next'])
  expect(await pane.find({ key: 'task-742' })).toBeUndefined()
  await pane.press({ key: 'kb-quiet' })
  expect(await pane.find({ key: 'task-742' })).toBeDefined()
  await pane.press({ key: 'done-207' })
  expect(runs.some(argv => argv[1] === 'done')).toBe(false)
  expect(await pane.find({ text: 'close [207]?' })).toBeDefined()
  await pane.press({ key: 'yes-207' })
  await clock.advance(0)
  expect(runs).toContainEqual(['kb', 'done', '207'])
  await pane.press({ key: 'week-282' })
  await clock.advance(0)
  expect(runs).toContainEqual(['kb', 'snooze', '282', '2026-10-14'])
  await pane.unmount()
})

test('the journal counts what the tools touched and writes the summary through kb', async ($, on) => {
  const runs: string[][] = []
  const clock = engine(on, runs)
  await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'Edit', file_path: '/p/src/a.py', old_string: 'a', new_string: 'b' })
  await $.tool.call({ tool: 'Bash', command: 'git -C src commit -m "[FIX] x: y"' })

  const band = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect(await band.find({ text: '1 files · 1 commits · 0 PRs · 0 kb writes' })).toBeDefined()
  await band.press({ key: 'kb-journal' })
  await clock.advance(0)
  expect(runs).toContainEqual([
    'kb', 'session', 'close', '--id', SID, '--title', 'Mods for kb', '--body-file', `/t/kb-journal-${SID}.md`,
  ])
  // an up-to-date summary needs no row; work after it makes the summary stale
  expect(await band.find({ key: 'kb-journal-row' })).toBeUndefined()
  expect(await band.find({ key: 'kb-journal-stale' })).toBeUndefined()
  await $.tool.call({ tool: 'Bash', command: 'git -C /r/kb commit -q -m "[FIX] quiet commit"' })
  expect(await band.find({ text: '1 new since the summary' })).toBeDefined()
  expect(await band.find({ key: 'kb-journal-update' })).toBeDefined()
  await band.unmount()
})

test('open shows the record in the same pane; the legend names the circles', async ($, on) => {
  const runs: string[][] = []
  const clock = engine(on, runs)
  await $.session.start({ cwd: ROOT, surface: 'desktop', isInteractive: true })

  const pane = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await pane.find({ text: '● loud' })).toBeDefined()
  expect(await pane.find({ text: '◉ scream' })).toBeDefined()
  await pane.press({ key: 'open-207' })
  await clock.advance(0)
  expect(runs).toContainEqual(['kb', '--json', 'show', '207'])
  await pane.unmount()

  for (const surface of ['terminal', 'desktop'] as const) {
    const opened = await $.ui.mount({ ...PANE, surface })
    expect(await opened.find({ text: '[207] part standard' })).toBeDefined()
    expect(await opened.find({ text: 'loud: 30 working days' })).toBeDefined()
    expect(await opened.find({ text: '[592] requests' })).toBeDefined()
    await opened.unmount()
  }
})

test('the journal also counts what the transcript held before the module loaded', async ($, on) => {
  const runs: string[][] = []
  const clock = engine(on, runs, [
    { tool_use_id: 'a', tool: 'Write', input: { file_path: '/p/x.md', content: 'x' } },
    { tool_use_id: 'b', tool: 'Bash', input: { command: 'git commit -m x' }, text: '[main 1234567] x' },
  ])
  await $.session.start({ cwd: ROOT, surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ text: '1 files · 1 commits · 0 PRs · 0 kb writes' })).toBeDefined()
  await band.unmount()
})

test('open and ← today switch the view at once, with no pane operation inside the press', async ($, on) => {
  const runs: string[][] = []
  const panes: Panes = { list: [{ id: 'kb-today', isShown: true }], calls: [] }
  const clock = engine(on, runs, [], panes)
  await $.session.start({ cwd: ROOT, surface: 'desktop', isInteractive: true })
  await clock.advance(0)
  panes.calls.length = 0

  const pane = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await pane.press({ key: 'open-207' })
  expect(await pane.find({ text: 'loading [207]…' })).toBeDefined()
  await clock.advance(0)
  expect(await pane.find({ text: '[207] part standard' })).toBeDefined()
  await pane.press({ key: 'node-today' })
  expect(await pane.find({ key: 'kb-head' })).toBeDefined()
  expect(panes.calls).toEqual([])
  await pane.unmount()
})

test("the band's open brings the pane from behind another tab", async ($, on) => {
  const runs: string[][] = []
  const panes: Panes = { list: [{ id: 'kb-today', isShown: false }], calls: [] }
  const clock = engine(on, runs, [], panes)
  await $.session.start({ cwd: ROOT, surface: 'desktop', isInteractive: true })
  await clock.advance(0)
  panes.calls.length = 0
  const band = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await band.press({ key: 'kb-open' })
  expect(panes.calls).toEqual(['close kb-today', 'open kb-today'])
  await band.unmount()
})
