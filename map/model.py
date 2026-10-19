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
        box=(16, 16, 190, 867),
        tone="host",
    ),
    Container(
        id="system",
        label="PRAXIS",
        sub="one process per command",
        box=(230, 16, 468, 1008),
        tone="server",
    ),
)

# A region contains a group of components with one function.
REGIONS = (
    Region(id="record", label="RECORD", box=(250, 80, 190, 204), container="system"),
    Region(id="ledger", label="LEDGER", box=(488, 80, 190, 112), container="system"),
    Region(
        id="commands", label="COMMANDS", box=(250, 320, 190, 112), container="system"
    ),
    Region(
        id="owner",
        label="SETUP AND CHECKS",
        box=(488, 320, 190, 204),
        container="system",
    ),
    Region(
        id="measure", label="MEASUREMENT", box=(250, 560, 190, 296), container="system"
    ),
    Region(
        id="analysis", label="ANALYSIS", box=(488, 560, 190, 204), container="system"
    ),
    Region(
        id="rejected",
        label="REJECTED INSTRUMENT",
        box=(250, 892, 190, 112),
        container="system",
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
        y=451,
    ),
    Component(
        id="Owner",
        does="The person who owns the repositories. The owner types praxis commands on a terminal and answers the consent and drill prompts.",
        kind="actor",
        container="outside",
        x=36,
        y=727,
    ),
    Component(
        id="Git",
        does="The git program. Praxis runs it as a subprocess and reads its output. It never runs git with a shell.",
        kind="actor",
        container="outside",
        x=36,
        y=543,
    ),
    Component(
        id="WorkingTree",
        does="The checkout of the owner's repository on disk. Praxis reads untracked files and Python source from it.",
        kind="actor",
        container="outside",
        x=36,
        y=819,
    ),
    Component(
        id="PraxisHome",
        does="The directory $PRAXIS_HOME (default ~/.praxis). It holds the SQLite ledger file ledger.db, settings backups, drill backups, owner component files, and scratch clones.",
        kind="actor",
        container="outside",
        x=36,
        y=635,
    ),
    # Source components.
    Component(
        id="CommandSurface",
        does="Parses the praxis command line and calls the component for each verb. It also prints attribution and session rows from the ledger.",
        interface="main(argv) -> int",
        implemented_by=("praxis.cli",),
        entry="main",
        region="commands",
        x=270,
        y=360,
        source_review="fc20e18c7fa12f25a3160902eb79d452fc01d2d0284ffbbb3d5903f42d7cf8a1",
    ),
    Component(
        id="HookEntry",
        does="Runs once for each hook event. It reads the payload, calls the recorder, records the hook event, and exits 0 on every input.",
        interface="run(stdin, praxis_home) -> int",
        implemented_by=("praxis.hooks",),
        entry="run",
        region="record",
        x=270,
        y=120,
        source_review="05dec417202f77cdf40437037a81e36f4ac7a0412cff566c9122ac45537e6b77",
    ),
    Component(
        id="Recorder",
        does="Snapshots the working tree as multisets of line hashes with git. It stores tool-call deltas and attributes lines to agent, owner, and baseline at prompt boundaries.",
        interface="handle(payload, store, now) -> Result",
        implemented_by=("praxis.recorder", "praxis.gitdiff"),
        entry="handle",
        region="record",
        x=270,
        y=212,
        source_review="8aa628d81bd84f698834513aea207d55c70813e0c8ea0e07f8c362bf5766437f",
    ),
    Component(
        id="Ledger",
        does="The SQLite ledger under $PRAXIS_HOME. It is the only module that inserts rows, and its CHECK constraints refuse derived rows without a claim class, sources, or coverage.",
        interface="Store.open / Store.attributions / Store.agent_multiset",
        implemented_by=("praxis.store",),
        entry="Store",
        kind="store",
        region="ledger",
        x=508,
        y=120,
        source_review="fe900c9bdf8deaec2ec44f28881ad84efd47014f54159a717a65058ffc426178",
    ),
    Component(
        id="ConsentedInstall",
        does="Prints the hook block for Claude Code or Codex. With --apply or --remove, it writes one settings file after the owner types yes on a terminal.",
        interface="apply(agent, remove, home, praxis_home, stdin, out) -> int",
        implemented_by=("praxis.install",),
        entry="apply",
        region="owner",
        x=508,
        y=360,
        source_review="737ce18cbd211a44fe02c16b11665b0fb40ea1850c9ded8f1e8cbbb9c60e0fc0",
    ),
    Component(
        id="Doctor",
        does="Reports what is installed, recorded, and pending. It opens the ledger in SQLite read-only mode and writes no file.",
        interface="run(cwd, home, praxis_home, out) -> int",
        implemented_by=("praxis.doctor",),
        entry="run",
        region="owner",
        x=508,
        y=452,
        source_review="0ce391beb5e7454a1dd13deb093d83b8100b2ca5c04177353dd5e7c6c7cb52d0",
    ),
    Component(
        id="ComponentMap",
        does="Makes the component map of a repository: ordered glob rules that give each path one component. It derives the map from git ls-files or loads the owner's file.",
        interface="derive(repo_root, repo_id, tracked) -> ComponentMap",
        implemented_by=("praxis.components",),
        entry="derive",
        region="measure",
        note="No other praxis module imports this module yet. The command surface has no components verb.",
        x=270,
        y=600,
        source_review="2a75a10f691c0783e3de5b8c1903445a7a408556928d07c4b7f232d18ef22e5e",
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
        y=692,
        source_review="da99158bd43c3fdd0e9ef78b1bcb5b706deb099285a372d631ac1bc7ba8da6ed",
    ),
    Component(
        id="TranscriptReader",
        does="Reads one Claude Code transcript at analysis time. It returns prompt, assistant, and tool-call records with identifiers, timestamps, counts, and hashes.",
        interface="read(path) -> Session",
        implemented_by=("praxis.transcript",),
        entry="read",
        region="analysis",
        note="No other praxis module imports this module yet. The name sets that it returns must never be stored.",
        x=508,
        y=600,
        source_review="cdcb088c4705b192065aa2446df307668375182a74fe8db20627df5c32c1fe39",
    ),
    Component(
        id="AgentCheck",
        does="Finds the repository root and the coding agent processes that work inside it. An unassisted probe must refuse when this check finds an agent.",
        interface="attached_agents(root) -> list[str]",
        implemented_by=("praxis.unassisted",),
        entry="attached_agents",
        region="measure",
        note="No other praxis module imports this module yet. praxis.drill still holds its own copy of these functions.",
        x=270,
        y=784,
        source_review="df33c064b1fd4bed5d7539acbfb065add6d4cd915ccfc70a86acb602dbb47c3b",
    ),
    Component(
        id="Scoring",
        does="Pure functions for proper scores, their decomposition, and small-sample randomization tests. They take numbers, return numbers, and give None when the data cannot give an answer.",
        interface="brier(p, o) -> float",
        implemented_by=("praxis.scoring",),
        entry="brier",
        region="analysis",
        note="No I/O and no imports from other praxis modules, so the map shows no flows for it. No other praxis module imports it yet.",
        x=508,
        y=692,
        source_review="04d2e1a0151fa3ea9b7a62c2f6b930e6489d7e7b79ddf640a10ac14aa9719f0a",
    ),
    Component(
        id="WriteItInstrument",
        does="The rejected write-it drill. It finds functions with agent lines, strips one body in place, runs the project's tests, and restores the file.",
        interface="run(cwd, home, store, test_cmd, min_fraction, list_only, stdin, out) -> int",
        implemented_by=("praxis.drill", "praxis.spans"),
        entry="run",
        region="rejected",
        note="docs/design/02-instrument.md is REJECTED. Design 03 section 12 deletes these modules. The agent check now has a copy in praxis.unassisted.",
        x=270,
        y=932,
        source_review="611a06d8680a75ea4c294a7c092df4a13f3f907684cc30df3f43cd661232659a",
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
            "praxis.cli:main@7f92ea345a0770f9619336ec9398b1192ee80fc1deabd3ad818ebe169cc66353",
            "praxis.hooks:run@274f9ceb29a04dcce555714a64215223e5b02e532d7d99bcc8850b99e35d8c6c",
        ),
        review_digest="66da05b30b49d994c8ace7f262f8fab9c98dfbbcb4f52a139e57df88fa185833",
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
            "praxis.store:Store@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
        ),
        review_digest="d5e37e8aefaf4eb6fc0a6a5e4b20034ebe49316eefcb63114fbea7cc91ed51eb",
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
            "praxis.store:Store@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
        ),
        id="rec-hashes",
        review_digest="903ea88297b8165a53334277763d75e6cb798a269eea727c2b083f3697c6deee",
    ),
    Flow(
        "Recorder",
        "Ledger",
        "attribution rows",
        "data",
        source_refs=(
            "praxis.recorder:attribute@51d9a05dc62fe47492b746dbe02ce3892111f0c5a310cbc10e9124882b20b092",
            "praxis.store:Attribution@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
        ),
        id="rec-attributions",
        review_digest="21fde126acfe5cf8593908c59c86b6e706bed36d6a6a0af6d44f05ddbe43fcc2",
    ),
    Flow(
        "Ledger",
        "Recorder",
        "stored multisets",
        "data",
        source_refs=(
            "praxis.store:Store@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
            "praxis.recorder:handle@51d9a05dc62fe47492b746dbe02ce3892111f0c5a310cbc10e9124882b20b092",
        ),
        review_digest="f1540bc3d7ac77a85620cd74e31d45ef1985f3d831dcd76896fc045b6e1db850",
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
            "praxis.cli:main@7f92ea345a0770f9619336ec9398b1192ee80fc1deabd3ad818ebe169cc66353",
            "praxis.install:apply@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
            "praxis.install:snippet@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
        ),
        review_digest="d5fe0caa336a1e8b90a3ca86a62a421fd9330ce52fe3a4693e1a2924d4d81a9f",
    ),
    Flow(
        "CommandSurface",
        "Doctor",
        "doctor call",
        "control",
        source_refs=(
            "praxis.cli:main@7f92ea345a0770f9619336ec9398b1192ee80fc1deabd3ad818ebe169cc66353",
            "praxis.doctor:run@ca1f8d48277796b39c5c5bc70bd0472591c4fde5b8b4c8dac0ad4923200d16f0",
        ),
        review_digest="ec5784caadf16cb22ab549b673f1eca7a8bf6d85e5bec9a4de7c7425fde98d71",
    ),
    Flow(
        "CommandSurface",
        "WriteItInstrument",
        "drill call",
        "control",
        source_refs=(
            "praxis.cli:main@7f92ea345a0770f9619336ec9398b1192ee80fc1deabd3ad818ebe169cc66353",
            "praxis.drill:run@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
            "praxis.drill:restore@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
        ),
        review_digest="487e9c43959a59a498f18385d21f4b488587b3842724d1b68c9da6b772b748f7",
    ),
    Flow(
        "Ledger",
        "CommandSurface",
        "attribution rows",
        "data",
        source_refs=(
            "praxis.store:Store@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
            "praxis.cli@7f92ea345a0770f9619336ec9398b1192ee80fc1deabd3ad818ebe169cc66353",
        ),
        review_digest="cc0e6733c1a2fe5566d04f150b9b9783b8bf2389a1946035837ed6946ae6185b",
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
            "praxis.doctor@ca1f8d48277796b39c5c5bc70bd0472591c4fde5b8b4c8dac0ad4923200d16f0",
            "praxis.install:load@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
            "praxis.install:is_ours@d3a4a8ea93aad6e8cbd2bab839ae50a063b38924c70b9e388e5a614f34fde984",
        ),
        review_digest="4b15b3875c1e4299c560cb8c5e6d5334d9ee64203e32928d5f947cf1efff369a",
    ),
    Flow(
        "Doctor",
        "WriteItInstrument",
        "agent check",
        "control",
        source_refs=(
            "praxis.doctor:run@ca1f8d48277796b39c5c5bc70bd0472591c4fde5b8b4c8dac0ad4923200d16f0",
            "praxis.drill:attached_agents@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
            "praxis.drill:pending_backups@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
        ),
        review_digest="a5ed8c4bdc260fcf74489d099777d393f1a1188c32b5089e042a041ca3bd9b0d",
    ),
    Flow("PraxisHome", "Doctor", "ledger counts", "data"),
    # The measurement.
    Flow("ComponentMap", "Git", "file listing", "control"),
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
    # The rejected instrument.
    Flow(
        "Ledger",
        "WriteItInstrument",
        "agent line hashes",
        "data",
        source_refs=(
            "praxis.store:Store@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
            "praxis.spans:candidates@0d95616db648a487c68db145c8b78fe06b96b614a26a0754068ce2cdeab237c3",
            "praxis.drill@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
        ),
        review_digest="61e4e3212190fc62e0fa1cfee6760c70dae0f3fd7ba53b4a3dcb4523d524499f",
    ),
    Flow(
        "WriteItInstrument",
        "Ledger",
        "items and results",
        "data",
        source_refs=(
            "praxis.drill@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
            "praxis.store:ItemRow@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
            "praxis.store:ResultRow@14573af1ba7d9502959e36f96434047562802a32c1eb99547034f77de1073c3d",
        ),
        review_digest="b90ab7715508b76fbb8ed13f06de4294d096786620ea09ef84f89176f6ef0c59",
    ),
    Flow(
        "WriteItInstrument",
        "Recorder",
        "line hashing",
        "control",
        source_refs=(
            "praxis.drill:locate@f6770b21d58a7607260c804269224a72a11496c0e58d018ea6aefadabecbf1eb",
            "praxis.spans:agent_line_numbers@0d95616db648a487c68db145c8b78fe06b96b614a26a0754068ce2cdeab237c3",
            "praxis.gitdiff:toplevel@7981f4d247dbe86168d192c4098b53805ea1bd77a91310b68ffb6b39c3a4cf0f",
            "praxis.gitdiff:line_hash@7981f4d247dbe86168d192c4098b53805ea1bd77a91310b68ffb6b39c3a4cf0f",
        ),
        review_digest="3fccffd2954051e5bcdf05f282fc378c9639a36c7fc677eeb6532bba31c3b9c9",
    ),
    Flow("WriteItInstrument", "WorkingTree", "function body", "data"),
    Flow("WriteItInstrument", "PraxisHome", "drill backup", "data"),
    Flow("Owner", "WriteItInstrument", "drill answer", "data"),
)

