#!/usr/bin/env bash
# SessionEnd hook (PROJECT-scoped). If project-scoped agents ran and were
# captured this session, spawn a headless READ-ONLY Claude to judge their
# effectiveness from their own transcripts and write ADVISORY improvement
# PROPOSALS (it does NOT edit the agent definitions). Proposals are surfaced for
# yes/no approval next session by domain-proposals-notice.sh (SessionStart hook).
# Mirrors the global adversarial-reviewer auditor, scoped to this repo's agents.
set -uo pipefail

# Recursion guard: the headless run we spawn also ends a session.
[ -n "${CLAUDE_DOMAIN_IMPROVER:-}" ] && exit 0

proj="$(cd "$(dirname "$0")/../.." && pwd)"
input=$(cat)
session=$(printf '%s' "$input" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("session_id","") or "")' 2>/dev/null)
[ -n "$session" ] || exit 0

base="$proj/.claude/agent-runs"
dir="$base/$session"
# Only fire if THIS session actually captured project-agent transcripts.
ls "$dir"/*.jsonl >/dev/null 2>&1 || exit 0

log="$base/improve.log"
pending="$base/pending"
mkdir -p "$pending"
agents_dir="$proj/.claude/agents"
stamp=$(date '+%Y%m%d-%H%M%S')
proposal="$pending/$stamp-$session.md"
echo "=== $(date '+%F %T') proposing from $dir -> $proposal ===" >>"$log"

# Distinct agents that ran this session.
ran=$(ls "$dir"/*.jsonl 2>/dev/null | xargs -n1 basename | sed 's/__.*//' | sort -u | tr '\n' ' ')

prompt="You audit this repo's PROJECT-scoped agent definitions in $agents_dir/
(each is a service/domain owner, e.g. search-agent.md, leads-agent.md). Read the
project's CLAUDE.md first for the architecture these agents operate under.

In the session that just ended these agents ran: $ran
Their FULL captured transcripts are the *.jsonl files in: $dir
(each named <agent>__<id>.jsonl; JSONL, one message/tool event per line).

READ-ONLY TASK — you have Read/Grep/Glob only. Do NOT edit any file. For each
agent that ran, read its transcript(s) and its current .md, and judge
EFFECTIVENESS against its stated remit: did it stay inside its owned directory and
boundary, or reach across (a boundary violation the .md should have prevented)?
did it honor the project invariants (per CLAUDE.md)? did it run the right verify
step? did it miss context it had to rediscover (a hint the .md should carry)? did
it waste tokens? were its hand-offs to other agents clean?

Then OUTPUT (to stdout) a markdown proposal in EXACTLY this shape:

# Project-agent improvement proposals — $stamp
_Session $session · agents that ran: ${ran}_

## <agent-name> (.claude/agents/<agent-name>.md)
### Suggestion 1: <one-line title>
- **Why (evidence):** <what in the transcript shows the gap>
- **Proposed edit:** <the specific change — quote current line(s) + replacement,
  or the exact text to add and where>
### Suggestion 2: ...
(If an agent performed well, write 'No change proposed.' under its heading.)

Keep suggestions minimal, concrete, and justified only by the transcripts. Output
ONLY the markdown proposal — no preamble."

if [ -n "${IMPROVE_DRYRUN:-}" ]; then
  echo "DRYRUN: would propose over $dir (ran: $ran) -> $proposal" >>"$log"
  exit 0
fi

CLAUDE_DOMAIN_IMPROVER=1 nohup claude -p "$prompt" \
  --allowedTools "Read,Grep,Glob" \
  --output-format text \
  >"$proposal" 2>>"$log" &

# Retention: keep the 20 most recent SESSION archives (never touch pending/applied).
ls -1dt "$base"/*/ 2>/dev/null | grep -vE '/(pending|applied)/$' | tail -n +21 | xargs -r rm -rf 2>/dev/null
exit 0
