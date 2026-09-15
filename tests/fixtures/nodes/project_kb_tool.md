---
name: project_kb_tool
description: "kb — the user's personal knowledge graph + todo on Neo4j Community (the kb repo, Python CLI, everything installed via Nix incl. the DB) replacing state.md / nodes / standup.sh; phase 0 DONE 2026-09-11 (PR 1 awaiting review), phase 1 next"
metadata:
  node_type: memory
  type: project
  created: 2026-09-11
  updated: 2026-09-11
---
# kb: one graph for tasks, notes and sessions

**Status 2026-09-11:** grilled (10 rounds, one question at a time), Alignment
Summary drafted, awaiting the user's build confirmation. State item [264].

## Decisions (owner: the user, 2026-09-11)
- **DB canonical.** Bodies are markdown text; every edit is an event; md export
  is a read-only mirror, never a source.
- **Storage = Neo4j Community from nixpkgs (2026.07.0, GPL-3.0), a systemd
  user service.** the user chose a server graph DB over SQLite+edge table for the
  wikilink graph (2026-09-11); under the constraint below only Neo4j and
  JanusGraph are packaged (Memgraph/FalkorDB/ArcadeDB are not). Kuzu, the one
  embedded Cypher engine, was abandoned Oct 2025 (Apple), fork LadybugDB is
  early-stage.
- **Everything installs through Nix, the DB included** (the user 2026-09-11):
  the kb flake ships the CLI package and the neo4j-kb service.
- **One DB, scoped.** scope = global | personal | <project>. A project session
  shows global + its project; personal is hidden there because entire pushes
  session transcripts to org-visible refs ([[reference_entire_session_recording]]).
- **KIO tasks = soft deadline + age.** Odoo date_deadline escalates to the date,
  then asks once for rebalance/done, never screams overdue. Age in PH working
  days from date_assign.
- **Odoo: pull automatically, push only via `kb promote <id>`.** The
  "mirror every state item to project.task" protocol retires.
- **Surface = session standup + CLI only.** Telegram/push declined (accepted
  risk: a Sunday tax deadline is seen only if a terminal opens).
- **Calendar = Mon–Fri minus Philippine holidays** ([[user_role]]).
- **Migrate everything, three flag days:** state items (IDs kept, closed →
  done events) → 84 nodes (slugs kept, wikilinks → link table) → journal as
  session records.
- **Separate private repo the kb repo, Python click CLI with --json; MCP
  later, HTTP API not before a second client.** Name: `kb`.

## Model
record(kind: task|note|howto|feedback|reference|decision|session, scope, slug,
title, body, pinned, source, external_id) · schedule(hard|soft|window|age) ·
recurrence(calendar rrule | interval from done) · event(created|edited|done|
skip|snooze|shown|synced|promoted) · relationships (vocabulary being decided) ·
Neo4j full-text index. **urgency(record, today)
is a function, not a column**; `kb today` = urgency > 0, top 7, rest counted.

## What it replaces here
state.md + state_counter + next_id.sh + the standup render; MEMORY.md becomes
`kb index --scope project-16` output (pinned feedback first, 200-line
budget); standup.sh shrinks to `kb today --json` + hygiene checks (entire
fan-out, stray files in nodes/). Skills stay skills (2026-08-26 decision):
a howto record is an env-specific recipe, not framework knowledge.

## Decisions, round 2 (2026-09-11, all the user's, none assumed)
- **Relationship vocabulary v1, closed set:** RELATED, SUPERSEDES, BLOCKS,
  OPENED_IN (record→session), TOUCHED (session→record), PART_OF. Old
  `[[wikilinks]]` migrate as RELATED. Adding a type = a code change.
- **Addressing:** every record has a monotonic integer id from a Counter node
  incremented inside the write transaction; ids 1..264 kept, counter starts
  at 265. Non-task kinds also carry a unique slug (old slugs kept); sessions
  slug = date + n. Every command accepts id or slug.
- **Install:** `nix profile install git+https://example.invalid/kb` → `kb` + `kb-setup`;
  kb-setup writes ~/.config/systemd/user/neo4j-kb.service (neo4j from the nix
  store), config + data under ~/.local/share/kb/, enables the unit, creates
  indexes and the Counter. Same pattern as create-systemd-service here.
