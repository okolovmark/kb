# 0007 Area tags from a repo-owned closed vocabulary

Date: 2026-10-22
Status: accepted; amended by [0008](0008-type-tags-pull-knowledge-by-topic.md) (type tags pull
knowledge records too)

## Context

Tags were free text. On 2026-10-22 the store carried 211 distinct tags. 190 of them were only on
sessions, from the journal migration, with three spellings of one module side by side. The other records carried 21, most of them markers of
the migrations (`migrated`, `reference`, `feedback`, `open_thread`), and 405 of 543 carried none.
Nothing could load "what we know about this module" when a task is taken into work, because
nothing said which module a record is about.

A project is shared by a team, and kb runs once per person: whatever names the modules must be
the same for everyone and change by review, not by whoever typed a tag first.

## Decision

- **Two kinds of tag.** An *area* names a module or subsystem and pulls context: `kb context`
  lists the knowledge records and open tasks that share one. A *type* says what kind of work a
  record is and pulls nothing.
- **The project's areas live in its repository**, in `.kb/tags.toml` at the root of the
  `[scopes]` path: `[areas] name = "description"`. The project owns the file and changes it by
  pull request. The copier template renders it for its projects and never overwrites it;
  `kb tags init` writes a seed elsewhere.
- **kb ships the rest.** Built-in areas for what global and personal records are about and for
  the setup around any project: `kb`, `claude_setup`, `tooling`, `odoo`, `workflow`,
  `writing`, `career`, `finance`, `home`. Type tags: `bug`, `feature`, `refactor`, `review`, `testing`,
  `docs`, `config`, `research`, `discussion`, `cleanup`, `migration`, `data_fix`. Changing either
  list is a kb release.
- **Which tags a record may carry.** In a project scope: the file's areas, the built-in areas and
  the types. In `global` and `personal`: the built-in areas and the types; a project's areas
  mean nothing outside it. A file area may share a built-in area's name (its description wins),
  never a type tag's.
- **Closed on write, lenient on read.** `add` and `edit --tag` refuse a tag outside the
  vocabulary (exit 4, listing the valid ones); tags a record already carries stay, and still
  match in `kb context`.
- **One area is required.** `kb add` refuses a record without an area tag (exit 4), for every
  kind but a session (sessions are written by hooks) and outside the personal scope. `edit
  --untag` refuses to remove the last one.
- **Tasks synced from Odoo arrive untagged.** `kb context` on a record without an area refuses
  (exit 4) with the `kb edit <id> --tag AREA` to run, so the tag is set when the task is taken
  into work. A tags-only edit is not local authorship: the title keeps following Odoo.
- **The file is parsed strictly**, as `config.toml` is: an unknown table, a name outside
  `[a-z0-9][a-z0-9_-]*` or a non-string description is an error naming the file (exit 1).

The built-in areas were chosen from what the 70 live global and personal records cover:
kb itself, the agent harness (settings, hooks, skills, MCP), developer tools and the machine,
working rules (estimates, reviews, status, conduct), texts written in the user's name, and
career, money and the household, which had records or tasks but no shared word. `odoo` was added
in review: knowledge about Odoo itself is shared by every Odoo project on the machine (16, 19,
20), and five global records were about it with no other area to go to.

## Consequences

- `kb context` can answer from tags alone; bodies are read on demand with `kb show`.
- `kb consolidate` proposes a merge only for records with the same set of areas.
- A teammate needs a PR to add an area, and a broken file stops every `add` in that scope
  until it is fixed; both are the price of one vocabulary per team.
- The backfill of existing records is a separate, reviewed step: the vocabulary first, then a
  table of record → tags for approval, then one batch write with events.
- Old free tags (`migrated`, `open_thread`, …) stay on their records; `kb tags` lists them as
  "not in the vocabulary" with their counts, so their retirement is a decision, not a side
  effect.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Free tags, as before | Nothing to load context by; spellings diverge per person. |
| The vocabulary in `config.toml` | Per machine: two teammates would tag the same module differently. |
| The vocabulary in the graph | Not reviewable by PR, and each person's store would drift. |
| Areas derived from the Odoo project or module | No reliable mapping from a ticket to a module; the person taking the work knows. |
| Tags required on sessions too | Hooks open sessions; there is nobody to choose the tag at that moment. |
| Unknown tags refused on read as well | Would hide the migrated tags, and their retirement is not this change. |
