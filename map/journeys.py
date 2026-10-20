# ruff: noqa: E501
"""The sequences of the praxis map: each starts at an entry point and names its steps.

Split from model.py so that neither file passes the 800-line limit. model.py imports
JOURNEYS from here; systemap puts this folder on the import path while it loads the model.
The sequences of the instrument commands are in journeys_instrument.py, for the same limit.
"""

from __future__ import annotations

import journeys_instrument
from systemap import (  # type: ignore[import-not-found, unused-ignore]
    Journey,
    Step,
)

# Each sequence starts at an entry point. Each step identifies its components and flow.
Steps = tuple[Step, ...]

HOOK_DELTA: Steps = (
    Step(
        ("CodingAgent",),
        (),
        ("CodingAgent", "CommandSurface"),
        "After an Edit, Write, MultiEdit, NotebookEdit, or Bash call, the agent runs praxis hook.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "HookEntry"),
        "The command surface calls the hook entry for the hook verb.",
    ),
    Step(
        ("CodingAgent",),
        ("HookEntry",),
        ("CodingAgent", "HookEntry"),
        "The hook entry reads the PostToolUse payload from stdin.",
    ),
    Step(
        ("HookEntry",),
        (),
        ("HookEntry", "Recorder"),
        "The hook entry calls the recorder with the parsed payload.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("Ledger", "Recorder"),
        "The recorder takes and deletes the pre-snapshot that PreToolUse stored for this tool_use_id, and it loads the session base commit.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "Git"),
        "The recorder reads HEAD, makes sure that it descends from the base commit, and runs git diff against the base commit.",
    ),
    Step(
        ("Recorder",),
        (),
        ("WorkingTree", "Recorder"),
        "The recorder hashes the lines of each untracked file.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder stores each changed line-hash count as an agent delta.",
        flow="rec-hashes",
    ),
    Step(
        ("Recorder",),
        ("HookEntry",),
        ("Recorder", "HookEntry"),
        "The recorder returns the outcome, a note, and the HEAD sha.",
    ),
    Step(
        ("HookEntry",),
        ("Ledger",),
        ("HookEntry", "Ledger"),
        "The hook entry records the hook event with the HEAD sha and exits 0.",
    ),
    Step(
        ("Ledger",),
        (),
        ("Ledger", "PraxisHome"),
        "The ledger writes the rows to ledger.db.",
    ),
)

STOP_ATTRIBUTION: Steps = (
    Step(
        ("CodingAgent",),
        (),
        ("CodingAgent", "CommandSurface"),
        "When the agent stops, it runs praxis hook.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "HookEntry"),
        "The command surface calls the hook entry for the hook verb.",
    ),
    Step(
        ("CodingAgent",),
        ("HookEntry",),
        ("CodingAgent", "HookEntry"),
        "The hook entry reads the Stop payload from stdin.",
    ),
    Step(
        ("HookEntry",),
        (),
        ("HookEntry", "Recorder"),
        "The hook entry calls the recorder with the parsed payload.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "Git"),
        "The recorder runs git diff against the session base commit for the boundary snapshot.",
    ),
    Step(
        ("Recorder",),
        (),
        ("WorkingTree", "Recorder"),
        "The recorder hashes the lines of each untracked file.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("Ledger", "Recorder"),
        "The recorder loads the session baseline, the sum of the agent deltas, and the HEAD of each earlier hook event.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder divides the lines of each file into agent, pre-existing, human, ambiguous, and reverted counts, and stores one row per file.",
        flow="rec-attributions",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("Ledger", "Recorder"),
        "The recorder loads the task bound to this session and the files that agent deltas touched since the previous Stop.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "ComponentMap"),
        "If an agent delta touched Python files since the previous Stop, or a task is bound, the recorder parses the frozen map.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "Extractors"),
        "The recorder calls static_imports on each touched Python file.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder stores the component imports of each scanned file.",
        flow="rec-edges",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("Ledger", "Recorder"),
        "For a bound task, the recorder loads the earlier edge scans of the files touched since the bind.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "After an agent delta since the bind, the recorder stores one outcome per product component and closes the task in one transaction. A changed map voids the task.",
        flow="rec-tasks",
    ),
    Step(
        ("Recorder",),
        ("HookEntry",),
        ("Recorder", "HookEntry"),
        "For a payload without turn_id, which marks Claude Code, the recorder returns the notice flag with its result.",
    ),
    Step(
        ("HookEntry",),
        (),
        ("HookEntry", "CodingAgent"),
        "The hook entry prints the one fixed terminalSequence notice.",
    ),
    Step(
        ("HookEntry",),
        ("Ledger",),
        ("HookEntry", "Ledger"),
        "The hook entry records the hook event and exits 0.",
    ),
)

