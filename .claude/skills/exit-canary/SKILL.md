---
name: exit-canary
description: Leave the funnelmanager canary in YOUR browser — clear the host-only fm_debug session cookie (via the gateway /debug/off endpoint, no secret needed) and stop the enter-canary launcher, dropping you back to stable prod. The counterpart to enter-canary. Use when done hand-navigating the canary. Does NOT scale down canary workloads (use `canary retire <svc>` for that).
---

# Exit the canary (human browser)

Undoes **enter-canary**: clears the single `fm_debug` session cookie (both its
stable `<secret>` and canary `<secret>|canary` forms) so your browser routes to
**stable prod** again, and stops the localhost launcher.

Clearing needs **no secret** — `/debug/off` only ever pushes you to stable (the
safe default), so unlike `/debug/canary/on` it isn't gated.

## Use it

```bash
.claude/skills/exit-canary/exit.sh
```

That opens `https://x9bc433.win/debug/off` in your default browser (302 →
`Set-Cookie: fm_debug=; Max-Age=0` → `/`) and stops `enter-canary`'s launcher.
Reload any open tab to confirm the red "CANARY · telemetry on" badge is gone.

(Manual equivalent: visit `https://x9bc433.win/debug/off`, or if the launcher is
running, `http://localhost:8799/off`.)

## Scope — cookie vs workloads

This exits **your browser's routing only**. The `<svc>-canary` **workloads keep
running** (and keep costing a pod). To actually scale them down:

```bash
.claude/skills/canary/canary.sh retire <svc>     # e.g. frontend / search
```

Thanks to the route-tied fallback, retiring cleanly drops cookie-holders to
stable (no 503).

## See also

- **enter-canary** — get onto the canary (the counterpart).
- **canary** — `retire` the actual canary workloads.
- **watch-canary** — have Claude observe your session while you're on it.
