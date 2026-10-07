import type { KbTask, KbToday } from '../types'

// `kb --json today`; null when the output is not the standup (kb missing, Neo4j down)
export function parseToday(stdout: string): KbToday | null {
  let raw: unknown
  try {
    raw = JSON.parse(stdout)
  } catch {
    return null
  }
  if (typeof raw !== 'object' || raw === null || !Array.isArray((raw as { tasks?: unknown }).tasks)) return null
  const data = raw as { date?: string; scope?: string; tasks: KbTask[]; quiet?: number; no_summary?: unknown[] }
  return {
    date: data.date ?? '',
    scope: data.scope ?? '',
    tasks: data.tasks,
    quiet: data.quiet ?? 0,
    noSummary: data.no_summary?.length ?? 0,
  }
}

export function countByLevel(tasks: readonly KbTask[]): { loud: number; normal: number; quiet: number; due: number } {
  return {
    loud: tasks.filter(task => task.level >= 3).length,
    normal: tasks.filter(task => task.level === 2).length,
    quiet: tasks.filter(task => task.level <= 1).length,
    due: tasks.filter(task => task.due !== null).length,
  }
}

export function plusDays(isoDate: string, days: number): string {
  const day = new Date(`${isoDate}T00:00:00Z`)
  day.setUTCDate(day.getUTCDate() + days)
  return day.toISOString().slice(0, 10)
}

// kb's urgency levels (urgency.py LEVEL_NAMES); `kb next` lists down to whisper, the "quiet" ones
export const LEVELS = [
  { level: 4, name: 'scream', glyph: '◉', color: 'magenta' },
  { level: 3, name: 'loud', glyph: '●', color: 'red' },
  { level: 2, name: 'normal', glyph: '○', color: 'yellow' },
  { level: 1, name: 'whisper', glyph: '◦', color: 'subtle' },
] as const

export function levelStyle(level: number): (typeof LEVELS)[number] {
  return LEVELS.find(style => level >= style.level) ?? LEVELS[3]
}
