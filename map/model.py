# ruff: noqa: E501
"""The system map of praxis.

All reader text must obey the writing profile in systemap.toml ([writing] profile).
Before you write names or sentences, read the language policy of the installed systemap skill.
The "plain" profile has no dictionary step. The "asd-ste100" profile needs the ASD dictionary.

The extractor supplies source facts. This file records component purposes and flow claims.
Each component identifies its source modules and an applicable public entry point.
Before a maintainer records support for a claim, the maintainer must examine the source.
"""

from __future__ import annotations

# The generated model uses the systemap tool package.
import flows
import journeys
from systemap import (  # type: ignore[import-not-found, unused-ignore]
    Component,
    Container,
    Layer,
    Meaning,
    Model,
    Region,
)

# The flows are in flows.py, so that no map file passes the 800-line limit.

# Containers give system boundaries.
# The actors are outside the praxis source. The praxis source runs as one process per command.
CONTAINERS = (
    Container(
        id="outside",
        label="OUTSIDE",
        sub="agent, owner, git, files",
        box=(16, 16, 190, 674),
        tone="host",
    ),
    Container(
        id="system",
        label="PRAXIS",
        sub="one process per command",
        box=(230, 16, 848, 860),
        tone="server",
    ),
)

# A region contains a group of components with one function.
# The record, ledger, and instrument regions follow the layers of
# docs/design/03-measurement.md section 12. The analysis region holds the protocol
# modules (report, scoring) and the transcript reader that the probe and the report call.
# The policy layer has no module yet.
REGIONS = (
    Region(id="record", label="RECORD", box=(250, 412, 190, 204), container="system"),
    Region(id="ledger", label="LEDGER", box=(868, 412, 190, 112), container="system"),
    Region(
        id="commands", label="COMMANDS", box=(868, 652, 190, 112), container="system"
    ),
    Region(
        id="owner",
        label="SETUP AND CHECKS",
        box=(250, 652, 190, 204),
        container="system",
    ),
    Region(
        id="measure", label="INSTRUMENT", box=(250, 80, 570, 296), container="system"
    ),
    Region(
        id="analysis", label="ANALYSIS", box=(868, 80, 190, 296), container="system"
    ),
)

