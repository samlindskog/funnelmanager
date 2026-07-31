---
name: provision-user
description: Provision human users in the funnelmanager PROD Keycloak realm via GROUPS (role bundles) — create a user, add/remove them from a group, list users/groups. Use when asked to "add a user", "give <person> access", "onboard <person>", "make <person> an admin", "create a login", "remove <person>'s access", or "what groups/users exist". Groups are the permissioning unit: roles are assigned to groups, users are added to groups. NOT for changing the roles/grants themselves (that is a realm + grants.py + data.json change) and NOT a health check (use prod-health).
---

# provision-user

Modular permission provisioning for funnelmanager humans, done the way Keycloak
recommends: **assign roles to GROUPS, add users to groups.** No new tooling — the
roles/grants are unchanged; a group is just a named bundle of the human-facing
`-access` roles (plus `admin`). To change what a person can do, change which
group they are in.

The driver is `.claude/skills/provision-user/provision.sh` (run from the repo
root). Every write goes through `kcadm` **inside the prod Keycloak pod** (usfr4
can't reach the public KC URL; admin creds live in the pod's env from the
`identity/keycloak-admin` secret) — the same ssh + `sudo -n kubectl -n identity
exec` path the `prod-health` and `deploy/bootstrap` scripts use.

## The group bundles (DEFAULTS — adjust to taste)

| Group | Realm roles it confers | Who it's for |
|---|---|---|
| `/standard` | `search-access`, `mail-access` | day-to-day users (search + mail) |
| `/power` | `search-access`, `mail-access`, `jobs-access`, `agents-access` | full product surface incl. jobs + agents |
| `/admins` | `admin` (KC composite of the four `-access` roles) | administrators |

These bundles are **defaults you may want to change.** They are triple-tracked:
the realm `groups` block (`deploy/keycloak/realm-funnelmanager-{dev,prod.example}.json`),
the `roles_for()` map in `provision.sh`, and the guard in
`fm_runtime.grants._verify_realm_groups`. Edit them together, then re-run
`fm_runtime.export --check … --realm` (below).

**Groups only ever map human-facing roles.** The MACHINE roles
`internal-service` and `jobs-internal` are held by service accounts via client
credentials and are **never** group-assignable — `fm_runtime.export --check`
fails closed if a group maps one (a human in such a group would silently gain a
service identity's grants).

## Verbs

```bash
.claude/skills/provision-user/provision.sh create <username> <email> [group] --yes
.claude/skills/provision-user/provision.sh add    <username> <group> --yes
.claude/skills/provision-user/provision.sh remove <username> <group> --yes
.claude/skills/provision-user/provision.sh list-users
.claude/skills/provision-user/provision.sh list-groups
```

**Writes mutate LIVE PROD identity and preview by default.** `create`/`add`/
`remove` print exactly what they would do and then STOP unless you pass `--yes`
(use `--dry-run` to preview without touching prod). `/admins` prints an extra
superuser warning. `list-*` are read-only (no gate). The one-time temp password
is generated **inside** the Keycloak pod, so it never appears in the ssh/sudo/
kubectl-exec argv on the client or usfr4 — only in the command output for
out-of-band hand-off.

- **`create`** — creates the user (enabled, email unverified), sets a random
  20-char **temporary** password, and forces `UPDATE_PASSWORD` at first login.
  The temp password is printed **once** to share out-of-band (never stored).
  With a third arg it also adds the user to that group. Idempotent: if the user
  already exists it skips creation and (if a group was given) just ensures
  membership.
- **`add` / `remove`** — group membership for an existing user. `add` is
  idempotent.
- **`list-users`** — username, email, enabled.
- **`list-groups`** — each group's path and the realm roles it actually confers
  (read live from KC role-mappings, not the static map).

**Create-if-missing:** before adding a user to a group the driver ensures the
group exists. If it has to create a known bundle (`standard`/`power`/`admins`) it
also assigns that bundle's roles — so provisioning works against the **live**
realm even before a realm re-import. An unknown group name is created **empty**
with a warning (assign roles in the console or use a known bundle).

## Typical flows

```bash
# Onboard a standard user (search + mail):
.claude/skills/provision-user/provision.sh create alice alice@acme.com standard

# Promote them to the full surface later:
.claude/skills/provision-user/provision.sh add alice power
.claude/skills/provision-user/provision.sh remove alice standard

# Make someone an admin:
.claude/skills/provision-user/provision.sh create sam sam@acme.com admins

# Audit:
.claude/skills/provision-user/provision.sh list-users
.claude/skills/provision-user/provision.sh list-groups
```

A user picks up a group's roles on their **next token** (access tokens are
short-lived, ~5 min) — no service restart needed. Sign the user out / have them
re-login for an immediate effect.

## Keep source in lockstep

`provision.sh` mutates the **live** realm (persisted in `kc-db`, survives
restarts). The tracked realm files are the import-time source of truth — if you
add/retire a group or change a bundle, edit the realm `groups` block in **both**
`deploy/keycloak/realm-funnelmanager-dev.json` and `…-prod.example.json` and the
`roles_for()` map, then prove the three legs agree:

```bash
leads/.venv/bin/python -m fm_runtime.export --check deploy/policy/data.json \
  --realm deploy/keycloak/realm-funnelmanager-dev.json
leads/.venv/bin/python -m fm_runtime.export --check deploy/policy/data.json \
  --realm deploy/keycloak/realm-funnelmanager-prod.example.json
```

Any realm/role/scope/group change is a **security-review** item.

## Gotchas

- **usfr4 cannot reach the public KC URL** (Cloudflare hairpin) — that's why
  kcadm runs **inside** the pod (`kubectl -n identity exec deploy/keycloak`),
  authing with the pod's `KC_BOOTSTRAP_ADMIN_*` env.
- **usernames are admin-controlled and un-renameable**; deleting a user does NOT
  delete their attributed rows (search history etc. is keyed on
  `preferred_username`) — a later user with the same name inherits them. Reuse
  names deliberately.
- **`admin` is a composite** of the four `-access` roles — `/admins` confers
  every service. Don't also add an admin to `/standard`/`/power` (redundant).
- Inputs are validated to a safe charset (`A-Za-z0-9._-`, plus a normal email
  shape) before they reach the pod — the driver refuses anything else rather
  than interpolate it into the remote command.
- **TODO (Phase-2 ops config):** `CP=usfr4` / namespace / realm are hardcoded;
  this skill will adopt the shared gitignored ops config the sibling skills move
  to. Not a blocker today.

## Relationship to other skills

`prod-health` *looks* (read-only); `deploy-funnelmanager` *ships code*;
**this** skill provisions *people*. It never deploys, reconciles, or touches app
images — only realm users/groups. A change to the *bundles themselves* (not just
who's in them) rides to prod as a realm edit reviewed by `security-reviewer`.
