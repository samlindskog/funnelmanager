#!/usr/bin/env bash
# SessionStart hook (PROJECT-scoped). If the session-end auditor left advisory
# proposals to improve this repo's project-scoped agents, inject a short notice so
# THIS session's agent surfaces them for yes/no approval and applies only approved
# edits. Silent (and cheap) when nothing is pending.
set -uo pipefail

[ -n "${CLAUDE_DOMAIN_IMPROVER:-}" ] && exit 0

proj="$(cd "$(dirname "$0")/../.." && pwd)"
base="$proj/.claude/agent-runs"
pending="$base/pending"

mapfile -t files < <(find "$pending" -maxdepth 1 -name '*.md' -size +1c 2>/dev/null | sort)
[ "${#files[@]}" -gt 0 ] || exit 0

printf 'There %s %d pending PROJECT-agent improvement proposal(s) awaiting approval:\n' \
  "$([ "${#files[@]}" -eq 1 ] && echo is || echo are)" "${#files[@]}"
for f in "${files[@]}"; do printf '  - %s\n' "$f"; done
cat <<EOF
At a natural break (do NOT derail the user's current task), read each proposal and
present its suggestions to the user for yes/no approval — one decision per
suggestion (AskUserQuestion is a good fit). For each APPROVED suggestion, edit the
named .claude/agents/<agent>.md accordingly. Then move the handled proposal file
into $base/applied/ (mkdir -p it) so it stops being offered. Apply nothing without
explicit approval; if the user isn't interested, move the file to applied/ as dismissed.
EOF
exit 0
