#!/usr/bin/env bash
# provision-user driver — create/assign human users in the funnelmanager PROD
# Keycloak realm via GROUPS (the provisioning unit: roles are assigned to groups,
# users are added to groups). Modular permissioning without a new tool.
#
# Prod Keycloak runs in the `identity` namespace on the k3s cluster (control
# plane usfr4). usfr4 cannot reach the public KC URL (Cloudflare hairpin), and
# the console admin creds live in the `identity/keycloak-admin` secret — so every
# kcadm call runs INSIDE the keycloak pod (the KC_BOOTSTRAP_ADMIN_* env is already
# present there), reached over ssh + `sudo -n kubectl -n identity exec`. This is
# the same invocation the sibling skills use (prod-health/check.sh,
# deploy/bootstrap/finish-agents-jobs-prod.sh).
#
# Groups map ONLY human-facing roles (per fm_runtime.export --check's group leg):
#   /standard -> search-access, mail-access
#   /power    -> search-access, mail-access, jobs-access, agents-access
#   /admins   -> admin (composite of the four -access roles)
# Machine roles (internal-service, jobs-internal) are NEVER group-assignable.
# The bundles are DEFAULTS — adjust the realm groups + this map together if the
# tiers change (then re-run fm_runtime.export --check --realm).
#
# The driver ENSURES the three bundle groups exist (create-if-missing, and it
# assigns the bundle's roles when it creates one), so provisioning works against
# the LIVE realm even before a realm re-import.
#
# Usage: .claude/skills/provision-user/provision.sh <verb> [args] [--yes|--dry-run]
#   create <username> <email> [group]   create user, temp password + forced reset,
#                                        optionally add to a group
#   add    <username> <group>            add existing user to a group
#   remove <username> <group>            remove user from a group
#   list-users                           list realm users (username,email,enabled)
#   list-groups                          list groups + the realm roles each confers
#
# WRITES (create/add/remove) MUTATE LIVE PROD IDENTITY. They preview by default
# and require --yes to apply (--dry-run to preview without touching prod). The
# one-time temp password is generated INSIDE the keycloak pod (never crosses the
# ssh/sudo/kubectl-exec argv) and printed once in the output for out-of-band
# hand-off. list-* are read-only (no gate).
#
# TODO(Phase-2 ops config): CP / namespace / realm are hardcoded here; adopt the
# gitignored ops config the sibling skills will move to, rather than these consts.

set -uo pipefail   # NOT -e: report a clean error, don't abort mid-message

CP=usfr4
NS=identity
REALM=funnelmanager
POD=deploy/keycloak
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 $CP"

OK="✅"; BAD="❌"; WARN="⚠️"

usage() { sed -n '/^# Usage:/,/read-only (no gate)/p' "$0"; exit 1; }

# Validate an identifier used in the ssh-interpolated command (defense against
# injection through the remote shell parse). Usernames / group names.
valid_id() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]; }
# A permissive-but-safe email charset (no shell metacharacters).
valid_email() { [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }

KNOWN_GROUPS="standard power admins"

# Run the in-pod dispatcher. Args (verb + values) ride as env on the exec; all
# are pre-validated to a safe charset so single-quote interpolation is injection-
# safe. The quoted heredoc is the pod-side script (no local expansion).
run_in_pod() {
  local verb="$1" u="${2:-}" e="${3:-}" g="${4:-}"
  # NOTE: no password crosses this boundary — the temp password is generated
  # in-pod (create case) so it never appears in the ssh/sudo/kubectl-exec argv
  # (nor any sudo command-line log) on the client or usfr4; only the in-pod
  # kcadm argv sees it, same visibility as the admin creds already there.
  $SSH "sudo -n kubectl -n $NS exec -i $POD -- env \
      VERB='$verb' U='$u' E='$e' G='$g' REALM='$REALM' \
      bash -s" <<'POD'
set -uo pipefail
K=/opt/keycloak/bin/kcadm.sh; R="$REALM"
$K config credentials --server http://localhost:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" \
  >/dev/null 2>&1 || { echo "ERR: kcadm login failed (keycloak-admin creds)"; exit 3; }

# id of a top-level group by exact name (empty if absent)
gid() { $K get groups -r "$R" --format csv --fields id,name 2>/dev/null \
  | awk -F',' -v n="\"$1\"" '$2==n{gsub(/"/,"",$1);print $1;exit}'; }
# id of a user by exact username (server-side exact match)
uid_of() { $K get users -r "$R" -q username="$1" -q exact=true --format csv --fields id,username 2>/dev/null \
  | awk -F',' -v n="\"$1\"" '$2==n{gsub(/"/,"",$1);print $1;exit}'; }

# Default role bundle for a known group name (empty for an unknown group).
roles_for() { case "$1" in
  standard) echo "search-access mail-access" ;;
  power)    echo "search-access mail-access jobs-access agents-access" ;;
  admins)   echo "admin" ;;
  *)        echo "" ;;
