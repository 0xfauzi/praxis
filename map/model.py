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
import journeys
from systemap import (  # type: ignore[import-not-found, unused-ignore]
    Component,
    Container,
    Flow,
    Layer,
    Meaning,
    Model,
    Region,
)

# Containers give system boundaries.
# The actors are outside the praxis source. The praxis source runs as one process per command.
CONTAINERS = (
    Container(
        id="outside",
        label="OUTSIDE",
        sub="agent, owner, git, files",
        box=(16, 16, 190, 660),
        tone="host",
    ),
    Container(
        id="system",
        label="PRAXIS",
        sub="one process per command",
        box=(230, 16, 468, 860),
        tone="server",
    ),
)

# A region contains a group of components with one function.
REGIONS = (
    Region(id="record", label="RECORD", box=(488, 320, 190, 204), container="system"),
    Region(id="ledger", label="LEDGER", box=(488, 652, 190, 112), container="system"),
    Region(
        id="commands", label="COMMANDS", box=(488, 80, 190, 112), container="system"
    ),
    Region(
        id="owner",
        label="SETUP AND CHECKS",
        box=(250, 80, 190, 204),
        container="system",
    ),
    Region(
        id="measure", label="MEASUREMENT", box=(250, 320, 190, 296), container="system"
    ),
    Region(
        id="analysis", label="ANALYSIS", box=(250, 652, 190, 204), container="system"
    ),
)