PROMPT_BIND: Steps = (
    Step(
        ("CodingAgent",),
        (),
        ("CodingAgent", "CommandSurface"),
        "When the owner submits a prompt, the agent runs praxis hook.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "HookEntry"),
        "The command surface calls the hook entry for the hook verb.",
    ),
    Step(
        ("CodingAgent",),
        ("HookEntry",),
        ("CodingAgent", "HookEntry"),
        "The hook entry reads the UserPromptSubmit payload with its prompt_id.",
    ),
    Step(
        ("HookEntry",),
        (),
        ("HookEntry", "Recorder"),
        "The hook entry calls the recorder with the parsed payload.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "Git"),
        "The recorder runs git diff against the session base commit for the boundary snapshot.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder stores one attribution row per file.",
        flow="rec-attributions",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("Ledger", "Recorder"),
        "The recorder loads the task bound to this session, or else the open task of the repository.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder binds an unbound task registered before this prompt. A different prompt in a bound session gets an owner_answered_question flag.",
        flow="rec-tasks",
    ),
    Step(
        ("HookEntry",),
        ("Ledger",),
        ("HookEntry", "Ledger"),
        "The hook entry records the hook event and exits 0.",
    ),
)

DECISION_POINTER: Steps = (
    Step(
        ("CodingAgent",),
        (),
        ("CodingAgent", "CommandSurface"),
        "After an AskUserQuestion or ExitPlanMode call returns, Claude Code runs praxis hook.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "HookEntry"),
        "The command surface calls the hook entry for the hook verb.",
    ),
    Step(
        ("CodingAgent",),
        ("HookEntry",),
        ("CodingAgent", "HookEntry"),
        "The hook entry reads the PostToolUse payload from stdin.",
    ),
    Step(
        ("HookEntry",),
        (),
        ("HookEntry", "Recorder"),
        "The hook entry calls the recorder with the parsed payload.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "TranscriptReader"),
        "The recorder hashes the option labels, or the plan of ExitPlanMode, with options_hash.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder stores the option count, the chosen index, and the hash, with unobservable coverage when the shape does not fit.",
        flow="rec-decisions",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "If a task is bound to this session, the recorder flags owner_answered_question.",
        flow="rec-tasks",
    ),
    Step(
        ("HookEntry",),
        ("Ledger",),
        ("HookEntry", "Ledger"),
        "The hook entry records the hook event and exits 0.",
    ),
)

INSTALL: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis install claude on a terminal.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ConsentedInstall"),
        "The command surface calls apply.",
    ),
    Step(
        ("Owner",),
        ("ConsentedInstall",),
        ("Owner", "ConsentedInstall"),
        "The install refuses without a terminal, shows the change, and reads the answer of the owner.",
    ),
    Step(
        ("ConsentedInstall",),
        (),
        ("ConsentedInstall", "PraxisHome"),
        "After a yes, the install copies the previous file to settings-backups.",
    ),
    Step(
        ("ConsentedInstall",),
        (),
        ("ConsentedInstall", "CodingAgent"),
        "The install writes the merged hook block to ~/.claude/settings.json.",
    ),
)

SHOW_STATUS: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis show status.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "Doctor"),
        "The command surface calls the doctor.",
    ),
    Step(
        ("Doctor",),
        (),
        ("Doctor", "ConsentedInstall"),
        "The doctor loads each agent settings file and counts the praxis hook events.",
    ),
    Step(
        ("Doctor",),
        (),
        ("PraxisHome", "Doctor"),
        "The doctor counts the ledger rows, frozen maps, tasks, void reasons, and flags in SQLite read-only mode.",
    ),
    Step(
        ("Doctor",),
        (),
        ("Doctor", "AgentCheck"),
        "The doctor finds the repository of the current directory and reports the number of attached agent processes.",
    ),
    Step(
        ("Doctor",),
        ("Probe",),
        ("Doctor", "Probe"),
        "If the ledger is at the current schema version, the doctor reports the number of items due now for praxis test structure.",
    ),
    Step(
        ("Doctor",),
        (),
        ("Doctor", "ScenarioRunner"),
        "The doctor asks the scenario runner for the scenario summary line.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        flow="scen-version",
        say="The scenario runner reads the ledger version in read-only mode, and it opens a Store only at the current version.",
    ),
)

