# Development

Building, testing and changing kb, and where the code lives. Back to the [README](README.md).

```sh
nix develop                           # Python 3.14 venv: kb editable from src/, pytest, ruff, uv; neo4j on PATH; KB_NEO4J_PACKAGE set
pytest                                # 238 tests; one throwaway Neo4j per run from KB_NEO4J_PACKAGE (auth off, tx-log preallocation off), ~45 s
ruff check . && ruff format --check .
uv lock                               # after editing dependencies; uv.lock is what the flake builds (UV_NO_SYNC=1 in the shell)
```

`nix develop` outside the checkout needs `REPO_ROOT=/path/to/kb` exported (the editable install
resolves `$REPO_ROOT/src`); `pytest` finds `src/` through `pythonpath` in pyproject.toml from any cwd.
`nix build` reads the git tree, not the working directory: a new file is invisible to it until
`git add`, and the build succeeds without it (the wrapper then fails on import).

The command tests share one Neo4j: the `kb_env` fixture deletes every Record, Event and Meta node,
resets the Counter to 264 and points the CLI at a config with scope `proj` and `KB_TODAY=2026-09-14`
before each test. The fixtures under `tests/fixtures/` are scrubbed copies of a real memory
directory; [tests/fixtures/README.md](tests/fixtures/README.md) says which shape each one exists for
and why live files must not be copied back in.

## Layout

```
flake.nix          uv2nix build: packages.{default,kb,neo4j}, apps.{default,kb-setup}, devShells.default
pyproject.toml     project, dev group, ruff and pytest config; uv.lock is the source of truth
src/kb/            cli.py (click group, every command), setup.py (kb-setup), config.py, paths.py,
                   db.py (driver, readiness, schema, counter), service.py (systemctl, neo4j-admin),
                   runner.py (subprocess seam), records.py (records, events, links, task bookkeeping),
                   urgency.py (levels, pure), calendar.py (working days, today), sync_odoo.py,
                   migrate.py (state.md), migrate_nodes.py (memory nodes),
                   migrate_journal.py (journal days)
tests/             pytest; conftest starts the throwaway Neo4j; fixtures/state_sample.md,
                   fixtures/nodes/ (five memory nodes and one file without frontmatter),
                   fixtures/journal/ (three day files and one of edge cases)
docs/              install.md, configuration.md, model.md, migration.md, adr/ (decision records)
```
