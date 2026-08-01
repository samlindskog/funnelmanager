---
name: refresh-grafana-token
description: Re-mint the Grafana MCP service-account token after a grafana pod roll wipes it (ephemeral storage), and rewrite ~/.claude.json. Use when mcp__grafana__* calls start returning 401 Unauthorized.
---

# refresh-grafana-token

Grafana runs on **ephemeral storage** (`emptyDir`, no PVC), so every time the
grafana pod rolls, its SQLite DB — **including all service-account tokens** — is
wiped. The token baked into the Grafana MCP config (`~/.claude.json`,
`GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_…`) then starts returning **401**, and every
`mcp__grafana__*` tool call fails. This skill mints a fresh SA token from the
in-cluster `grafana-admin` creds and rewrites the MCP config in place.

## When to use

- `mcp__grafana__*` tools return `401 Unauthorized` / `get datasource by uid … Unauthorized`.
- Right after you (or Flux) rolled the grafana pod and observability queries broke.
- Any time you want to rotate the Grafana MCP token (`--force`).

## Use it

```bash
.claude/skills/refresh-grafana-token/refresh.sh          # mint only if the current token is dead
.claude/skills/refresh-grafana-token/refresh.sh --force  # rotate even if it still works
```

What it does:
1. Reads the current `glsa_…` from `~/.claude.json` and **liveness-checks** it
   (`GET /api/datasources`). If it's still `200` and you didn't pass `--force`,
   it's a **no-op**.
2. Otherwise mints a fresh token: ssh to the control plane, read the
   `grafana-admin` secret (`monitoring` ns), create/reuse the `claude-mcp`
   service account (Admin role — the MCP runs `--disable-write`, so it stays
   read-only in practice), and mint a token via the Grafana API. Admin creds +
   the token stay in-cluster / local; **nothing touches git**.
3. **Verifies the new token** against `/api/datasources` *before* writing it (never
   persists a broken token), then string-replaces it into `~/.claude.json`
   (backup at `~/.claude.json.bak-grafana-token`, JSON re-validated).

## ⚠️ You must restart afterward

The change only takes effect on **MCP startup** — the running grafana MCP holds
the old token in its docker process env. **Restart the Claude session** (or the
MCP) after the skill reports success, then re-run your `mcp__grafana__*` query.

## Prerequisites

- The gitignored ops env (`FM_CP_HOST` etc. — see `_lib/ops-env.example`) and ssh
  to the control plane; the `grafana-admin` secret in `monitoring`; the public
  Grafana URL reachable. Override `FM_GRAFANA_URL` / `FM_GRAFANA_SA` /
  `CLAUDE_CONFIG` if they differ.

## Durable fix (so this stops recurring)

This is a band-aid for ephemeral Grafana storage. The real fix is to give Grafana
a **PersistentVolumeClaim** (persist `/var/lib/grafana`) so its DB — SA tokens and
any non-provisioned dashboards — survives a pod roll. Until then, run this skill
after each roll.

## See also

- **observe-grafana** — the LogQL/TraceQL/PromQL query cookbook that needs a live token.
- **drive-canary** — the browser half of the canary loop that hands trace ids to observe-grafana.
