# kb

Your notes, your todo and your session history in one graph, on a Neo4j you never have to think about.

```console
$ kb today
kb today 2026-09-15 (acme-erp)
[283] L4 scream  file the quarterly return           · T-0
[144] L3 loud    chase the supplier price list       · 16 working days since 2026-08-20
[207] L2 normal  write up the migration decision     · T-5
+17 quiet (whisper), kb next
```

Nothing above was typed into a list that morning. The deadline counts down on its own, the untouched
thread gets louder as it ages, and the quiet ones stay quiet until they are worth the attention.

## Why it exists

A flat list of todos lies to you. Everything on it looks equally urgent, so you re-read all of it
every morning and re-decide what matters. kb keeps the decision inside the task instead. A tax
payment screams on its due date and never before. A weekend chore appears on Saturday and goes quiet
on Monday. A ticket grows louder the longer it sits on you. None of them shout on a day off.

The same store holds what you know, not only what you owe. A note, a rule you want followed, a
reference and the session you learned it in are all records, linked to each other, searchable in one
place, and small enough to hand to an assistant at the start of every session.

## Highlights

- **Urgency is computed, never maintained.** Five levels from silence to a scream, derived from the
  schedule, the events and a working-day calendar. `kb why <id>` prints the arithmetic.
- **Five kinds of schedule**, because "due" is not one thing: a hard deadline, a soft plan, a
  recurring window, an ageing thread, and a one-off notice that waits, off the standup, until you
  dismiss it.
- **Scopes keep work and life apart.** A personal record never appears in a project session, and a
  command reaching across scopes refuses by name instead of leaking the title.
- **Sessions are records too.** What you did, what it touched and what it opened form one subgraph.
- **Context by area.** Records carry area tags from a vocabulary the project keeps in its own repo;
  `kb context <task>` lists what is known about those areas, and the rules for the kind of work
  the task does, before the work starts, and `kb add` refuses a record that duplicates an open
  one.
- **Raw Cypher whenever you want it.** `kb query` reads and writes; the commands are a convenience,
  not a cage.
- **Installed through Nix, database included.** One `nix profile install`, one `kb-setup`, no
  container, no cloud, nothing listening outside loopback.

## Install

```sh
nix profile install github:okolovmark/kb
kb-setup
kb status
```

`kb-setup` is idempotent and safe to re-run. It renders a Neo4j config, generates a password, writes
and starts the `neo4j-kb` user service, and creates the schema. Everything it touches lives under
`~/.config/kb`, `~/.local/share/kb` and one systemd unit, plus the kb plugin on
`CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json` when Claude Code is installed and
`~/.gemini/config/plugins/kb` when Antigravity is installed (a standup pane and a session journal;
`--no-claude-plugin` / `--no-antigravity-plugin` skip them). `kb status` exits non-zero whenever
something needs `kb-setup` again, an upgrade included. The exact file list, the upgrade step and the
disk footprint are in [docs/install.md](docs/install.md).

## Everyday use

```sh
kb add "pay the quarterly tax" --due 2026-10-15               # deadline: screams from the day itself
kb add "write the channel post" --window weekend              # only on Saturday and Sunday
kb add "clean the filter" --plan 2026-09-20 --repeat interval:30d   # 30 days after each time you finish
kb add "review the distributor PR"                            # no schedule: ages in working days

kb today                     # the standup: everything at normal or above
kb next                      # the same, down to a whisper
kb notices show              # the one-off notices (--once), kept off the standup
kb notices dismiss 301       # close one once read (the plugin pane has a dismiss button)
kb why 144                   # why this one is loud today
kb done 283 "shipped in the September release"
kb snooze 144 2026-09-19
```

Outside the personal scope every record names at least one area (`--tag`): one of the project's,
from its `.kb/tags.toml`, or a built-in one. `kb tags` lists them. A record too close to an open
one is refused with the match (exit 5); `kb append` adds to that one, `--distinct` creates it
anyway.

Facts are the same command with a kind:

```sh
kb add "Payment terms are shared across companies" --kind reference --tag billing \
  --summary "One term record serves every company, since the 2026 cleanup." --body-file note.md
kb search "payment terms"
kb show reference_payment_terms       # by slug or by id
kb links reference_payment_terms      # what it relates to, blocks or supersedes
```

