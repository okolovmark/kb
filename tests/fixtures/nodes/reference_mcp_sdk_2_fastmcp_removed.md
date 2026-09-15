---
name: reference_mcp_sdk_2_fastmcp_removed
description: "uvx-launched MCP servers die with \"No module named 'mcp.server.fastmcp'\" — mcp SDK 2.0.0 dropped the module; pin --with 'mcp<2'"
metadata: 
  node_type: memory
  type: reference
  created: 2026-07-31
  tags: 
    - mcp
    - uvx
    - tooling
  originSessionId: ee774e3d-07b3-41f8-944d-fce21c34529f
  modified: 2026-07-31T03:47:26.949Z
---

# mcp SDK 2.0.0 removed `mcp.server.fastmcp`

`/mcp` showing `Failed to reconnect to <server>: -32000` for a **uvx-launched**
Python MCP server usually means the server crashed on import, not a transport or
config problem. Run its command by hand to see the real traceback:

```bash
timeout 60 uvx --from 'mcp-pdb==0.4.0' mcp-pdb </dev/null 2>&1 | tail
# ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

The `mcp` SDK dropped `mcp.server.fastmcp` in **2.0.0** (`mcp/server/` now ships
`mcpserver/` instead). Servers that do `from mcp.server.fastmcp import FastMCP`
and declare `mcp` without an upper bound get 2.0.0 from a fresh uvx resolve and
die. Hit 2026-07-31 on `postgres-mcp` and `mcp-pdb==0.4.0` simultaneously — a
version-resolution change, so it looks like both servers broke "by themselves".

Fix — pin in the launch command, no upstream patch needed:

```jsonc
"uvx --with 'mcp<2' postgres-mcp --access-mode=unrestricted $DATABASE_URI"
["--from", "mcp-pdb==0.4.0", "--with", "mcp<2", "mcp-pdb"]
```

Servers depending on the standalone **`fastmcp`** package (2.x) need no pin —
fastmcp caps `mcp` itself (resolves 1.26–1.29). That is why `odoo-fast-mcp`
kept working while the other two failed.

Shipped through the template per [[feedback_improve_via_template]]:
`nixodoo-copier-template` v0.8.4, then `copier update` here (commit `909816c`).
`.mcp.json` is template-generated — never hand-edit it, see
[[reference_nixodoo_copier_template]]. Copier refuses a dirty destination and
counts **untracked** files: move them aside for the run rather than committing
them (`odoo12.env` did this).

Related: [[reference_npx_mcp_install_gotchas]] (same failure signature from a
truncated npx extraction), [[reference_odoo_mcp_gotchas]].

**Status 2026-09-03:** `mcp-pdb` removed from the template and `.mcp.json` (zero use in 62 sessions); the `mcp<2` pin now only concerns `postgres-mcp`.