- **Escalation:** levels 0..4 (silent, whisper, normal, loud, scream).
  Defaults in ~/.config/kb/config.toml, per-record `escalation` override.
  hard: whisper T-14 cal days, normal T-7, loud T-2, scream T-0 and every day
  after. soft: same until the date, after it normal + "decide: rebalance or
  done". age (work): whisper 5 working days, normal 10, loud 15, no scream.
  window: normal inside, silent outside, backlog after 2 skipped windows.
- **Age anchor for Odoo tasks = date_assign** (fallback create_date); stage
  moves never reset it.
- **Dates:** schedules are Neo4j Date (no time); "today" = local date in the
  configured tz Asia/Manila, not the machine tz; events carry UTC timestamps.
- **Scope resolution:** `[scopes]` path table in config; cwd inside a path →
  that project; outside all: `today` shows everything, `add` refuses without
  --scope. The flag always wins over cwd.
- **Context loading = SessionStart hook prints the index FROM THE DB**
  (matchers startup, resume, clear, compact). MEMORY.md becomes a 2–3 line
  stub ("memory lives in kb, call it like this"). If Neo4j is down the hook
  tells the user, runs `systemctl --user start neo4j-kb`, waits for bolt, reads
  again; if still down, the session starts with an explicit "no memory" warning.
- **Standup: FULL list on every session start and every compaction**, not once
  a day. `shown` events are still written (window/skip semantics), they are
  not a daily gate.
- **`today` prints every record at level ≥ normal (2..4), one line each (id,
  level, title, reason), sorted by level; whisper as one counter line "+N
  quiet, kb next". No numeric cap.**
- **Backup is MANUAL, native, no extra tools (the user: no rclone):** `kb backup`
  stops neo4j-kb.service, runs `neo4j-admin database dump` into
  ~/kb-backups/kb-YYYY-MM-DD.dump, restarts the service; the user uploads to
  Google Drive himself. `kb restore <file>` = stop, `database load`, start.
  No timer, no automatic backup (today there is none either, his call).
- **Odoo is an OPTIONAL source:** `[sources.odoo] enabled = true|false` in
  config.toml; when disabled kb is fully local and prints nothing about Odoo.
  When enabled the SessionStart hook runs `kb sync odoo` (10 s timeout) before
  `today`: upsert of my open KIO by (source, external_id), tasks closed in Odoo
  get a done event tagged source. On failure `today` uses the last snapshot and
  prints "Odoo: sync failed, data as of <ts>" first. Odoo never breaks the standup.
- **Input syntax = explicit flags, no natural-language parsing** (Claude does
  that in the session): `--due DATE` hard, `--plan DATE` soft, `--window
  weekend|sat,sun|<rrule>`, none = age. `--repeat monthly:15` calendar,
  `--repeat 30d` interval from done. `--days work|off|any`, default work for a
  project scope and off for personal, flag wins.
- **Recurring = ONE record forever; the schedule advances on done (calendar:
  next rule date; interval: done + N days); every done, with its comment, is an
  event.** `kb show <id>` prints the current date and the event feed.
- **snooze/skip:** `snooze <id> [until]` = silent until the date (default
  tomorrow); on hard it can never jump past the deadline, deadline day still
  screams; on age it hides while age keeps ticking. `skip <id>` = window only:
  closes the current window, skips +1, backlog on the 2nd; a window that ends
  untouched counts as skipped automatically.
- **"Tell me once" = task with `--once`:** level normal until first shown in a
  session the user is in (SessionStart hook, not a script call), then auto-done
  with a "shown" reason; history stays in the DB and the session links.
- **Nothing is deleted from the CLI:** `kb archive <id> --reason` (hidden from
  index and today, found with --all), `kb supersede <old> <new>` (SUPERSEDES
  edge, old archived, references resolve to the new one), `kb merge <dup>
  <keep>` (events and edges move, dup archived with a note). No delete command;
  raw Cypher by hand is the only way.
- **Raw Cypher escape hatch: `kb query "<cypher>"` for READ AND WRITE** (the user's
  call over my read-only recommendation). Operating rule for me: commands first,
  raw writes only when no command fits, and a raw write must still allocate the
  id from the Counter and write an event, or it is a bug I introduced.