FLOW_KINDS: tuple[str, ...] = ()

# The invariants and the import rules are in the contract file, map/contract.py.
MODEL = Model(
    canvas=(714, 1040),
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
    "WriteItInstrument": "the rejected write-it instrument",
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
        "CommandSurface",
        "WriteItInstrument",
    ): "The command surface opens the ledger and calls the drill, or calls the drill restore.",
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
        "WriteItInstrument",
    ): "The doctor calls the drill module for pending drill backups and for agent processes attached to the repository.",
    (
        "PraxisHome",
        "Doctor",
    ): "The doctor opens ledger.db in SQLite read-only mode and counts rows in each table.",
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
        "Ledger",
        "WriteItInstrument",
    ): "The ledger returns the files and the line hashes that the agent added on the repository, and the test command.",
    (
        "WriteItInstrument",
        "Ledger",
    ): "The drill records the test command, one item for each validated candidate, and the result.",
    (
        "WriteItInstrument",
        "Recorder",
    ): "The drill calls the snapshot module for the repository root and for the line hash function.",
    (
        "WriteItInstrument",
        "WorkingTree",
    ): "The drill replaces one function body in the file, runs the project's tests, and restores the original bytes.",
    (
        "WriteItInstrument",
        "PraxisHome",
    ): "The drill keeps the original file bytes under drills until it restores them.",
    (
        "Owner",
        "WriteItInstrument",
    ): "The owner presses Enter to run the tests, or types q to stop the drill.",
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