COMPONENTS: tuple[Component, ...] = (
    # Actors. They have no source modules.
    Component(
        id="CodingAgent",
        does="Claude Code or Codex. Its hook configuration runs praxis hook for each hook event and writes the event payload as JSON on stdin. Claude Code writes the hook notice to the owner terminal.",
        kind="actor",
        container="outside",
        x=36,
        y=626,
    ),
    Component(
        id="Owner",
        does="The person who owns the repositories. The owner types praxis commands on a terminal, answers the consent prompt, and answers task forecasts and probe items.",
        kind="actor",
        container="outside",
        x=36,
        y=534,
    ),
    Component(
        id="Git",
        does="The git program. Praxis runs it as a subprocess and reads its output. It never runs git with a shell.",
        kind="actor",
        container="outside",
        x=36,
        y=258,
    ),
    Component(
        id="WorkingTree",
        does="The checkout of the owner's repository on disk. Praxis reads untracked files and Python source from it.",
        kind="actor",
        container="outside",
        x=36,
        y=350,
    ),
    Component(
        id="PraxisHome",
        does="The directory $PRAXIS_HOME (default ~/.praxis). It holds ledger.db, settings-backups/, owner component maps as components/<repo_id>.toml, owner scenario files as scenarios/<repo_id>.toml, and scratch clones under scratch/.",
        kind="actor",
        container="outside",
        x=36,
        y=442,
    ),
    # Source components.
    Component(
        id="CommandSurface",
        does="Parses the praxis command line, a verb and an object, and calls the handler of each command from one command table. Each handler imports its praxis module. The command surface prints line and session rows from the ledger.",
        interface="main(argv) -> int",
        implemented_by=("praxis.cli", "praxis"),
        entry="main",
        region="commands",
        note="The package module praxis holds only the docstring and __version__, which the command surface prints for --version. The command table builds the argparse parsers in a loop, so the extractor finds no subcommand entry point.",
        x=888,
        y=692,
        source_review="56d7325b3f9fcc4d926207d410439e9aa3afcb9f3f03acf5085c695867cca929",
    ),
    Component(
        id="HookEntry",
        does="Runs once for each hook event. It reads the payload, calls the recorder, records the hook event, and exits 0 on every input. Its stdout stays empty, except one fixed notice when the recorder closes a task segment.",
        interface="run(stdin, praxis_home) -> int",
        implemented_by=("praxis.hooks",),
        entry="run",
        region="record",
        x=270,
        y=544,
        source_review="e02cbea43c40f4d5f78b7b8557afd1e640f688c24ff01e23ee52b61b09a28d9c",
    ),
    Component(
        id="Recorder",
        does="Snapshots the working tree with git against the session base commit. It stores tool-call deltas, attributions, decisions, and file edges, and it binds, closes, and voids registered tasks.",
        interface="handle(payload, store, now) -> Result",
        implemented_by=("praxis.recorder", "praxis.gitdiff"),
        entry="handle",
        region="record",
        note="A HEAD that is not a descendant of the base commit makes the snapshot unobservable and voids a bound task. The notice flag is set only at the Stop that closes a segment, for a payload without turn_id. The recorder imports components, extract, and transcript inside the functions that use them.",
        x=270,
        y=452,
        source_review="b382c68b5d3816f7a356f10c1010b7c6f1f92e89ce7ffd13ed1bab2d053573e6",
    ),
    Component(
        id="Ledger",
        does="The SQLite ledger under $PRAXIS_HOME. praxis.schema holds its DDL and closed vocabularies, and praxis.store holds every INSERT. It keeps recorder rows, component maps, scenario runs, repository starts, tasks, probes, forecasts, decisions, and flags.",
        interface="Store.open / Store.task_register / Store.segment_close / Store.probe_response",
        implemented_by=("praxis.store", "praxis.schema"),
        entry="Store",
        kind="store",
        region="ledger",
        note="Each table in schema.CLAIMS has a CHECK that fixes its one claim class and refuses an empty source list. The attributions table accepts any claim class, and hook_events has none. Triggers make the measurement tables and repository_starts append-only, and a task or a map sets each transition column once. repository_starts has one row per repository, and its start week must be one of its candidate weeks. A task fixes forced_control at registration. segment_close writes the outcomes and the task close in one transaction. A scenario run must name the HEAD tree it traced, unless its outcome is not_python. On a ledger that already held runs, migrate adds tree_hash without that CHECK. migrate also adds forced_control and makes the trigger for the fixed task columns again. probe_items_unprobed leaves out an item that a probe asked, so the binding pool and the validation draw hold blind items only. The survival lookup intersects agent line-hash multisets with a probe multiset, so two identical lines count as two. When PRAGMA user_version differs from SCHEMA_VERSION, Store.open runs the DDL and then migrate.",
        x=888,
        y=452,
        source_review="e961f5f375c4aac7b3c00295c2c07ab91e322a2492d2306eb4c0c152f97bf84a",
    ),
    Component(
        id="ConsentedInstall",
        does="Prints the hook block for Claude Code or Codex. For praxis install or praxis remove, it writes one settings file after the owner types yes on a terminal. The Claude Code PostToolUse matcher also selects AskUserQuestion and ExitPlanMode.",
        interface="apply(agent, remove, home, praxis_home, stdin, out) -> int",
        implemented_by=("praxis.install",),
        entry="apply",
        region="owner",
        x=270,
        y=692,
        source_review="4d9b8c232d8f287b8a90218d0ea0714c636762047076f5fa14f7e2cd8c2230c8",
    ),
    Component(
        id="Doctor",
        does="For praxis show status, reports the installed hooks, the ledger counts, the frozen maps, tasks by status, the binding rate, void reasons, and flags. It also reports attached agents, items due, and the scenarios with their last runs. It never creates the ledger.",
        interface="run(cwd, home, praxis_home, out) -> int",
        implemented_by=("praxis.doctor",),
        entry="run",
        region="owner",
        note="Counts come from a SQLite read-only connection. Items due come from probe.due, which opens a Store. The doctor asks for them only at SCHEMA_VERSION, so no migration can run. The scenario line comes from scenarios.summary, which also opens a Store only at SCHEMA_VERSION. The doctor imports hooks, probe, and scenarios inside the functions that use them.",
        x=270,
        y=784,
        source_review="eb3f4e8bda6ec04606e1023d794fe8c448dc9e2789dd9aa8d1be78dd8e93ca1e",
    ),
    Component(
        id="ComponentMap",
        does="Makes the component map of a repository: ordered glob rules that give each path one component. It derives the map from git ls-files or loads the owner's file, and it prints a map in the owner's toml format.",
        interface="derive(repo_root, repo_id, tracked) -> ComponentMap",
        implemented_by=("praxis.components",),
        entry="derive",
        region="measure",
        note="A flat package has more than 30 children at depth 1. Its children are grouped by the name before the first underscore. Then each one-file group joins the multi-file group with the most import edges, when that maximum is unique. The edges come from static_imports. The ledger calls from_json before it stores a map. The recorder, the item pool, the scenario runner, the task registration, the probe, and the report parse the frozen map and assign paths with it.",
        x=460,
        y=212,
        source_review="5d9cdc72f15a4ceca9c7e2d63da8ed93a57645f2483005204369a407feaad2c4",
    ),
    Component(
        id="Extractors",
        does="Extracts ground truth at component grain: static imports, runtime call traces in a scratch clone, and SQLite table references. It returns identifiers and counts only.",
        interface="static_imports(repo_root, assign, files) -> StaticGraph",
        implemented_by=("praxis.extract",),
        entry="static_imports",
        region="measure",
        note="The recorder, the component map, the item pool, the task registration, and the probe call static_imports. A resolved TYPE_CHECKING import is an edge that is not module-level, and type_only counts it, not blind_spots (EXTRACTOR_VERSION x1-v2). The scenario runner calls scratch_clone and run_scenario, and its rows carry X2_VERSION.",
        x=270,
        y=120,
        source_review="178cb7bc49df2fb4297c4f22c1eb5d3aed7c63478a4ac8ec1c2e242d7f3659fb",
    ),
    Component(
        id="ItemPool",
        does="For praxis set components, freezes the component map of a repository and writes a D1 item for each ordered pair of parsed product components. It also shows the frozen map and the pool supply, and it prints or saves an owner map draft.",
        interface="freeze(repo_root, praxis_home, stdout, now, reason) -> int",
        implemented_by=("praxis.pool",),
        entry="freeze",
        region="measure",
        note="A change to a frozen map needs a --reason from MAP_REASONS. Freeze refuses fewer than 5 or more than 30 product components and prints the draft of a mechanical map. A second freeze writes only the missing pairs, and a pair whose truth flipped gets a new item at the next epoch. A component without a parsed Python file gives no items, not items with truth 0. After each freeze, it calls draw_start, which draws the start week at the first freeze only and prints nothing. praxis make components --write never writes over an existing owner file.",
        x=460,
        y=304,
        source_review="258b4eddec0b681c34fd6bec88614fca1952c25672136e3b514d3ef3154455be",
    ),
    Component(
        id="TaskRegistration",
        does="Draws the start week of a repository once. For praxis start task, it registers the next task and draws its bound items and its arm from the committed seed. In the treatment arm, it asks for forecasts.",
        interface="register(repo_root, praxis_home, stdin, stdout, now, seed) -> int",
        implemented_by=("praxis.task",),
        entry="register",
        region="measure",
        note="A task before the start week is control with forced_control set, and its text is the text of any control task. From the start week, the arm generator reads only the seed, the repository id, and the task ordinal. The region touch rate counts closed tasks in TOUCH_WINDOW_WEEKS, where None counts all. The binding pool holds only items that no task exposed and no probe asked. The recorder binds the task and closes its segment. praxis show task calls last, which prints only the result.",
        x=650,
        y=304,
        source_review="07030a53db16c81f1734d57bc5c15a9a539a0168eb60ad5e0d67bfde84d9b7e8",
    ),
    Component(
        id="Probe",
        does="For praxis test, gives one unassisted probe kind: structural, drift, decision, knockout, or Pathfinder. It refuses and records a flag when an agent is attached, and it stores closed-vocabulary answers only.",
        interface="administer(repo_root, praxis_home, stdin, stdout, now, kind) -> int",
        implemented_by=("praxis.probe",),
        entry="administer",
        region="measure",
        note="A structural item without a truth from the extractors now is not asked and stays due. After the due items, a structural probe asks up to N_VALIDATION never-drawn validation items each ISO week, ranked by a hash of task.SEED. It asks an item answered on the grid once more inside the P1 window, as a retest. The knockout reads only scenario runs of the HEAD tree. Pathfinder ratings stay in memory and only the scores are stored.",
        x=650,
        y=212,
        source_review="8b42c03ec28f5f5b3d220d197523693afb32e4efb9958bc15ffc54a7e1e31cfd",
    ),
    Component(
        id="ScenarioRunner",
        does="Reads the owner's scenario file and runs each scenario in its own scratch clone of HEAD with the runtime tracer. It stores the components each run entered with the HEAD tree hash, and it lists the scenarios with their last runs.",
        interface="run(repo_root, praxis_home, stdout, now) -> int",
        implemented_by=("praxis.scenarios",),
        entry="run",
        region="measure",
        note="The scenario file is a closed shape. A scenario id is stored as an identifier, and the argv is stored only as a hash. PRAXIS_HOME points into the clone, so a scenario cannot write the owner's ledger. The clone is deleted after each scenario, also when it fails.",
        x=270,
        y=212,
        source_review="6b2f11e5ed75ea3e6e10434faf45617f3e29f8613e5faaeac8bcfaf8a86f9936",
    ),
    Component(
        id="TranscriptReader",
        does="Finds and reads one Claude Code transcript at analysis time. It returns prompt, assistant, and tool-call records with identifiers, timestamps, counts, and hashes.",
        interface="read(path) -> Session",
        implemented_by=("praxis.transcript",),
        entry="read",
        region="analysis",
        note="The probe and the report call locate and read, and the recorder calls options_hash. The name sets that it returns must never be stored.",
        x=888,
        y=120,
        source_review="e9d49c458bb60f5303da2ec639f0a196f8a0211d5015d5b176ca0683bdd0d14b",
    ),
    Component(
        id="AgentCheck",
        does="Finds the repository root and the coding agent processes that work inside it. An unassisted probe must refuse when this check finds an agent.",
        interface="attached_agents(root) -> list[str]",
        implemented_by=("praxis.unassisted",),
        entry="attached_agents",
        region="measure",
        note="The command surface, the doctor, the item pool, the scenario runner, the task registration, and the probe call locate. The doctor and the probe call attached_agents.",
        x=270,
        y=304,
        source_review="2b80eb94c0e4d2b81e7b21109edacfbec4b5b519b46f320f1fb8e98d16863788",
    ),
    Component(
        id="Scoring",
        does="Pure functions for proper scores, their decomposition, Pathfinder networks, and small-sample randomization tests. They take numbers, return numbers, and give None when the data cannot give an answer.",
        interface="brier(p, o) -> float",
        implemented_by=("praxis.scoring",),
        entry="brier",
        region="analysis",
        note="No I/O and no imports from other praxis modules. The probe calls pfnet and pfsim. The report calls bss, decomposition, corp_reliability, randomization_test, and snap.",
        x=888,
        y=212,
        source_review="534298a1749413732c3be895c5cd5f67689f049a234508c1d52b8489fce29ca4",
    ),
    Component(
        id="Report",
        does="Computes references, Brier scores, decomposition, CORP bands, randomization tests, the pipeline placebo, test-retest, and the general effect across repositories from the ledger. It writes nothing, and its text holds counts, numbers, and reason codes only.",
        interface="scores(store, repo_id, seed, draws) -> Scores",
        implemented_by=("praxis.report", "praxis.render", "praxis.general"),
        entry="scores",
        region="analysis",
        note="praxis.report computes the scores. praxis.general computes the general effect with koehler_levin over the repository starts. praxis.render holds the result shapes and the text, and report re-exports them. A forced-control task is out of the P1 blocks and arm contrasts. R_fam counts closed tasks in the touch window. render refuses the words cause, impact, and would unless a pre-registered rule is met, and then it names the rule. A pipeline placebo below ALPHA suspends each P1 arm contrast in the text. Validation items never enter a P1 block or an arm contrast. A reference that cannot be fitted is NULL, never another reference.",
        x=888,
        y=304,
        source_review="c73ecb83e7085d69253ead25ff048c5f64e76675d60ca02cf6d5380e72c40851",
    ),
)


