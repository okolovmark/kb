# 0005 Records, events and links in the graph

Date: 2026-09-13
Status: accepted

## Context

Phase 1 puts the domain into the store from ADR 0001: tasks with schedules, notes and the
other kinds, their history, and the links between them. The urgency function (ADR 0002)
needs the schedule and the events of a task in one read.

## Decision

- One node per record: `(:Record:<Kind>)` with `Kind` in Task, Note, Howto, Feedback,
  Reference, Decision, Session. Common properties: `id`, `slug` (unique, absent on tasks
  unless given), `kind`, `scope`, `title`, `body`, `created_at`, `updated_at`, `pinned`,
  `tags`, `source`, `external_id`, `journal_ref`, `archived_at`, `done_at`.
- `summary` (phase 2) is an optional one-line property beside `title`: the index prints it
  in place of the title, `show` under it; the node migration fills it from `description`.
- The schedule is a set of properties on the Task node, one schedule per task: `sched`
  (hard, soft, window, age, once), `due`, `window_rrule`, `anchor`, `days`, `repeat`,
  `snoozed_until`, `skips`, `backlog`, `escalation`. No schedule node, no schedule history:
  a changed schedule is an `edited` event plus the new values.
- Events are nodes: `(:Event {kind, at, seq, note, payload, session})-[:ON]->(:Record)`.
  `at` is UTC, `payload` a JSON string, `session` the `KB_SESSION` of the writing process,
  `seq` a per-process counter that orders events written at the same instant.
- Record-to-record relationships are a closed vocabulary: `RELATED`, `SUPERSEDES`, `BLOCKS`,
  `OPENED_IN`, `TOUCHED`, `PART_OF`, each with `created_at`. Another type is a CLI error;
  adding one is a code change.
- Window bookkeeping is lazy. A window is a run of consecutive rrule days (Saturday and
  Sunday are one window). When `today`, `next` or `show` evaluate a window task whose most
  recent window ended before today with no `done` or `skip` closing it, they write one
  `skip` event and increment `skips`; at `skips_to_backlog` the task is `backlog` and silent.
  The skip is `MERGE`d on (record, window last day), so two sessions evaluating the same
  window count it once. `skip` increments the count; `done`, `snooze` and `edit` reset it.
  Nothing is counted while a task is snoozed, and a window that ended inside a snooze period
  counts as touched. A `done` inside a window names the window in its payload.
- Recurrence advances on `done` to the first rrule occurrence strictly after max(today, due)
  (interval: today + N). Doing the 15th's taxes on the 14th moves the due to next month, not
  to tomorrow. A rule with nothing left (`COUNT`/`UNTIL`) closes the task with the note
  `recurrence exhausted`. A second `done` on the same day is a no-op.
- Day fit: when a task's `days` (work / off / any) does not match today, its level drops by
  one, except at scream (4) and silent (0). The reason gets ` (off-day)` or ` (workday)`.
  `once` tasks are exempt (a notification fires on the next session whatever the day) and
  respect snooze.
- The Odoo sync owns a task's schedule and `stage`; the person owns title and body. The body
  is written once at creation; the title follows Odoo only on records the sync created itself
  (`origin = odoo-sync`) and nobody edited; migrated, promoted and hand-added records keep
  theirs. `synced_at` marks records the sync has seen; only those are closed
  when they vanish from Odoo, the rest (promoted, migrated) are reported as untracked. A row
  matches by key or by Odoo id, so migrated `[odoo:NNN]` items are found and renamed to the
  key rather than duplicated.
- A matched record that was closed with `kb done` while Odoo still lists the task is reopened
  by the next sync: Odoo decides whether a ticket is open; `snooze` or `archive` silence it
  locally. An unchanged row moves `synced_at` only, never `updated_at`.
- `edited` events carry the previous title/body in their payload (`before`), by design: the
  body may be long, but the log is the only undo there is.

### Sessions (phase 3)

- A session is a record of kind `session`, not a second node type: it carries a body, tags,
  events and edges like everything else, so `show`, `search`, `log` and `links` need no session
  case beyond the rendering. Its own properties are `session_id` (Claude's id, unique among
  sessions and indexed), `source` (the SessionStart matcher, or `migrated`), `opened_at`,
  `closed_at`, `reason` and `no_summary`. The slug is `<YYYY-MM-DD>-<n>` — a date is how a
  session is looked for, and `n` counts the sessions opened that date in that scope; slugs are
  unique store-wide, so a second scope's first session of the day takes the next free number.
- The hooks build the skeleton, Claude writes the body: SessionStart runs `kb session open`,
  SessionEnd runs `kb session close --reason`. `open` is idempotent on `session_id` because
  resume, clear and compact re-enter the same session and must not start a second record;
  `close` is idempotent on the record, and a later `close --id X --body-file F` fills in the
  narrative. Until then the title is `session <date> <n> (<source>)`, a placeholder that says
  what it is.
- `no_summary` is a property, not a query over the body, because it is what `today` filters on:
  closed sessions of the last 7 days that have it surface as one line naming the slugs and the
  command that writes them. A session killed without a body is the case this exists for, and it
  is the only nagging kb does about its own bookkeeping. `note` and a late `close --body` clear
  it.
