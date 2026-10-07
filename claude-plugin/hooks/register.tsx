import { atom, read, update } from 'claude-code'
import type { EngineInterface, ProcessRunResult, Register } from 'claude-code'

import type { Journal, KbTask } from '../types'
import {
  EMPTY_JOURNAL,
  closePrompt,
  commitDirOf,
  factCount,
  factsOf,
  isEmpty,
  journalPrompt,
  mergeFacts,
  parseJournalReply,
  sinceSummary,
} from './journal'
import { parseNode } from './node'
import { LEVELS, countByLevel, levelStyle, parseToday, plusDays } from './today'

const PANE = 'kb-today'
const REFRESH_MS = 5 * 60_000
const JOURNALS_KEPT = 30
const WIDE = 110

type Tip = 'kb-standup' | 'kb-journal'

// what each band row means; shown above the rows while the pointer is on the row
const TIPS: { scope: Tip; text: string }[] = [
  {
    scope: 'kb-standup',
    text: "kb's open tasks in this project by urgency: loud and normal are the standup, quiet = whisper (aging, not urgent yet), due = has a deadline. open = the pane with all of them.",
  },
  {
    scope: 'kb-journal',
    text: "This session in kb: not closed, closed, or closed with N new facts (files edited, commits, PRs, kb writes) since. close session asks Claude for the full close: open threads and lessons into kb, the repos checked, the journal written, the session archived when Claude has nothing left to ask. /journal writes only the journal.",
  },
]

type Verb = 'done' | 'snooze' | 'week' | 'skip'
type Target = { id: number; sched: string }

const today = atom({ plugin: 'kb', key: 'today' } as const, null)
const armed = atom({ plugin: 'kb', key: 'armed' } as const, null)
const busy = atom({ plugin: 'kb', key: 'busy' } as const, null)
const node = atom({ plugin: 'kb', key: 'node' } as const, null)
const nodeLoading = atom({ plugin: 'kb', key: 'nodeLoading' } as const, null)
const view = atom({ plugin: 'kb', key: 'view' } as const, null)
const showQuiet = atom({ plugin: 'kb', key: 'showQuiet' } as const, false)
const journal = atom({ plugin: 'kb', key: 'journal' } as const, EMPTY_JOURNAL)

// kb resolves its scope from the working directory, so it runs in the session's project root
async function kb($: EngineInterface, args: readonly string[]): Promise<ProcessRunResult> {
  return $.process.run(['kb', ...args], { cwd: await $.session.root(), timeoutMs: 60_000 })
}

function failure(run: ProcessRunResult): string {
  return (run.stderr.trim() || run.stdout.trim()).split('\n')[0]?.slice(0, 160) ?? ''
}

async function refreshToday($: EngineInterface): Promise<void> {
  try {
    // `next` is today's list down to whisper; it writes no shown events
    const run = await kb($, ['--json', 'next'])
    const parsed = run.exitCode === 0 ? parseToday(run.stdout) : null
    if (parsed === null) {
      $.ui.log(`kb: no standup: ${failure(run)}`, { to: 'debug' })
      return
    }
    await update($, today, () => parsed)
  } catch (error) {
    $.ui.log(`kb: no standup: ${String(error)}`, { to: 'debug' })
  }
}

// A press runs under the 10 s hook budget, and a render hook's $ in its closure does not stop that
// clock while kb or the model answers: such work runs from a timer, and the press returns at once.
function soon($: EngineInterface, work: () => Promise<unknown>): void {
  $.clock.after(0, () => void work().catch(error => $.ui.toast(`kb: ${String(error)}`)))
}

// close session: the full close, done by Claude on the person's press; the band says closing until the
// journal lands. Not awaited, so the press returns while the session is still busy.
async function requestClose($: EngineInterface): Promise<void> {
  await update($, journal, j => ({ ...j, isClosing: true }))
  const scope = (await read($, today))?.scope ?? ''
  void $.prompt.submit({ text: closePrompt(scope, await $.session.id()), asUser: true })
}

