# 0008 Type tags pull knowledge by topic

Date: 2026-10-28
Status: accepted; amends [0007](0007-area-tags-from-a-repo-owned-closed-vocabulary.md)

## Context

[0007](0007-area-tags-from-a-repo-owned-closed-vocabulary.md) made `kb context` list the
knowledge records that share an *area* with the task; type tags pulled nothing. Some rules bind
by the kind of work, not by the module. On 2026-10-26 (kb record [1244]) a task in a purchase
module shipped a data migration. The rule for migrations (an ORM write can vanish while the
version stamp survives: flush, then check by SQL) carried the framework area and a product
module's area, so the task's `kb context` did not list it. An agent session that briefs its
workers from the bodies of every record `kb context` lists only had the rule because someone
sent it by hand.

Nothing marked such a record. It carried no flag, and the only link was a `RELATED` edge from
another record in the list. The type tags already name the topics in question (`migration`,
`data_fix`), and on 2026-10-28 only four knowledge records carried any type tag.

## Decision

- **A type tag on the task pulls knowledge too.** `kb context` lists the knowledge records that
  share an area *or* a type tag with the task: a rule tagged `migration` reaches every task
  tagged `migration`, whatever its module.
- **Types pull knowledge only.** The open tasks are still the ones that share an area, because
  another module's migration task is not context for this one.
- **Areas rank first.** The knowledge list is ordered by shared areas, then shared types, then
  critical, pinned and the most recently updated, as before. The text output gains a
  `types: …` line after `areas: …` when the task has type tags. The JSON output gains
  `types` at the top and on each knowledge entry.
- **An area is still required.** A record with type tags and no area is refused as before.

## Consequences

- A rule about a kind of work is tagged with that type (`kb edit REF --tag migration`), and a
  task gets the type tags of the work it will do before `kb context` runs. In a pipeline, the
  type is often known only after the requirements are settled, so the run tags the task then and
  runs `kb context` again.
- A type that knowledge records rarely carry (`bug`, `feature`) pulls little; one that many
  carry would lengthen every list. That is a reason to put a type on a knowledge record only when
  it states a rule for that kind of work.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Follow `RELATED` one hop from the listed records | Reached the field case, but grew its list from 10 records to 23, most of them unrelated. |
| Areas a project marks as pulled for every task | Coarse: every task gets every record of the area, and a rule reaches a task only if it carries that area. |
| A record flag, "applies to every task" | The same as pinning, which already puts a record in front of every session. |
| Full-text search on the task's title | Lexical: the task's title shares no words with the rule's. |
| Accept the gap | The rule reaches the work only by hand. |
