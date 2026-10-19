# ruff: noqa: E501
"""The architecture contract of the map in map/model.py.

The contract gives the rules of the system: the invariants and the import rules.
Each rule has a reason from the code, the documentation, or the history of the repository.
An agent changes the map freely. An agent changes a rule of this file only with a reason.
If a reviewer refuses a change to a rule, add the cause to the reason of the rule that stays.
"""

from __future__ import annotations

# The contract uses the systemap tool package.
from systemap import (  # type: ignore[import-not-found, unused-ignore]
    Contract,
    ImportRule,
    Invariant,
)

# Each invariant gives a number, the rule, the applicable components, and the reason.
INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        1,
        "A praxis hook exits 0 on every input and never changes the output or exit code of the agent. Its stdout is empty, except one fixed terminalSequence notice, recorder.NOTICE, at the Claude Code Stop that closes a registered task segment (AGENTS.md, invariant 7; src/praxis/hooks.py docstring and _notify; src/praxis/recorder.py NOTICE).",
        governs=("HookEntry", "Recorder"),
        reason="The hook runs inside the agent session of the owner, so a failure in praxis must not change that session (src/praxis/hooks.py docstring). Claude Code writes a terminalSequence to the owner terminal and never to the agent, so the one notice is permitted. The notice has no component id or other content (AGENTS.md, invariant 7; docs/design/03-measurement.md, section 5.4).",
    ),
    Invariant(
        2,
        "Only src/praxis/store.py contains INSERT statements for the ledger tables. The derived-writes-only-in-store pre-commit hook refuses them in other modules (AGENTS.md, invariant 2; .pre-commit-config.yaml).",
        governs=(
            "Ledger",
            "Recorder",
            "HookEntry",
            "ItemPool",
            "ScenarioRunner",
            "TaskRegistration",
            "Probe",
        ),
        reason="The CHECK constraints in schema.py and the writers in store.py are then the only way into the derived tables (AGENTS.md, invariant 2; src/praxis/store.py docstring).",
    ),
    Invariant(
        3,
        "The ledger stores hashes, counts, and identifiers. It never stores a line of a file, a prompt, or a tool input or response. A scenario argv is stored only as a hash (AGENTS.md, invariant 5; src/praxis/schema.py docstring; src/praxis/scenarios.py docstring; docs/design/01-recorder.md, What is stored).",
        governs=(
            "Ledger",
            "Recorder",
            "ComponentMap",
            "Extractors",
            "ScenarioRunner",
            "TranscriptReader",
            "TaskRegistration",
            "Probe",
            "Report",
        ),
        reason="The integration suite reads the database bytes to show the absence, and the design keeps all text where the agent wrote it (AGENTS.md, invariant 5).",
    ),
    Invariant(
        4,
        "Without a baseline, the human, pre-existing, and reverted counts are NULL and the coverage is no_baseline. A CHECK constraint keeps the two together (AGENTS.md, invariant 4; src/praxis/schema.py:162-165).",
        governs=("Recorder", "Ledger"),
        reason="Unknown stays unknown: a missing value is None through the model, the ledger, and the report (AGENTS.md, invariant 4; docs/design/01-recorder.md, Attribution).",
    ),
    Invariant(
        5,
        "Attribution matches multisets of line hashes: two identical lines are two lines, and an agent line that the diff no longer holds is ambiguous, not human (AGENTS.md, invariant 13; src/praxis/gitdiff.py docstring).",
        governs=("Recorder",),
        reason="Content-identical lines from two authors are indistinguishable, so only counts can divide them (AGENTS.md, invariant 13; docs/design/01-recorder.md, The snapshot).",
    ),
    Invariant(
        6,
        "praxis setup PRINTS the hook block. With --apply or --remove, it writes exactly one named file after an interactive yes, and it refuses when stdin is not a terminal (AGENTS.md, invariant 8; src/praxis/install.py:195-203).",
        governs=("ConsentedInstall",),
        reason="Global configuration outside the repository and $PRAXIS_HOME is read-only unless the owner gives consent for each file (AGENTS.md, invariant 8).",
    ),
    Invariant(
        7,
        "praxis doctor never creates the ledger and counts its rows in SQLite read-only mode. It asks probe.due for probes due only when the ledger is at SCHEMA_VERSION, so the Store that probe.due opens runs no migration. The scenario summary that it prints opens a Store only at SCHEMA_VERSION too (src/praxis/doctor.py docstring, _ledger_state, and _due; src/praxis/scenarios.py summary and _last_runs).",
        governs=("Doctor", "Probe", "ScenarioRunner"),
        reason="The doctor reports state and must not change the state that it reports (src/praxis/doctor.py docstring). Store.open runs the DDL and migrate when PRAGMA user_version differs from SCHEMA_VERSION. Thus the version check keeps the probes-due read and the scenario summary from changing the ledger (src/praxis/store.py, Store.open; src/praxis/scenarios.py _last_runs).",
    ),
    Invariant(
        8,
        "The dependencies list in pyproject.toml is empty. Praxis uses the Python standard library only (AGENTS.md, invariant 6; pyproject.toml line 15).",
        governs=(
            "CommandSurface",
            "HookEntry",
            "Recorder",
            "Ledger",
            "ConsentedInstall",
            "Doctor",
            "ComponentMap",
            "Extractors",
            "TranscriptReader",
            "AgentCheck",
            "Scoring",
            "ItemPool",
            "ScenarioRunner",
            "TaskRegistration",
            "Probe",
            "Report",
        ),
        reason="The hook runs beside the agent, so each added import is a cost that the agent session pays (AGENTS.md, invariant 6).",
    ),
    Invariant(
        9,
        "Code that must run for a measurement runs in a scratch clone under $PRAXIS_HOME that the caller deletes. The working tree and .git of the owner are never written. praxis scenarios run gives each scenario its own clone and deletes it after the scenario, also on failure. PRAXIS_HOME points into the clone (AGENTS.md, invariant 5; src/praxis/extract.py scratch_clone docstring; src/praxis/scenarios.py docstring and _run_one).",
        governs=("Extractors", "ScenarioRunner"),
        reason="A git worktree would write .git/worktrees into the repository of the owner, so the design uses git clone --local (docs/design/03-measurement.md, section 3.5).",
    ),
    Invariant(
        10,
        "Probe items are scored by deterministic, versioned extractors. No project test is a score or an item source (AGENTS.md, invariant 9; src/praxis/extract.py EXTRACTOR_VERSION and X2_VERSION).",
        governs=("Extractors", "ComponentMap", "ItemPool", "ScenarioRunner", "Probe"),
        reason="The owner rejected the write-it instrument because the project tests are code-level, often written by the agent, and absent for most design decisions (docs/design/02-instrument.md, Status).",
    ),
    Invariant(
        11,
        "The attached-agent check looks for claude or codex processes with a working directory in the repository. Where /proc is missing, it counts any such process anywhere. praxis probe runs the check first, and when it finds an agent it records an agent_attached flag and asks nothing (AGENTS.md, invariant 9; src/praxis/unassisted.py attached_agents; src/praxis/probe.py docstring and _administer).",
        governs=("AgentCheck", "Probe"),
        reason="A probe answered with an agent attached is not unassisted, so the harness refuses it and records the refusal (AGENTS.md, invariant 9; docs/kickoff.md, Phase 3).",
    ),
    Invariant(
        12,
        "praxis report writes nothing when its text holds the words cause, impact, or would and no pre-registered rule is met. The text names each rule with its result (AGENTS.md, invariant 3; src/praxis/render.py docstring, _REFUSED, and render).",
        governs=("Report",),
        reason="A claim class is never upgraded. A report may narrow a claim or drop it, and it refuses these words unless the pre-registered decision rule was met (AGENTS.md, invariant 3; docs/preregistration.md).",
    ),
    Invariant(
        13,
        "Owner answers are closed vocabulary. Other typed text is asked again and dropped, and a free recall is shown and discarded, with not even its length kept (AGENTS.md, invariant 5; src/praxis/task.py docstring; src/praxis/probe.py docstring).",
        governs=("TaskRegistration", "Probe"),
        reason="No prompt, assistant, or code text is persisted, and the owner input that praxis keeps is grid probabilities, component ids, option indices, and reason codes (AGENTS.md, invariant 5).",
    ),
)

# Each import rule gives an id, a kind, the components, and the reason.
IMPORT_RULES: tuple[ImportRule, ...] = ()

CONTRACT = Contract(invariants=INVARIANTS, import_rules=IMPORT_RULES)
