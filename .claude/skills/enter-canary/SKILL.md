---
name: enter-canary
description: Put YOUR OWN browser on the funnelmanager telemetry canary by setting the host-only fm_canary cookie, via a bookmarkable localhost launcher that redirects to the gateway /canary/on endpoint (secret stays out of your bookmark; cookie is set server-side HttpOnly). Use when you want to hand-drive the canary UI as a human (then sign in as e2e-canary) — pairs with watch-canary to have Claude observe your session. NOT for the headless agent loop (that's drive-canary).
---

# Enter the canary (human browser)

Gets **your** browser onto the telemetry-enabled canary at https://x9bc433.win so
you can click around as a real user. The canary shows a **thin red outline +
"CANARY · telemetry on"** badge, and every action/error/trace you generate lands
in Loki/Tempo where `watch-canary` (Claude) can read it.

Two independent things are involved — keep them straight:

1. **The `fm_canary` cookie** (routing) — this skill sets it. It's a *host-only*
   cookie on `x9bc433.win`, so it can only be set on that origin; this launcher
   redirects you there. The gateway sets it **HttpOnly** (page JS can't read it).
2. **Your login** (identity) — separate and normal: after the cookie is set you
   land on the canary and sign in through Keycloak as **`e2e-canary`** (creds in
   `~/.config/fm-e2e/creds.env`). Nothing special — the canary uses the same auth
   as prod; the cookie only decides *which pods* serve you.

## How it works

```
bookmark  http://localhost:8799/            (clean; no secret)
  └─302→  https://x9bc433.win/canary/on?t=<secret>     (secret injected by the launcher, from creds.env)
            └─ canary-cookie-gate EnvoyFilter validates t → Set-Cookie fm_canary (HttpOnly) → 302 /
                 └─ you're on the canary; sign in as e2e-canary
```

The secret (`FM_CANARY_TOKEN`) lives only in `~/.config/fm-e2e/creds.env` and the
gateway EnvoyFilter — never in your bookmark. It appears once, transiently, in the
`/canary/on?t=` redirect (browser history); rotate it (see PLACEHOLDERS.md
"Canary access") if that matters.

## Use it

```bash
# Start the launcher (foreground; Ctrl-C to stop). Runs on 127.0.0.1:8799.
python3 .claude/skills/enter-canary/launcher.py
# override the port:  FM_CANARY_LAUNCHER_PORT=9000 python3 .claude/skills/enter-canary/launcher.py
```

Then in your browser:
- **Bookmark `http://localhost:8799/`** → click it anytime to enter the canary.
- `http://localhost:8799/off` → clear the cookie (back to stable prod).

To confirm you're on the canary: you'll see the red outline + "CANARY · telemetry
on" badge. (Cloudflare caches `/`; if you were just on stable, a hard-reload or a
`?x=1`-style cache-buster forces the origin — the routing itself is always correct.)

## Prerequisites

- `~/.config/fm-e2e/creds.env` present (`FM_CANARY_TOKEN`, plus the `e2e-canary`
  login for signing in). `drive-canary/setup.sh` writes/verifies this file.
- The gateway `/canary/on|off` endpoint deployed (canary-cookie-gate EnvoyFilter).
- A canary must actually be **active** for its pages/APIs to serve — otherwise the
  cookie is harmless and you fall through to stable (`canary`-if-exists-else-stable).
  Activate one with the **canary** skill (`canary deploy <svc> <ref>`).

## See also

- **watch-canary** — have Claude observe your live canary session and diagnose
  bugs you hit (the intended pairing).
- **canary** — activate/retire the canary workloads you're testing.
- **drive-canary** — the *headless agent* version of this (Playwright, not your
  browser).
