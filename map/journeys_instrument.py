# ruff: noqa: E501
"""The sequences of the instrument verbs: praxis task, last, probe, components, scenarios, and report.

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
        ("Ledger",),
        ("TaskRegistration", "Probe"),
        "After the due items, a structural probe ranks the unprobed items with the committed seed and adds this week's validation items and retests.",
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
        "The probe reads HEAD, its tree hash, and whether the working tree is dirty. A knockout counts only scenario runs of the HEAD tree.",
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
        ("ComponentMap",),
        (),
        ("ComponentMap", "Extractors"),
        "For a flat package, the component map groups the children by name prefix and by import edges.",
    ),
    Step(
        ("ItemPool",),
        ("Ledger",),
        ("Ledger", "ItemPool"),
        "The item pool loads the frozen map. A different map needs --reason. Fewer than 5 or more than 30 product components refuse the freeze.",
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
        "The item pool writes an item at the next epoch for each D1 pair whose truth flipped. Then it writes an epoch 0 item for each new pair.",
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
        "The item pool prints the rules, the number of unassigned tracked paths, and the unassigned Python files beside mapped files.",
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
        "The report reads the frozen map, the tasks, the outcomes, the forecasts, and the probe responses, also those of never-drawn validation items.",
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
        "The report computes skill scores, decompositions, CORP bands, the randomization tests, and the pipeline placebo.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "Report"),
        "The command surface calls render. A placebo below ALPHA suspends the arm contrasts. A refused word without a met rule writes nothing.",
    ),
)

_COMPONENTS_DERIVE: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis components derive, with --write to save the draft.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ItemPool"),
        "The command surface calls draft.",
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
        "The item pool derives the mechanical map and prints it in the owner's toml format.",
    ),
    Step(
        ("ComponentMap",),
        (),
        ("ComponentMap", "Git"),
        "The component map lists the tracked paths with git ls-files.",
    ),
    Step(
        ("ComponentMap",),
        (),
        ("ComponentMap", "Extractors"),
        "For a flat package, the component map groups the children by name prefix and by import edges.",
    ),
    Step(
        ("ItemPool",),
        (),
        ("ItemPool", "PraxisHome"),
        "With --write, the item pool saves the draft as the owner map, and it refuses when that file exists.",
    ),
)

_SCENARIOS_RUN: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "PraxisHome"),
        "The owner names each scenario id and argv in scenarios/<repo_id>.toml.",
    ),
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis scenarios run.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ScenarioRunner"),
        "The command surface calls run with the clock reading of the command start.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "AgentCheck"),
        "The scenario runner finds the repository and its hashed id.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        flow="scen-file",
        say="The scenario runner loads the scenario file, and it refuses a missing file or a file of another shape.",
    ),
    Step(
        ("ScenarioRunner",),
        ("Ledger",),
        ("Ledger", "ScenarioRunner"),
        "The scenario runner loads the frozen map. Without a ledger or a frozen map, it refuses and writes nothing.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "Git"),
        "The scenario runner refuses when HEAD has no tree, because each scenario runs in a clone of HEAD.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "Extractors"),
        "For each scenario, the scenario runner makes a scratch clone of HEAD.",
    ),
    Step(
        ("Extractors",),
        (),
        ("Extractors", "Git"),
        "The extractors run git clone --local and remove the remote of the clone.",
    ),
    Step(
        ("Extractors",),
        (),
        ("Extractors", "PraxisHome"),
        "The clone is a new directory under $PRAXIS_HOME/scratch.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "Git"),
        "The scenario runner reads the tree hash of the clone and lists its tracked files.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "ComponentMap"),
        "The scenario runner puts the clone root and each package root of a product component first on PYTHONPATH.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "Extractors"),
        "With PRAXIS_HOME inside the clone, run_scenario traces the scenario and assigns each traced file to a component.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "PraxisHome"),
        "The scenario runner deletes the clone, also when the scenario fails.",
    ),
    Step(
        ("ScenarioRunner",),
        ("Ledger",),
        ("ScenarioRunner", "Ledger"),
        "The scenario runner stores the run with its outcome, components, call edges, argv hash, and the tree hash of the clone.",
    ),
)

_SCENARIOS_SHOW: Steps = (
    Step(
        ("Owner",),
        (),
        ("Owner", "CommandSurface"),
        "The owner types praxis scenarios.",
    ),
    Step(
        ("CommandSurface",),
        (),
        ("CommandSurface", "ScenarioRunner"),
        "The command surface calls show.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "AgentCheck"),
        "The scenario runner finds the repository and its hashed id.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        flow="scen-file",
        say="The scenario runner loads the scenario file. Without one, it prints that no scenario is configured.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        flow="scen-version",
        say="The scenario runner reads the ledger version in read-only mode, and at another version it gives unknown.",
    ),
    Step(
        ("ScenarioRunner",),
        ("Ledger",),
        ("Ledger", "ScenarioRunner"),
        "At the current version, the scenario runner loads the frozen map and the last run of each scenario on it.",
    ),
    Step(
        ("ScenarioRunner",),
        (),
        ("ScenarioRunner", "Git"),
        "The scenario runner reads the HEAD tree and prints each last run with its tree and a changed argv.",
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

COMPONENTS_DERIVE = Journey(
    id="components-derive",
    label="The owner drafts an owner map",
    starts="praxis derive (subcommand)",
    steps=_COMPONENTS_DERIVE,
    covers=('["subcommand","praxis.cli","praxis","derive"]',),
)

SCENARIOS_RUN = Journey(
    id="scenarios-run",
    label="The owner runs the scenarios",
    starts="praxis run (subcommand)",
    steps=_SCENARIOS_RUN,
    covers=('["subcommand","praxis.cli","praxis","run"]',),
)

SCENARIOS_SHOW = Journey(
    id="scenarios-show",
    label="The owner reads the scenario runs",
    starts="praxis scenarios (subcommand)",
    steps=_SCENARIOS_SHOW,
    covers=('["subcommand","praxis.cli","praxis","scenarios"]',),
)