- A record created during a session gets `(record)-[:OPENED_IN]->(session)`, a record merely
  touched `(session)-[:TOUCHED]->(record)`. Both directions are MERGEd on the pair, so a session
  that edits one record twenty times still holds one edge, and the arrows point the way the
  vocabulary was decided on 2026-09-11.
- The session a write belongs to is `KB_SESSION` when the environment names one, else the most
  recently opened open session in the view scopes within the last 24 hours, else nothing. The
  fallback is a guess and is documented as one: two open sessions without the variable both
  write to the newer one. It is there so a command run by hand in a terminal still lands in the
  session, and the 24-hour bound keeps a session the hook never closed from collecting next
  week's work. A `KB_SESSION` no session carries links nothing and fails nothing: the graph
  losing an edge must never cost a command.
- A session whose `session_id` is the caller's `KB_SESSION` is addressable by `session open`,
  `close`, `note` and `current` from any cwd; every other session obeys the view. It is this
  process's own session, and the hook that opened it knew its scope better than the directory
  does. The four commands must agree on this, and the asymmetry is what makes it a rule rather
  than a convenience: with `open` alone ignoring the view, a session opened in one scope and
  resumed from another reopened and then could not be closed, so it stayed open, kept winning
  the 24-hour fallback and never reached the no-summary line.
- The journal migration reuses the same shape rather than a `journal` kind: one Session per
  `## Session` block with `session_id = journal:<date>-<n>` and `source = migrated`, the old
  text as the body. Its `[ID]` mentions become `TOUCHED` and the ids introduced by "added",
  "filed" or "opened" also `OPENED_IN`, which is the same rule the live hooks apply, derived
  from the text instead of from the writes. A `**Nodes created:**` line names records the same
  way, up to the first `;`: the corpus writes `created [[a]]; updated [[b]]` on one line.
- A journal `[NNN]` binds only to a **task** whose `created_at` is at most one day past the
  block's date. The number was a state-item id, and the numbers above the last one state.md
  used were later handed out to nodes and sessions: unfiltered, a 2026-09-11 block reads as
  having created the howto that now wears id 266. The day of slack covers an entry written
  after midnight; the comparison is made in the kb timezone, because a record created at
  midnight Manila is stored as 16:00 the day before and would otherwise get a free extra day.
  Everything filtered out goes to the report with its reason.
- The migration recognises its own sessions by `session_id`, never by slug. A slug is a date,
  and a live session of that date holds one already; keyed on the slug, an imported block would
  be dropped as "already there" and hand its edges to that live session. It takes the next free
  number instead, and the report names the swap. The `session_id` keeps the block's own
  numbering, so a re-run finds it whatever slug it ended up with.
- `opened_at` comes from the heading's clock time, read in three steps: a time in parentheses
  is ignored, a remaining range `HH:MM-HH:MM` gives its first time, anything else gives its
  last. Three headings in the corpus carry two times and no single "first" or "last" suits all
  of them. `(started 2026-08-03 ~16:30, spans midnight) — 11:30` states the previous evening as
  an aside, so the parenthesised start is deliberately ignored and the block is dated by the
  time on its own line. `started … 16:30, closed … 12:05` is dated 12:05, its close.
  `02:00-08:00` is dated 02:00, its start. The rule is judged by one property: blocks must sort
  in file order inside their day, and taking the first time everywhere put two of the three
  before the block written above them.

## Consequences

- One `MATCH` returns a task with everything the level needs; events come from a second
  pattern on the same id. No joins across schedule tables.
- Range indexes on `Record.kind`, `Record.scope`, `Record.external_id`, `Event.kind`; the
  full-text index `record_text` covers title and body; `(:Meta {name})` holds sync state.
- Skips are counted only when a standup runs; weeks without a session count as one skip.
- Merging records moves events and edges by recreating them on the kept record; the
  dropped record keeps its own `created` event and gets an `archived` one. A merged-away
  duplicate therefore loses its `OPENED_IN`: the session that created it lists the kept record.
- A unique constraint on `Session.session_id` makes `open` idempotent in the store rather than
  in the command, so two hooks racing on the same id cannot make two records.
- Every writing command resolves the current session before it writes, one extra read per
  command. The read is a single indexed match and the command works without a session at all.

## Alternatives considered

| Option | Why not |
| --- | --- |
| A `(:Schedule)` node per task, history as superseded schedule nodes | Two hops for every level computation; the event log already records changes. |
| Events as properties on a relationship or as a list on the record | No per-event payload, no cheap "newest first", no `session` per entry. |
| Free relationship types | Reports and `graph` would have to guess what a type means; the vocabulary was decided on 2026-09-11. |
| A timer that closes windows and counts skips | A second writer and a stale count between runs; lazy evaluation matches ADR 0002. |
| Sessions as their own node type outside `Record` | Every listing, search and link command would need a second code path for the kind that links to all the others. |
| Deriving the session of a write from the process (ppid, tty) | Nothing ties a shell to a Claude session; the hook already knows the id and can export it. |
| `no_summary` computed from the body at read time | `today` would scan bodies; the flag is what the write already knows. |
