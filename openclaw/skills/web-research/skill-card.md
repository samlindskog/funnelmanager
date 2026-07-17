## Description: <br>
Web Research turns a specific research question into a reviewable plan, parallel findings files, and a cited synthesis report saved on disk. <br>

This skill is ready for commercial/non-commercial use. <br>

## Publisher: <br>
[anderskev](https://clawhub.ai/user/anderskev) <br>

### License/Terms of Use: <br>
MIT-0 <br>


## Use Case: <br>
External users, developers, and agent workflows use this skill to gather auditable public web evidence for a focused research question and produce a plan, subtopic findings, and cited synthesis report. <br>

### Deployment Geography for Use: <br>
Global <br>

## Known Risks and Mitigations: <br>
Risk: Research questions may be sent to web search providers. <br>
Mitigation: Use the skill only for public web research and avoid sensitive or confidential queries. <br>
Risk: The skill saves research plans, findings, and reports locally. <br>
Mitigation: Choose an appropriate output directory and review saved artifacts before sharing or committing them. <br>
Risk: Automatically proceeding can skip user review of the research plan. <br>
Mitigation: Keep auto_proceed disabled unless an upstream workflow has already reviewed the research framing. <br>
Risk: Web research can contain incomplete or unsupported claims. <br>
Mitigation: Use the required citation schema, review Gaps & Limitations, and verify the plan and final report before relying on results. <br>


## Reference(s): <br>
- [ClawHub Web Research Skill](https://clawhub.ai/anderskev/skills/web-research) <br>
- [Citation Schema](references/citation-schema.md) <br>
- [Companion Invocation Contract](references/companion-contract.md) <br>
- [Failure Modes](references/failure-modes.md) <br>
- [Synthesis Skeleton](references/report-template.md) <br>
- [Subagent Brief Template](references/subagent-brief.md) <br>


## Skill Output: <br>
**Output Type(s):** [text, markdown, shell commands, configuration, guidance] <br>
**Output Format:** [Markdown files with numbered citations and structured status output for failures.] <br>
**Output Parameters:** [1D] <br>
**Other Properties Related to Output:** [Writes a plan, one findings file per subtopic, and a synthesized report to disk; returns paths or structured error status.] <br>

## Skill Version(s): <br>
3.0.3 (source: release evidence) <br>

## Ethical Considerations: <br>
Users should evaluate whether this skill is appropriate for their environment, review any generated or modified files before relying on them, and apply their organization's safety, security, and compliance requirements before deployment. <br>
