import type { Journal } from '../types'

export const EMPTY_JOURNAL: Journal = {
  files: [],
  commits: [],
  prs: [],
  kbWrites: [],
  summarized: false,
  title: null,
  mark: 0,
  isWriting: false,
}

// `kb done 207`, `KB_SESSION=x kb session note "…"` after a separator or env assignments
const KB_WRITE = /(?:^|[;&|(]\s*)(?:[A-Z_][A-Z0-9_]*=\S+\s+)*kb\s+(add|append|done|edit|link|promote|archive|snooze|skip|session\s+note)\b([^\n;&|]*)/g
const SUMMARY_WRITE = /\bkb\s+session\s+close\b[^\n]*--(?:body|body-file|title)\b/
const COMMIT_LINE = /^\[([^\]\s]+)(?: \(root-commit\))? ([0-9a-f]{7,40})\] (.+)$/gm
const PR_URL = /https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+/g

export function relativeTo(path: string, root: string): string {
  const prefix = `${root.replace(/\/+$/, '')}/`
  return path.startsWith(prefix) ? path.slice(prefix.length) : path
}

// `git commit` prints `[branch sha] subject`
export function commitsIn(output: string): string[] {
  return [...output.matchAll(COMMIT_LINE)].map(match => `${match[2]} ${match[3]} (${match[1]})`)
}

export function prUrlsIn(output: string): string[] {
  return [...new Set(output.match(PR_URL) ?? [])]
}

export function kbWritesIn(command: string): string[] {
  return [...command.matchAll(KB_WRITE)].map(match =>
    `kb ${(match[1] ?? '').replace(/\s+/g, ' ')}${(match[2] ?? '').trimEnd()}`.slice(0, 100),
  )
}

export function writesSummary(command: string): boolean {
  return SUMMARY_WRITE.test(command)
}

export function isGitCommit(command: string): boolean {
  return /\bgit\b[^\n;&|]*\bcommit\b/.test(command)
}

export function isPrCreate(command: string): boolean {
  return /\bgh\s+pr\s+create\b/.test(command)
}

export const FILE_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit'])

// one tool call as the transcript keeps it (ToolUseSummary) or as tool.call sees it
export type ToolUseLike = { tool: string; input: Record<string, unknown>; text?: string; isError?: true }

export type Facts = { files: string[]; commits: string[]; prs: string[]; kbWrites: string[]; isSummary: boolean }

export function factsOf(use: ToolUseLike, root: string): Facts {
  const facts: Facts = { files: [], commits: [], prs: [], kbWrites: [], isSummary: false }
  if (use.isError === true) return facts
  if (FILE_TOOLS.has(use.tool)) {
    const path = typeof use.input.file_path === 'string' ? use.input.file_path : use.input.notebook_path
    if (typeof path === 'string') facts.files.push(relativeTo(path, root))
  } else if (use.tool === 'Bash' && typeof use.input.command === 'string') {
    const command = use.input.command
    const output = use.text ?? ''
    if (isGitCommit(command)) facts.commits.push(...commitsIn(output))
    if (isPrCreate(command)) facts.prs.push(...prUrlsIn(output))
    facts.kbWrites.push(...kbWritesIn(command))
    facts.isSummary = writesSummary(command)
  }
  return facts
}

export function isEmpty(facts: Facts): boolean {
  return facts.files.length + facts.commits.length + facts.prs.length + facts.kbWrites.length === 0 && !facts.isSummary
}

export function mergeFacts(journal: Journal, facts: Facts): Journal {
  const merged = {
    ...journal,
    files: add(journal.files, facts.files),
    commits: add(journal.commits, facts.commits),
    prs: add(journal.prs, facts.prs),
    kbWrites: add(journal.kbWrites, facts.kbWrites),
  }
  // Claude's own `kb session close --body…` is a summary of everything so far
  return facts.isSummary ? { ...merged, summarized: true, mark: factCount(merged) } : merged
}

export function factCount(journal: Pick<Journal, 'files' | 'commits' | 'prs' | 'kbWrites'>): number {
  return journal.files.length + journal.commits.length + journal.prs.length + journal.kbWrites.length
}

// facts the session gathered after its last summary; 0 before any summary
export function sinceSummary(journal: Journal): number {
  return journal.summarized ? Math.max(0, factCount(journal) - journal.mark) : 0
}

const GIT_COMMIT_IN = /\bgit\b((?:\s+-[Cc]\s+\S+)*)\s+commit\b/

// The repository a `git commit` ran in: its -C directory, else cwd; null for a variable path the
// harness cannot expand. `git commit -q` prints no `[branch sha]` line, so the hook asks git log.
export function commitDirOf(command: string, cwd: string): string | null {
  const match = GIT_COMMIT_IN.exec(command)
  if (match === null) return null
  const raw = /-C\s+(\S+)/.exec(match[1] ?? '')?.[1]
  if (raw === undefined) return cwd
  const dir = raw.replace(/^["']|["']$/g, '')
  if (dir.startsWith('$')) return null
  return dir.startsWith('/') ? dir : `${cwd.replace(/\/+$/, '')}/${dir}`
}

export function hasWork(journal: Journal): boolean {
  return journal.files.length + journal.commits.length + journal.prs.length + journal.kbWrites.length > 0
}

// `add` keeps the order things happened in and never repeats an entry
export function add(list: readonly string[], items: readonly string[]): string[] {
  return [...list, ...items.filter(item => !list.includes(item))]
}

function listed(items: readonly string[]): string {
  return items.length === 0 ? 'none' : items.map(item => `  - ${item}`).join('\n')
}

export function journalPrompt(journal: Journal): string {
  return [
    'Write the kb journal entry for this session, the way earlier session records read.',
    '',
    'Answer with exactly this and nothing else:',
    'TITLE: <one line, at most 120 characters: what this session was for>',
    '',
    'Intent: <what the user asked for and why, one or two sentences>',
    '',
    'Tags: <#tags>',
    '',
    'Files touched: <from the recorded facts below; never a file that is not listed there>',
    '',
    'Done:',
    '- <one or two sentences each: what was done, with ids, PRs, commits>',
    '',
    'Decisions:',
    '- <who decided what and why; "none" when there were none>',
    '',
    'Open threads:',
    '- <what is left and where it is tracked; "none" when nothing is left>',
    '',
    'Write it in English. Facts recorded by the harness from the tool calls themselves:',
    `- files:\n${listed(journal.files)}`,
    `- commits:\n${listed(journal.commits)}`,
    `- pull requests:\n${listed(journal.prs)}`,
    `- kb writes:\n${listed(journal.kbWrites)}`,
  ].join('\n')
}

export function parseJournalReply(text: string): { title: string; body: string } {
  const lines = text.trim().split('\n')
  const first = lines[0] ?? ''
  const title = first.replace(/^TITLE:\s*/i, '').trim().slice(0, 120)
  const body = lines.slice(1).join('\n').trim()
  return { title, body }
}