esac; }

# Ensure a top-level group exists; when creating a KNOWN bundle, assign its roles.
# Prints the group id on stdout; progress on stderr.
ensure_group() {
  local name="$1" id rl r; id=$(gid "$name")
  if [ -z "$id" ]; then
    $K create groups -r "$R" -s name="$name" >/dev/null 2>&1
    id=$(gid "$name")
    [ -z "$id" ] && { echo "ERR: could not create group /$name" >&2; return 4; }
    echo "  created group /$name" >&2
    rl=$(roles_for "$name")
    if [ -n "$rl" ]; then
      for r in $rl; do
        $K add-roles -r "$R" --gid "$id" --rolename "$r" >/dev/null 2>&1 \
          && echo "    + role $r" >&2
      done
    else
      echo "  WARN: /$name is not a known bundle — created with NO roles; assign" >&2
      echo "        roles in the console or use standard|power|admins" >&2
    fi
  fi
  printf '%s' "$id"
}

join_group() { # $1=uid $2=gid
  $K update "users/$1/groups/$2" -r "$R" -s "realm=$R" -s "userId=$1" -s "groupId=$2" -n >/dev/null 2>&1
}

case "$VERB" in
  list-groups)
    echo "groups (path -> realm roles conferred):"
    $K get groups -r "$R" --format csv --fields id,name,path 2>/dev/null \
      | while IFS=',' read -r gi gn gp; do
          gi=${gi//\"/}; gp=${gp//\"/}
          [ -z "$gi" ] && continue
          rr=$($K get "groups/$gi/role-mappings/realm" -r "$R" --fields name --format csv 2>/dev/null \
                | tr -d '"' | tr '\n' ' ')
          printf '  %s -> [%s]\n' "$gp" "$(echo "$rr" | sed 's/ *$//; s/ /, /g')"
        done
    ;;
  list-users)
    echo "users (username, email, enabled):"
    $K get users -r "$R" --format csv --fields username,email,enabled 2>/dev/null \
      | sed 's/^/  /'
    ;;
  create)
    if [ -n "$(uid_of "$U")" ]; then
      echo "user '$U' already exists — skipping create (use 'add' to change groups)"
    else
      $K create users -r "$R" \
        -s username="$U" -s email="$E" -s enabled=true -s emailVerified=false \
        -s 'requiredActions=["UPDATE_PASSWORD"]' >/dev/null \
        || { echo "ERR: create user '$U' failed"; exit 5; }
      # Temp password generated HERE (in-pod) — never crosses the client/usfr4
      # argv. 20 url-safe alnum chars; forced reset at first login.
      TEMP=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
      $K set-password -r "$R" --username "$U" --new-password "$TEMP" --temporary >/dev/null \
        || { echo "ERR: set temp password for '$U' failed"; exit 6; }
      echo "created user '$U' <$E> (UPDATE_PASSWORD forced at first login)"
      echo "TEMP_PASSWORD for '$U': $TEMP"
      echo "  (one-time — share out-of-band; not stored; reset at first login)"
    fi
    if [ -n "$G" ]; then
      gi=$(ensure_group "$G") || exit $?
      ui=$(uid_of "$U")
      join_group "$ui" "$gi" && echo "added '$U' to /$G"
    fi
    ;;
  add)
    ui=$(uid_of "$U"); [ -z "$ui" ] && { echo "ERR: no such user '$U'"; exit 7; }
    gi=$(ensure_group "$G") || exit $?
    join_group "$ui" "$gi" && echo "added '$U' to /$G"
    ;;
  remove)
    ui=$(uid_of "$U"); [ -z "$ui" ] && { echo "ERR: no such user '$U'"; exit 7; }
    gi=$(gid "$G");    [ -z "$gi" ] && { echo "ERR: no such group /$G"; exit 8; }
    $K delete "users/$ui/groups/$gi" -r "$R" >/dev/null 2>&1 \
      && echo "removed '$U' from /$G"
    ;;
  *) echo "ERR: unknown verb '$VERB'"; exit 2 ;;
