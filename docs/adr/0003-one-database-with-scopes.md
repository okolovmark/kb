# 0003 One database with scopes

Date: 2026-09-11
Status: accepted

## Context

kb serves personal items and every project on the machine. Project sessions are recorded
by `entire`, which pushes session transcripts to org-visible refs: anything printed in a
project session is effectively shared with the organisation.

## Decision

- One Neo4j database for everything; every record carries `scope`.
- `scope` is one of `global`, `personal`, or a project name.
- `[scopes]` in `config.toml` maps project name to an absolute path; the cwd inside a path
  selects that project. `--scope` always wins over cwd.
- A project session shows `global` + that project. `personal` is hidden there.
- The view applies to every REF, not only to listings (2026-09-13): a record outside the view
  is refused without printing its title, and `links`, `graph`, `show` hide neighbours outside
  the view behind a count. Cross-scope edges exist; they are only ever shown from a view that
  contains both ends.
- Outside every configured path: `today` shows everything; `add` refuses without `--scope`.

## Consequences

- No per-project database, one backup file, one counter, cross-scope links are possible.
- Every read command filters by scope; forgetting the filter leaks personal items into an
  org-visible transcript. The filter belongs in one query helper, not in each command.
- Renaming a project means rewriting `scope` on its records.

## Alternatives considered

| Option | Why not |
| --- | --- |
| One database per project | No cross-project links, N backups, N counters, N services. |
| Separate personal database | Same split cost for the one scope that needs hiding; a filter does it. |
| Neo4j multi-database | Enterprise-only feature set for what is one property. |
