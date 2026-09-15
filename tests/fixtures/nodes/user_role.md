---
name: user_role
description: User's role, environment, and communication preferences
metadata:
  node_type: memory
  type: user
  created: 2026-04-09
  updated: 2026-09-11
  originSessionId: 66d2cc5c-2f29-4226-aeac-e0173d4e3759
  modified: 2026-08-11T05:40:33.202Z
---
# User — role and preferences

## Role

Tech lead at the company with 8 years of Odoo experience. Soon transitioning
to a developer role at another company — see [[project_ai_solution_architect_track]]
for implications on how to scope suggestions.

## Environment

See `CLAUDE.md` in the project root for the technical stack (Nix flake,
PostgreSQL on `16432`, Odoo HTTP on `1669`, systemd user services).
Nothing user-specific to add beyond that during bootstrap.

**Lives in the Philippines** (stated 2026-09-11): timezone Asia/Manila, and the
days off at work follow the **Philippine** public-holiday calendar, not Hong
Kong's. Working day = Mon–Fri minus PH holidays. Any business-day or
"is today a workday" computation uses this calendar.

## Communication preferences

- **Respond to the user in Russian** in the CLI conversation (stated
  2026-06-18, re-confirmed 2026-07-21). Technical terms / identifiers / code
  stay as-is. Outward content keeps its own rules: Teams always English
  ([[feedback_teams_conduct]]); anything published as the user (LinkedIn/CV)
  follows [[feedback_no_ai_style_tells]]; commit messages, code comments and
  PR text stay English per repo convention.
- Terse and direct; dry facts, no bloat — see [[feedback_terse_docs]].

## Decision-making style

_Not captured during bootstrap — fill as patterns emerge from real work._

## Workflow expectations

- **Bug fixes:** read code first → if not obvious, reproduce with a
  targeted test or `odoo shell` script early (mcp-pdb retired 2026-09-03)
  → patch → test → commit. See [[feedback_workflow]].
- **Tests:** every fix gets a test, at the right level (unit / HttpCase
  / Tour). See [[feedback_testing]].
- **Scope:** unrelated improvements spotted mid-task become open threads
  in `state.md` with a new ID, never opportunistic in-PR fixes. See
  [[feedback_scope_discipline]].

## Links

- **Related:** [[conventions_node_format]], [[conventions_behavior_protocol]], [[feedback_workflow]], [[feedback_testing]], [[feedback_scope_discipline]], [[project_ai_solution_architect_track]]
