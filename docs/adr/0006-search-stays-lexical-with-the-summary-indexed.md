# 0006 Search stays lexical, with the summary indexed

Date: 2026-10-22
Status: accepted

## Context

`kb search` ran raw Lucene over `title` and `body`. An assessment on 2026-10-22 (kb record
[1176]) measured it against 61 labelled queries on a replica of the store: 49 real queries as
agents typed them, each paired with the record the agent opened next, and 12 paraphrases of
pinned records. The set is employer content and stays outside this repository; it is re-run
locally against the live store.

| Variant | Real (49) | Paraphrases (12) |
| --- | --- | --- |
| title + body, raw Lucene (before) | 43 | 6 |
| + summary in the index | 42 | 10 |
| + summary, English analyzer, escaping | 43 | 10 |
| vector search alone (two models, per record or per chunk) | 24-35 | 7 |
| hybrid: lexical + vector, reciprocal rank fusion | 41-45 | 9 |

The best hybrid gained 2 real queries in 49, within noise at this size, and cost a model, an
ONNX runtime under uv2nix, about 0.4 s and 200 MB more per search, and an embedding on every
write path. The summary field carried the paraphrase gain: summaries are the hand-written
paraphrase of each record. One query crashed: Lucene rejects `/tmp` (ProcedureCallFailed).

Two facts about Neo4j shaped the migration: `CREATE FULLTEXT INDEX … IF NOT EXISTS` is a silent
no-op when an index of that name exists with another definition, and Neo4j refuses a second
full-text index on the same label and properties.

## Decision

- **`record_text` covers `title`, `summary` and `body`,** with the analyzer named explicitly:
  `standard-no-stop-words`, Neo4j's default and what the index already used. The English
  analyzer was measured as noise either way, and stemming would change how identifiers split.
- **Queries stay raw Lucene.** When Lucene cannot parse one, `search` runs it again with the
  special characters escaped (and `AND`/`OR`/`NOT` lowered) and prints one stderr line saying it
  searched as plain text. Of 222 distinct real queries, one used Lucene syntax, and it was
  malformed; always escaping would have cost the syntax for nothing.
- **The schema is checked by definition, not by name.** `kb-setup` reads `SHOW FULLTEXT INDEXES`
  (labels, properties in order, analyzer), drops and recreates a stale `record_text` and waits
  for it to be online. `kb status` reports `schema: stale (record_text), run kb-setup`,
  `schema_stale` under `--json`, and exits 1. The name does not change, so search keeps
  answering from the old index until `kb-setup` runs.
- **The duplicate check is lexical too.** `kb add` and `kb sync odoo` search with the new title
  and summary, escaped, for open records of the same family (a task against tasks, knowledge
  against notes, howtos, feedback, references and decisions) in the view. Lucene supplies the
  top 20 candidates; the decision is a cosine of title and summary words, each weighted by its
  inverse document frequency over all records, at **0.40**.

## Calibration of the duplicate threshold

Measured 2026-10-22 on a copy of the store (952 records). Each record was checked against the
store as it was when it was created, approximated by leaving out records created after it and
records archived or done before it.

- **Six real duplicates**, merged by hand by 2026-10-21 ([682]/[677],
  [279]/[215], [277]/[234], [867]/[852], [1095]/[910], [1094]/[907]). Lucene ranks the older
  record first in all six. The cosine catches four: 0.61, 0.43, 0.51 and 0.45. The two it misses
  share their subject but hardly a word of their titles (0.30, and 0.04 where only the older
  body mentions the topic).
- **358 records created in the 30 days before**, five of the six excluded. Through `kb add`
  (314): 7 false refusals (**2.2 %**) and one real duplicate the merges had missed. Through the
  sync (44): 1 false hint and one real duplicate. The false ones are siblings: follow-ups of
  one ticket, tickets of one review.

| Threshold | Pairs caught | False refusals on add | False hints on sync |
| --- | --- | --- | --- |
| 0.30 | 4 / 6 | 21 / 314 (6.7 %) | 2 / 44 |
| 0.35 | 4 / 6 | 9 / 314 (2.9 %) | 2 / 44 |
| **0.40** | **4 / 6** | **7 / 314 (2.2 %)** | **1 / 44** |
| 0.45 | 2 / 6 | 2 / 314 (0.6 %) | 1 / 44 |

Lucene's own score could not decide: it grows with the length of the query, and the median top
score of the 358 ordinary records (14.2) sat between the scores of the six duplicates (12.8 to
23.9). Normalised by the candidate's score against its own title, it flagged 11 % of ordinary
records to catch four of the six; the margin over the second hit flagged 32 % to catch all six.

## Consequences

- Measured after the change on the store copy: real queries 41 of 47, paraphrases 10 of 12
  (before: 42 and 6; the two personal-scope queries of the 49 are left out of the local set).
  One real query lost its top-5 place to records whose summaries mention its words.
- `--distinct` writes `{"distinct": true, "similar": [ids]}` on the created event, so the refusal
  rate can be measured again from the store: due after 30 days of use.
- A stale index costs a drop and a rebuild; on 952 records that took 0.5 s.
- Reopen vector search only if a re-run of the eval falls below about 80 % hit@5 on real queries,
  or the store passes about 3000 records.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Vector or hybrid search | +2/49 real queries at a medium build, a Nix packaging rabbit hole and 3x search latency; the summary field gave the paraphrase gain for free. |
| English analyzer | Noise on both sets; it would change how identifiers tokenise. |
| Always escape queries | Loses the syntax for the one query in 222 that used it, to save a retry. |
| `CREATE … IF NOT EXISTS` with the new definition | A silent no-op on the existing name: the store would keep the old index forever. |
| Lucene score threshold for duplicates | Grows with query length; it cannot separate the six duplicates from ordinary records. |
| A ticket key found in an older record | Catches 3 of 6, and flags 11 % of records: follow-ups name the tickets they follow. |