// $.ui.open on a pane already open only retitles it: one open behind another tab is closed and opened
// again, which brings it to the front
async function showPane($: EngineInterface, id: string, title: string): Promise<void> {
  const open = (await $.ui.panes()).find(pane => pane.id === id)
  if (open !== undefined && !open.isShown) await $.ui.close({ id })
  await $.ui.open({ id, title })
}

// the band's open and /kb: the pane in front, on the list
async function openPane($: EngineInterface): Promise<void> {
  await update($, view, () => null)
  await showPane($, PANE, 'kb')
}

// One pane, two views. Opening a record or going back is a state change the pane redraws on at once:
// a pane operation (open, close, panes) inside a press waited on the surface the press itself held,
// which took two clicks or ran out the press's 10 s.
async function backToToday($: EngineInterface): Promise<void> {
  await update($, view, () => null)
}

async function openNode($: EngineInterface, id: number): Promise<void> {
  await update($, nodeLoading, () => id)
  await update($, view, () => id)
  soon($, () => fetchNode($, id))
}

async function fetchNode($: EngineInterface, id: number): Promise<void> {
  try {
    const run = await kb($, ['--json', 'show', String(id)])
    const shown = run.exitCode === 0 ? parseNode(run.stdout) : null
    if (shown === null) {
      $.ui.toast(`kb show ${id} failed: ${failure(run)}`)
      return
    }
    await update($, node, () => shown)
  } finally {
    await update($, nodeLoading, () => null)
  }
}

async function act($: EngineInterface, task: Target, verb: Verb): Promise<void> {
  const date = (await read($, today))?.date ?? ''
  const id = String(task.id)
  const args =
    verb === 'done'
      ? ['done', id]
      : verb === 'skip'
        ? ['skip', id]
        : verb === 'week' && date !== ''
          ? ['snooze', id, plusDays(date, 7)]
          : ['snooze', id]
  await update($, armed, () => null)
  await update($, busy, () => task.id)
  try {
    const run = await kb($, args)
    $.ui.toast(run.exitCode === 0 ? `kb ${args.join(' ')}` : `kb ${args[0]} [${id}] failed: ${failure(run)}`)
    await refreshToday($)
    if ((await read($, node))?.id === task.id) await fetchNode($, task.id)
  } finally {
    await update($, busy, () => null)
  }
}

// the commit a `git commit -q` made, which printed nothing to read it from
async function quietCommit($: EngineInterface, command: string): Promise<string[]> {
  const dir = commitDirOf(command, await $.session.cwd())
  if (dir === null) return []
  const run = await $.process.run(['git', '-C', dir, 'log', '-1', '--format=%h %s'], { timeoutMs: 5000 })
  const line = run.stdout.trim()
  return run.exitCode === 0 && line !== '' ? [`${line} (${dir.split('/').filter(Boolean).at(-1) ?? dir})`] : []
}

async function record($: EngineInterface, change: (current: Journal) => Journal): Promise<void> {
  await update($, journal, current => change(current))
  await $.store.set(`journal:${await $.session.id()}`, { ...(await read($, journal)), isWriting: false })
}

