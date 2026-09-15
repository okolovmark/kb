# State — Current Open Items

Mutable source of truth for currently active work. Standups read this file
directly. Updated incrementally by Claude. You can edit it manually any time.

**Item format:** `- [ID] YYYY-MM-DD: description — see journal/YYYY-MM-DD`
**Next ID:** read from `state_counter` (next to this file), then write back `id+1`.
**Date:** when the item was first added (not today's date).

**Section priority** (top = most urgent):
1. Ship blockers
2. Blocked
3. Ready for review
4. WIP
5. Open threads

<!-- 2026-08-11: closed-item comments purged in the memory consolidation; the history lives in journal/ -->

## Ship blockers

<!-- Empty -->

## Blocked

- [144] 2026-08-20: vendor payment term defaults — owning ticket is now **KIO-1007** (the KIO-1008 code + backfill itself shipped, that task is Delivered); 71 open POs / 57 vendor-company pairs stay term-less — see journal/2026-08-20

## Ready for review

- [264] 2026-09-11: **kb** — phase 0 MERGED (PR 1, 2026-09-13), installed in the nix profile (kb 0.1.0, neo4j-kb.service active, counter 264). **Phases 1–3 in progress autonomously (the user away until ~2026-09-15, self-merge authorised 2026-09-13)** — see journal/2026-09-13
<!-- [202] CLOSED 2026-08-26: PR 104 merged and deployed to prod; KIO-1003/1004/1005/1006 all Delivered. The header adoption ran on prod and removed the shadowed root as ADR 0024 predicted. See journal/2026-08-26 -->

## WIP

- [208] 2026-08-26: KIO-1002 description template mechanism — **PR https://example.invalid/addons-16/pull/103 awaiting review, now on JSON storage.** 27.08 PM: pivot to fields.Properties shipped (`21f24ef6`, the user's call — 87 columns would not scale) — see journal/2026-08-27

## Open threads
- [265] 2026-09-11: kb: production neo4j.conf keeps `db.tx_log.preallocate=true` (515 MiB of empty tx logs under ~/.local/share/kb); turning it off is the user's call, only the test fixture disables it — see journal/2026-09-11
- [256] 2026-09-09: KIO-1001 the 2,906 parts whose distributor rows sit outside the convention get neither update nor new row — migration or worklist, undecided — see journal/2026-09-09 [odoo:33172]
<!-- [210] CLOSED 2026-09-01: [210] 2026-08-26: the 202 products whose Type is a value no template knows need the reviewer's call then a cleanup: 188 just have the category code in Type (DIO='DIO', CAP='CAP'), 22 say SMD (a mounting style, not a type), 3 one-offs. the reviewer ruled 2026-09-01: blank them all
and let the template fill Type from the description — see journal/2026-09-01 -->

- [251] 2026-09-09: decide whether product create should render the description from the spec (today the throwaway name sticks until `recompute_spec_state`; the KIO-1009 fix showed create CAN render, via an echo write) — see journal/2026-09-09