FLOW_KINDS: tuple[str, ...] = ()

# The invariants and the import rules are in the contract file, map/contract.py.
MODEL = Model(
    canvas=(1094, 892),
    containers=CONTAINERS,
    regions=REGIONS,
    components=COMPONENTS,
    flows=flows.FLOWS,
    flow_kinds=FLOW_KINDS,
)


PLAIN: dict[str, str] = {
    "CodingAgent": "the coding agent",
    "Owner": "the owner at a terminal",
    "Git": "the git program",
    "WorkingTree": "the repository checkout",
    "PraxisHome": "the praxis home directory",
    "CommandSurface": "the command surface",
    "HookEntry": "the hook entry",
    "Recorder": "the recorder",
    "Ledger": "the ledger",
    "ConsentedInstall": "the consented install",
    "Doctor": "the read-only doctor",
    "ComponentMap": "the component map",
    "Extractors": "the extractors",
    "ItemPool": "the item pool",
    "TaskRegistration": "the task registration",
    "Probe": "the unassisted probe",
    "ScenarioRunner": "the scenario runner",
    "TranscriptReader": "the transcript reader",
    "AgentCheck": "the agent-presence check",
    "Scoring": "the scoring functions",
    "Report": "the report",
}

LAYERS: tuple[Layer, ...] = ()

