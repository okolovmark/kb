# The model

Records and their properties, the five kinds of schedule and the urgency each produces, events,
relationships, sessions, and the commands that work on them. Back to the [README](../README.md).

Records are addressed by id or slug. Mutating commands print `[id] title (slug)`; `--json` (before
or after the command name) prints the record instead. Errors are one line, exit 1 (usage errors 2).
Every command takes `--help`.

## Records

Every record is `(:Record)` plus one kind label: `Task`, `Note`, `Howto`, `Feedback`, `Reference`,
`Decision`, `Session`. Properties: `id` (from the Counter), `slug` (unique; derived from the title
for every kind but Task), `kind`, `scope` (`global`, `personal` or a `[scopes]` name), `title`
(120 chars), `summary` (optional one line; `index` prints it in place of the title, `show` under
it), `body` (markdown), `created_at`, `updated_at`, `pinned` (default true for Feedback), `tags`,
`source` (`manual` | `odoo`), `external_id`, `journal_ref`, `archived_at`, `done_at`, and on
synced tasks `stage`, `synced_at` and `origin` (`odoo-sync` on records the sync created).

`kb add TITLE` creates one, a task unless `--kind` says otherwise, with the scope from `--scope`,
else the cwd inside a `[scopes]` path, else an error. Its other flags: `--body TEXT|-`,
`--body-file F`, `--summary S`, `--slug S`, `--pin/--no-pin`, `--tag T` (repeatable),
`--link REL:REF` (repeatable), the schedule flags below, `--source odoo --external-id X` and
`--journal-ref R`.

- `kb append REF TEXT` — body += `**<today>:** TEXT`.
- `kb edit REF [--title T] [--summary S] [--body TEXT|-] [--body-file F]` — `--summary ""` clears
  the summary; with no flag it opens `$EDITOR` on the body. An `edited` event, carrying the previous
  values, is written only when something changed. On a task it resets `skips` and `backlog`.
- `kb show REF` — header, the summary when set, schedule, `urgency: L<n> <name> — <reason>`, body,
  links, last 10 events. On a session it adds the state line (`session: <id>  opened: …
  open|closed: …`) and groups its `OPENED_IN` / `TOUCHED` neighbours as `created here:` /
  `touched here:`.
- `kb search QUERY [--scope S] [--kind K] [--all] [--limit N]` — full-text (Lucene) over title and
  body, printing `[id] kind slug title  score`. Archived records only with `--all`.
- `kb archive REF --reason T` — hidden from today, next, search and index, found again with
  `--all`. Archiving an archived record prints a message and writes no event.
- `kb supersede OLD NEW` — `NEW -[SUPERSEDES]-> OLD`, OLD archived, `superseded` events on both.
- `kb merge DUP KEEP` — move DUP's events and edges onto KEEP, archive DUP (`merged into [keep]`),
  a `merged` event on KEEP.
- `kb promote REF KEY` — tasks only: `source = odoo`, `external_id = KEY`. The ticket itself is
  created elsewhere; notes and howtos link to a task with `kb link`.
- `kb index [--scope S] [--budget N] [--kinds K,...]` — the memory index a session hook prints:
  a header, `## Pinned` (pinned records of the scope plus global), then `## Recent` (the rest by
  `updated_at`), as lines `- [id] slug — summary` (the title when there is no summary, cut at 160
  characters with `…`). Kinds default to `feedback,note,howto,reference,decision`. N is the total
  line budget, pinned first (`[index].budget_lines`, N >= 1). Exit 0 always.

## Schedules and urgency

A Task carries its schedule as properties: `sched` plus

