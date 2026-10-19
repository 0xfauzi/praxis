# ruff: noqa: E501
"""The sequences of the instrument verbs: praxis task, last, probe, components, and report.

Split from journeys.py so that no map file passes the 800-line limit. journeys.py lists
these sequences in JOURNEYS; systemap puts this folder on the import path while it loads
the model.
"""

from __future__ import annotations

from systemap import (  # type: ignore[import-not-found, unused-ignore]
    Journey,
    Step,
)

Steps = tuple[Step, ...]

_TASK_REGISTER: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "Before the prompt of the next task, the owner types praxis task.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "TaskRegistration"),
        "The command surface calls register with the committed seed and the clock reading of the command start.",
    ),
    Step(
        ("TaskRegistration",),
        (),
        ("TaskRegistration", "AgentCheck"),
        "The task registration finds the repository and its hashed id.",
    ),
    Step(
        ("TaskRegistration",),
        ("Ledger",),
        ("Ledger", "TaskRegistration"),
        "The task registration loads the frozen map, and it refuses when there is none.",
    ),
    Step(
        ("TaskRegistration",),
        (),
        ("TaskRegistration", "ComponentMap"),
        "The task registration parses the map and takes its product components.",
    ),
    Step(
        ("TaskRegistration",),
        (),
        ("TaskRegistration", "Extractors"),
        "It shows the feedback of the last closed task first, with the truth of each bound item read again now.",
    ),
    Step(
        ("TaskRegistration",),
        ("Ledger",),
        ("Ledger", "TaskRegistration"),
        "The task registration loads the closed-task outcomes and the unexposed items, and it draws the bound, related, and drawn items.",
    ),
    Step(
        ("TaskRegistration",),
        ("Ledger",),
        ("TaskRegistration", "Ledger"),
        "The task registration stores the task with its exposures, its arm, and its seed hash.",
    ),
    Step(
        ("Owner",),
        ("TaskRegistration",),
        ("Owner", "TaskRegistration"),
        "In the treatment arm, the owner answers a forecast for each sampled component and each bound item.",
    ),
    Step(
        ("TaskRegistration",),
        ("Ledger",),
        ("TaskRegistration", "Ledger"),
        "The task registration stores the forecast rows. A skipped forecast stays NULL.",
    ),
)

_LAST_FEEDBACK: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis last.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "TaskRegistration"),
        "The command surface calls last.",
    ),
    Step(
        ("TaskRegistration",),
        (),
        ("TaskRegistration", "AgentCheck"),
        "The task registration finds the repository and its hashed id.",
    ),
    Step(
        ("TaskRegistration",),
        ("Ledger",),
        ("Ledger", "TaskRegistration"),
        "The task registration loads the last closed task with its outcomes, divergences, bound exposures, and forecasts.",
    ),
    Step(
        ("TaskRegistration",),
        (),
        ("TaskRegistration", "ComponentMap"),
        "The task registration parses the frozen map, and it gives unknown truth when the map changed.",
    ),
    Step(
        ("TaskRegistration",),
        (),
        ("TaskRegistration", "Extractors"),
        "The task registration reads the D1 truth of each bound item now, and it prints the feedback.",
    ),
)

_PROBE: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis probe with a kind: structural, drift, decision, knockout, or pathfinder.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "Probe"),
        "The command surface refuses an unknown kind and calls administer.",
    ),
    Step(
        ("Probe",),
        (),
        ("Probe", "AgentCheck"),
        "The probe finds the repository and then the agent processes that work in it.",
    ),
    Step(
        ("Probe",),
        ("Ledger",),
        ("Probe", "Ledger"),
        "If an agent is attached, the probe stores an agent_attached flag, asks nothing, and exits 2.",
    ),
    Step(
        ("Probe",),
        ("Ledger",),
        ("Ledger", "Probe"),
        "The probe loads the frozen map and the rows of its kind, for example the due exposures.",
    ),
    Step(
        ("Probe",),
        (),
        ("Probe", "ComponentMap"),
        "The probe parses the map and takes its product components.",
    ),
    Step(
        ("Probe",),
        (),
        ("Probe", "Extractors"),
        "For a structural or Pathfinder probe, the probe reads the import graph of the working tree.",
    ),
    Step(
        ("Probe",),
        (),
        ("Probe", "Git"),
        "The probe reads HEAD, its tree hash, and whether the working tree is dirty.",
    ),
    Step(
        ("Probe",),
        (),
        ("Probe", "TranscriptReader"),
        "For a decision probe, the probe reads the transcript and voids a decision whose options changed or whose transcript is gone.",
    ),
    Step(
        ("Owner",),
        ("Probe",),
        ("Owner", "Probe"),
        "The owner answers each item in closed vocabulary. A free recall is shown and then discarded.",
    ),
    Step(
        ("Probe",),
        (),
        ("Probe", "Scoring"),
        "For a Pathfinder probe, the probe computes the owner network and its similarity to the import network.",
    ),
    Step(
        ("Probe",),
        ("Ledger",),
        ("Probe", "Ledger"),
        "The probe stores each answer with its truth. At end of input it stores an abandoned flag.",
    ),
)