SHOW_LINES: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis show lines or praxis show sessions.",
    ),
    Step(
        ("CommandSurface",),
        ("Ledger",),
        ("Ledger", "CommandSurface"),
        "The command surface prints the line counts of each file at each hook event, and prints unknown for each NULL count.",
    ),
)

SHOW_SUPPLY: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis show supply, with --weeks N and --json as options.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "Supply"),
        "The command surface calls show with the home directory, the --weeks value, and the --json flag.",
    ),
    Step(
        ("Supply",),
        (),
        ("CommandSurface", "Supply"),
        "The supply count refuses a --weeks value that is not a whole number of 1 or more, and the command exits 2. Without --weeks, it counts 4 weeks.",
    ),
    Step(
        ("Supply",),
        (),
        ("CodingAgent", "Supply"),
        "The supply count lists the transcript files under ~/.claude/projects. It counts the Codex rollout files and does not read them.",
    ),
    Step(
        ("TranscriptReader",),
        ("Supply",),
        ("TranscriptReader", "Supply"),
        "For each file, the reader returns the prompts and the tool calls with their file suffixes. An unreadable file is counted and skipped.",
    ),
    Step(
        ("Supply",),
        (),
        ("CodingAgent", "Supply"),
        "The supply count reads the cwd of each prompt line in the file, and keeps it only in memory.",
    ),
    Step(
        ("Supply",),
        (),
        ("Supply", "Recorder"),
        "For each new cwd, the supply count gets the git root and its hashed id. A cwd outside a repository is counted apart.",
    ),
    Step(
        ("Supply",),
        (),
        ("CommandSurface", "Supply"),
        "The supply count prints one column for each ISO week and a total, or one JSON object with --json. An unknown value prints as unknown, not 0.",
    ),
)

JOURNEYS: tuple[Journey, ...] = (
    Journey(
        id="hook-delta",
        label="A tool call becomes an agent delta",
        starts="praxis (console script)",
        steps=HOOK_DELTA,
        covers=(
            '["console_script","praxis.cli","main","praxis"]',
            '["main_function","praxis.cli","main","main"]',
        ),
    ),
    Journey(
        id="stop-attribution",
        label="A Stop attributes the lines and closes a segment",
        starts="praxis (console script)",
        steps=STOP_ATTRIBUTION,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
    Journey(
        id="prompt-bind",
        label="A prompt binds the registered task",
        starts="praxis (console script)",
        steps=PROMPT_BIND,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
    Journey(
        id="decision-pointer",
        label="An agent question becomes a decision row",
        starts="praxis (console script)",
        steps=DECISION_POINTER,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
    Journey(
        id="install",
        label="The owner installs the hooks",
        starts="praxis (console script)",
        steps=INSTALL,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
    Journey(
        id="show-status",
        label="The owner shows the status",
        starts="praxis (console script)",
        steps=SHOW_STATUS,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
    Journey(
        id="show-lines",
        label="The owner shows the line counts",
        starts="praxis (console script)",
        steps=SHOW_LINES,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
    journeys_instrument.START_TASK,
    journeys_instrument.SHOW_TASK,
    journeys_instrument.TEST,
    journeys_instrument.SET_COMPONENTS,
    journeys_instrument.SHOW_COMPONENTS,
    journeys_instrument.MAKE_COMPONENTS,
    journeys_instrument.RUN_SCENARIOS,
    journeys_instrument.SHOW_SCENARIOS,
    journeys_instrument.SHOW_REPORT,
    Journey(
        id="show-supply",
        label="The owner shows the transcript supply",
        starts="praxis (console script)",
        steps=SHOW_SUPPLY,
        covers=('["console_script","praxis.cli","main","praxis"]',),
    ),
)
