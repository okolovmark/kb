# Install, upgrade, footprint

What `kb-setup` writes and where, how to upgrade, what the database costs on disk and in memory,
and the commands that manage the service. Back to the [README](../README.md).

## Install

```sh
nix profile install github:okolovmark/kb
kb-setup                                                      # idempotent; --dry-run [--home DIR] prints unit + conf, touches nothing
```

`kb-setup` writes the files below, sets the initial password, enables the unit, waits for bolt and
a `RETURN 1`, then creates the schema (constraints `record_id`, `record_slug`, `counter_name`,
`meta_name`, `session_id`; full-text index `record_text` on title and body; range indexes
`record_kind`, `record_scope`, `record_external_id`, `event_kind`) and the `Counter` node (value
264, first allocated id 265). A re-run rewrites what changed and restarts a running service only
when the unit or conf changed.

| Path | Content |
| --- | --- |
| `~/.config/kb/config.toml` | default config, written only if absent |
| `~/.config/kb/neo4j-auth` | `neo4j:<password>`, generated on the first run, 0600 |
| `~/.config/kb/odoo-auth` | the Odoo password, one line, 0600; written by hand when `[sources.odoo]` is enabled |
| `~/.config/systemd/user/neo4j-kb.service` | `ExecStart=<neo4j store path>/bin/neo4j console`, `Environment=NEO4J_CONF=<conf dir>`, `SuccessExitStatus=143`, `WantedBy=default.target` |
| `~/.local/share/kb/neo4j/conf/` | rendered `neo4j.conf` (package defaults + kb keys) and the package `server-logs.xml`, `user-logs.xml` |
| `~/.local/share/kb/neo4j/{data,logs,run,import,plugins}` | the writable `server.directories.*`, 0700; tx logs under `data/transactions` |
| `~/.local/share/kb/neo4j-pkg` | indirect nix GC root for the neo4j package the unit runs |
| `~/kb-backups/` | dumps; created by the first `kb backup` / `kb restore`, 0700 |

Neo4j is `pkgs.neo4j` from the flake's nixpkgs pin (`nix build .#neo4j`); its store path is baked
into the `kb` and `kb-setup` wrappers as `KB_NEO4J_PACKAGE` and written into the unit.
Neo4j Browser: `http://127.0.0.1:7474`, user `neo4j`, password from `neo4j-auth`.
A user service stops with the last login session unless `loginctl enable-linger $USER`.

## Checking on it

`kb status` prints the unit state, bolt, the schema, the counter and the versions. It exits 1 when
the unit is not active, bolt is unreachable, the schema is missing a constraint or index, or the
unit runs an older neo4j than the installed package.

`kb service start|stop|restart` drives `systemctl --user` on `neo4j-kb.service`; `start` and
`restart` return once the database answers. `kb service logs [-n N] [-f]` is
`journalctl --user -u neo4j-kb.service`, 200 lines by default.

Every command that starts the service waits for bolt and a `RETURN 1`, up to `start_timeout`, then
points at the journal.

`kb config show` prints the effective config as TOML and `kb config init` writes the default file,
never overwriting one that exists. The keys themselves are in
[configuration.md](configuration.md).

## Backup and restore

```sh
kb backup                 # stop, neo4j-admin database dump, start
kb restore FILE           # stop, dump the current database, load FILE, start
```

`backup` prints `<backup.dir>/kb-YYYY-MM-DD.dump`, suffixed `-2`, `-3`, … on the same day.
`restore` first dumps the current database to `<backup.dir>/pre-restore-YYYY-MM-DD.dump` (the path
goes to stderr), then runs `neo4j-admin database load`. Backup is manual, with no timer.

Both commands start the service again even when neo4j-admin fails; its output is captured and the
last 20 lines appear only on failure. `database load` runs with `--overwrite-destination`, which
empties the store before reading the archive, so a failed load puts the pre-restore dump back
before the error is reported; if that fails too, the error names the dump to load by hand.

## Upgrade

```sh
nix profile upgrade kb
kb-setup          # rewrites the unit for the new store path, restarts, re-runs the schema
```

Until then `kb status` exits 1 with `upgrade: unit runs <old>, package is <new>: run kb-setup`.
A release that adds a constraint or an index exits 1 the same way, through `schema: incomplete,
run kb-setup`: a database keeps the schema it was bootstrapped with until `kb-setup` runs again.

## Footprint (measured 2026-09-15, 412 records)

`neo4j console` is two JVMs: the bootloader (`-Xmx128m`, 183 MB RSS) and the server (heap 512m +
page cache 128m, 976 MB RSS); the unit's cgroup sits at 1.17 GB idle.

Neo4j preallocates 256 MiB per transaction log (`db.tx_log.preallocate`, default on), one per
database, so `data/transactions` holds 515 MB beside a 4.5 MB graph. Setting
`[neo4j] tx_log_preallocate = false` and running
`kb-setup` turns it off and gets that half gigabyte back. The point of preallocation is to avoid
fragmentation and keep write latency predictable; at this size neither effect is measurable.
Existing log files do not shrink when the setting changes, only once the service restarts and
rotates them.

`systemctl start` returns 3 to 4 s before bolt listens; a stop takes about 10 s. A stop during boot
ends the bootloader with exit 143, which the unit counts as success.
