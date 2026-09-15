# Configuration

Every key of `config.toml`, the environment variables that override it, and the optional Odoo
source. Back to the [README](../README.md).

`~/.config/kb/config.toml`, override with `KB_CONFIG`. Missing file = defaults; unknown keys and
wrong types are errors. `kb config init` writes this file, `kb config show` prints the effective
one:

```toml
[general]
timezone = "Asia/Manila"

[neo4j]
bolt_port = 7687
http_port = 7474
data_dir = "~/.local/share/kb"          # neo4j/{data,logs,run,import,plugins,conf} under it
start_timeout = 30                       # seconds the hook / kb-setup waits for bolt
heap_max = "512m"
pagecache = "128m"
tx_log_preallocate = true                # false reclaims ~516 MB of empty transaction logs

[scopes]                                 # name = absolute path; cwd inside -> that scope
# my-project = "~/projects/my-project"

[sources.odoo]
enabled = false
url = ""                                 # https://odoo.example.com
database = ""
username = ""                            # the RPC login (may be a service account)
assignee_login = ""                      # whose tasks to pull; empty = the RPC user
password_file = "~/.config/kb/odoo-auth" # one line, 0600
project_id = 0                           # 0 = every project
scope = ""                               # kb scope the synced tasks get
timeout = 10                             # seconds per RPC call

[calendar]
country = "PH"
extra_days_off = []                      # "YYYY-MM-DD"

[escalation.hard]      # calendar days before the deadline
whisper = 14
normal = 7
loud = 2
[escalation.age]       # working days since the anchor
whisper = 5
normal = 10
loud = 15
[escalation.window]
skips_to_backlog = 2

[index]
budget_lines = 200                       # kb index prints at most this many lines

[backup]
dir = "~/kb-backups"
```

`sources.odoo.scope` must be `global` or a `[scopes]` name, checked when the config loads and
`enabled = true`.

## Environment

- `KB_CONFIG` — the config file to read instead of `~/.config/kb/config.toml`.
- `KB_NEO4J_AUTH` — the auth file path; an absent auth file means connect without auth.
- `KB_TODAY=YYYY-MM-DD` — overrides today's date (what would the standup say on Monday?). Events
  written under it carry that date too, at the real time of day.
- `KB_SESSION=<id>` — tags every event the process writes with that session id, and decides which
  session record a write belongs to (see [model.md](model.md)).

When nothing listens on the bolt port every command fails in under a second with `neo4j-kb is not
reachable on bolt://127.0.0.1:<port>: kb status / kb service start`; `today`, `next`, `index` and
`sync odoo` print that line on stderr and exit 0.

## Raw Cypher

`kb query "<cypher>" [-p K=V ...]` runs raw Cypher, read or write: one JSON object per record on
stdout, update counters on stderr. A `V` that parses as JSON is passed as JSON, otherwise as a
string.

## Odoo source

```toml
[sources.odoo]
enabled = true
url = "https://odoo.example.com"
database = "prod"
username = "rpc-service"          # the RPC login, a service account will do
assignee_login = "me@example.com" # whose open tasks to pull; empty = the RPC user
project_id = 12                   # 0 = every project the user is assigned in
scope = "my-project"
```

```sh
(umask 077; printf '%s\n' 'the-password' > ~/.config/kb/odoo-auth)
kb sync odoo
```

`kb sync odoo` is silent when the source is disabled. It logs in over JSON-RPC (`common.login`;
with `assignee_login` set a `res.users` `search_read` on that login gives the uid, else the RPC
user's own; then `object.execute_kw` on `project.task` `search_read` with
`[["user_ids","in",[uid]],["is_closed","=",false]]` plus `project_id` when set).

A row matches every existing Task with `source = odoo` whose `external_id` is its `key` or its
numeric id (a migrated `[odoo:NNN]` item; several items may point at one ticket); each gets the
update and its `external_id` normalised to the key. A new row becomes a Task: title
`<key> <name>` (cut to 120 chars), body = the form URL (written once, never again), `stage`, `soft`
schedule with `due = date_deadline` when set (else `age`), `anchor = date_assign` (fallback
`create_date`, both read as UTC and turned into a kb-timezone date), `days = work`. On later runs
only `sched`, `due`, `anchor` and `stage` are refreshed, and the title only on records the sync
itself created (`origin = odoo-sync`) and nobody edited: a migrated, promoted, hand-added or edited
record keeps its title forever.

Every matched or created record gets `synced_at`; a record with `synced_at` that vanishes from the
result gets a `done` event `closed in Odoo`. Odoo-sourced records the sync has never seen
(promoted, migrated) are left alone and listed as `n untracked odoo records (not in the result
set): <up to 8 ids, … and N more>` when their number differs from the previous run (the count lives
in the Meta node). A row the mapping rejects (no dates) is skipped and named in the summary.

The outcome lands in `(:Meta {name: "odoo_sync"})`: `at`, `ok`, `error`, `count` (kept from the last
good run when the failure came before any row was fetched). Any failure prints one line on stderr
and exits 0; `kb today` then starts with `Odoo: sync failed, data as of <at>: <error>`.

A matched record that was closed with `kb done` while Odoo still lists the task is reopened by the
next sync: `done` cannot silence an Odoo task, `snooze` or `archive` can. An unchanged row moves
`synced_at` only, never `updated_at`. The password file must be mode 0600 or 0400; a FUSE or exFAT
mount (a synced Drive folder) refuses chmod, so keep it on a local filesystem.
