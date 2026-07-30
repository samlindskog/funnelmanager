# Keycloak script provider — fm_origin passthrough

`fm-origin-provider.jar` is a Keycloak [script provider](https://www.keycloak.org/docs/26.2/server_development/#_script_providers)
that ships one OIDC protocol mapper, **`fm-origin-passthrough`**
(provider id `script-fm-origin-passthrough.js`).

## Why it exists

`fm_origin` (`user` | `agent`) attributes who initiated a request so a persisted
record renders "alice (via agent)". The `agents` client **mints** `fm_origin=agent`
on the first exchange hop (its own hardcoded-claim mapper). But every *subsequent*
hop is a token exchange performed by a **different** client (`mcp→search`,
`search→leads`, …). A hardcoded-claim mapper on those clients re-stamps `user` and
the agent origin is lost — the reviewer-confirmed bug.

This mapper **carries the inbound subject token's `fm_origin` claim onto the newly
issued token** (default `user`), so origin survives every downstream exchange. It is
attached to every service client through the **`fm-origin` client scope**. The
`agents` client keeps its hardcoded `agent` mint and does **not** carry this scope.

## Security

The script reads **only** the `subject_token` being exchanged (already validated by
Keycloak for this exchange) — never a caller-supplied `fm_origin`/`claims` request
parameter — so a client cannot forge origin. Verified: passing `-d fm_origin=agent`
on a user exchange yields `fm_origin=user`.

## Requirements

- Keycloak **feature `scripts`** (preview) enabled: `KC_FEATURES=scripts`.
- This JAR present in `/opt/keycloak/providers/`.
- dev/prod compose: bind-mount the JAR + `KC_FEATURES=scripts`.
- k3s: mounted from the `keycloak-providers` ConfigMap (`binaryData`) +
  `KC_FEATURES=scripts` env (see `deploy/infrastructure/identity/`).

The Keycloak image is **pinned** (tag + `@sha256` digest) in
`docker-compose.dev.yml`, `docker-compose.prod.yml`, and
`deploy/infrastructure/identity/deployment.yaml`. The script-mapper mechanism was
proven on 26.2 — stay on **26.2.x**. Bump the tag and digest together (verify the
digest via the quay.io tags API before pinning).

## Source of truth vs. generated artifacts

`src/` is the **only** thing you edit by hand:

- `src/fm-origin-passthrough.js` — the mapper script.
- `src/META-INF/keycloak-scripts.json` — its provider descriptor.

Two artifacts are **generated from `src/` and committed** (they are not
hand-authored):

- `fm-origin-provider.jar` — a ZIP of `src/`, bind-mounted by dev/prod compose.
- `../../infrastructure/identity/providers-configmap.yaml` — the same JAR as
  base64 `binaryData`, mounted in k3s (Flux applies it — GitOps).

## Rebuilding

Run the build script — the **same** command CI and developers use:

```sh
deploy/keycloak/providers/build.sh
```

It rebuilds the JAR from `src/` and regenerates the ConfigMap, then you commit
both. The build is **byte-reproducible** on any POSIX host (STORED zip, fixed
1980 timestamp, no zlib dependency), so a fresh build on macOS and on Ubuntu CI
produce identical bytes.

Two CI guardrails enforce this:

- **`keycloak-provider` job** (`.github/workflows/ci.yml`, every PR/push) runs
  `build.sh` and fails if the committed JAR or ConfigMap differ from a fresh
  build — so the committed artifacts can never drift from `src/`.
- **On release** (`build-images.yml` `pin` job, invoked by `release-prod.yml`)
  CI regenerates and commits both artifacts alongside the
  `sha-…` image pin, exactly like the image pins. Because the build is
  deterministic, this is a no-op unless `src/` changed.

If you edit `src/`, run `build.sh` locally and commit all three (`src/`, the JAR,
the ConfigMap) so CI stays green.

**k3s operational note:** the `keycloak-providers` ConfigMap has a static name, so
updating the JAR does **not** by itself roll the Keycloak pod (Keycloak loads
providers at boot). After the ConfigMap changes, restart Keycloak to pick up the
new provider: `kubectl -n identity rollout restart deploy/keycloak`. (Follow-up:
switch to a hashed `configMapGenerator` so the rollout is automatic.)
