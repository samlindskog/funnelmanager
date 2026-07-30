#!/usr/bin/env bash
# SessionEnd hook: if the ending session did deploy work, spawn a headless
# Claude (file-edit tools only) to fold new learnings into the deploy skill.
# Registered in .claude/settings.json. Logs to improve.log next to this file.
set -uo pipefail

# Recursion guard: the headless run we spawn also ends a session.
[ -n "${CLAUDE_SKILL_IMPROVER:-}" ] && exit 0

input=$(cat)
transcript=$(printf '%s' "$input" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null)
[ -n "$transcript" ] && [ -f "$transcript" ] || exit 0

# Only fire for sessions that actually touched the deploy path.
grep -q -m1 -E 'release-prod|flux reconcile|rollout status|deploy-funnelmanager' "$transcript" || exit 0

skill_dir="$(cd "$(dirname "$0")" && pwd)"
log="$skill_dir/improve.log"
echo "=== $(date '+%F %T') improving from $transcript ===" >>"$log"

prompt="You maintain the deploy skill at $skill_dir/SKILL.md (driver: $skill_dir/deploy.sh).
Review the session transcript at $transcript for deploy-related work only.
If the session hit an error, workaround, changed procedure, or useful command
that the skill does not yet cover, fold it in: minimal edits to the Gotchas or
Troubleshooting sections of SKILL.md, or a targeted fix to deploy.sh. Do not
rewrite sections wholesale. Only add facts evidenced in the transcript. You
have file tools only — never attempt to run deploys, ssh, git, or any command.
If nothing new was learned, make no edits and say so."

if [ -n "${IMPROVE_DRYRUN:-}" ]; then
  echo "DRYRUN: would run claude -p against $transcript" >>"$log"
  exit 0
fi

CLAUDE_SKILL_IMPROVER=1 nohup claude -p "$prompt" \
  --allowedTools "Read,Grep,Glob,Edit,Write" \
  >>"$log" 2>&1 &
exit 0