- Body authoring (no fork, all included): `add`/`edit` take `--body-file` or
  stdin; `edit` with no source opens $EDITOR; `append <id> "text"` adds a dated
  paragraph.
- **Network/auth:** Neo4j binds 127.0.0.1 only; ports in config.toml (default
  bolt 7687 / http 7474, change if taken); auth ON, kb-setup generates the
  password into ~/.config/kb/neo4j-auth (0600), kb reads it from there; Neo4j
  Browser reachable locally with that password.
- **Build = uv2nix on Python 3.14 (the user):** uv.lock is the source of truth, the flake reads it (same
  shape as project-16). Deps: neo4j driver 6.3.0, click, python-dateutil,
  holidays (tomllib is stdlib on 3.14, no tomli). Dev via uv, install via `nix profile install`.
  Reuse the nixodoo template's 3.14 uv2nix overrides ([[reference_nixodoo_copier_template]]);
  verify neo4j-driver builds on 3.14 in phase 0.
- **Sessions (phase 3): hooks build the skeleton, I write the body.**
  SessionStart → `kb session open --scope X --id <claude session id>`; every
  done/add in the session gets TOUCHED/OPENED_IN via env KB_SESSION; the body
  (done, decisions, files) is written by me on the close signal as today's
  journal is; Stop hook sets closed_at. A session killed without a body is
  marked "no summary" and surfaces as one line at the next start.
- **`kb promote <id> KIO-xxxx` only LINKS:** the ticket itself is created by
  the odoo-tickets skill over MCP (protocol stays in one place); kb never
  writes to Odoo. After promote the record carries source=odoo + external_id
  and lives on as a work task.
- **Hook lives in the nixodoo template** (copier option `kb = true`):
  .claude/hooks/kb-session-start.sh = sync odoo → index → today → session open,
  then the project's own checks (entire fan-out, stray md in nodes/). kb knows
  nothing about entire or nodes/.
- **CLI language: English.**
- **Broken wikilinks at migration (70 of 152 targets):** targets whose content
  moved to a skill or was merged into another node are NOT migrated (no edge,
  not reported); every other unresolved target keeps its `[[slug]]` text, no
  edge, and lands in the post-migration report.
- **Hook waits 30 s** (config value) for bolt after `systemctl --user start
  neo4j-kb`, then starts the session with the "no memory" warning.
- **BUILD CONFIRMED by the user 2026-09-11; phase 0 DONE the same day: PR https://example.invalid/kb/pull/1 awaiting review (commit 3ea9a13, 60 tests). neo4j-kb.service is running on the workstation from the checkout's result link; `nix profile install` + `kb-setup` after merge, then phase 1.**

## Phase 0 build facts (measured 2026-09-11 on the workstation)
- Real `kb-setup`: 7 s to an active service, bolt up, schema + Counter 264; unit
  `neo4j-kb.service` enabled (default.target), ExecStart = the nixpkgs neo4j
  2026.07.0 store path, `Environment=NEO4J_CONF=~/.local/share/kb/neo4j/conf`;
  GC root symlink `~/.local/share/kb/neo4j-pkg`.
- **`neo4j console` = TWO JVMs**: Neo4j's bootloader (-Xmx128m, ~194 MB RSS) +
  the server (heap 512m + pagecache 128m, ~936 MB RSS). **Total ≈ 1.1 GB RSS
  idle**, not the 0.5 GB I quoted in the grill. Lowering heap is the user's call.
- SIGTERM during boot → exit 143 → systemd "failed" unless
  `SuccessExitStatus=143` (added in gate round 3).
- `systemctl start` returns before bolt listens (~4–8 s); every kb command that
  starts the service must wait for bolt + `RETURN 1` (gate round 3).
- 20 parallel `kb query` counter increments → 265..284, no duplicates (Neo4j
  node lock). Counter reset to 264 after validation; ids 1..264 stay reserved.
- Tests: 60 (after round 3), one throwaway Neo4j per run from
  $KB_NEO4J_PACKAGE, `db.tx_log.preallocate=false` keeps /tmp under 1 MB.