// The stored journal, then everything the transcript already holds: work done before this module
// loaded (a first load mid-session, a reload, a resume) counts as well.
async function restoreJournal($: EngineInterface): Promise<void> {
  const saved = await $.store.get(`journal:${await $.session.id()}`)
  if (saved !== undefined) {
    // a close that was running when the module last unloaded is not running now
    const stored = { ...EMPTY_JOURNAL, ...(saved as Journal), isClosing: false }
    // a journal stored before `mark` existed: its summary covered what it held then
    const hasMark = typeof (saved as { mark?: unknown }).mark === 'number'
    await update($, journal, () => (stored.summarized && !hasMark ? { ...stored, mark: factCount(stored) } : stored))
  }
  const keys = (await $.store.keys()).filter(key => key.startsWith('journal:'))
  for (const key of keys.slice(0, Math.max(0, keys.length - JOURNALS_KEPT))) await $.store.delete(key)

  const root = await $.session.root()
  const messages = await $.session.messages()
  const uses = messages.flatMap(message => message.toolUses)
  // a stored journal already knows its summary and what it covered; the replay only adds facts
  const isStored = saved !== undefined
  await record($, current =>
    uses.reduce((j, use) => {
      const facts = factsOf({ ...use, tool: String(use.tool) }, root)
      return mergeFacts(j, isStored ? { ...facts, isSummary: false } : facts)
    }, current),
  )
}