esac
POD
}

# Gate a MUTATING verb: preview the action, and require an explicit --yes to
# apply (P4-in-spirit — these writes hit live prod identity and can grant admin).
# Returns 0 to proceed, 1 to stop (dry-run, or --yes not given).
confirm_write() { # $1=action text  $2(optional)=group name
  echo "== WOULD $1 — PROD $CP/$NS (realm $REALM) =="
  [ "${2:-}" = admins ] && echo "$WARN /admins confers the FULL admin composite (service:*) — a superuser."
  if [ "$DRY" = 1 ]; then echo "(--dry-run: nothing changed)"; return 1; fi
  if [ "$APPLY" != 1 ]; then
    echo "$WARN this MUTATES live prod identity. Re-run with --yes to apply."
    return 1
  fi
  return 0
}

main() {
  # --yes / -y (apply) and --dry-run (preview) may appear anywhere; strip them.
  local APPLY=0 DRY=0; local args=()
  local a; for a in "$@"; do
    case "$a" in
      --yes|-y) APPLY=1 ;;
      --dry-run) DRY=1 ;;
      *) args+=("$a") ;;
    esac
  done
  set -- ${args+"${args[@]}"}
  local verb="${1:-}"; shift || true
  case "$verb" in
    create)
      local u="${1:-}" e="${2:-}" g="${3:-}"
      [ -n "$u" ] && [ -n "$e" ] || { echo "$BAD create needs <username> <email> [group]"; usage; }
      valid_id "$u"    || { echo "$BAD invalid username '$u' (allowed: A-Z a-z 0-9 . _ -)"; exit 1; }
      valid_email "$e" || { echo "$BAD invalid email '$e'"; exit 1; }
      if [ -n "$g" ]; then
        valid_id "$g" || { echo "$BAD invalid group '$g'"; exit 1; }
        echo "$KNOWN_GROUPS" | grep -qw "$g" \
          || echo "$WARN '$g' is not a default bundle (standard|power|admins) — it will be created with NO roles unless it already exists"
      fi
      confirm_write "create user '$u' <$e>${g:+ and add to /$g}" "$g" || exit 0
      run_in_pod create "$u" "$e" "$g"
      ;;
    add|remove)
      local u="${1:-}" g="${2:-}"
      [ -n "$u" ] && [ -n "$g" ] || { echo "$BAD $verb needs <username> <group>"; usage; }
      valid_id "$u" || { echo "$BAD invalid username '$u'"; exit 1; }
      valid_id "$g" || { echo "$BAD invalid group '$g'"; exit 1; }
      confirm_write "$verb group membership: '$u' <-> /$g" "$g" || exit 0
      run_in_pod "$verb" "$u" "" "$g"
      ;;
    list-users)  echo "== users in realm $REALM ($CP/$NS) ==";  run_in_pod list-users ;;
    list-groups) echo "== groups in realm $REALM ($CP/$NS) =="; run_in_pod list-groups ;;
    -h|--help|help|"") usage ;;
    *) echo "$BAD unknown verb '$verb'"; usage ;;
  esac
}

main "$@"
