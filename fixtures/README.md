# Recorded hook payloads

`claude/2.1.257/*.json`: one body per hook event, taken verbatim from telltale's E01
recording of Claude Code 2.1.257 (scenario S1), with the two machine paths already replaced
by `<repo>` and `<home>` by that experiment's sanitizer. The integration suite substitutes a
temporary repository and home back in, so the recorder is driven by the field names and
shapes a real session sends, not by hand-written approximations of them.

These are evidence. Do not reformat, reorder keys or edit values; a new agent version gets a
new directory.
