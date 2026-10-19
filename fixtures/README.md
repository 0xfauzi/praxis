# Recorded hook payloads

`claude/2.1.257/*.json`: one body per hook event, taken verbatim from telltale's E01
recording of Claude Code 2.1.257 (scenario S1), with the two machine paths already replaced
by `<repo>` and `<home>` by that experiment's sanitizer. The integration suite substitutes a
temporary repository and home back in, so the recorder is driven by the field names and
shapes a real session sends, not by hand-written approximations of them.

`claude/2.1.303/*.json`: recorded on this machine from Claude Code 2.1.303 run with `-p`
against a scratch repository (2026-10-19), paths replaced by `<repo>` and `<home>`. Every
event carries `prompt_id` except SessionStart, which carries neither `prompt_id` nor
`permission_mode`; subagent tool events carry `agent_id` and `agent_type`; no payload
carries the CLI version. AskUserQuestion and ExitPlanMode are unavailable in `-p` and no
Codex binary is installed here, so those payloads are not recorded: the integration suite
builds them from the documented fields (code.claude.com/docs/en/hooks and the Codex hooks
reference) and says so where it does. The same recordings showed that 2.1.303 writes each
Stop hook's command path into a `stop_hook_summary` entry and a hook's stdout into a
`hook_success` attachment of the on-disk JSONL.

These are evidence. Do not reformat, reorder keys or edit values; a new agent version gets a
new directory.