- Gate history: round 1 = 1 CRITICAL (restore wiped the DB on a bad file) +
  4 MAJOR, all CONFIRMED by verifiers; round 2 = 2 MAJOR (REPO_ROOT cwd,
  neo4j.dump clobber); round 3 = wait-for-ready, exit 143, neo4j-admin noise.

## 2026-09-13: PR 1 merged, autonomy granted
- the user merged PR 1, then: "do 266 and 267, explain 265 when you are done, I am back in a couple
  of days, I expect you to finish all of it, and you may merge your own pull
  requests". **Standing authorisation: I may merge my own PRs in
  the kb repo** (this repo only; the work repos unchanged). Phases 1–3 run
  without him; every design call the grill did not settle is written as a
  DECISION in the PR body and here, never silently.
- [266] DONE: `loginctl enable-linger` → Linger=yes. [267] DONE: `nix profile
  install git+ssh://git@example.invalid/kb` (kb 0.1.0 at f394fd9),
  `kb-setup` from the profile, status green, unit + GC root unchanged.
- [265] still open: explain `db.tx_log.preallocate` (515 MiB of empty tx logs)
  in the final report, the user decides.
- Phase 1 dev spec sent 2026-09-13 (agent kb-dev-phase1, branch phase-1-tasks):
  graph model with schedule as Task properties, Event nodes, closed edge
  vocabulary, calendar PH, urgency table, commands add/append/edit/show/search/
  links/graph/log/done/skip/snooze/today/next/why/archive/supersede/merge/link/
  promote/sync odoo/index/migrate state, ADR 0005. Orchestrator decisions in
  that spec: day-fit lowers the level by 1 (never a scream), window skips are
  bookkept lazily at evaluation time, Odoo creds in `~/.config/kb/odoo-auth`.
- Phase 1 QC round 1 (2026-09-13): review found 1 CRITICAL (sync rewrote
  locally edited title/body of odoo tasks every session), 7 MAJOR (migrated
  [odoo:NNN] items closed as "closed in Odoo" on first sync; double done
  advances twice; search leaked personal in a project cwd; over-long Odoo name
  aborted sync silently; 30 s driver retry stalled every command with the DB
  down; snoozed window tasks reached backlog; untestable Counter-raise), 16
  minors. Orchestrator design calls made in the user's absence: **`once` is exempt
  from day-fit and respects snooze**; **`next_occurrence` = first occurrence
  strictly after max(today, due)**; **migration sets the Counter to 267 via
  `--counter`** (state_counter is 268; ids 265..267 used by state/journal);
  **sync never rewrites body, rewrites title only until the first edit, and
  closes only records it has synced before**; hook passes `--scope` explicitly
  so worktree cwds never show personal.
- 2026-09-13 live validation of phase 1 (branch build, before merge): live config
  got `[scopes]` (odoo-16, odoo-19) and `[sources.odoo]` (prod url/db, service
  account user, project 8, scope project-16), `~/.config/kb/odoo-auth`
  0600; `kb backup` → kb-backups/kb-2026-09-13.dump; **`kb migrate state` ran
  on the LIVE state.md: 47 tasks (36 open, 11 closed), Counter 267**; probes
  (personal hidden in project today, T-0 scream, snooze refused, weekend
  window, interval repeat advanced to +30 d, once auto-done on --record-shown,
  link/graph/search) all OK and deleted afterwards (ids 268–272 burned,
  Counter 272). Two live-only findings → round 4: the sync filtered on the
  SERVICE ACCOUNT's uid (0 tasks) → `[sources.odoo] assignee_login`; the
  neo4j driver printed GQL notifications to stderr on every command.
  Note for the user: [68] and [228] migrated into scope
  project-16 (as they were in state.md); rescope to personal if wanted.
- **PHASE 1 DONE 2026-09-13:** PR 2 merged (6e89f22), profile upgraded, live DB
  holds 47 migrated tasks + 27 synced KIO; template v0.26.0 hook; project-16
  PR 57 (hook + local checks) merged; MEMORY.md is the kb stub, state.md retired.
  Live checkout still on the user's branch → hook inactive there; MEMORY.md fallback
  command covers it. Phase 2 (nodes) dev running; phase 3 (journal) next.
