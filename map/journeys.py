# ruff: noqa: E501
"""The sequences of the praxis map: each starts at an entry point and names its steps.

Split from model.py so that neither file passes the 800-line limit. model.py imports
JOURNEYS from here; systemap puts this folder on the import path while it loads the model.
"""

from __future__ import annotations

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
        "The recorder takes and deletes the pre-snapshot that PreToolUse stored for this tool_use_id.",
    ),
    Step(
        ("Recorder",),
        (),
        ("Recorder", "Git"),
        "The recorder runs git diff against HEAD for the new snapshot.",
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
        ("HookEntry",),
        ("Ledger",),
        ("HookEntry", "Ledger"),
        "The hook entry records the hook event and exits 0.",
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
        "The recorder runs git diff against HEAD for the boundary snapshot.",
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
        "The recorder loads the session baseline and the sum of the agent deltas.",
    ),
    Step(
        ("Recorder",),
        ("Ledger",),
        ("", ""),
        "The recorder divides the lines of each file into agent, pre-existing, human, ambiguous, and reverted counts, and stores one row per file.",
        flow="rec-attributions",
    ),
    Step(
        ("HookEntry",),
        ("Ledger",),
        ("HookEntry", "Ledger"),
        "The hook entry records the hook event and exits 0.",
    ),
)

CONSENTED_INSTALL: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis setup claude --apply on a terminal.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ConsentedInstall"),
        "The command surface calls the install consent flow.",
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

DOCTOR: Steps = (
    Step(("Owner",), (), ("Owner", "CommandSurface"), "The owner types praxis doctor."),
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
        "The doctor counts the ledger rows in SQLite read-only mode.",
    ),
    Step(
        ("Doctor",),
        (),
        ("Doctor", "AgentCheck"),
        "The doctor finds the repository of the current directory and reports the number of attached agent processes.",
    ),
)

LEDGER_ROWS: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis attributions or praxis sessions.",
    ),
    Step(
        ("CommandSurface",),
        ("Ledger",),
        ("Ledger", "CommandSurface"),
        "The command surface prints the attribution rows, and prints unknown for each NULL count.",
    ),
)

JOURNEYS: tuple[Journey, ...] = (
    Journey(
        id="hook-delta",
        label="A tool call becomes an agent delta",
        starts="praxis hook (subcommand)",
        steps=HOOK_DELTA,
        covers=(
            '["subcommand","praxis.cli","praxis","hook"]',
            '["console_script","praxis.cli","main","praxis"]',
        ),
    ),
    Journey(
        id="stop-attribution",
        label="A Stop attributes the lines",
        starts="praxis hook (subcommand)",
        steps=STOP_ATTRIBUTION,
        covers=('["subcommand","praxis.cli","praxis","hook"]',),
    ),
    Journey(
        id="consented-install",
        label="The owner installs the hooks",
        starts="praxis setup (subcommand)",
        steps=CONSENTED_INSTALL,
        covers=('["subcommand","praxis.cli","praxis","setup"]',),
    ),
    Journey(
        id="doctor",
        label="The doctor reports the state",
        starts="praxis doctor (subcommand)",
        steps=DOCTOR,
        covers=('["subcommand","praxis.cli","praxis","doctor"]',),
    ),
    Journey(
        id="ledger-rows",
        label="The owner reads ledger rows",
        starts="CommandSurface",
        steps=LEDGER_ROWS,
        covers=(
            '["subcommand","praxis.cli","praxis","attributions"]',
            '["subcommand","praxis.cli","praxis","sessions"]',
        ),
    ),
)