| `sched` | Set by | Properties | Level |
| --- | --- | --- | --- |
| `hard` | `--due DATE` | `due` | whisper T-14, normal T-7, loud T-2, scream from T-0 on; snooze ignored from the due date |
| `soft` | `--plan DATE` | `due` | as hard until the date, then normal with `decide: rebalance or done (T+n)` |
| `age` | no flag | `anchor` (default today) | working days since the anchor: whisper 5, normal 10, loud 15, never screams |
| `window` | `--window weekend\|sat,sun\|RRULE` | `window_rrule`, `anchor` (rrule start), `skips`, `backlog` | normal inside a window, silent outside; an untouched window counts as skipped when the next standup runs (`today`, `next` and `show` write the `skip`); backlog after 2 skips; nothing is counted while snoozed |
| `once` | `--once` | | normal until a standup with `--record-shown` prints it, then done; not lowered by the day fit; snooze respected |

Shared: `days` (`work` / `off` / `any`, default by scope: project work, personal off, global any;
a mismatch with today lowers the level by one except at scream), `repeat`
(`calendar:<rrule>` or `interval:<N>`, with `--due`/`--plan`; `done` moves `due` to the first
occurrence after max(today, due), or today + N, and keeps the task open; a `COUNT`/`UNTIL` rule with
nothing left closes the task with the note `recurrence exhausted`), `snoozed_until`, `escalation`
(JSON overrides: `whisper`, `normal`, `loud`, `skips_to_backlog`). Thresholds come from
`[escalation.*]`. `done`, `snooze` and `edit` reset `skips` and `backlog`.

Working days are Monday to Friday minus the public holidays of `[calendar] country`, plus whatever
`extra_days_off` lists.

- `kb done REF [COMMENT]` — a `done` event. Recurring and window tasks advance and stay open, and
  print the next date; the rest get `done_at`. A second `done` on the same day, or inside an
  already closed window, is a no-op.
- `kb skip REF` — window tasks only: close the current (or last) window, `skips += 1`, backlog at
  the threshold.
- `kb snooze REF [DATE]` — silent until DATE, tomorrow by default; refused on or past a hard
  deadline.
- `kb today [--scope S] [--record-shown]` — the standup: `kb today <date> (<scope>)`, the Odoo line
  when the last sync failed, every open task at level 2..4 as `[id] L<n> <name>  <title>  ·
  <reason>`, then `+N quiet (whisper), kb next`, then the sessions closed without a summary in the
  last 7 days. `--record-shown` writes a `shown` event per printed task and closes printed `once`
  tasks. Exit 0 always.
- `kb next [--scope S]` — like today, down to whisper; writes no `shown` events, though the lazy
  window `skip`s still happen.
- `kb why REF` — the level computation spelled out: schedule, thresholds and their source, calendar,
  snooze, day fit, result.

## Events and relationships

