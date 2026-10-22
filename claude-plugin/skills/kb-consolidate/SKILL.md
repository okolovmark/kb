---
name: kb-consolidate
description: Review kb's knowledge records with the user and apply only what they approve. `kb consolidate` proposes merges of overlapping notes, references, feedback and howtos, condensing of long append-logs, and archiving of what is no longer relevant; the proposals go into an artifact the user ticks, then the ticked ones are applied with kb merge, kb edit and kb archive. Use when the monthly consolidation task comes up, or when the user asks to consolidate, clean up, condense or de-duplicate kb memory.
---

# kb consolidate

`kb consolidate` only proposes, and nothing is applied without the user's tick. History is
never lost: every `kb edit` stores the previous body in its `edited` event (`kb log <id>`),
`kb merge` and `kb archive` only hide a record, and `--all` finds it again.

## 1. Collect

```sh
kb consolidate --json            # the cwd scope plus global; --scope S for another view
```

Each proposal has an `action` (`merge`, `condense`, `archive`), its `records`, a `reason`, the
`commands` that would apply it and, for a merge, the record to `keep`.

## 2. Prepare each proposal

Read every record involved with `kb show <id>` before you write anything.

- **merge.** Draft the kept record's new body: everything still true from all of them, once.
  The merge itself moves events and links, never text, so a merge without that edit loses
  the absorbed record's body from view.
- **condense.** Draft the current state: facts, rules, gotchas and open questions as they
  stand now. Drop superseded paragraphs, keep dates where a date is the fact. A record that
  is a log by nature (a journal, a measurement series) can be declined instead.
- **archive.** Check the reason against the record. A superseded decision is the only
  decision that ever appears here; a note is proposed when its tasks are closed and nothing
  cites it.

Tasks and sessions never appear here, and a decision is never rewritten.

## 3. Ask

Put the proposals in one artifact: one row per proposal with the action, the records (id,
kind, title, size), the reason, the drafted body or a link to it, and a checkbox. Leave every
box unticked. If no artifact can be published, give the same table in the reply. Then wait.

## 4. Apply only the ticked rows

```sh
kb edit <keep> --body-file merged.md && kb merge <dup> <keep>         # merge, per dup
kb edit <id> --body-file condensed.md                                  # condense
kb archive <id> --reason "superseded by [<new>]"                      # archive, reason as proposed
```

Report what was applied and what was declined, with ids. To undo a condense, take the
`before.body` of that `edited` event from `kb --json log <id>` and write it back with
`kb edit <id> --body-file`.
