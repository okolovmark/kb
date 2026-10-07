import type { KbNode } from '../types'

type Raw = Record<string, unknown>

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

// `kb --json show <ref>`, cut to what the node pane draws; null when it is not a record
export function parseNode(stdout: string): KbNode | null {
  let raw: unknown
  try {
    raw = JSON.parse(stdout)
  } catch {
    return null
  }
  if (typeof raw !== 'object' || raw === null || typeof (raw as Raw).id !== 'number') return null
  const r = raw as Raw
  const urgency = r.urgency as { level?: number; name?: string; reason?: string } | null | undefined
  const links = Array.isArray(r.links) ? (r.links as Raw[]) : []
  const events = Array.isArray(r.events) ? (r.events as Raw[]) : []
  return {
    id: r.id as number,
    kind: text(r.kind),
    scope: text(r.scope),
    title: text(r.title),
    body: text(r.body),
    tags: Array.isArray(r.tags) ? (r.tags as unknown[]).map(String) : [],
    sched: text(r.sched),
    due: typeof r.due === 'string' ? r.due : null,
    isDone: typeof r.done_at === 'string',
    urgency:
      urgency && typeof urgency.level === 'number'
        ? { level: urgency.level, name: urgency.name ?? '', reason: urgency.reason ?? '' }
        : null,
    links: links
      .filter(link => typeof link.id === 'number')
      .map(link => ({ type: text(link.type), direction: text(link.direction), id: link.id as number, title: text(link.title) })),
    events: events.slice(0, 8).map(event => ({ kind: text(event.kind), at: text(event.at).slice(0, 16).replace('T', ' ') })),
  }
}
