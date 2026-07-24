#!/usr/bin/env bash
# SubagentStop hook (PROJECT-scoped). When a PROJECT-scoped agent finishes,
# archive its transcript so the SessionEnd auditor can judge its effectiveness.
#
# "Project-scoped" is defined structurally: the stopped agent counts iff
# .claude/agents/<name>.md exists in this repo. That auto-covers every domain
# agent (search-agent, leads-agent, …) and excludes the global reviewers
# (handled by the global capture hook). No matcher — this script self-filters.
set -uo pipefail

# Never capture during the auditor's own headless run.
[ -n "${CLAUDE_DOMAIN_IMPROVER:-}" ] && exit 0

proj="$(cd "$(dirname "$0")/../.." && pwd)"
input=$(cat)
j() { printf '%s' "$input" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('$1','') or '')" 2>/dev/null; }

session=$(j session_id)
atype=$(j agent_type); [ -z "$atype" ] && atype=$(j agent_name)
[ -z "$atype" ] && atype=$(j subagent_type)
aid=$(j agent_id);   [ -z "$aid" ]   && aid=$(j subagent_id)
tpath=$(j transcript_path)

# Only project-scoped agents (a definition file in THIS repo).
[ -n "$atype" ] && [ -f "$proj/.claude/agents/$atype.md" ] || exit 0

base="$proj/.claude/agent-runs"
dir="$base/${session:-unknown}"
mkdir -p "$dir"

# One-time: keep a raw payload so the true field names are verifiable.
[ -f "$base/_payload-sample.json" ] || printf '%s' "$input" >"$base/_payload-sample.json"

stamp=$(date +%s)
dest="$dir/${atype}__${aid:-$stamp}.jsonl"

if [ -n "$tpath" ] && [ -f "$tpath" ]; then
  cp "$tpath" "$dest" 2>/dev/null
elif [ -n "$aid" ]; then
  found=$(ls -1 "$HOME"/.claude/projects/*/*/subagents/*"$aid"* \
                 /private/tmp/*/*/tasks/*"$aid"* 2>/dev/null | head -1)
  [ -n "$found" ] && cp "$found" "$dest" 2>/dev/null
fi

printf '%s\t%s\t%s\t%s\n' "$stamp" "$atype" "${aid:-?}" "${tpath:-$dest}" >>"$dir/manifest.tsv"
exit 0
