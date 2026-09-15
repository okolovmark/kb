# 0001 Neo4j Community is the canonical store

Date: 2026-09-11
Status: accepted

## Context

kb replaces the Claude Code memory files (`state.md`, `nodes/*.md`, `journal/`) with one
queryable graph of tasks, notes, sessions and their links. Everything on this machine
installs through Nix, the database included. Candidates were checked against nixpkgs on
2026-09-11.

## Decision

- The database is canonical. Record bodies are markdown text stored in the DB; a markdown
  export, when it exists, is a read-only mirror.
- Every edit is an event node (created, edited, done, skip, snooze, shown, synced, promoted).
- Storage is Neo4j Community from nixpkgs (2026.07.0, GPL-3.0), run as a systemd user
  service `neo4j-kb.service`, bound to 127.0.0.1, auth on, password in
  `~/.config/kb/neo4j-auth` (0600).
- The kb flake ships the CLI and installs the service (`kb-setup`).

## Consequences

- One service per machine. `neo4j console` runs two JVMs: Neo4j's bootloader (`-Xmx128m`)
  and the server (heap 512m, page cache 128m by default).
- The nixpkgs `bin/neo4j` execs the store's `share/neo4j/bin/neo4j`, so Neo4j home is the
  read-only store path (`lib/` resolves from it, and the store copy carries a
  `data/dbms/auth.ini`). `kb-setup` therefore redirects every writable `server.directories.*`
  (data, logs, run, import, plugins, transaction logs root); `lib`, `certificates` and
  `licenses` stay in the store. The unit sets only `NEO4J_CONF`; the package's `neo4j.conf`
  is the baseline the rendered conf inherits from.
- Backup is `neo4j-admin database dump` with the service stopped (`kb backup`).
- Cypher is the query language, including the raw escape hatch (ADR 0004).

## Alternatives considered

| Option | Why not |
| --- | --- |
| SQLite + edge table, recursive CTEs for the link graph | Rejected in favour of a native graph for wikilink traversal. |
| Embedded Cypher engine (Kuzu) | Abandoned Oct 2025 after the Apple acquisition; the LadybugDB fork is early-stage. |
| Memgraph, FalkorDB, ArcadeDB | Not packaged in nixpkgs; everything must install through Nix. |
| JanusGraph | Packaged, but Gremlin plus a separate storage backend; heavier than needed. |
