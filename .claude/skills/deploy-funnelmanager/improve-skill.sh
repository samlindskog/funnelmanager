#!/usr/bin/env bash
# SessionEnd hook: if the ending session did deploy work, spawn a headless
# READ-ONLY Claude to judge the deploy skill from the session transcript and write
# an ADVISORY improvement PROPOSAL. Registered in .claude/settings.json.
#
# WHY ADVISORY (not direct edits): the previous version spawned `claude -p` with
# Edit/Write to patch SKILL.md/deploy.sh directly, but Edit/Write is
# permission-blocked in that headless context — so it detected learnings it could
# never apply. It now mirrors the domain-agent hooks: it writes a proposal to
# .claude/agent-runs/pending/, which domain-proposals-notice.sh (SessionStart)
# surfaces next session for yes/no approval — a human/agent applies the edits.
#
# Logs to improve.log next to this file.
set -uo pipefail

# Recursion guard: the headless run we spawn also ends a session.
[ -n "${CLAUDE_SKILL_IMPROVER:-}" ] && exit 0

input=$(cat)
transcript=$(printf '%s' "$input" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null)
[ -n "$transcript" ] && [ -f "$transcript" ] || exit 0

# Only fire for sessions that actually touched the deploy path.
grep -q -m1 -E 'release-prod|flux reconcile|rollout status|deploy-funnelmanager' "$transcript" || exit 0

skill_dir="$(cd "$(dirname "$0")" && pwd)"
proj="$(cd "$skill_dir/../../.." && pwd)"
log="$skill_dir/improve.log"

# Advisory proposals live where the domain-agent hooks put theirs, so the same
# SessionStart notice + apply-on-approval flow handles them.
base="$proj/.claude/agent-runs"
pending="$base/pending"
mkdir -p "$pending"
stamp=$(date '+%Y%m%d-%H%M%S')
proposal="$pending/$stamp-deploy-skill.md"
echo "=== $(date '+%F %T') proposing (advisory) from $transcript -> $proposal ===" >>"$log"

prompt="You audit the deploy skill at $skill_dir/SKILL.md (driver: $skill_dir/deploy.sh).
Read the project's CLAUDE.md first for the GitOps/prod topology the skill operates under.
Review the session transcript at $transcript for deploy-related work ONLY.

READ-ONLY TASK — you have Read/Grep/Glob only. Do NOT edit any file, and never run
deploys, ssh, git, flux, kubectl, or any command. If the session hit an error,
workaround, changed procedure, or useful command that the skill does not yet cover,
propose folding it into the Gotchas/Troubleshooting sections of SKILL.md or a targeted
fix to deploy.sh. Only propose facts EVIDENCED in the transcript; keep it minimal.

OUTPUT (to stdout) a markdown proposal in EXACTLY this shape:

# Deploy-skill improvement proposals — $stamp
_Session transcript: ${transcript}_

## .claude/skills/deploy-funnelmanager/SKILL.md   (or deploy.sh)
### Suggestion 1: <one-line title>
- **Why (evidence):** <what in the transcript shows the gap>
- **Proposed edit:** <the specific change — quote current line(s) + replacement, or the exact text to add and where>
### Suggestion 2: ...

If nothing new was learned, output a single line: 'No change proposed.' Output ONLY the
markdown proposal — no preamble."

if [ -n "${IMPROVE_DRYRUN:-}" ]; then
  echo "DRYRUN: would propose (advisory) over $transcript -> $proposal" >>"$log"
  exit 0
fi

CLAUDE_SKILL_IMPROVER=1 nohup claude -p "$prompt" \
  --allowedTools "Read,Grep,Glob" \
  --output-format text \
  >"$proposal" 2>>"$log" &
exit 0