COMPONENTS: tuple[Component, ...] = (
    # Actors. They have no source modules.
    Component(
        id="CodingAgent",
        does="Claude Code or Codex. Its hook configuration runs praxis hook for each hook event and writes the event payload as JSON on stdin.",
        kind="actor",
        container="outside",
        x=36,
        y=336,
    ),
    Component(
        id="Owner",
        does="The person who owns the repositories. The owner types praxis commands on a terminal and answers the consent prompt.",
        kind="actor",
        container="outside",
        x=36,
        y=126,
    ),
    Component(
        id="Git",
        does="The git program. Praxis runs it as a subprocess and reads its output. It never runs git with a shell.",
        kind="actor",
        container="outside",
        x=36,
        y=612,
    ),
    Component(
        id="WorkingTree",
        does="The checkout of the owner's repository on disk. Praxis reads untracked files and Python source from it.",
        kind="actor",
        container="outside",
        x=36,
        y=520,
    ),
    Component(
        id="PraxisHome",
        does="The directory $PRAXIS_HOME (default ~/.praxis). It holds the SQLite ledger file ledger.db, settings backups, owner component files, and scratch clones.",
        kind="actor",
        container="outside",
        x=36,
        y=428,
    ),
    # Source components.
    Component(
        id="CommandSurface",
        does="Parses the praxis command line and calls the component for each verb. It also prints attribution and session rows from the ledger.",
        interface="main(argv) -> int",
        implemented_by=("praxis.cli",),
        entry="main",
        region="commands",
        x=508,
        y=120,
        source_review="ac30632cfe18834fccf59223768a00c2f5bf81cac2faef99fcd1f25f5f2c2081",
    ),
    Component(
        id="HookEntry",
        does="Runs once for each hook event. It reads the payload, calls the recorder, records the hook event, and exits 0 on every input.",
        interface="run(stdin, praxis_home) -> int",
        implemented_by=("praxis.hooks",),
        entry="run",
        region="record",
        x=508,
        y=360,
        source_review="b15fd9542dd7535b2758b82c24405bae9e738629f918d6c02e5e3e59245ec224",
    ),
    Component(
        id="Recorder",
        does="Snapshots the working tree as multisets of line hashes with git. It stores tool-call deltas and attributes lines to agent, owner, and baseline at prompt boundaries.",
        interface="handle(payload, store, now) -> Result",
        implemented_by=("praxis.recorder", "praxis.gitdiff"),
        entry="handle",
        region="record",
        x=508,
        y=452,
        source_review="ffa2f84ee33b649ae23420caf4706978f6b13bcc12e9097379ffd98359c72e09",
    ),
    Component(
        id="Ledger",
        does="The SQLite ledger under $PRAXIS_HOME. praxis.schema holds its DDL and closed vocabularies, and praxis.store holds every INSERT. It keeps recorder rows, component maps, tasks, probes, forecasts, decisions, and flags.",
        interface="Store.open / Store.attributions / Store.agent_multiset / Store.survival",
        implemented_by=("praxis.store", "praxis.schema"),
        entry="Store",
        kind="store",
        region="ledger",
        note="Each table in schema.CLAIMS has a CHECK that fixes its one claim class and refuses an empty source list. The attributions table accepts any claim class, and hook_events has none. Triggers make the measurement tables append-only, and a task or a map sets each transition column once. The survival lookup intersects agent line-hash multisets with a probe multiset, so two identical lines count as two. When PRAGMA user_version differs from SCHEMA_VERSION, Store.open runs the DDL and then migrate, which drops the tables of the rejected drill.",
        x=508,
        y=692,
        source_review="7a245fe2dae76a064af5aff00673893af9c754a74f0317c4c2debe7dbe52f43a",
    ),
    Component(
        id="ConsentedInstall",
        does="Prints the hook block for Claude Code or Codex. With --apply or --remove, it writes one settings file after the owner types yes on a terminal.",
        interface="apply(agent, remove, home, praxis_home, stdin, out) -> int",
        implemented_by=("praxis.install",),
        entry="apply",
        region="owner",
        x=270,
        y=120,
        source_review="e78831d1bab7e37d04dac4ae0a0381af9ff5533c522092f8bdd8124754eae9e8",
    ),
    Component(
        id="Doctor",
        does="Reports the installed hooks, the ledger row counts, and the agent processes attached to the current repository. It opens the ledger in SQLite read-only mode and writes no file.",
        interface="run(cwd, home, praxis_home, out) -> int",
        implemented_by=("praxis.doctor",),
        entry="run",
        region="owner",
        x=270,
        y=212,
        source_review="c192ca046d1b468e32ba7ac7c0b71060a8acf27f75423e6941471029ab77613a",
    ),
    Component(
        id="ComponentMap",
        does="Makes the component map of a repository: ordered glob rules that give each path one component. It derives the map from git ls-files or loads the owner's file.",
        interface="derive(repo_root, repo_id, tracked) -> ComponentMap",
        implemented_by=("praxis.components",),
        entry="derive",
        region="measure",
        note="Only the ledger imports this module: Store.component_map_put parses a map with from_json before it stores the rules. The command surface has no components verb.",
        x=270,
        y=544,
        source_review="e8f271fd2355b7f588b2b014f61bedd4ddd5131dbb0136c08b19f4dcefb6436f",
    ),
    Component(
        id="Extractors",
        does="Extracts ground truth at component grain: static imports, runtime call traces in a scratch clone, and SQLite table references. It returns identifiers and counts only.",
        interface="static_imports(repo_root, assign, files) -> StaticGraph",
        implemented_by=("praxis.extract",),
        entry="static_imports",
        region="measure",
        note="No other praxis module imports this module yet. Runtime traces and schema references wait for their experiments.",
        x=270,
        y=452,
        source_review="0e123df13e271f1c40ed270cd132faa1457bd9d470593c46b075384bcec14c31",
    ),
    Component(
        id="TranscriptReader",
        does="Reads one Claude Code transcript at analysis time. It returns prompt, assistant, and tool-call records with identifiers, timestamps, counts, and hashes.",
        interface="read(path) -> Session",
        implemented_by=("praxis.transcript",),
        entry="read",
        region="analysis",
        note="No other praxis module imports this module yet. The name sets that it returns must never be stored.",
        x=270,
        y=692,
        source_review="2c1320e42460edc00afa776a2dede311f9122afd0bb1c9d5acb43f82f22c28e1",
    ),
    Component(
        id="AgentCheck",
        does="Finds the repository root and the coding agent processes that work inside it. An unassisted probe must refuse when this check finds an agent.",
        interface="attached_agents(root) -> list[str]",
        implemented_by=("praxis.unassisted",),
        entry="attached_agents",
        region="measure",
        note="The doctor imports locate and attached_agents from this module. No probe harness imports it yet.",
        x=270,
        y=360,
        source_review="8a61a3f1b1c4aa74f804c64bacd6fc29fb52c2c1bb28ec053c53f48262bb8582",
    ),
    Component(
        id="Scoring",
        does="Pure functions for proper scores, their decomposition, and small-sample randomization tests. They take numbers, return numbers, and give None when the data cannot give an answer.",
        interface="brier(p, o) -> float",
        implemented_by=("praxis.scoring",),
        entry="brier",
        region="analysis",
        note="No I/O and no imports from other praxis modules, so the map shows no flows for it. No other praxis module imports it yet.",
        x=270,
        y=784,
        source_review="65c94aab0a52d96a62a4dcd439be1ceb7a76b38b22cf81096da11d8df434e41d",
    ),
)