A rule an assistant must follow every session is a pinned record: feedback is pinned when it is
created, `kb pin` pins anything else. Pins have two levels. The few rules that must never be
missed are critical (`kb add ... --critical`, or `kb pin <id> --critical`): `kb index` prints them
first, marked `CRITICAL`, so a session start short on room drops the normal ones before them.
`kb pin <id> --normal` demotes one, `kb unpin <id>` takes it off the list.

Every command explains itself with `--help`, and `kb --help` lists them all. Worth knowing beyond the
above: `append` and `edit` grow a record, `archive`, `supersede` and `merge` retire one without
deleting anything, `skip` closes a missed window, `promote` ties a thread to a ticket, `index` prints
the summary an assistant reads at session start, `context` lists what is known about a task's
areas, `consolidate` proposes merges, condensing and archiving of knowledge records (and writes
nothing), `tags` shows the vocabulary, `session` manages the session record, `backup` and
`restore` handle the dumps, and `query` takes Cypher.

## How it is organised

A **record** is a node with an id, a title, a markdown body and a scope. Its kind is one of `task`,
`note`, `howto`, `feedback`, `reference`, `decision` or `session`. Ids are never reused, and
everything except a task also carries a slug you can type instead of the number.

A **scope** is `global`, `personal`, or the name of a project directory you configured. What you see
depends on where you stand: inside a project you see that project plus `global`, and `--scope
personal` is how you look at the rest.

**Events** are nodes as well, so a record carries its own history: every edit, completion, skip,
snooze and sync, each one stamped with the session that wrote it.

**Relationships** come from a closed vocabulary: `RELATED`, `SUPERSEDES`, `BLOCKS` and `PART_OF`,
plus `OPENED_IN` and `TOUCHED`, which a session uses to say what it created and what it changed.

**Tags** are closed too. An area tag names a module or subsystem and pulls context; a type tag
(`bug`, `migration`, `data_fix`, …) says what kind of work it is and pulls the knowledge records
of the same type, whatever their area. A project's areas live in
`.kb/tags.toml` in its repository, so a team shares one list and changes it by pull request.

The urgency table, the working-day calendar and the session rules are in
[docs/model.md](docs/model.md).

## Configuration

`~/.config/kb/config.toml`, written by `kb config init`, printed by `kb config show`. Unknown keys
and wrong types are errors, so a typo is loud. The keys most people touch:

| Key | Default | Decides |
| --- | --- | --- |
| `general.timezone` | `Asia/Manila` | which day "today" means |
| `scopes` | empty | project name to directory, the map behind the scope view; `kb scope add NAME` registers the current directory |
| `calendar.country` | `PH` | whose public holidays are not working days |
| `escalation.*` | 14/7/2 calendar days, 5/10/15 working days | when each schedule starts speaking up |
| `sources.odoo.enabled` | `false` | pull your open tickets in as tasks |
| `neo4j.tx_log_preallocate` | `false` | `true` restores Neo4j's 256 MiB of empty space per transaction log |

Every key, the Odoo source and the per-record overrides are in
[docs/configuration.md](docs/configuration.md).

## Backups

Manual and native, no timer and no third-party tool:

```sh
kb backup                                     # stop, dump to ~/kb-backups/kb-<date>.dump, start
kb restore ~/kb-backups/kb-2026-09-15.dump
```

`restore` dumps the current database before it loads anything, so a truncated archive cannot cost you
the store.

## Coming from markdown notes

`kb migrate state|nodes|journal` imports the three shapes of a file-based memory and keeps ids,
slugs, dates and cross-references. Each has a `--dry-run` and writes a report of what it could not
resolve. Recipes in [docs/migration.md](docs/migration.md).

## More

- [docs/model.md](docs/model.md) — records, tags, duplicates, schedules, urgency, sessions
- [docs/configuration.md](docs/configuration.md) — every config key, `.kb/tags.toml`, the Odoo source
- [docs/install.md](docs/install.md) — what is written where, upgrading, footprint
- [docs/migration.md](docs/migration.md) — importing a markdown memory
- [docs/adr/](docs/adr/) — why the hard-to-reverse choices were made
- [DEVELOPMENT.md](DEVELOPMENT.md) — building, testing and changing kb

A personal project: no support promised, no contributions expected.
