# 0004 Raw Cypher, read and write

Date: 2026-09-11
Status: accepted

## Context

The CLI covers the common paths. Migrations, one-off repairs and inspection need the full
query language.

## Decision

- `kb query "<cypher>"` runs any Cypher, read or write, with `--param K=V` (repeatable) and
  JSON-lines output on stdout; a write reports its counters on stderr.
- No CLI command deletes a record: the CLI archives, supersedes or merges instead. Raw Cypher
  is the only way to delete.

## Consequences

- A raw write that creates a record must still take its `id` from the `Counter` node inside
  the write transaction and write an event; skipping either is a bug in the caller, not in kb.
- The CLI commands remain the primary path: commands first, raw writes only when no
  command fits.
- Constraints (`record_id`, `record_slug` unique) catch duplicate ids and slugs from raw
  writes; nothing catches a missing event.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Read-only `kb query` | Would push every repair to cypher-shell or Neo4j Browser with the same risk and no `--param`/JSON. |
| No raw access | Migrations and repairs would need a command each. |
