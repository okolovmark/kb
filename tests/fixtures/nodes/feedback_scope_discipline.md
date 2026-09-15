---
name: feedback_scope_discipline
description: "Unrelated improvements become open threads in state.md, not opportunistic fixes; a bug found while testing a ticket's branch is committed to THAT branch/PR, never a fresh branch off 16.0"
metadata:
  node_type: memory
  type: feedback
  created: 2026-04-09
  updated: 2026-08-18
  originSessionId: 66d2cc5c-2f29-4226-aeac-e0173d4e3759
  modified: 2026-08-18T05:04:39.078Z
---
When Claude spots an unrelated improvement while working on a task (dead
code, missing docstrings, style nits, obvious refactors, stale comments),
file it as an **open thread in `state.md`** with a new ID. Do NOT fix it
in the current PR — not even as a "while I'm in here" small opportunistic
edit.

**Why:** User wants PRs scoped narrowly to the task at hand and prefers
to address follow-ups in separate focused sessions. Mixing unrelated
cleanup into a bug-fix PR widens blast radius, makes review harder, and
blurs the intent of the change. User chose option (c) from the bootstrap
scope-discipline question: "add unrelated improvements as open threads
so I can run separate sessions for them".

**How to apply:** See something off-topic? Add a line to `state.md` under
`## Open threads` with a new ID pulled from `state_counter`, a short
description, and a journal anchor. Continue with the current task without
touching the unrelated issue. Mention the new open-thread ID in the
session's journal `**State changes:**` field.

**Exception:** if the "unrelated" finding is actually blocking the current
fix (e.g. a broken helper function called by the code under repair), fix
it, and note the scope expansion explicitly in the journal's `### Decisions`
section so it's auditable.

## A fix found on a feature branch belongs to that branch

When a bug surfaces while testing the currently active ticket — its worktree,
its test box, its data — commit the fix **on that ticket's branch**, into its
open PR. Do not open a fresh branch off `16.0` just because the fix is
topically unrelated. (2026-07-27: the `base.main_company`/HKD pricing fix
([[reference_product_list_price_read_as_hkd]]) went on a new branch and had to
be cherry-picked back into PR 103 — the fix belonged on the cash-flow branch.)

**Why:** the test box and the reviewer both live on the ticket's branch; a
separate branch means the box can't take the fix without dragging in unrelated
`16.0` commits, and the fix ships on a different schedule from the work that
needs it.

**How to apply:** before branching, check what branch the relevant
worktree/test box is on (`git -C <repo> branch --show-current`) and what PR it
belongs to. If the fix is a prerequisite for that work, commit there. Branch
off `16.0` only when the fix is genuinely standalone. (Scope discipline above
is about the *content* of a change; this is about not splitting it onto its
own branch.)

## Drift I created myself is not a "thread" — fix it in the same breath

An open thread is for work that is genuinely someone else's turn or a separate
session. It is NOT a place to park a one-line consequence of the change I just
made. 2026-08-18: after bumping the odoo MCP pin in `.mcp.json` and in the
copier template (tag v0.15.1), I filed the child's stale
`.copier-answers.yml` `_commit: v0.15.0` as follow-up work — the user told me to just fix the answers file instead. Fixed on the spot instead (`8fa1a5a`).

**Why:** filing it read as offloading a chore I could finish in one edit. The
thread list is a queue of real work; padding it with self-inflicted loose ends
devalues it and leaves the repo in a half-migrated state between sessions.

**How to apply:** before writing an open thread, ask *did my own change cause
this, and can I close it now?* If yes on both, just do it. Threads are for
findings that predate me, need someone else's input, or need their own session.
The banner "NEVER edit manually" on a copier answers file does not block this:
when the tag delta is provably already applied (`git diff --stat <old> <new>`
in the template), recording the answer is what `copier update` would have
written anyway — and copier refuses to run over a dirty tree in any case.

## Links

- **Related:** [[user_role]], [[conventions_behavior_protocol]],
  [[feedback_improve_via_template]], [[reference_mcpscore_audit]]
