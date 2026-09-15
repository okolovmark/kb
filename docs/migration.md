# Importing a markdown memory

The three one-off imports, what each reads, what it writes and what it reports. Back to the
[README](../README.md).

Run them in this order — state, then nodes, then journal — because the later ones only link to
records that already exist. Each takes `--scope S` and a `--dry-run` that writes nothing.

## state.md to tasks

```sh
kb migrate state FILE --scope my-project [--dry-run] [--counter N]
```

Sections `## Ship blockers | Blocked | Ready for review | WIP | Open threads` become the tags
`ship_blocker`, `blocked`, `ready_for_review`, `wip`, `open_thread`. Open lines
`- [ID] YYYY-MM-DD: TEXT` and closed comments `<!-- [ID] CLOSED YYYY-MM-DD: TEXT -->` (multi-line
allowed) become age tasks with THAT id, `anchor` = the item date, `days = work`, title = TEXT up to
the first ` — ` (100 chars, bold markers dropped), body = TEXT, `journal_ref` from `see journal/...`,
`source = odoo` + `external_id` from a trailing `[odoo:NNN]`, a `created` event at the item date;
closed items also get `done` and `done_at` at the CLOSED date. Existing ids are skipped and listed.

The Counter is untouched unless an item id is above it, in which case it moves up to that id;
`--counter N` sets it to N instead. N must not be below the highest item id, the current Counter or
the highest existing record id (the error names all three): the old `state_counter` may be ahead
of state.md because ids were also spent on journal and node files, so read it first and pass
`--counter <state_counter - 1>` on a database where nothing was added yet.

## Memory nodes to records

```sh
kb migrate nodes DIR --scope my-project [--scope-file F] [--dry-run] [--report F]
```

Every `*.md` with a `---` frontmatter block becomes a record (a file without one is skipped and
listed): slug = frontmatter `name` (else the file stem; an existing record with that slug is
skipped and listed), kind from `metadata.type` (`feedback` -> Feedback, pinned; `user` -> Note,
pinned, tag `user`; `project` -> Note, tag `project`; `reference` -> Reference; missing -> Note;
a slug starting with `conventions_` -> Howto whatever the type), title = the leading `# ` heading
(the heading line and any leading blank lines leave the body, everything else stays verbatim;
without a heading the slug minus its type prefix, `_` -> space, first letter capitalised:
`feedback_attach_prod_scripts` -> `Attach prod scripts`), summary = `description`, tags = the type
plus `migrated`.

Dates are midnight in the kb timezone: `created_at` = `metadata.created`, else `modified`, else
now; `updated_at` = `metadata.updated`, else `modified`, else `created_at`. A file with none of the
three is listed in the report under "Dated now". Events: `created` at `created_at` with the note
`migrated from nodes/<file>`, and `edited` at `updated_at` (`last markdown update`) when the two
differ. A slug must already be in slug form (`slugify(slug) == slug`, so `reference_KIO-1` is
refused before anything is written: the store would slugify it and a re-run could not find it).

Then, once every record exists, each `[[slug]]` and `](nodes/slug.md)` in a body, outside fenced
blocks and inline code spans, becomes one `RELATED` edge to the record with that slug (migrated now
or already there; one edge per pair, no self-links, a `linked` event on the source); a target with
no record stays text and lands in the report as `slug ← n files`. The link pass also covers skipped
records, so a re-run after a missing target appeared adds the edge.

The scope file is optional: `slug<whitespace>scope` per line, `#` comments, blank lines allowed, a
slug listed twice is an error; every scope (the default too) must be `global`, `personal` or a
`[scopes]` name, checked before anything is written; slugs not in DIR are reported on stderr and
ignored. `--report FILE` needs an existing directory, checked before the run. `--dry-run` prints one
line per file (slug, kind, scope, pinned, links, title) plus the report and writes nothing; the
report (created / skipped / no frontmatter / dated now / unresolved targets) goes to
`--report FILE` as markdown or to stdout after the run, followed by `created N records (F feedback,
U user notes, P project notes, R references, H howtos), skipped S existing, linked L edges,
U unresolved link targets`.

## Journal days to sessions

```sh
kb migrate journal DIR --scope my-project [--dry-run] [--report F]
```

Run it last: the edges only land on records that already exist, so migrate state and nodes first.
Files named `YYYY-MM-DD.md` are read, every other `*.md` (the directory's `README.md`) is listed and
skipped. Inside a file a block starts at a `## Session` heading at column 0 outside a code fence
(`## Session 2 — 14:06`, an em dash or a hyphen, the time optional) and ends at the next `## `
heading at column 0, at a `---` line followed by a blank line or EOF, or at EOF. Everything outside
a block — the `# YYYY-MM-DD` header, `## Daily standup` sections, stray headings — is dropped, so
an `[ID]` mentioned in a standup does not link. The block number is the first integer of the
heading, unless that integer is an hour (`## Session 11:45`); a repeated or missing number becomes
the next integer above every number the file uses and is reported under "Numbered by position".

Each block becomes a Session: slug `<date>-<N>` (the next free number of that date when a live
session already holds it, reported under `## Renumbered`), `session_id` `journal:<date>-<N>`,
`source = migrated`, title = the `**Intent:**` value (its wrapped continuation lines joined, cut to
120 characters with `…`; `session <date> <N>` without one), body = the block verbatim minus its
heading line, tags = the `**Tags:**` hashtags without `#` plus `migrated`, `no_summary = false`.
Events: `created` with the note `migrated from journal/<file>`, `opened` and `closed`, all three at
the block's timestamp.

`opened_at` = `closed_at` = `created_at` is the heading's clock time in the kb timezone, read in
three steps: a time inside parentheses is an aside about another day and is ignored, a remaining
range `HH:MM-HH:MM` gives its first time, anything else gives its last. Midnight when the heading
names no time. The rule keeps blocks in file order inside their day.

Then, per block and outside fenced blocks and inline code spans, every `[NNN]` becomes
`(session)-[:TOUCHED]->(record NNN)`, and an id introduced by `added [`, `filed [` or `opened [`
(the whole chain: `added [48] [49] [50]`, `added [20] (first), [21]`) also gets
`(record)-[:OPENED_IN]->(session)`. An id binds only to a **task** created no more than a day after
the block: `[NNN]` was a state-item number, and the numbers above the last one state.md used went
to nodes and sessions later, so binding by id alone makes a 2026-09 block look like the author of a
howto. Every `[[slug]]` that resolves becomes one more `TOUCHED`, and the slugs a
`**Nodes created:**` line names before its first `;` also get `OPENED_IN`.

Edges are MERGEd, so a re-run adds nothing twice, and the link pass covers blocks whose session
already existed: run it again once the missing targets are in. Ids and slugs that resolve to nothing
stay text and land in the report with the sessions that mention them and the reason. The migration
is idempotent by `session_id`, not by slug, so a live session holding a block's slug renumbers the
block instead of swallowing it. `--dry-run` prints one line per block (slug, time, edges,
unresolved, title) plus the report on stdout (`--report FILE` is for real runs) and writes nothing
at all. The report (created / skipped / numbered by position / renumbered / skipped files /
unresolved targets) goes to `--report FILE` as markdown or to stdout, followed by `created N
sessions across D days, linked T touched / O opened_in edges, U unresolved targets, skipped S
existing`.
