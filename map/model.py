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
        box=(16, 16, 190, 760),
        tone="host",
    ),
    Container(
        id="system",
        label="PRAXIS",
        sub="one process per command",
        box=(230, 16, 848, 952),
        tone="server",
    ),
)

# A region contains a group of components with one function.
# The record, ledger, and instrument regions follow the layers of
# docs/design/03-measurement.md section 12. The analysis region holds the protocol
# modules (report, scoring) and the transcript reader that the probe and the report call.
# The policy layer has no module yet.
REGIONS = (
    Region(id="record", label="RECORD", box=(250, 744, 190, 204), container="system"),
    Region(id="ledger", label="LEDGER", box=(250, 412, 190, 112), container="system"),
    Region(
        id="commands", label="COMMANDS", box=(868, 80, 190, 112), container="system"
    ),
    Region(
        id="owner",
        label="SETUP AND CHECKS",
        box=(868, 744, 190, 204),
        container="system",
    ),
    Region(
        id="measure", label="INSTRUMENT", box=(250, 80, 570, 296), container="system"
    ),
    Region(
        id="analysis", label="ANALYSIS", box=(868, 412, 190, 296), container="system"
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
        y=712,
    ),
    Component(
        id="Owner",
        does="The person who owns the repositories. The owner types praxis commands on a terminal, answers the consent prompt, and answers task forecasts and probe items.",
        kind="actor",
        container="outside",
        x=36,
        y=436,
    ),
    Component(
        id="Git",
        does="The git program. Praxis runs it as a subprocess and reads its output. It never runs git with a shell.",
        kind="actor",
        container="outside",
        x=36,
        y=344,
    ),
    Component(
        id="WorkingTree",
        does="The checkout of the owner's repository on disk. Praxis reads untracked files and Python source from it.",
        kind="actor",
        container="outside",
        x=36,
        y=620,
    ),
    Component(
        id="PraxisHome",
        does="The directory $PRAXIS_HOME (default ~/.praxis). It holds ledger.db, settings-backups/, owner component maps as components/<repo_id>.toml, owner scenario files as scenarios/<repo_id>.toml, and scratch clones under scratch/.",
        kind="actor",
        container="outside",
        x=36,
        y=528,
    ),
    # Source components.
    Component(
        id="CommandSurface",
        does="Parses the praxis command line and calls the module of each verb from one dispatch table. It imports the measurement modules only inside their handlers, and it prints attribution and session rows from the ledger.",
        interface="main(argv) -> int",
        implemented_by=("praxis.cli", "praxis"),
        entry="main",
        region="commands",
        note="The package module praxis holds only the docstring and __version__, which the command surface prints for --version.",
        x=888,
        y=120,
        source_review="5a19140262bb61de1765b3764e090a591d742bc59d98e0d05c0d59808c6f92d5",
    ),
    Component(
        id="HookEntry",
        does="Runs once for each hook event. It reads the payload, calls the recorder, records the hook event, and exits 0 on every input. Its stdout stays empty, except one fixed notice when the recorder closes a task segment.",
        interface="run(stdin, praxis_home) -> int",
        implemented_by=("praxis.hooks",),
        entry="run",
        region="record",
        x=270,
        y=876,
        source_review="81303600f107cb38f6ee0ba1d8d857aff44f2be7be7490fd675e951935b5fd8b",
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
        y=784,
        source_review="e537229ea339e382a7895d86b294ffdefa8289dfa1a27387337ca294ecf36608",
    ),
    Component(
        id="Ledger",
        does="The SQLite ledger under $PRAXIS_HOME. praxis.schema holds its DDL and closed vocabularies, and praxis.store holds every INSERT. It keeps recorder rows, component maps, scenario runs, tasks, probes, forecasts, decisions, and flags.",
        interface="Store.open / Store.task_register / Store.segment_close / Store.probe_response",
        implemented_by=("praxis.store", "praxis.schema"),
        entry="Store",
        kind="store",
        region="ledger",
        note="Each table in schema.CLAIMS has a CHECK that fixes its one claim class and refuses an empty source list. The attributions table accepts any claim class, and hook_events has none. Triggers make the measurement tables append-only, and a task or a map sets each transition column once. segment_close writes the outcomes and the task close in one transaction. A scenario run must name the HEAD tree it traced, unless its outcome is not_python. On a ledger that already held runs, migrate adds tree_hash without that CHECK. probe_items_unprobed leaves out an item that a probe asked, so the binding pool and the validation draw hold blind items only. The survival lookup intersects agent line-hash multisets with a probe multiset, so two identical lines count as two. When PRAGMA user_version differs from SCHEMA_VERSION, Store.open runs the DDL and then migrate.",
        x=270,
        y=452,
        source_review="2ba04248a1e4ca84d9ccfc39039aedfb8d478422e7068c09e7c68338a5bb6409",
    ),
    Component(
        id="ConsentedInstall",
        does="Prints the hook block for Claude Code or Codex. With --apply or --remove, it writes one settings file after the owner types yes on a terminal. The Claude Code PostToolUse matcher also selects AskUserQuestion and ExitPlanMode.",
        interface="apply(agent, remove, home, praxis_home, stdin, out) -> int",
        implemented_by=("praxis.install",),
        entry="apply",
        region="owner",
        x=888,
        y=784,
        source_review="b61e43371ea2524db917a7ae0c3dcd23a7a5af3fc6971f32d575bd5094f9558f",
    ),
    Component(
        id="Doctor",
        does="Reports the installed hooks, the ledger counts, the frozen maps, tasks by status, the binding rate, void reasons, and flags. It also reports attached agents, probes due, and the configured scenarios with their last runs, and it never creates the ledger.",
        interface="run(cwd, home, praxis_home, out) -> int",
        implemented_by=("praxis.doctor",),
        entry="run",
        region="owner",
        note="Counts come from a SQLite read-only connection. Probes due come from probe.due, which opens a Store. The doctor asks for them only at SCHEMA_VERSION, so no migration can run. The scenario line comes from scenarios.summary, which also opens a Store only at SCHEMA_VERSION.",
        x=888,
        y=876,
        source_review="4ca4ef441e556e21812b3cca140c1b6382ee2d64e357dd2220d87b8662ca074b",
    ),
    Component(
        id="ComponentMap",
        does="Makes the component map of a repository: ordered glob rules that give each path one component. It derives the map from git ls-files or loads the owner's file, and it prints a map in the owner's toml format.",
        interface="derive(repo_root, repo_id, tracked) -> ComponentMap",
        implemented_by=("praxis.components",),
        entry="derive",
        region="measure",
        note="A flat package has more than 30 children at depth 1. Its children are grouped by the name before the first underscore. Then each one-file group joins the multi-file group with the most import edges, when that maximum is unique. The edges come from static_imports. The ledger calls from_json before it stores a map. The recorder, the item pool, the scenario runner, the task registration, the probe, and the report parse the frozen map and assign paths with it.",
        x=270,
        y=212,
        source_review="c9307d38dc9d54f89192d79e7ccff8db0b7ca187e80686230dcf0d9c1a1461b0",
    ),
    Component(
        id="Extractors",
        does="Extracts ground truth at component grain: static imports, runtime call traces in a scratch clone, and SQLite table references. It returns identifiers and counts only.",
        interface="static_imports(repo_root, assign, files) -> StaticGraph",
        implemented_by=("praxis.extract",),
        entry="static_imports",
        region="measure",
        note="The recorder, the component map, the item pool, the task registration, and the probe call static_imports. The scenario runner calls scratch_clone and run_scenario, and its rows carry X2_VERSION.",
        x=270,
        y=120,
        source_review="e49894bd3188eb2e0549b62b8f1774a31a95bc76e766f4cf8b496e6096259f4b",
    ),
    Component(
        id="ItemPool",
        does="Freezes the component map of a repository and writes a D1 item for each ordered pair of parsed product components. It also shows the frozen map and the pool supply, and it prints or saves the mechanical map as an owner map draft.",
        interface="freeze(repo_root, praxis_home, stdout, now, reason) -> int",
        implemented_by=("praxis.pool",),
        entry="freeze",
        region="measure",
        note="A change to a frozen map needs a --reason from MAP_REASONS. Freeze refuses fewer than 5 or more than 30 product components and prints the draft of a mechanical map. A second freeze writes only the missing pairs, and a pair whose truth flipped gets a new item at the next epoch. A component without a parsed Python file gives no items, not items with truth 0. derive --write never writes over an existing owner file.",
        x=460,
        y=304,
        source_review="0968d38880a29a403928bd7a14a5b07d310e8b35a840a265da9d66d125ece350",
    ),
    Component(
        id="TaskRegistration",
        does="Registers the next task before its prompt and draws its bound items and its arm from the committed seed. In the treatment arm, it asks the owner for forecasts. It shows the feedback of the last closed task.",
        interface="register(repo_root, praxis_home, stdin, stdout, now, seed) -> int",
        implemented_by=("praxis.task",),
        entry="register",
        region="measure",
        note="The arm generator reads only the seed, the repository id, and the task ordinal. The binding pool holds only items that no task exposed and no probe asked. The recorder, not this module, binds the task to the next prompt and closes its segment. praxis last calls last, which prints only the feedback.",
        x=460,
        y=212,
        source_review="8bd3734632fb7ec40f1b2b102ad5169ede39b8235bfa1e6106fcca5192d29c48",
    ),
    Component(
        id="Probe",
        does="Gives one unassisted probe kind: structural, drift, decision, knockout, or Pathfinder. It refuses and records a flag when an agent is attached, and it stores closed-vocabulary answers only.",
        interface="administer(repo_root, praxis_home, stdin, stdout, now, kind) -> int",
        implemented_by=("praxis.probe",),
        entry="administer",
        region="measure",
        note="A structural item without a truth from the extractors now is not asked and stays due. After the due items, a structural probe asks up to N_VALIDATION never-drawn validation items each ISO week, ranked by a hash of the committed seed. It asks an item answered on the grid once more inside the P1 window, as a retest. The knockout reads only scenario runs of the HEAD tree. Pathfinder ratings stay in memory and only the scores are stored.",
        x=650,
        y=304,
        source_review="5b20dde541e9f7d4734da177e5d090d4775ac9dba36efcba00d166d3a6db1624",
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
        y=304,
        source_review="905348d889dcb6bcf52ff33715b9fba86278ccc287bf7a874d49f6f4d95f79ad",
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
        y=636,
        source_review="761c4ca57c235916f2f2daac336699012eae82797493232c164f7328925be993",
    ),
    Component(
        id="AgentCheck",
        does="Finds the repository root and the coding agent processes that work inside it. An unassisted probe must refuse when this check finds an agent.",
        interface="attached_agents(root) -> list[str]",
        implemented_by=("praxis.unassisted",),
        entry="attached_agents",
        region="measure",
        note="The command surface, the doctor, the item pool, the scenario runner, the task registration, and the probe call locate. The doctor and the probe call attached_agents.",
        x=460,
        y=120,
        source_review="082764c013e5f8bdf4c784ce71a2e552c21a77021e87effe1f6274209cede62d",
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
        y=544,
        source_review="67383eb5f5ce2b4712fd3ac45f07888991ecc078cfdf6a3df243bdc072e1035f",
    ),
    Component(
        id="Report",
        does="Computes references, Brier scores, decomposition, CORP bands, randomization tests, the pipeline placebo, and test-retest from the ledger at analysis time. It writes nothing, and its text holds counts, numbers, and reason codes only.",
        interface="scores(store, repo_id, seed, draws) -> Scores",
        implemented_by=("praxis.report", "praxis.render"),
        entry="scores",
        region="analysis",
        note="praxis.report computes the scores. praxis.render holds the result shapes and the text, and report re-exports them. render refuses the words cause, impact, and would unless a pre-registered rule is met, and then it names the rule. A pipeline placebo below ALPHA suspends each P1 arm contrast in the text. Validation items never enter a P1 block or an arm contrast. A reference that cannot be fitted is NULL, never another reference.",
        x=888,
        y=452,
        source_review="b89284420cbef66b3c170878965f50662e518d620646bf36d9b50a302b773a2f",
    ),
)