# Each flow gives source, target, artifact, and kind.
FLOWS: tuple[Flow, ...] = (
    # The coding agent and the hook.
    Flow("CodingAgent", "CommandSurface", "praxis hook", "control"),
    Flow("CodingAgent", "HookEntry", "hook payload", "data"),
    Flow(
        "CommandSurface",
        "HookEntry",
        "hook call",
        "control",
        source_refs=(
            "praxis.cli:main@ea9564e4199500544b11e719846616df53144d5a7d9e29fd5d9c8db1bbc9f008",
            "praxis.hooks:run@274f9ceb29a04dcce555714a64215223e5b02e532d7d99bcc8850b99e35d8c6c",
        ),
        review_digest="35f21b17279c02401a424e44829f38fd611c1b519bc7d21ebd6480987035ba74",
    ),
    Flow(
        "HookEntry",
        "Recorder",
        "event payload",
        "data",
        source_refs=(
            "praxis.hooks:run@274f9ceb29a04dcce555714a64215223e5b02e532d7d99bcc8850b99e35d8c6c",
            "praxis.recorder:handle@51d9a05dc62fe47492b746dbe02ce3892111f0c5a310cbc10e9124882b20b092",
        ),
        review_digest="4913d8334c8246279584ba22c94d49b1fb05acfa1be448e5e8cd350b3b83e7db",
    ),
    Flow(
        "HookEntry",
        "Ledger",
        "hook event",
        "data",
        source_refs=(
            "praxis.hooks:run@274f9ceb29a04dcce555714a64215223e5b02e532d7d99bcc8850b99e35d8c6c",
            "praxis.store:Store@9a54e6e3255bcaface7173b343bcd8812279c1a90e80a25fb030c70d9bdb8a80",
        ),
        review_digest="2747754f364eaec8c42eb70c8351b2c3bdb0e2f5f15a21177d85cea10e7bc1ee",
    ),
    # The recorder.
    Flow("Recorder", "Git", "diff command", "control"),
    Flow("WorkingTree", "Recorder", "untracked files", "data"),
    Flow(
        "Recorder",
        "Ledger",
        "line-hash counts",
        "data",
        source_refs=(
            "praxis.recorder:handle@51d9a05dc62fe47492b746dbe02ce3892111f0c5a310cbc10e9124882b20b092",
            "praxis.store:Store@9a54e6e3255bcaface7173b343bcd8812279c1a90e80a25fb030c70d9bdb8a80",
        ),
        id="rec-hashes",
        review_digest="5c0fd78a59e0b72e26a8a413badefefb71f017e7a13104d3fd88fb189d255d36",
    ),
    Flow(
        "Recorder",
        "Ledger",
        "attribution rows",
        "data",
        source_refs=(
            "praxis.recorder:attribute@51d9a05dc62fe47492b746dbe02ce3892111f0c5a310cbc10e9124882b20b092",
            "praxis.store:Store@9a54e6e3255bcaface7173b343bcd8812279c1a90e80a25fb030c70d9bdb8a80",
            "praxis.schema:Attribution@f41c1417f45ac88ccc9db47207cfdfa45f0bccb933b82748a0fc9b5947849da4",
        ),
        id="rec-attributions",
        review_digest="52fcb3bbc677a0b57c1d7e08293a0d23375591d329964b9b236b0fed87e0b0b6",
    ),
    Flow(
        "Ledger",
        "Recorder",
        "stored multisets",
        "data",
        source_refs=(
            "praxis.store:Store@9a54e6e3255bcaface7173b343bcd8812279c1a90e80a25fb030c70d9bdb8a80",
            "praxis.recorder:handle@51d9a05dc62fe47492b746dbe02ce3892111f0c5a310cbc10e9124882b20b092",
        ),
        review_digest="750909fe58c766624de556ee2e3a6cc9d289630b4697350b0a58c518b32c42e7",
    ),
    Flow("Ledger", "PraxisHome", "SQLite rows", "data"),
    # The owner's commands.
    Flow("Owner", "CommandSurface", "command line", "control"),
    Flow(
        "CommandSurface",
        "ConsentedInstall",
        "setup call",
        "control",
        source_refs=(
            "praxis.cli:main@ea9564e4199500544b11e719846616df53144d5a7d9e29fd5d9c8db1bbc9f008",
            "praxis.install:apply@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
            "praxis.install:snippet@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
        ),
        review_digest="4e3ae0abd07f00d86a4def0dd4cc5a4a9c696dd225e6e20ed01f38180c6989b7",
    ),
    Flow(
        "CommandSurface",
        "Doctor",
        "doctor call",
        "control",
        source_refs=(
            "praxis.cli:main@ea9564e4199500544b11e719846616df53144d5a7d9e29fd5d9c8db1bbc9f008",
            "praxis.doctor:run@22fa9d94aaeae45630c14accade1f5d2b32e6924ab027568f75f27834e9dff2c",
        ),
        review_digest="bc2530f8893aacb1ab6af960440852936bfd7dce2635e80907841618efeefe61",
    ),
    Flow(
        "Ledger",
        "CommandSurface",
        "attribution rows",
        "data",
        source_refs=(
            "praxis.store:Store@9a54e6e3255bcaface7173b343bcd8812279c1a90e80a25fb030c70d9bdb8a80",
            "praxis.cli@ea9564e4199500544b11e719846616df53144d5a7d9e29fd5d9c8db1bbc9f008",
        ),
        review_digest="da4afbc729abc6a2fb290aff790cd33edb5082cd0de5ec95a5b7f3942ff8e9f6",
    ),
    # The consented install.
    Flow("Owner", "ConsentedInstall", "consent answer", "data"),
    Flow("ConsentedInstall", "CodingAgent", "hook block", "data"),
    Flow("ConsentedInstall", "PraxisHome", "settings backup", "data"),
    # The doctor.
    Flow(
        "Doctor",
        "ConsentedInstall",
        "settings check",
        "control",
        source_refs=(
            "praxis.doctor@22fa9d94aaeae45630c14accade1f5d2b32e6924ab027568f75f27834e9dff2c",
            "praxis.install:load@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
            "praxis.install:is_ours@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
        ),
        review_digest="2c531662bc4eef9b3379148416d989476ba5dce16dc47b87d9054e68492100e2",
    ),
    Flow(
        "Doctor",
        "AgentCheck",
        "agent check",
        "control",
        source_refs=(
            "praxis.doctor:run@22fa9d94aaeae45630c14accade1f5d2b32e6924ab027568f75f27834e9dff2c",
            "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce",
            "praxis.unassisted:attached_agents@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce",
        ),
        review_digest="dcc77071c4125303f0a3eb3305d21c70472d57b23cfd881824698feea372c0df",
    ),
    Flow("PraxisHome", "Doctor", "ledger counts", "data"),
    # The measurement.
    Flow("ComponentMap", "Git", "file listing", "control"),
    Flow(
        "Ledger",
        "ComponentMap",
        "map check",
        "control",
        source_refs=(
            "praxis.store:Store@9a54e6e3255bcaface7173b343bcd8812279c1a90e80a25fb030c70d9bdb8a80",
            "praxis.components:from_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2",
        ),
        review_digest="9fd14c278939e0884db979f79e54227de3d7943da4d022f957ed1510ef2b6013",
    ),
    Flow("PraxisHome", "ComponentMap", "owner map file", "data"),
    Flow("Extractors", "Git", "clone command", "control"),
    Flow("WorkingTree", "Extractors", "Python source", "data"),
    Flow("Extractors", "PraxisHome", "scratch clone", "data"),
    Flow("CodingAgent", "TranscriptReader", "session transcript", "data"),
    Flow("CodingAgent", "AgentCheck", "agent processes", "data"),
    Flow(
        "AgentCheck",
        "Recorder",
        "repository root",
        "control",
        source_refs=(
            "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce",
            "praxis.gitdiff:toplevel@7981f4d247dbe86168d192c4098b53805ea1bd77a91310b68ffb6b39c3a4cf0f",
            "praxis.gitdiff:repo_id@7981f4d247dbe86168d192c4098b53805ea1bd77a91310b68ffb6b39c3a4cf0f",
        ),
        review_digest="fa69df84b7d788c15722bd761dcb0309ccb4757eb455106d1ddf5ec787044b72",
    ),
)