Events are nodes `(:Event {kind, at, seq, note, payload, session})-[:ON]->(:Record)`; kinds:
`created`, `edited` (payload: the changed fields and their `before` values), `done`, `skip` (one
per record and window, keyed on the window's last day), `snooze`, `shown`, `synced`, `promoted`,
`archived`, `superseded`, `merged`, `linked`, `opened`, `closed`.

Relationships between records are a closed vocabulary: `RELATED`, `SUPERSEDES`, `BLOCKS`,
`OPENED_IN`, `TOUCHED`, `PART_OF`, each with `created_at`. `kb link A TYPE B` and
`kb unlink A TYPE B` manage them by hand; a type outside the vocabulary is an error. The reasoning
is in [adr/0005-records-events-and-links-in-the-graph.md](adr/0005-records-events-and-links-in-the-graph.md).

## Scopes and the view

The scope filter of `today`, `next`, `search` and `index`: `--scope S` shows S plus `global`;
without the flag, the cwd inside a `[scopes]` path selects that project, and outside every path
everything is shown, `personal` included. `add` outside every path needs `--scope`.

The same view rules every command that takes a REF (`show`, `links`, `graph`, `log`, `why`,
`done`, `skip`, `snooze`, `append`, `edit`, `archive`, `supersede`, `merge`, `link`, `unlink`,
`promote`, and the `--link` targets of `add`), each of which accepts `--scope S`: a record outside
the view is refused with `not visible in this scope: [id] is in scope <s>; rerun with --scope <s>`
(exit 1, no title printed), reads and writes alike. `links`, `graph` and the link section of `show`
hide neighbours outside the view and end with `n linked records in other scopes (hidden)`
(`hidden_links` under `--json`).

## Sessions

A Session is the record of one Claude session: the hooks build the skeleton, Claude writes the
body. Its slug is `<YYYY-MM-DD>-<n>`, where `n` is the lowest number no record of that date carries,
counted across every scope rather than per scope, so a second scope's first session of the day takes
the next free number. Its own properties: `session_id` (Claude's id, unique among sessions),
`source` (`startup` | `resume` | `clear` | `compact` | `fork` | `migrated`), `opened_at`,
`closed_at`, `reason` (the SessionEnd reason) and `no_summary`. The title is the intent, the body
the narrative in the old journal shape (Intent / Tags / Files touched / Done / Decisions / Open
threads).

- `kb session open --scope S --id SESSION_ID --source SRC` — the SessionStart hook. Creates the
  Session record for that id (slug `<date>-<n>`, an `opened` event) or, when the id is already
  there, leaves it as it is, so resume and compact re-enter the same session; a closed one is
  re-opened, and a placeholder title takes the new source.
- `kb session close [--id SESSION_ID] [--reason R] [--body TEXT|-|--body-file F] [--title T]` — the
  SessionEnd hook plus Claude's summary: `closed_at`, `reason` and a `closed` event on the first
  call. `--body` and `--title` write the narrative, then or any time later; `no_summary` follows the
  body being empty, so a later `--body-file` clears it.
- `kb session note [--id SESSION_ID] TEXT | --body-file F` — body += `**<today>:** TEXT`, an
  `edited` event, and `no_summary` cleared. An empty note is an error.
- `kb session current [--scope S]` — `[id] slug (open since <ts>)` of the current session; exit 1
  with `no open session` when there is none.
- `kb session list [--scope S] [--limit N] [--open]` — newest opened first, as
  `[id] slug  title  · opened <ts>  open|closed <ts>  · N created / M touched`.

### The current session

Every command that writes an event stamps the event with the session it belongs to and links the
record to it: a record created now gets `(record)-[:OPENED_IN]->(session)`, a record merely touched
gets `(session)-[:TOUCHED]->(record)`, one edge per pair. Which session that is:

1. `KB_SESSION` in the environment: the Session carrying that `session_id`, whatever its scope. The
   hook exports it, so this is the normal path. An id no session carries links nothing, silently.
2. Otherwise the most recently opened Session still open and not archived, inside the view scopes
   (`--scope S` = S + global, else the cwd scope + global), opened within the last 24 hours.
3. Otherwise nothing: the event carries no session, no edge is written, and no command fails.

Two sessions open at once and no `KB_SESSION` means both write to the newer one; a session opened
in `global` sits inside every project's view, so it outranks an older project session. Exporting
`KB_SESSION` is the answer, and the hook does.

`--id` is a REF like any other, with one exception: **a session whose id is the caller's
`KB_SESSION` is addressable from any cwd** by `session open`, `close`, `note` and `current`. It is
this process's own session, and the hook that opened it knew the scope better than the directory
does. Every other session obeys the view and is refused without printing its title. All four
commands share the rule deliberately: were `open` alone to ignore the view, a session opened in one
scope and resumed from another cwd would reopen and then refuse to close, so it would stay open,
keep winning the 24-hour fallback and never reach the no-summary line. Export `KB_SESSION` around
the hook's `session open` call; without it a cross-scope resume is refused loudly instead of
leaving behind a session nothing can close.

`Event.session` still carries the raw `KB_SESSION` when it names no session record, since it
answers which process wrote the event, not which record was open. `no_summary` follows the body
whichever command writes it, `session note`, `session close --body`, `edit --body` or `append`.