// One request over this session's own transcript (prompt-cached), then `kb session close` with the body.
async function writeJournal($: EngineInterface): Promise<string> {
  const current = await read($, journal)
  if (current.isWriting) return 'kb: the journal is already being written.'
  await update($, journal, j => ({ ...j, isWriting: true }))
  try {
    const reply = await $.model.fork({ prompt: journalPrompt(current) })
    if (!reply.isAnswered) return `kb: no journal written (${reply.reason}).`
    const { title, body } = parseJournalReply(reply.text)
    if (title === '' || body === '') return `kb: no journal written: the reply had no title or body.\n\n${reply.text}`

    const sid = await $.session.id()
    const file = `${(await $.env.get('TMPDIR')) ?? '/tmp'}/kb-journal-${sid}.md`
    await $.fs.write(file, `${body}\n`)
    const run = await $.process.run(['kb', 'session', 'close', '--id', sid, '--title', title, '--body-file', file], {
      cwd: await $.session.root(),
      env: { KB_SESSION: sid },
      timeoutMs: 60_000,
    })
    if (run.exitCode !== 0) return `kb: session close failed: ${failure(run)}`
    await record($, j => ({ ...j, summarized: true, isClosing: false, title, mark: factCount(current) }))
    return `kb: session journal written.\n\n${title}\n\n${body}`
  } finally {
    await update($, journal, j => ({ ...j, isWriting: false }))
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.command.register({ name: 'kb', description: "Open kb's standup pane (today's tasks with done/snooze)" })
    await $.command.register({
      name: 'journal',
      description: "Write this session's kb journal from the transcript and the files, commits and PRs it touched",
    })
    await $.tool.register({
      name: 'write_journal',
      description:
        "Writes this session's kb journal (title + Intent/Tags/Files touched/Done/Decisions/Open threads) and closes the kb session record with it. The files, commits, PRs and kb writes come from the tool calls the harness recorded, not from memory. Call it when the user signals the session is over, instead of writing `kb session close --body-file` by hand. Returns the title and body written.",
      inputSchema: { type: 'object', properties: {} },
    })
    await restoreJournal($)
    await refreshToday($)
    const loaded = await read($, today)
    if (loaded !== null && countByLevel(loaded.tasks).loud > 0) void openPane($)
    $.clock.every(REFRESH_MS, () => void refreshToday($))
    return started
  })

  on('command.run', { command: 'kb' }, async ($, e) => {
    const id = Number(e.args.trim())
    if (Number.isInteger(id) && id > 0) {
      await openNode($, id)
      await showPane($, PANE, 'kb')
      return { text: `kb: [${id}] opened.` }
    }
    await refreshToday($)
    await openPane($)
    return { text: 'kb: standup pane opened.' }
  })

  on('command.run', { command: 'journal' }, async $ => ({ text: await writeJournal($) }))

  on('tool.call', { tool: 'mcp__kb__write_journal' }, async $ => ({ result: await writeJournal($) }))

  // the journal's facts: what the tools did, including inside subagents
  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    if (ran.deny !== undefined || ran.isError === true) return ran
    const input = e as unknown as Record<string, unknown>
    const facts = factsOf({ tool: String(e.tool), input, text: ran.text }, await $.session.root())
    if (String(e.tool) === 'Bash' && typeof input.command === 'string' && facts.commits.length === 0) {
      facts.commits.push(...(await quietCommit($, input.command)))
    }
    if (!isEmpty(facts)) {
      await record($, j => mergeFacts(j, facts))
      if (facts.kbWrites.length > 0) await refreshToday($)
    }
    return ran
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const below = await next(e)
    if (e.props.hasSurvey) return below
    const { Box, Button, Text } = $.ui.resolve(e)
    const data = await read($, today)
    const j = await read($, journal)
    const counts = data === null ? null : countByLevel(data.tasks)
    // a row joins its tip's hover group: pointing at it underlines it and reveals the tip
    const hint = (scope: Tip) => ({ scope, underline: true })

    return (
      <Box flexDirection="column">
        {/* every band plugin draws its tips before the plugins beneath it and its rows after them: all tips sit on top */}
        {TIPS.map(tip => (
          <Box key={`tip-${tip.scope}`} display="none" hover={{ scope: tip.scope, display: 'flex' }} flexDirection="row" columnGap={1}>
            <Text color="cyan">ⓘ</Text>
            <Text dimColor>{tip.text}</Text>
          </Box>
        ))}
        {below}
        {counts !== null && (
          <Box key="kb-today-row" flexDirection="row" columnGap={1} alignItems="center">
            <Text dimColor hover={hint('kb-standup')}>kb</Text>
            {counts.loud > 0 && <Text color="red" hover={hint('kb-standup')}>{`${counts.loud} loud`}</Text>}
            <Text color="yellow" hover={hint('kb-standup')}>{`${counts.normal} normal`}</Text>
            {counts.due > 0 && <Text bold hover={hint('kb-standup')}>{`${counts.due} due`}</Text>}
            <Text dimColor hover={hint('kb-standup')}>{`${counts.quiet} quiet`}</Text>
            {(data?.noSummary ?? 0) > 0 && <Text color="yellow">{`${data?.noSummary} without summary`}</Text>}
            <Button key="kb-open" label="open" onPress={() => openPane($)} />
          </Box>
        )}
        <Box key="kb-session-row" flexDirection="row" columnGap={1} alignItems="center">
          <Text dimColor hover={hint('kb-journal')}>session</Text>
          {!j.summarized && <Text color="yellow" hover={hint('kb-journal')}>not closed</Text>}
          {!j.summarized && (
            <Text dimColor hover={hint('kb-journal')}>{`${j.files.length} files · ${j.commits.length} commits · ${j.prs.length} PRs · ${j.kbWrites.length} kb writes`}</Text>
          )}
          {j.summarized && (
            <Text color="green" hover={hint('kb-journal')}>
              {sinceSummary(j) > 0 ? 'closed' : '✓ closed'}
            </Text>
          )}
          {sinceSummary(j) > 0 && <Text color="yellow" hover={hint('kb-journal')}>{`${sinceSummary(j)} new since closing`}</Text>}
          {(!j.summarized || sinceSummary(j) > 0) &&
            (j.isClosing || j.isWriting ? (
              <Text dimColor>closing…</Text>
            ) : (
              <Button key="kb-close" label="close session" onPress={() => requestClose($)} />
            ))}
        </Box>
      </Box>
    )
  })

  // the list view: a record view takes the pane while one is open
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e, next) => {
    if ((await read($, view)) !== null) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const data = await read($, today)
    const armedId = await read($, armed)
    const busyId = await read($, busy)
    const isQuietShown = await read($, showQuiet)
    if (data === null) return <Text dimColor>kb: no standup (kb on PATH? neo4j-kb.service up?)</Text>
    const counts = countByLevel(data.tasks)
    // a CSS surface draws a bordered Box as a rounded card; the terminal would spend two rows on it
    const hasCards = e.surface !== 'terminal'

    const controls = (task: Target) =>
      busyId === task.id ? (
        <Text dimColor>working…</Text>
      ) : armedId === task.id ? (
        <Box flexDirection="row" columnGap={1} alignItems="center">
          <Text color="yellow">{`close [${task.id}]?`}</Text>
          <Button key={`yes-${task.id}`} label="yes, close" onPress={() => soon($, () => act($, task, 'done'))} />
          <Button key={`no-${task.id}`} label="no" onPress={() => update($, armed, () => null)} />
        </Box>
      ) : (
        <Box flexDirection="row" columnGap={1} alignItems="center">
          <Button key={`open-${task.id}`} label="open" onPress={() => openNode($, task.id)} />
          <Button key={`done-${task.id}`} label="done" onPress={() => update($, armed, () => task.id)} />
          <Button key={`snooze-${task.id}`} label="snooze" onPress={() => soon($, () => act($, task, 'snooze'))} />
          <Button key={`week-${task.id}`} label="+7d" onPress={() => soon($, () => act($, task, 'week'))} />
          {task.sched === 'window' && (
            <Button key={`skip-${task.id}`} label="skip" onPress={() => soon($, () => act($, task, 'skip'))} />
          )}
        </Box>
      )

    const card = (task: KbTask) => (
      <Box
        key={`task-${task.id}`}
        flexDirection="column"
        rowGap={hasCards ? 1 : 0}
        borderStyle={hasCards ? 'round' : undefined}
        borderDimColor={hasCards ? true : undefined}
      >
        <Box flexDirection="row" columnGap={1}>
          <Text dimColor>{`[${task.id}]`}</Text>
          <Text>{task.title}</Text>
        </Box>
        <Box flexDirection="row" columnGap={2} rowGap={1} justifyContent="space-between" alignItems="center" flexWrap="wrap">
          <Text dimColor={task.due === null} color={task.due !== null ? 'yellow' : undefined}>
            {task.reason}
          </Text>
          {controls(task)}
        </Box>
      </Box>
    )

    const groups = LEVELS.map(style => ({ style, tasks: data.tasks.filter(task => levelStyle(task.level) === style) }))

    return (
      <Box flexDirection="column" rowGap={1}>
        <Box key="kb-head" flexDirection="row" columnGap={1} justifyContent="space-between" alignItems="center">
          <Box flexDirection="column">
            <Text bold>{`kb today ${data.date}`}</Text>
            <Text dimColor>{`${data.scope} · ${data.tasks.length - counts.quiet} on the standup · ${counts.quiet} quiet`}</Text>
          </Box>
          <Button key="kb-refresh" label="refresh" onPress={() => soon($, () => refreshToday($))} />
        </Box>
        <Box key="kb-legend" flexDirection="row" columnGap={2} flexWrap="wrap">
          {LEVELS.map(style => (
            <Text color={style.color}>{`${style.glyph} ${style.name}`}</Text>
          ))}
          <Text dimColor>kb urgency, from age or deadline; whisper = quiet, folded below</Text>
          <Text color="yellow">yellow line: has a due date (T-n = days left)</Text>
        </Box>
        {groups
          .filter(group => group.tasks.length > 0)
          .map(group => (
            <Box key={`group-${group.style.name}`} flexDirection="column" rowGap={1}>
              <Box flexDirection="row" columnGap={1} marginTop={1} alignItems="center">
                <Text bold color={group.style.color}>{`${group.style.glyph} ${group.style.name}`}</Text>
                <Text dimColor>{`${group.tasks.length}`}</Text>
                {group.style.level === 1 && (
                  <Button
                    key="kb-quiet"
                    label={isQuietShown ? 'hide' : 'show'}
                    onPress={() => update($, showQuiet, shown => !shown)}
                  />
                )}
              </Box>
              {(group.style.level > 1 || isQuietShown) && group.tasks.map(card)}
            </Box>
          ))}
      </Box>
    )
  })

  // the record view
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e, next) => {
    const viewing = await read($, view)
    if (viewing === null) return next(e)
    const { Box, Button, Markdown, Text } = $.ui.resolve(e)
    const shown = await read($, node)
    const loading = await read($, nodeLoading)
    const armedId = await read($, armed)
    const back = <Button key="node-today" label="← today" onPress={() => backToToday($)} />
    if (shown?.id !== viewing) {
      return (
        <Box flexDirection="column" rowGap={1}>
          {back}
          <Text dimColor>{loading === viewing ? `loading [${viewing}]…` : `kb: [${viewing}] could not be read`}</Text>
        </Box>
      )
    }
    const hasCards = e.surface !== 'terminal'

    const style = shown.urgency === null ? null : levelStyle(shown.urgency.level)
    const isOpenTask = shown.kind === 'task' && !shown.isDone
    const meta = [
      shown.kind,
      shown.scope,
      shown.tags.length > 0 ? shown.tags.map(tag => `#${tag}`).join(' ') : '',
      shown.sched !== '' ? `sched ${shown.sched}` : '',
      shown.due !== null ? `due ${shown.due}` : '',
      shown.isDone ? 'done' : '',
    ].filter(part => part !== '')

    return (
      <Box flexDirection="column" rowGap={1}>
        <Box key="node-actions" flexDirection="row" columnGap={1} flexWrap="wrap" alignItems="center">
          {back}
          {isOpenTask &&
            (armedId === shown.id ? (
              <Box flexDirection="row" columnGap={1} alignItems="center">
                <Text color="yellow">{`close [${shown.id}]?`}</Text>
                <Button key="node-yes" label="yes, close" onPress={() => soon($, () => act($, shown, 'done'))} />
                <Button key="node-no" label="no" onPress={() => update($, armed, () => null)} />
              </Box>
            ) : (
              <Box flexDirection="row" columnGap={1}>
                <Button key="node-done" label="done" onPress={() => update($, armed, () => shown.id)} />
                <Button key="node-snooze" label="snooze" onPress={() => soon($, () => act($, shown, 'snooze'))} />
                <Button key="node-week" label="+7d" onPress={() => soon($, () => act($, shown, 'week'))} />
              </Box>
            ))}
        </Box>
        <Box key="node-head" flexDirection="column">
          <Box flexDirection="row" columnGap={1}>
            {style !== null && <Text color={style.color}>{style.glyph}</Text>}
            <Text bold>{`[${shown.id}] ${shown.title}`}</Text>
          </Box>
          <Text dimColor>{meta.join(' · ')}</Text>
          {shown.urgency !== null && (
            <Text color={style?.color}>{`${shown.urgency.name}: ${shown.urgency.reason}`}</Text>
          )}
        </Box>
        <Box
          key="node-body"
          flexDirection="column"
          borderStyle={hasCards ? 'round' : undefined}
          borderDimColor={hasCards ? true : undefined}
        >
          <Markdown key="node-markdown" text={shown.body === '' ? '_no body_' : shown.body} />
        </Box>
        {shown.links.length > 0 && (
          <Box key="node-links" flexDirection="column" rowGap={1}>
            <Text bold>{`Links · ${shown.links.length}`}</Text>
            {shown.links.map(link => (
              <Box flexDirection="row" columnGap={1} alignItems="center">
                <Button key={`link-${link.id}`} label="open" onPress={() => openNode($, link.id)} />
                <Text dimColor>{`${link.type} ${link.direction}`}</Text>
                <Text>{`[${link.id}] ${link.title}`}</Text>
              </Box>
            ))}
          </Box>
        )}
        {shown.events.length > 0 && (
          <Box key="node-events" flexDirection="column">
            <Text bold>Events</Text>
            {shown.events.map(event => (
              <Text dimColor>{`${event.at}  ${event.kind}`}</Text>
            ))}
          </Box>
        )}
      </Box>
    )
  })
}
