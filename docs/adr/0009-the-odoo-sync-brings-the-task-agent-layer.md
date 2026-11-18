# 0009 The Odoo sync brings the task's agent layer

Date: 2026-11-18
Status: accepted; amends [0006](0006-search-stays-lexical-with-the-summary-indexed.md) and
[0007](0007-area-tags-from-a-repo-owned-closed-vocabulary.md)

## Context

`kb sync odoo` brought seven fields of a task and wrote its form URL as the body. Every synced
task arrived without an area tag, so `kb context` refused it until an agent picked an area by
hand, at the start of every task. An Odoo whose tasks are shaped for agents offers more: the
modules a task touches, its type, its parent and blockers, an estimate, question rows and a
brief (the whole task as one computed Markdown text). The user asked for all four kinds of
data: area and type, links between tasks, the brief, the estimate and the open questions; and
for no knowledge records made from the task's notes or answers, which the brief carries.

## Decision

- **Optional fields, one `fields_get`.** Each field is read only where the task model has it:
  a `search_read` naming a missing field fails as a whole. An Odoo without them syncs as before.
- **A module is an area by name, or nothing.** A module whose technical name is an area of the
  scope's vocabulary tags the task; any other is skipped. The vocabulary stays closed (0007):
  the sync never adds an area. The task type maps to a type tag only where kb has one of the
  same meaning (`bug`, `feature`).
- **The sync owns the tags it added, no others.** `odoo_tags` remembers them; one Odoo no longer
  names is taken away. A tag added by hand is never taken away, even when Odoo named it too.
- **Links between the records of the result, owned by the sync.** The parent becomes `PART_OF`
  (child to parent) and each blocker `BLOCKS` (blocker to blocked), marked `origin: odoo-sync`.
  An edge is removed only when Odoo drops it while both tasks are still in the result; an edge
  drawn by hand is never touched.
- **A blocked task is silent.** A task an open task (neither done nor archived) `BLOCKS` is at
  level 0 with the reason `blocked by [id]`, as a snoozed one is, and like a snooze it cannot
  silence a hard deadline that has come. Urgency stays a function (0002), now of the graph too:
  `today`, `next`, `show` and `why` read the blockers when they compute it. The rule holds for
  edges drawn by hand as well.
- **The brief lives beside the body.** The body is the user's: notes are appended to it and the
  sync wrote it once. The brief is its own property, refreshed whenever Odoo's text differs,
  printed by `kb show` and indexed in `record_text` with title, summary and body.
- **The duplicate check ranks on title, summary and body.** A brief is long and repeats its
  task's words, so its field score could push a closer record out of the 20 candidates `kb add`
  asks Lucene for. The candidate query names the three fields it always searched; the decision
  stays the cosine of titles and summaries (0006).
- **Estimate and open questions are numbers on the record**, `estimate` in hours (none at 0)
  and `open_questions`, the task's question rows in state `open`.
- **`--dry-run`** runs the sync in a transaction it rolls back and prints the changes record by
  record, so a new mapping can be checked against a live Odoo before it writes.

## Consequences

- The release changes `record_text`: `kb status` reports the schema stale and exits 1 until
  `kb-setup` rebuilds the index, and search answers from the old index until then.
- The first sync after the upgrade updates every synced task once (tags, brief, estimate,
  question count).
- A task blocked by someone else's task, or by one outside the configured project, has no
  blocker record and is not silenced; its brief still names the blocker.
- An edge to a task that left the result (closed, or reassigned) stays, since the sync cannot
  tell which; a done blocker blocks nothing, so the edge only records history.