_COMPONENTS_FREEZE: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis components freeze, with --reason when the map changes.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ItemPool"),
        "The command surface calls freeze.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "AgentCheck"),
        "The item pool finds the repository and its hashed id.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "ComponentMap"),
        "The item pool asks for the owner map of the repository, or else a derived map.",
    ),
    Step(
        ("ComponentMap",),
        (),
        ("PraxisHome", "ComponentMap"),
        "The component map loads components/<repo_id>.toml when the owner wrote one.",
    ),
    Step(
        ("ComponentMap",),
        (),
        ("ComponentMap", "Git"),
        "Without an owner map, the component map derives one from git ls-files.",
    ),
    Step(
        ("ItemPool",),
        ("Ledger",),
        ("Ledger", "ItemPool"),
        "The item pool loads the frozen map. A different map needs --reason, and too few components get a flag.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "Extractors"),
        "The item pool reads the import graph of the working tree with the new map.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "Git"),
        "The item pool reads the HEAD tree hash and whether the working tree is dirty.",
    ),
    Step(
        ("ItemPool",),
        ("Ledger",),
        ("ItemPool", "Ledger"),
        "The item pool stores the map and freezes it.",
    ),
    Step(
        ("Ledger",),
        (),
        ("Ledger", "ComponentMap"),
        "Before it stores the map, the ledger parses it with from_json.",
    ),
    Step(
        ("ItemPool",),
        ("Ledger",),
        ("ItemPool", "Ledger"),
        "The item pool writes a D1 item at epoch 0 for each parsed pair that has none.",
    ),
    Step(
        ("ItemPool",),
        ("Ledger",),
        ("Ledger", "ItemPool"),
        "The item pool loads the item counts and prints the supply.",
    ),
)

_COMPONENTS_SHOW: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis components.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ItemPool"),
        "The command surface calls show.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "AgentCheck"),
        "The item pool finds the repository and its hashed id.",
    ),
    Step(
        ("ItemPool",),
        ("Ledger",),
        ("Ledger", "ItemPool"),
        "If the ledger exists, the item pool loads the frozen map and the item counts.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "Git"),
        "The item pool lists the tracked paths with git ls-files.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "ComponentMap"),
        "The item pool prints the rules and the number of tracked paths that the map does not assign.",
    ),
)

_REPORT: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis report.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "AgentCheck"),
        "The command surface finds the repository and its hashed id.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "Report"),
        "The command surface opens the ledger and calls scores with the committed seed and draw count.",
    ),
    Step(
        ("Report",),
        ("Ledger",),
        ("Ledger", "Report"),
        "The report reads the frozen map, the tasks, the outcomes, the forecasts, and the probe responses.",
    ),
    Step(
        ("Report",),
        (),
        ("Report", "ComponentMap"),
        "The report parses the map, and it refuses a map with too few product components.",
    ),
    Step(
        ("TranscriptReader",),
        ("Report",),
        ("TranscriptReader", "Report"),
        "For the F1 references, the reader returns the names in each bound prompt, and the report keeps only component ids.",
    ),
    Step(
        ("Report",),
        (),
        ("Report", "Scoring"),
        "The report computes skill scores, decompositions, CORP bands, and randomization tests.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "Report"),
        "The command surface calls render. A refused word without a met rule writes nothing and exits 1.",
    ),
)

TASK_REGISTER = Journey(
    id="task-register",
    label="The owner registers a task",
    starts="praxis task (subcommand)",
    steps=_TASK_REGISTER,
    covers=('["subcommand","praxis.cli","praxis","task"]',),
)

LAST_FEEDBACK = Journey(
    id="last-feedback",
    label="The owner reads the last feedback",
    starts="praxis last (subcommand)",
    steps=_LAST_FEEDBACK,
    covers=('["subcommand","praxis.cli","praxis","last"]',),
)

PROBE = Journey(
    id="probe",
    label="The owner answers an unassisted probe",
    starts="praxis probe (subcommand)",
    steps=_PROBE,
    covers=('["subcommand","praxis.cli","praxis","probe"]',),
)

COMPONENTS_FREEZE = Journey(
    id="components-freeze",
    label="The owner freezes the component map",
    starts="praxis freeze (subcommand)",
    steps=_COMPONENTS_FREEZE,
    covers=('["subcommand","praxis.cli","praxis","freeze"]',),
)

COMPONENTS_SHOW = Journey(
    id="components-show",
    label="The owner reads the map and its pool",
    starts="praxis components (subcommand)",
    steps=_COMPONENTS_SHOW,
    covers=('["subcommand","praxis.cli","praxis","components"]',),
)

REPORT = Journey(
    id="report",
    label="The owner reads the report",
    starts="praxis report (subcommand)",
    steps=_REPORT,
    covers=('["subcommand","praxis.cli","praxis","report"]',),
)
