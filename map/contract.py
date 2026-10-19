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
        "A praxis hook exits 0 on every input, prints nothing to stdout, and never changes the output or exit code of the agent (AGENTS.md, invariant 7; src/praxis/hooks.py docstring).",
        governs=("HookEntry", "Recorder"),
        reason="The hook runs inside the agent session of the owner, so a failure in praxis must not change that session (src/praxis/hooks.py docstring).",
    ),
    Invariant(
        2,
        "Only src/praxis/store.py contains INSERT statements for the ledger tables. The derived-writes-only-in-store pre-commit hook refuses them in other modules (AGENTS.md, invariant 2; .pre-commit-config.yaml).",
        governs=("Ledger", "Recorder", "HookEntry"),
        reason="The CHECK constraints in schema.py and the writers in store.py are then the only way into the derived tables (AGENTS.md, invariant 2; src/praxis/store.py docstring).",
    ),
    Invariant(
        3,
        "The ledger stores hashes, counts, and identifiers. It never stores a line of a file, a prompt, or a tool input or response (AGENTS.md, invariant 5; src/praxis/schema.py docstring; docs/design/01-recorder.md, What is stored).",
        governs=(
            "Ledger",
            "Recorder",
            "ComponentMap",
            "Extractors",
            "TranscriptReader",
        ),
        reason="The integration suite reads the database bytes to show the absence, and the design keeps all text where the agent wrote it (AGENTS.md, invariant 5).",
    ),
    Invariant(
        4,
        "Without a baseline, the human, pre-existing, and reverted counts are NULL and the coverage is no_baseline. A CHECK constraint keeps the two together (AGENTS.md, invariant 4; src/praxis/schema.py:155-158).",
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
        "praxis setup PRINTS the hook block. With --apply or --remove, it writes exactly one named file after an interactive yes, and it refuses when stdin is not a terminal (AGENTS.md, invariant 8; src/praxis/install.py:189-197).",
        governs=("ConsentedInstall",),
        reason="Global configuration outside the repository and $PRAXIS_HOME is read-only unless the owner gives consent for each file (AGENTS.md, invariant 8).",
    ),
    Invariant(
        7,
        "praxis doctor opens the ledger in SQLite read-only mode and never creates it (src/praxis/doctor.py docstring and line 62).",
        governs=("Doctor",),
        reason="The doctor reports state and must not change the state that it reports (src/praxis/doctor.py docstring).",
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
        ),
        reason="The hook runs beside the agent, so each added import is a cost that the agent session pays (AGENTS.md, invariant 6).",
    ),
    Invariant(
        9,
        "Code that must run for a measurement runs in a scratch clone under $PRAXIS_HOME that the caller deletes. The working tree and .git of the owner are never written (AGENTS.md, invariant 5; src/praxis/extract.py scratch_clone docstring).",
        governs=("Extractors",),
        reason="A git worktree would write .git/worktrees into the repository of the owner, so the design uses git clone --local (docs/design/03-measurement.md, section 3.5).",
    ),
    Invariant(
        10,
        "Probe items are scored by deterministic, versioned extractors. No project test is a score or an item source (AGENTS.md, invariant 9; src/praxis/extract.py EXTRACTOR_VERSION).",
        governs=("Extractors", "ComponentMap"),
        reason="The owner rejected the write-it instrument because the project tests are code-level, often written by the agent, and absent for most design decisions (docs/design/02-instrument.md, Status).",
    ),
    Invariant(
        11,
        "The attached-agent check looks for claude or codex processes with a working directory in the repository. Where /proc is missing, it counts any such process anywhere (AGENTS.md, invariant 9; src/praxis/unassisted.py docstring and attached_agents).",
        governs=("AgentCheck",),
        reason="A probe answered with an agent attached is not unassisted, so the harness refuses it and records the refusal (AGENTS.md, invariant 9; docs/kickoff.md, Phase 3).",
    ),
)

# Each import rule gives an id, a kind, the components, and the reason.
IMPORT_RULES: tuple[ImportRule, ...] = ()

CONTRACT = Contract(invariants=INVARIANTS, import_rules=IMPORT_RULES)
