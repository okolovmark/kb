// One row of `kb --json today`.
export type KbTask = {
  id: number
  level: number
  name: string
  reason: string
  title: string
  scope: string
  sched: string
  due: string | null
}

export type KbToday = {
  date: string
  scope: string
  tasks: KbTask[]
  quiet: number
  noSummary: number
}

// `kb --json show <ref>`, as much of it as the node pane draws
export type KbNode = {
  id: number
  kind: string
  scope: string
  title: string
  body: string
  tags: string[]
  sched: string
  due: string | null
  isDone: boolean
  urgency: { level: number; name: string; reason: string } | null
  links: { type: string; direction: string; id: number; title: string }[]
  events: { kind: string; at: string }[]
}

// What the session touched, recorded from the tool calls themselves rather than recalled.
export type Journal = {
  files: string[]
  commits: string[]
  prs: string[]
  kbWrites: string[]
  // a body reached `kb session close` (from the button, the tool, or Claude's own command)
  summarized: boolean
  title: string | null
  // how many facts the last summary covered: the ones after it make the summary stale
  mark: number
  isWriting: boolean
}

declare module 'claude-code' {
  interface PluginState {
    kb: {
      today: KbToday | null
      // the task whose `done` waits for a yes, and the task whose command runs
      armed: number | null
      busy: number | null
      // the record the node pane shows; whether the pane lists the whisper tasks
      node: KbNode | null
      showQuiet: boolean
      journal: Journal
    }
  }
}