LAYER_OF_KIND: dict[str, str] = {}

RELATIONS: dict[tuple[str, str] | str, str] = {
    # The coding agent and the hook.
    (
        "CodingAgent",
        "CommandSurface",
    ): "The agent runs the command praxis hook for each configured hook event.",
    (
        "CodingAgent",
        "HookEntry",
    ): "The agent writes one hook payload as JSON on stdin, and the hook entry reads it.",
    (
        "HookEntry",
        "CodingAgent",
    ): "At the Stop that closes a task segment, the hook entry prints one fixed terminalSequence object. Claude Code writes it to the owner terminal, not to the agent.",
    (
        "CommandSurface",
        "HookEntry",
    ): "For praxis hook, the command surface calls run with stdin and the praxis home path. Other commands call now_iso for the clock reading at their start.",
    (
        "HookEntry",
        "Recorder",
    ): "The hook entry parses the payload as a JSON object and calls the recorder with it.",
    (
        "Recorder",
        "HookEntry",
    ): "The recorder returns the outcome, a note, the HEAD sha, and the notice flag, and the hook entry records them.",
    (
        "HookEntry",
        "Ledger",
    ): "The hook entry records one hook event with its outcome, note, HEAD sha, and duration, whatever happened.",
    # The recorder.
    (
        "Recorder",
        "Git",
    ): "The recorder runs git rev-parse, merge-base --is-ancestor, git diff against the session base commit, and git ls-files. Each has a 2 s timeout.",
    (
        "WorkingTree",
        "Recorder",
    ): "The recorder reads each untracked file, skips binary and large files, and hashes each line.",
    "rec-hashes": "The recorder stores baselines, pending pre-snapshots, and agent deltas as line-hash counts against the session base commit.",
    "rec-attributions": "At UserPromptSubmit and Stop, the recorder stores one attribution row per file.",
    "rec-edges": "At a Stop, the recorder stores the component imports of each Python file that an agent delta touched since the previous Stop.",
    "rec-tasks": "The recorder binds an open task to a later prompt, closes it with its outcomes through segment_close, voids it, and flags owner_answered_question.",
    "rec-decisions": "After an AskUserQuestion or ExitPlanMode call, the recorder stores the option count, the chosen index, and a hash of the labels.",
    (
        "Ledger",
        "Recorder",
    ): "The ledger returns the session baseline and base commit, the pending pre-snapshot, the agent multiset, the bound task, the frozen map, and the edge history.",
    (
        "Recorder",
        "ComponentMap",
    ): "At a Stop, the recorder parses the frozen map with from_json and assigns each touched file to a component.",
    (
        "Recorder",
        "Extractors",
    ): "At a Stop, the recorder calls static_imports on the touched Python files at file grain.",
    (
        "Recorder",
        "TranscriptReader",
    ): "The recorder calls options_hash, so a decision row and the transcript use the same label hash.",
    (
        "Ledger",
        "PraxisHome",
    ): "The ledger writes its tables to ledger.db with SQLite in WAL mode.",
    # The owner's commands.
    ("Owner", "CommandSurface"): "The owner types a praxis command on a terminal.",
    (
        "CommandSurface",
        "ConsentedInstall",
    ): "The command surface calls snippet for praxis show install, and apply for praxis install and praxis remove.",
    (
        "CommandSurface",
        "Doctor",
    ): "For praxis show status, the command surface calls the doctor with the current directory and the home paths.",
    (
        "Ledger",
        "CommandSurface",
    ): "The ledger returns attribution rows and session ids for praxis show lines and praxis show sessions.",
    (
        "CommandSurface",
        "TaskRegistration",
    ): "The command surface calls register for praxis start task and last for praxis show task. For praxis show report, it reads SEED.",
    (
        "CommandSurface",
        "Probe",
    ): "The command surface changes the object of praxis test into a probe kind and calls administer with that kind.",
    (
        "CommandSurface",
        "ItemPool",
    ): "The command surface calls freeze for praxis set components, draft for praxis make components, and show for praxis show components.",
    (
        "CommandSurface",
        "Report",
    ): "The command surface opens the ledger, calls scores with the committed seed and draw count, and calls render on stdout.",
    (
        "CommandSurface",
        "AgentCheck",
    ): "For praxis show report, the command surface calls locate for the repository id.",
    # The consented install.
    (
        "Owner",
        "ConsentedInstall",
    ): "The owner types yes or no on the terminal after the install shows the change.",
    (
        "ConsentedInstall",
        "CodingAgent",
    ): "The install writes the merged hook block to the agent settings file with an atomic replace.",
    (
        "ConsentedInstall",
        "PraxisHome",
    ): "The install copies the previous settings file to settings-backups before it writes.",
    # The doctor.
    (
        "Doctor",
        "ConsentedInstall",
    ): "The doctor calls the install functions that load the settings file and find the praxis hook entries.",
    (
        "Doctor",
        "AgentCheck",
    ): "The doctor calls locate for the repository of the current directory and attached_agents for the agent processes inside it.",
    (
        "Doctor",
        "Probe",
    ): "When the ledger is at the current schema version, the doctor calls due for the structural items due now.",
    (
        "PraxisHome",
        "Doctor",
    ): "The doctor opens ledger.db in SQLite read-only mode and counts hook events, attributions, frozen maps, tasks, void reasons, and flags.",
    # The component map and the extractors.
    (
        "Ledger",
        "ComponentMap",
    ): "Before it stores a map, the ledger calls from_json, which refuses a map whose source or version does not match its rules.",
    (
        "ComponentMap",
        "Git",
    ): "The component map runs git ls-files to get the tracked paths when the caller gives none.",
    (
        "PraxisHome",
        "ComponentMap",
    ): "The component map loads the owner's components file for one repository with tomllib.",
    (
        "Extractors",
        "Git",
    ): "The extractors run git ls-files for Python files and git clone --local for a scratch clone.",
    (
        "WorkingTree",
        "Extractors",
    ): "The extractors read each Python file and parse it in memory with ast.",
    (
        "Extractors",
        "PraxisHome",
    ): "The extractors make a scratch clone of HEAD under $PRAXIS_HOME/scratch, with its remote removed, for the caller to run scenarios in and delete.",
    (
        "CodingAgent",
        "TranscriptReader",
    ): "The agent writes each session as a JSONL file under ~/.claude/projects, and the reader parses it in memory.",
    (
        "CodingAgent",
        "AgentCheck",
    ): "The check reads /proc, or ps where /proc is missing, for claude or codex processes with a working directory in the repository.",
    (
        "AgentCheck",
        "Recorder",
    ): "The check calls the snapshot module for the repository root and its hashed id.",
    # The item pool.
    (
        "ItemPool",
        "AgentCheck",
    ): "The item pool calls locate for the repository root and its hashed id.",
    (
        "ItemPool",
        "ComponentMap",
    ): "The item pool loads the owner map or derives one, serializes it with to_json or to_toml, and assigns paths with it.",
    (
        "ItemPool",
        "Extractors",
    ): "The item pool calls static_imports and reach on the working tree for the truth of each D1 pair.",
    (
        "ItemPool",
        "Git",
    ): "The item pool runs git rev-parse for the HEAD tree, git status with --no-optional-locks, and git ls-files.",
    (
        "ItemPool",
        "Ledger",
    ): "The item pool stores and freezes the map, writes the D1 items at epoch 0, and flags too_few_components. A truth flip gets an item at the next epoch.",
    (
        "Ledger",
        "ItemPool",
    ): "The ledger returns the frozen map, the D1 pairs that already have items, the current epoch of each pair, and the item counts.",
    # The task registration.
    (
        "Owner",
        "TaskRegistration",
    ): "In the treatment arm, the owner types one grid key or s for each forecast and each bound item.",
    (
        "TaskRegistration",
        "AgentCheck",
    ): "The task registration calls locate for the repository root and its hashed id.",
    (
        "TaskRegistration",
        "ComponentMap",
    ): "The task registration parses the frozen map and takes its product components for the forecast sample.",
    (
        "TaskRegistration",
        "Extractors",
    ): "For the result, the task registration calls static_imports again to read the D1 truth of each bound item now.",
    (
        "TaskRegistration",
        "Ledger",
    ): "The task registration stores the start week of a repository once. It stores each task with its exposures, arm, seed hash, and forced_control, and then the forecast rows.",
    (
        "Ledger",
        "TaskRegistration",
    ): "The ledger returns the frozen map, the repository start, the unexposed and unprobed items, the closed tasks with outcomes, and the last task.",
    # The probe.
    (
        "Owner",
        "Probe",
    ): "The owner types closed-vocabulary answers: grid keys, y or n, option numbers, component numbers, or ratings.",
    (
        "Probe",
        "AgentCheck",
    ): "The probe calls locate, and then attached_agents before it asks anything.",
    (
        "Probe",
        "ComponentMap",
    ): "The probe parses the frozen map, assigns paths, and takes its product components.",
    (
        "Probe",
        "Extractors",
    ): "The probe calls static_imports and reach on the working tree for the truth of structural and Pathfinder items.",
    (
        "Probe",
        "Scoring",
    ): "The probe gives the Pathfinder distances to pfnet and compares the owner network with the import network in pfsim.",
    (
        "Probe",
        "TranscriptReader",
    ): "For a decision, the probe locates and reads the session transcript to confirm that the options hash still holds.",
    (
        "Probe",
        "Git",
    ): "The probe runs git rev-parse for HEAD and its tree, and git status with --no-optional-locks.",
    (
        "Probe",
        "Ledger",
    ): "The probe stores structural, drift, decision, knockout, and Pathfinder results, and the agent_attached and abandoned flags. A validation answer has task and lag NULL.",
    (
        "Ledger",
        "Probe",
    ): "The ledger returns the frozen map, unanswered exposures, unprobed items, earlier responses, edge history, unprobed decisions, scenario runs, and the drift answers of the week.",
    # The scenario runner.
    (
        "CommandSurface",
        "ScenarioRunner",
    ): "The command surface calls run for praxis run scenarios and show for praxis show scenarios.",
    (
        "Owner",
        "PraxisHome",
    ): "The owner writes the component map file and the scenario file of a repository under $PRAXIS_HOME.",
    (
        "Doctor",
        "ScenarioRunner",
    ): "The doctor calls summary for the configured scenarios and their last runs on the frozen map.",
    (
        "ComponentMap",
        "Extractors",
    ): "For a flat package, the component map calls static_imports on the Python files of the product root and groups children by import edges.",
    (
        "ItemPool",
        "PraxisHome",
    ): "For praxis make components --write, the item pool saves the draft as components/<repo_id>.toml, and it refuses when that file exists.",
    (
        "TaskRegistration",
        "Probe",
    ): "The task registration holds the committed SEED, which the probe reads to rank the never-drawn validation items of each week.",
    (
        "ItemPool",
        "TaskRegistration",
    ): "After a freeze, the item pool calls draw_start. At the first freeze of a repository, draw_start draws and stores its start week.",
    (
        "TaskRegistration",
        "Report",
    ): "The report calls in_window and window_assumption for the touch window of R_fam. praxis.general calls week_of for the ISO week of each probe.",
    "scen-file": "The scenario runner loads scenarios/<repo_id>.toml with tomllib and refuses the whole file when it has another shape.",
    "scen-version": "Before it opens a Store for a summary, the scenario runner reads PRAGMA user_version from ledger.db in SQLite read-only mode.",
    (
        "ScenarioRunner",
        "AgentCheck",
    ): "The scenario runner calls locate for the repository root and its hashed id.",
    (
        "Ledger",
        "ScenarioRunner",
    ): "The ledger returns the frozen map and the scenario runs on it.",
    (
        "ScenarioRunner",
        "ComponentMap",
    ): "The scenario runner parses the frozen map, takes its product components for PYTHONPATH, and assigns each traced file to a component.",
    (
        "ScenarioRunner",
        "Extractors",
    ): "For each scenario, the scenario runner makes a scratch clone of HEAD and calls run_scenario in it.",
    (
        "ScenarioRunner",
        "Git",
    ): "The scenario runner runs git rev-parse for the HEAD tree and git ls-files in the clone.",
    (
        "ScenarioRunner",
        "PraxisHome",
    ): "After each scenario, also when it fails, the scenario runner deletes its scratch clone.",
    (
        "ScenarioRunner",
        "Ledger",
    ): "The scenario runner stores one row per scenario with its outcome, components, call edges, argv hash, extractor version, and HEAD tree hash.",
    # The report.
    (
        "Ledger",
        "Report",
    ): "The ledger returns the frozen map, tasks, outcomes, flagged tasks, forecasts, probe responses with the never-drawn ones, D1 items, edge history, and repository starts.",
    (
        "Report",
        "ComponentMap",
    ): "The report parses the frozen map and assigns the paths in a prompt to components.",
    (
        "TranscriptReader",
        "Report",
    ): "The reader returns the name set of each bound prompt. The report keeps only component ids and drops the rest.",
    (
        "Report",
        "Scoring",
    ): "The report calls bss, decomposition, corp_reliability, and randomization_test, which is two-sided for the placebo. praxis.general calls koehler_levin with the weekly series of each repository.",
}

VERBS: dict[str, tuple[str, str]] = {
    "data": ("sends to", "receives from"),
    "control": ("calls", "is called by"),
}

MEANING = Meaning(
    plain=PLAIN,
    layers=LAYERS,
    layer_of_kind=LAYER_OF_KIND,
    relations=RELATIONS,
    journeys=journeys.JOURNEYS,
    verbs=VERBS,
)
