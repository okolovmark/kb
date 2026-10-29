# The model

Records and their properties, the five kinds of schedule and the urgency each produces, events,
relationships, sessions, and the commands that work on them. Back to the [README](../README.md).

Records are addressed by id or slug. Mutating commands print `[id] title (slug)`; `--json` (before
or after the command name) prints the record instead. Errors are one line, exit 1 (usage errors 2;
4 when a tag is refused, 5 when `add` finds a duplicate). Every command takes `--help`.

## Records

Every record is `(:Record)` plus one kind label: `Task`, `Note`, `Howto`, `Feedback`, `Reference`,
`Decision`, `Session`. Properties: `id` (from the Counter), `slug` (unique; derived from the title
for every kind but Task), `kind`, `scope` (`global`, `personal` or a `[scopes]` name), `title`
(120 chars), `summary` (optional one line; `index` prints it in place of the title, `show` under
it), `body` (markdown), `created_at`, `updated_at`, `pinned` (default true for Feedback),
`pin_level` (`critical` | `normal` on a pinned record, absent otherwise; a record pinned before the
levels has none and counts as normal), `tags`, `source` (`manual` | `odoo`), `external_id`,
`journal_ref`, `archived_at`, `done_at`, and on synced tasks `stage`, `synced_at` and `origin`
(`odoo-sync` on records the sync created).