FLOW_KINDS: tuple[str, ...] = ()

# The invariants and the import rules are in the contract file, map/contract.py.
MODEL = Model(
    canvas=(1094, 984),
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
    ): "The command surface calls the hook entry with stdin and the praxis home path for the hook verb.",
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
    ): "The command surface calls the install to print the hook block or to apply or remove it.",
    (
        "CommandSurface",
        "Doctor",
    ): "The command surface calls the doctor with the current directory and the home paths.",
    (
        "Ledger",
        "CommandSurface",
    ): "The ledger returns attribution rows and session ids for the attributions and sessions verbs.",
    (
        "CommandSurface",
        "TaskRegistration",
    ): "The command surface calls register for praxis task and last for praxis last.",
    (
        "CommandSurface",
        "Probe",
    ): "The command surface refuses an unknown kind and calls administer with the probe kind.",
    (
        "CommandSurface",
        "ItemPool",
    ): "The command surface calls freeze for praxis components freeze, draft for praxis components derive, and show for praxis components.",
    (
        "CommandSurface",
        "Report",
    ): "The command surface opens the ledger, calls scores with the committed seed and draw count, and calls render on stdout.",
    (
        "CommandSurface",
        "AgentCheck",
    ): "For praxis report, the command surface calls locate for the repository id.",
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
    ): "For the feedback, the task registration calls static_imports again to read the D1 truth of each bound item now.",
    (
        "TaskRegistration",
        "Ledger",
    ): "The task registration stores the task with its exposures, arm, and seed hash, which supersedes an open task. Then it stores the forecast rows.",
    (
        "Ledger",
        "TaskRegistration",
    ): "The ledger returns the frozen map, the unexposed and unprobed items, the closed tasks with outcomes, and the details of the last task.",
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
    ): "The command surface calls run for praxis scenarios run and show for praxis scenarios.",
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
    ): "With derive --write, the item pool saves the draft as components/<repo_id>.toml, and it refuses when that file exists.",
    (
        "TaskRegistration",
        "Probe",
    ): "The task registration holds the committed SEED, which the probe reads to rank the never-drawn validation items of each week.",
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
    ): "The ledger returns the frozen map, tasks, outcomes, flagged tasks, forecasts, probe responses with the never-drawn ones, D1 items, and edge history.",
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
    ): "The report calls bss, decomposition, corp_reliability, and randomization_test with forecasts and outcomes, and a two-sided randomization_test for the pipeline placebo.",
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