FLOW_KINDS: tuple[str, ...] = ()

# The invariants and the import rules are in the contract file, map/contract.py.
MODEL = Model(
    canvas=(714, 892),
    containers=CONTAINERS,
    regions=REGIONS,
    components=COMPONENTS,
    flows=FLOWS,
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
    "TranscriptReader": "the transcript reader",
    "AgentCheck": "the agent-presence check",
    "Scoring": "the scoring functions",
}

LAYERS: tuple[Layer, ...] = ()

LAYER_OF_KIND: dict[str, str] = {}

RELATIONS: dict[tuple[str, str] | str, str] = {
    (
        "CodingAgent",
        "AgentCheck",
    ): "The check reads /proc, or ps where /proc is missing, for claude or codex processes with a working directory in the repository.",
    (
        "AgentCheck",
        "Recorder",
    ): "The check calls the snapshot module for the repository root and its hashed id.",
    (
        "CodingAgent",
        "CommandSurface",
    ): "The agent runs the command praxis hook for each configured hook event.",
    (
        "CodingAgent",
        "HookEntry",
    ): "The agent writes one hook payload as JSON on stdin, and the hook entry reads it.",
    (
        "CommandSurface",
        "HookEntry",
    ): "The command surface calls the hook entry with stdin and the praxis home path for the hook verb.",
    (
        "HookEntry",
        "Recorder",
    ): "The hook entry parses the payload as a JSON object and calls the recorder with it.",
    (
        "HookEntry",
        "Ledger",
    ): "The hook entry records one hook event with its outcome, note, and duration, whatever happened.",
    (
        "Recorder",
        "Git",
    ): "The recorder runs git diff against HEAD and git ls-files, each with a 2 s timeout.",
    (
        "WorkingTree",
        "Recorder",
    ): "The recorder reads each untracked file, skips binary and large files, and hashes each line.",
    "rec-hashes": "The recorder stores baselines, pending pre-snapshots, and agent deltas as line-hash counts.",
    "rec-attributions": "At UserPromptSubmit and Stop, the recorder stores one attribution row per file.",
    (
        "Ledger",
        "Recorder",
    ): "The ledger returns the session baseline, the pending pre-snapshot, and the agent multiset.",
    (
        "Ledger",
        "PraxisHome",
    ): "The ledger writes its tables to ledger.db with SQLite in WAL mode.",
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
    (
        "Doctor",
        "ConsentedInstall",
    ): "The doctor calls the install functions that load the settings file and find the praxis hook entries.",
    (
        "Doctor",
        "AgentCheck",
    ): "The doctor calls locate for the repository of the current directory and attached_agents for the agent processes inside it.",
    (
        "PraxisHome",
        "Doctor",
    ): "The doctor opens ledger.db in SQLite read-only mode and counts sessions, hook events, unobservable or error events, and attribution rows.",
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