`kb add TITLE` creates one, a task unless `--kind` says otherwise, with the scope from `--scope`,
else the cwd inside a `[scopes]` path, else an error. Its other flags: `--body TEXT|-`,
`--body-file F`, `--summary S`, `--slug S`, `--pin/--no-pin`, `--critical` (pinned at level
critical, implies `--pin`; neither applies to a task), `--tag T` (repeatable, from the
scope's vocabulary, one area required: see [Tags](#tags)), `--link REL:REF` (repeatable), the
schedule flags below, `--source odoo --external-id X`, `--journal-ref R` and `--distinct` (see
[Duplicates](#duplicates)).

- `kb append REF TEXT` — body += `**<today>:** TEXT`. An empty or blank TEXT is refused (exit 2).
- `kb edit REF [--title T] [--summary S] [--body TEXT|-] [--body-file F] [--tag T] [--untag T]` —
  `--summary ""` clears the summary; `--tag` adds a tag from the vocabulary of the record's scope,
  `--untag` removes one (never the last area tag of a record that needs one); with no flag it
  opens `$EDITOR` on the body. An `edited` event, carrying the previous values, is written only
  when something changed. On a task it resets `skips` and `backlog`.
- `kb pin REF [--critical | --normal]` — `pinned = true` at that level, normal by default;
  `--normal` demotes a critical record. A task is refused. `kb unpin REF` — `pinned = false`,
  `pin_level` removed. A change writes an `edited` event whose payload names the changed fields
  with their `before` and `after` values; an unchanged state writes nothing and prints
  `already pinned <level>` / `already unpinned` (`written: false` under `--json`, which also
  carries the `before` state).
- `kb show REF` — header (`pinned: critical | normal | no`), the summary when set, schedule, `urgency: L<n> <name> — <reason>`, body,
  links, last 10 events. On a session it adds the state line (`session: <id>  opened: …
  open|closed: …`) and groups its `OPENED_IN` / `TOUCHED` neighbours as `created here:` /
  `touched here:`.
- `kb search QUERY [--scope S] [--kind K] [--all] [--limit N]` — full-text over title, summary
  and body, printing `[id] kind slug title  score`. The query is Lucene syntax; one Lucene cannot
  parse (a `/`, an unbalanced quote) is searched again as plain text, with
  `kb search: not valid Lucene syntax, searched as plain text` on stderr (`plain_text: true` under
  `--json`). Archived records only with `--all`.
- `kb archive REF --reason T` — hidden from today, next, search and index, found again with
  `--all`. Archiving an archived record prints a message and writes no event.
- `kb supersede OLD NEW` — `NEW -[SUPERSEDES]-> OLD`, OLD archived, `superseded` events on both.
- `kb merge DUP KEEP` — move DUP's events and edges onto KEEP, archive DUP (`merged into [keep]`),
  a `merged` event on KEEP.
- `kb promote REF KEY` — tasks only: `source = odoo`, `external_id = KEY`. The ticket itself is
  created elsewhere; notes and howtos link to a task with `kb link`.
- `kb index [--scope S] [--budget N] [--kinds K,...]` — the memory index a session hook prints:
  a header, `## Pinned` (pinned records of the scope plus global, critical ones first, then by
  id), then `## Recent` (the rest by `updated_at`), as lines `- [id] slug — summary` (the title
  when there is no summary, cut at 160 characters with `…`); a critical record reads
  `- [id] CRITICAL slug — summary`. Every record line starts with `- [`, which is how a hook
  finds the Pinned rules. Kinds default to `feedback,note,howto,reference,decision`. N is the
  total line budget, pinned first, so a short budget drops normal rules before critical ones
  (`[index].budget_lines`, N >= 1). `--json` prints `{date, scope, pinned, recent}`, each entry
  `{id, kind, scope, slug, title, summary, pin_level}`, cut where the text is cut; on an error
  `{error, pinned: [], recent: []}`. Exit 0 always.

## Tags

Two kinds: an **area** names a module or subsystem and pulls context (`kb context`); a **type**
says what kind of work a record is (`bug`, `feature`, `refactor`, `review`, `testing`, `docs`,
`config`, `research`, `discussion`, `cleanup`, `migration`, `data_fix`) and pulls the knowledge
records of that type from any area, so a rule about migrations reaches every task tagged
`migration`. The vocabulary is closed. Reasoning:
[adr/0007](adr/0007-area-tags-from-a-repo-owned-closed-vocabulary.md),
[adr/0008](adr/0008-type-tags-pull-knowledge-by-topic.md).

- **Areas.** A project scope's areas come from `.kb/tags.toml` at its `[scopes]` path (format in
  [configuration.md](configuration.md#kbtagstoml)), plus kb's built-in areas: `kb`,
  `claude_setup`, `tooling`, `odoo`, `workflow`, `writing`, `career`, `finance`, `home`.
  `global` and `personal` records use the built-in areas only.
- **Checked on write.** `add` and `edit --tag` refuse a tag outside the vocabulary of the
  record's scope (exit 4, listing the valid tags); tags are lowered. Tags a record already
  carries stay, whatever they are.
- **One area required.** `kb add` refuses a record without an area tag (exit 4), for every kind
  but a session and outside the personal scope. Tasks synced from Odoo arrive untagged; tagging
  one (`edit --tag` alone) does not stop its title from following Odoo.
- `kb tags [--scope S]` — the vocabulary of S (default the cwd scope): the file in use, areas and
  types with how many records in the view carry each, and the tags in use outside the vocabulary.
- `kb tags init [--scope S]` — writes a seed `.kb/tags.toml` at the project's root; an existing
  file is left alone.
- `kb context REF [--scope S]` — what to read before working on a record: its area tags, its
  type tags (a `types:` line, only when it has some), then every knowledge record (note,
  feedback, howto, reference, decision) in the view sharing an area or a type, as
  `[id] kind title — summary`, most shared areas first, then most shared types, then critical,
  then pinned, then most recently updated, then the other open tasks in its areas (types pull no
  tasks). `--json` prints `{id, kind, scope, title, areas, types, knowledge, tasks}`, each
  knowledge entry with the `areas` and `types` it shares. A record without an area tag is
  refused (exit 4) with the `kb edit REF --tag AREA` that fixes it.

## Duplicates

Before `kb add` creates a record it searches the view for open records of the same family (a
task against tasks; knowledge against notes, howtos, feedback, references and decisions): the
title and summary, escaped, against the full-text index, the top 20, then a cosine of the two
records' title and summary words, each word weighted by its rarity among all records. At 0.40 or
more the add is refused with exit 5:

```
similar: [412] task The export cron has no savepoint per batch
hint: kb append 412 TEXT, or re-run with --distinct
```

up to three `similar:` lines on stderr, or `{"similar": [{id, kind, scope, slug, title,
similarity}]}` under `--json`. Nothing is written. `--distinct` creates the record anyway and
records `{"distinct": true, "similar": [ids]}` on its `created` event.

`kb sync odoo` runs the same check when it creates a task for a new ticket, against open tasks
that are not Odoo tasks, and prints once
`possible duplicate: [new] <title> ~ [old] <title>: kb promote <old> <KEY>, then kb merge <new> <old>`
(`possible_duplicates` under `--json`). The threshold and its measurement:
[adr/0006](adr/0006-search-stays-lexical-with-the-summary-indexed.md).

## Consolidation

`kb consolidate [--scope S]` proposes and writes nothing: for notes, references, feedback and
howtos in the view,

- **merge** — records whose titles and summaries pass the duplicate threshold and whose area tags
  are the same set; the kept one is critical, else pinned, else the longest, else the oldest.
  Commands:
  `kb edit <keep> --body-file <merged.md>`, then `kb merge <dup> <keep>`.
- **condense** — a body of 10 KB or more, with its count of dated paragraphs. Command:
  `kb edit <id> --body-file <condensed.md>`; the `edited` event keeps the full previous body.
- **archive** — a record a live record `SUPERSEDES` (decisions included, the only case a decision
  appears), and a note of finished work: every task it is linked to closed 14 days ago or more, the
  note unchanged since, no live knowledge record linked to it.

Each proposal carries its records, a reason and the commands. Tasks and sessions never appear;
a decision is never merged or condensed. The plugin skill `kb-consolidate` puts the proposals in
front of the user and applies only what they approve.

## Schedules and urgency

A Task carries its schedule as properties: `sched` plus

| `sched` | Set by | Properties | Level |
| --- | --- | --- | --- |
| `hard` | `--due DATE` | `due` | whisper T-14, normal T-7, loud T-2, scream from T-0 on; snooze ignored from the due date |
| `soft` | `--plan DATE` | `due` | as hard until the date, then normal with `decide: rebalance or done (T+n)` |
| `age` | no flag | `anchor` (default today) | working days since the anchor: whisper 5, normal 10, loud 15, never screams |
| `window` | `--window weekend\|sat,sun\|RRULE` | `window_rrule`, `anchor` (rrule start), `skips`, `backlog` | normal inside a window, silent outside; an untouched window counts as skipped when the next standup runs (`today`, `next` and `show` write the `skip`); backlog after 2 skips; nothing is counted while snoozed |
| `once` | `--once` | | a notice for the user: normal until `kb notices dismiss` closes it, never on the standup (`today`, `next`), only in `kb notices`; not lowered by the day fit; snooze respected |

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
  last 7 days. `--record-shown` writes a `shown` event per printed task. `once` tasks (notices) are
  left out of the list, the `--json` tasks and the `quiet` count. `--json` also carries `others`:
  every open task below normal, whisper and silent, listed or not. Exit 0 always.
- `kb next [--scope S] [--all]` — like today, down to whisper (`--all`: down to silent, every open
  task), notices left out as well; writes no `shown` events, though the lazy window `skip`s still
  happen.
- `kb notices [--scope S]` — how many notices (`once` tasks at normal) wait, never their text:
  `N notices wait: kb notices show prints them, kb notices dismiss <id> closes one`, or
  `no notices`; `{scope, count, ids}` under `--json`. Writes nothing; exit 0 always. Notices are
  the user's to read: the kb plugin's pane lists them above the standup, a card each with a
  dismiss button, and its band shows the count; their text never enters the assistant's context.
- `kb notices show [--scope S]` — prints `kb notices <date> (<scope>)` and each waiting notice as
  `[id] title` with its body indented; `{date, scope, notices: [{id, scope, title, body}]}` under
  `--json`. Writes nothing: a notice stays until it is dismissed.
- `kb notices dismiss REF [--scope S]` — closes one notice: a `done` event with the note
  `dismissed`, then `done_at`; prints `[id] title  · dismissed`, the record under `--json`. Refuses
  (exit 1) a record that is not a `once` task, one done already or archived, and one outside the
  scope view.
- `kb why REF` — the level computation spelled out: schedule, thresholds and their source, calendar,
  snooze, day fit, result.

## Events and relationships

Events are nodes `(:Event {kind, at, seq, note, payload, session})-[:ON]->(:Record)`; kinds:
`created`, `edited` (payload: the changed fields and their `before` values; `pin` and `unpin` add
their `after` values), `done`, `skip` (one
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
`done`, `skip`, `snooze`, `append`, `edit`, `pin`, `unpin`, `archive`, `supersede`, `merge`,
`link`, `unlink`, `promote`, and the `--link` targets of `add`), each of which accepts `--scope S`: a record outside
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
`closed_at`, `reason` (the SessionEnd reason), `no_summary` and `title_hint`. The title is the intent, the body
the narrative in the old journal shape (Intent / Tags / Files touched / Done / Decisions / Open
threads).

- `kb session open --scope S --id SESSION_ID --source SRC [--title T]` — the SessionStart hook.
  Creates the Session record for that id (slug `<date>-<n>`, an `opened` event) or, when the id is
  already there, leaves it as it is, so resume and compact re-enter the same session; a closed one
  is re-opened, and a placeholder title takes the new source. `--title` is the session's title in
  the app, stored as `title_hint` (120 characters, the record's own title untouched); a later open
  with another one updates it.
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
2. `CLAUDE_CODE_SESSION_ID`: the agent's own session, open or closed. Claude Code puts this in the
   environment of every command it runs, while `KB_SESSION` only ever reaches a hook's own
   process - so for everything an agent types itself, this is the step that answers. An id no
   session carries falls through to the guess below.
3. Otherwise the most recently opened Session still open and not archived, inside the view scopes
   (`--scope S` = S + global, else the cwd scope + global), opened within the last 24 hours.
4. Otherwise nothing: the event carries no session, no edge is written, and no command fails.

Step 3 is a guess, and it is wrong whenever more than one session is live in a scope: they all
resolve to whichever opened last, so one agent's notes land in another's journal. It is wrong
again right after `session close` - the closed session drops out and the next write picks up
whoever is still running. A session opened in `global` sits inside every project's view, so it
outranks an older project session too. Steps 1 and 2 are the answer, and between them they cover
the hook and the agent.

A session the caller owns is returned whether it is open or closed: a note written after the
journal belongs to the session that earned it, not to whoever is still running.

`--id` is a REF like any other, with one exception: **a session whose id is the caller's
`KB_SESSION` is addressable from any cwd** by `session open`, `close`, `note` and `current`. It is
this process's own session, and the hook that opened it knew the scope better than the directory
does. Every other session obeys the view and is refused without printing its title. All four
commands share the rule deliberately: were `open` alone to ignore the view, a session opened in one
scope and resumed from another cwd would reopen and then refuse to close, so it would stay open,
keep winning the 24-hour fallback and never reach the no-summary line. Export `KB_SESSION` around
the hook's `session open` call; without it a cross-scope resume is refused loudly instead of
leaving behind a session nothing can close.

**`OPENED_IN` is single-valued on write.** A record is created once, in one session, so writing
one moves it: `kb link <record> OPENED_IN <session>` drops the record's edge to any other session
and says `(moved from [id])`. Re-pointing a record at the session that really created it is that
one command, with no `unlink` behind it. The one place two survive is a merge - `records merge`
carries the absorbed record's `OPENED_IN` onto the survivor, which genuinely then holds two
histories.

`Event.session` still carries the raw `KB_SESSION` when it names no session record, since it
answers which process wrote the event, not which record was open. `no_summary` follows the body
whichever command writes it, `session note`, `session close --body`, `edit --body` or `append`.
