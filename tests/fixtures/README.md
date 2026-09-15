# Test fixtures

`journal/`, `nodes/` and `state_sample.md` feed the three migrations. They started as copies of a
real working memory directory and were **scrubbed on 2026-09-15**: ticket keys became `KIO-100x`,
people became "the user" / "a colleague" / "the reviewer", hosts and URLs became `example.invalid`,
and module, company and database names became generic (`demo_*`).

**Do not re-copy live files into this directory.** Write a fixture by hand, or copy one and scrub it
before committing. The migrations are parsers: what they need is the *shape* of the input, and every
shape the real corpus had is preserved here deliberately —

- `journal/2026-04-17.md` — a `## Daily standup` block whose `[ID]` mentions must not link, a
  session with no time and no `**Intent:**`, `---` separators, a nested `###` structure.
- `journal/2026-05-01.md` — synthetic, never copied: a `## Session` heading and a `---` rule hidden
  inside a code fence, a repeated session number, a heading whose only number is a time, a
  non-session `## ` heading, `[[links]]` in code spans, a wrapped `**Intent:**` over 120 characters.
- `journal/2026-06-09.md` — a `# YYYY-MM-DD` header, three sessions, `**Files touched:**`,
  `**Nodes referenced:**` and `**Nodes created:**` lines, one of them with the `created …; updated …`
  form that the `;` split depends on.
- `journal/2026-06-15.md` — a standup with an extra `### Odoo (prod)` section, `[odoo:NNN]` suffixes,
  two sessions whose second one is timed earlier than the first.
- `nodes/` — frontmatter with and without `created`/`updated`/`modified`, one file with no
  frontmatter at all, `[[wikilinks]]` inside and outside code, a `conventions_` slug that maps to a
  howto whatever its type says.
- `state_sample.md` — every section heading, an open item, a closed `<!-- ... -->` comment that spans
  several lines, `[odoo:NNN]` external ids, a `**bold**` title prefix, a title over 100 characters.

Slugs are kept as they are: `feedback_testing`, `user_role`, `conventions_node_format` and the rest
are kb's own vocabulary, and the link tests resolve them by name.
