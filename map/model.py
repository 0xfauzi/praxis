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
        box=(16, 16, 190, 754),
        tone="host",
    ),
    Container(
        id="system",
        label="PRAXIS",
        sub="one process per command",
        box=(230, 16, 658, 952),
        tone="server",
    ),
)

# A region contains a group of components with one function.
# The record, ledger, and instrument regions follow the layers of
# docs/design/03-measurement.md section 12. The analysis region holds the protocol
# modules (report, scoring) and the transcript reader that the probe and the report call.
# The policy layer has no module yet.
REGIONS = (
    Region(id="record", label="RECORD", box=(250, 80, 190, 204), container="system"),
    Region(id="ledger", label="LEDGER", box=(250, 320, 190, 112), container="system"),
    Region(
        id="commands", label="COMMANDS", box=(488, 80, 190, 112), container="system"
    ),
    Region(
        id="owner",
        label="SETUP AND CHECKS",
        box=(250, 652, 190, 204),
        container="system",
    ),
    Region(
        id="measure", label="INSTRUMENT", box=(488, 320, 380, 296), container="system"
    ),
    Region(
        id="analysis", label="ANALYSIS", box=(488, 652, 190, 296), container="system"
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
        y=430,
    ),
    Component(
        id="Owner",
        does="The person who owns the repositories. The owner types praxis commands on a terminal, answers the consent prompt, and answers task forecasts and probe items.",
        kind="actor",
        container="outside",
        x=36,
        y=614,
    ),
    Component(
        id="Git",
        does="The git program. Praxis runs it as a subprocess and reads its output. It never runs git with a shell.",
        kind="actor",
        container="outside",
        x=36,
        y=522,
    ),
    Component(
        id="WorkingTree",
        does="The checkout of the owner's repository on disk. Praxis reads untracked files and Python source from it.",
        kind="actor",
        container="outside",
        x=36,
        y=338,
    ),
    Component(
        id="PraxisHome",
        does="The directory $PRAXIS_HOME (default ~/.praxis). It holds ledger.db, settings-backups/, owner component maps as components/<repo_id>.toml, and scratch clones under scratch/.",
        kind="actor",
        container="outside",
        x=36,
        y=706,
    ),
    # Source components.
    Component(
        id="CommandSurface",
        does="Parses the praxis command line and calls the module of each verb from one dispatch table. It imports the measurement modules only inside their handlers, and it prints attribution and session rows from the ledger.",
        interface="main(argv) -> int",
        implemented_by=("praxis.cli",),
        entry="main",
        region="commands",
        x=508,
        y=120,
        source_review="9943b438b5108ba1d9b81728fc40c35449281db8d3a8d52f37a1c6e9b05ad78b",
    ),
    Component(
        id="HookEntry",
        does="Runs once for each hook event. It reads the payload, calls the recorder, records the hook event, and exits 0 on every input. Its stdout stays empty, except one fixed notice when the recorder closes a task segment.",
        interface="run(stdin, praxis_home) -> int",
        implemented_by=("praxis.hooks",),
        entry="run",
        region="record",
        x=270,
        y=120,
        source_review="1094b45d12571847700e916fa123b908bc4c301e5c0f5dbf63a20c2aebb08907",
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
        y=212,
        source_review="6c1936be9e3688470365bbec0c2e9f8cc5f60d2238294b3e81dbf67eafbc3461",
    ),
    Component(
        id="Ledger",
        does="The SQLite ledger under $PRAXIS_HOME. praxis.schema holds its DDL and closed vocabularies, and praxis.store holds every INSERT. It keeps recorder rows, component maps, tasks, probes, forecasts, decisions, and flags.",
        interface="Store.open / Store.task_register / Store.segment_close / Store.probe_response",
        implemented_by=("praxis.store", "praxis.schema"),
        entry="Store",
        kind="store",
        region="ledger",
        note="Each table in schema.CLAIMS has a CHECK that fixes its one claim class and refuses an empty source list. The attributions table accepts any claim class, and hook_events has none. Triggers make the measurement tables append-only, and a task or a map sets each transition column once. segment_close writes the outcomes and the task close in one transaction. The survival lookup intersects agent line-hash multisets with a probe multiset, so two identical lines count as two. When PRAGMA user_version differs from SCHEMA_VERSION, Store.open runs the DDL and then migrate.",
        x=270,
        y=360,
        source_review="1cf8544cd4c3eed65392c975a23d8de6b01bf0bc348acf774b953fe92a3a3f5c",
    ),
    Component(
        id="ConsentedInstall",
        does="Prints the hook block for Claude Code or Codex. With --apply or --remove, it writes one settings file after the owner types yes on a terminal. The Claude Code PostToolUse matcher also selects AskUserQuestion and ExitPlanMode.",
        interface="apply(agent, remove, home, praxis_home, stdin, out) -> int",
        implemented_by=("praxis.install",),
        entry="apply",
        region="owner",
        x=270,
        y=692,
        source_review="a89741af43f721cd00bdc87424b63d81c30a80d6cf46d22c54bac897d746faa0",
    ),
    Component(
        id="Doctor",
        does="Reports the installed hooks, the ledger counts, the frozen maps, tasks by status, the binding rate, void reasons, and flags. It also reports attached agents and probes due, and it never creates the ledger.",
        interface="run(cwd, home, praxis_home, out) -> int",
        implemented_by=("praxis.doctor",),
        entry="run",
        region="owner",
        note="Counts come from a SQLite read-only connection. Probes due come from probe.due, which opens a Store. The doctor asks for them only at SCHEMA_VERSION, so no migration can run.",
        x=270,
        y=784,
        source_review="3c64779158b75e23546606ee7f913dc203ed5e103cc7b386b191f77e59f7acea",
    ),
    Component(
        id="ComponentMap",
        does="Makes the component map of a repository: ordered glob rules that give each path one component. It derives the map from git ls-files or loads the owner's file.",
        interface="derive(repo_root, repo_id, tracked) -> ComponentMap",
        implemented_by=("praxis.components",),
        entry="derive",
        region="measure",
        note="The ledger calls from_json before it stores a map. The recorder, the item pool, the task registration, the probe, and the report parse the frozen map and assign paths with it.",
        x=698,
        y=544,
        source_review="2c00328d5807fd4016fbec1ce09b31885a4d6785f28d8d9dc2b545064c2d3c5f",
    ),
    Component(
        id="Extractors",
        does="Extracts ground truth at component grain: static imports, runtime call traces in a scratch clone, and SQLite table references. It returns identifiers and counts only.",
        interface="static_imports(repo_root, assign, files) -> StaticGraph",
        implemented_by=("praxis.extract",),
        entry="static_imports",
        region="measure",
        note="The recorder, the item pool, the task registration, and the probe call static_imports. No praxis module calls run_scenario or scratch_clone yet.",
        x=508,
        y=452,
        source_review="38da1f04d4378f8be40b33755924efb69bb12ae916098a09810cc10748751a15",
    ),
    Component(
        id="ItemPool",
        does="Freezes the component map of a repository and writes a D1 item for each ordered pair of parsed product components. Without freeze, it shows the frozen map and the pool supply.",
        interface="freeze(repo_root, praxis_home, stdout, now, reason) -> int",
        implemented_by=("praxis.pool",),
        entry="freeze",
        region="measure",
        note="A change to a frozen map needs a --reason from MAP_REASONS. A second freeze writes only the missing pairs. A component without a parsed Python file gives no items, not items with truth 0.",
        x=508,
        y=360,
        source_review="8b0a62dcd0e19b9f51a444942f9365f600499cd228d76678a717b98ad810873d",
    ),
    Component(
        id="TaskRegistration",
        does="Registers the next task before its prompt and draws its bound items and its arm from the committed seed. In the treatment arm, it asks the owner for forecasts. It shows the feedback of the last closed task.",
        interface="register(repo_root, praxis_home, stdin, stdout, now, seed) -> int",
        implemented_by=("praxis.task",),
        entry="register",
        region="measure",
        note="The arm generator reads only the seed, the repository id, and the task ordinal. The recorder, not this module, binds the task to the next prompt and closes its segment. praxis last calls last, which prints only the feedback.",
        x=698,
        y=452,
        source_review="03504b8f10eec4c88a0aefbbbacf53314fdb38e6b0c11e94402153bb60f7bd5b",
    ),
    Component(
        id="Probe",
        does="Gives one unassisted probe kind: structural, drift, decision, knockout, or Pathfinder. It refuses and records a flag when an agent is attached, and it stores closed-vocabulary answers only.",
        interface="administer(repo_root, praxis_home, stdin, stdout, now, kind) -> int",
        implemented_by=("praxis.probe",),
        entry="administer",
        region="measure",
        note="A structural item without a truth from the extractors now is not asked and stays due. The knockout is simulated from stored scenario runs. Pathfinder ratings stay in memory and only the scores are stored.",
        x=508,
        y=544,
        source_review="50160a556516ac8a5ac034308100ca83ff293b9cbcedac4d2e18e0592a245090",
    ),
    Component(
        id="TranscriptReader",
        does="Finds and reads one Claude Code transcript at analysis time. It returns prompt, assistant, and tool-call records with identifiers, timestamps, counts, and hashes.",
        interface="read(path) -> Session",
        implemented_by=("praxis.transcript",),
        entry="read",
        region="analysis",
        note="The probe and the report call locate and read, and the recorder calls options_hash. The name sets that it returns must never be stored.",
        x=508,
        y=692,
        source_review="a3e90cb3014a9d27f0fbdfd362ab8ef587bf7ca92a7a81ab624aadbf114f1fd7",
    ),
    Component(
        id="AgentCheck",
        does="Finds the repository root and the coding agent processes that work inside it. An unassisted probe must refuse when this check finds an agent.",
        interface="attached_agents(root) -> list[str]",
        implemented_by=("praxis.unassisted",),
        entry="attached_agents",
        region="measure",
        note="The command surface, the doctor, the item pool, the task registration, and the probe call locate. The doctor and the probe call attached_agents.",
        x=698,
        y=360,
        source_review="1c037910c4c0160ab303957611b8fa10685a12261420ede77b5e243008353adf",
    ),
    Component(
        id="Scoring",
        does="Pure functions for proper scores, their decomposition, Pathfinder networks, and small-sample randomization tests. They take numbers, return numbers, and give None when the data cannot give an answer.",
        interface="brier(p, o) -> float",
        implemented_by=("praxis.scoring",),
        entry="brier",
        region="analysis",
        note="No I/O and no imports from other praxis modules. The probe calls pfnet and pfsim. The report calls bss, decomposition, corp_reliability, randomization_test, and snap.",
        x=508,
        y=876,
        source_review="0d81a3563421865202aa3e11bfc122d80ae6b4a9a14dc8dc13492490bda7d375",
    ),
    Component(
        id="Report",
        does="Computes references, Brier scores, decomposition, CORP bands, and randomization tests from the ledger at analysis time. It writes nothing, and its text holds counts, numbers, and reason codes only.",
        interface="scores(store, repo_id, seed, draws) -> Scores",
        implemented_by=("praxis.report",),
        entry="scores",
        region="analysis",
        note="render refuses the words cause, impact, and would unless a pre-registered rule is met, and then it names the rule. A reference that cannot be fitted is NULL, never another reference.",
        x=508,
        y=784,
        source_review="882b1d8435c1228e2326c6f697c1c6ee61e7e90009d9e9e79079e4d6ae719be9",
    ),
)

# Each flow gives source, target, artifact, and kind.
# fmt: off
FLOWS: tuple[Flow, ...] = (
    # The coding agent and the hook.
    Flow("CodingAgent", "CommandSurface", "praxis hook", "control"),
    Flow("CodingAgent", "HookEntry", "hook payload", "data"),
    Flow("HookEntry", "CodingAgent", "terminal notice", "data"),
    Flow("CommandSurface", "HookEntry", "hook call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.hooks:run@012b2082ad462d9ea21f2371c2dfa98b3db6a55743dc8b283c63baa0db5229f3"),
         review_digest="154d76144b78c4724bf3504a442fe5fe27a396b904e85ecb1ede2d498bcf8a5b"),
    Flow("HookEntry", "Recorder", "event payload", "data",
         source_refs=("praxis.hooks:run@012b2082ad462d9ea21f2371c2dfa98b3db6a55743dc8b283c63baa0db5229f3", "praxis.recorder:handle@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6"),
         review_digest="1fa5e4cdedeb4a5538c4bde8176808a3a1206ffc519e9195e5475559365038b8"),
    Flow("Recorder", "HookEntry", "hook result", "data",
         source_refs=("praxis.recorder:Result@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.hooks:run@012b2082ad462d9ea21f2371c2dfa98b3db6a55743dc8b283c63baa0db5229f3"),
         review_digest="0c7cce7769e2c0c31e694202815eb2f0b5322872b3beade753491541ab42d13a"),
    Flow("HookEntry", "Ledger", "hook event", "data",
         source_refs=("praxis.hooks:run@012b2082ad462d9ea21f2371c2dfa98b3db6a55743dc8b283c63baa0db5229f3", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="c0a2a77ca7a594e204ad69ec2eb15e3e0ba9cb59b00f6e0b5669cea4cff97c64"),
    # The recorder.
    Flow("Recorder", "Git", "git commands", "control"),
    Flow("WorkingTree", "Recorder", "untracked files", "data"),
    Flow("Recorder", "Ledger", "line-hash counts", "data", id="rec-hashes",
         source_refs=("praxis.recorder:handle@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="5d440f086d0b92538911968cf630b058b102b23d4f914f3f56c278b5322f88cb"),
    Flow("Recorder", "Ledger", "attribution rows", "data", id="rec-attributions",
         source_refs=("praxis.recorder:attribute@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.schema:Attribution@7a7e05f58e0fb6178da60ad17032b4b5265ae8798f42f490b366c93d3f06d0c4"),
         review_digest="3eeca85d45225cebefdd3009b10a70dae9132de40dc2ddc1dd436c67261ce00a"),
    Flow("Recorder", "Ledger", "file edges", "data", id="rec-edges",
         source_refs=("praxis.recorder:scan_files@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="7736a2247ad6cce2f93b889e2f0cede4a25a4c352f49c45a550a31f693319718"),
    Flow("Recorder", "Ledger", "task transitions", "data", id="rec-tasks",
         source_refs=("praxis.recorder:handle@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="6b9242a37705a70ff123843b0f0a867a0637ab6eb537d835ec0f44aed7a1a05d"),
    Flow("Recorder", "Ledger", "decision rows", "data", id="rec-decisions",
         source_refs=("praxis.recorder:decision_shape@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="24e726f6806a1964e74acdc6f7934564a6caa1dc97149910c5f8aaab17ab209c"),
    Flow("Ledger", "Recorder", "session state", "data",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.recorder:handle@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6"),
         review_digest="5f6a712ccc022f1c7081b7a87bfc526ea73d5f1bf943b8072b11ad2e06224974"),
    Flow("Recorder", "ComponentMap", "map lookup", "control",
         source_refs=("praxis.recorder:scan_files@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.components:from_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2", "praxis.components:assign@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2"),
         review_digest="6144c6a7a2a04ce6b775d5f6102169801804590f4e74a4467d015648548950f3"),
    Flow("Recorder", "Extractors", "import scan", "control",
         source_refs=("praxis.recorder:scan_files@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.extract:static_imports@d6ed8095f6dc9cda58ceee603412fb46d33c1a8e253d3bc49b53abd14c64c74a"),
         review_digest="52dd8b2f8719a3afb8b6c2ae5b32ee2698316f22e6df1c06ee2fa6ef3b0e437b"),
    Flow("Recorder", "TranscriptReader", "options hash", "control",
         source_refs=("praxis.recorder:decision_shape@7d746a71d87ccfc4bceb91b32296b5cb0f7fee3dc25c50a5ae1ed503b56f8be6", "praxis.transcript:options_hash@e8e0a3a34038444eed2b2e311e431a9ed522347635084331d2a9be32446a6019"),
         review_digest="624e49b2dc1975be4dc66fe2bea72f1c5c68ea9b7a02509555e89ab48e7e8581"),
    Flow("Ledger", "PraxisHome", "SQLite rows", "data"),
    # The owner's commands.
    Flow("Owner", "CommandSurface", "command line", "control"),
    Flow("CommandSurface", "ConsentedInstall", "setup call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.install:apply@ad42006138b9a380bdac7ea842d438951043d63a4e9be486e9692e06586f1465", "praxis.install:snippet@ad42006138b9a380bdac7ea842d438951043d63a4e9be486e9692e06586f1465"),
         review_digest="d4356b0a45f404dea712292983983b8643edd362689e9097824f893a02f9de05"),
    Flow("CommandSurface", "Doctor", "doctor call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.doctor:run@b1a4d79ea904697e83ac5886e6d2a38285dcdfb9fbc953ab51476eb667a4e2f5"),
         review_digest="202bb2e3df7ceafad044e57ef0315e3edc5d485ab9e57d96c6b5f543a8bed0e9"),
    Flow("Ledger", "CommandSurface", "attribution rows", "data",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a"),
         review_digest="fd644587c9308dcb81a02680ccc535191039420ecf0f5776c39b3b493aada7ba"),
    Flow("CommandSurface", "TaskRegistration", "task call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.task:register@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88", "praxis.task:last@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88"),
         review_digest="2771a8d69e127381593594d1a0cd55775a40c90b92351bbe2f7f3c1a1168c9d5"),
    Flow("CommandSurface", "Probe", "probe call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.probe:administer@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75"),
         review_digest="89b79f24433619776b733cac5b17692f22f1707bcfa548491cc24df53a02f213"),
    Flow("CommandSurface", "ItemPool", "components call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.pool:freeze@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a", "praxis.pool:show@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a"),
         review_digest="16a27d7cab9eb05a511d6dc82843f0362ff933e9e31b2429dd9595f653da65fb"),
    Flow("CommandSurface", "Report", "report call", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.report:scores@68c935a88d75d81366b4ce27f1ae783455e5e1885fbe240a35de077e194b9b78", "praxis.report:render@68c935a88d75d81366b4ce27f1ae783455e5e1885fbe240a35de077e194b9b78"),
         review_digest="48c6d8c05c4850cbf49f9707dc3dfb40cbd2055126a25e4b0b24d299687cfbeb"),
    Flow("CommandSurface", "AgentCheck", "repository lookup", "control",
         source_refs=("praxis.cli@ada29dc9a30cb6e8ba96e8a4688965f967c7faeefad15ad56cb44108b9e3737a", "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce"),
         review_digest="0a57c6149ec5c6e67c684cf42ff156585116848dd7e6a476f9834ecfb0150560"),
    # The consented install.
    Flow("Owner", "ConsentedInstall", "consent answer", "data"),
    Flow("ConsentedInstall", "CodingAgent", "hook block", "data"),
    Flow("ConsentedInstall", "PraxisHome", "settings backup", "data"),
    # The doctor.
    Flow("Doctor", "ConsentedInstall", "settings check", "control",
         source_refs=("praxis.doctor@b1a4d79ea904697e83ac5886e6d2a38285dcdfb9fbc953ab51476eb667a4e2f5", "praxis.install:load@ad42006138b9a380bdac7ea842d438951043d63a4e9be486e9692e06586f1465", "praxis.install:is_ours@ad42006138b9a380bdac7ea842d438951043d63a4e9be486e9692e06586f1465"),
         review_digest="71c1d73e016a4704a96a7e0ee2f78e6a1e2e16e00e1637487d9b66b1e3436ae8"),
    Flow("Doctor", "AgentCheck", "agent check", "control",
         source_refs=("praxis.doctor:run@b1a4d79ea904697e83ac5886e6d2a38285dcdfb9fbc953ab51476eb667a4e2f5", "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce", "praxis.unassisted:attached_agents@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce"),
         review_digest="839441e290c07b145a3d33a0110fe77440fdf61ab610ed7cacf3adbafeb5afe4"),
    Flow("Doctor", "Probe", "due query", "control",
         source_refs=("praxis.doctor@b1a4d79ea904697e83ac5886e6d2a38285dcdfb9fbc953ab51476eb667a4e2f5", "praxis.probe:due@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75"),
         review_digest="3ec4de6b31484330b39be16775dd2074dc88bf9e9659cc99650b4063dc94f2be"),
    Flow("PraxisHome", "Doctor", "ledger counts", "data"),
    # The component map and the extractors.
    Flow("ComponentMap", "Git", "file listing", "control"),
    Flow("Ledger", "ComponentMap", "map check", "control",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.components:from_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2"),
         review_digest="1d32dadaacf90c547130204159b805619981f103ef56902a82bb7bd5bda78ef4"),
    Flow("PraxisHome", "ComponentMap", "owner map file", "data"),
    Flow("Extractors", "Git", "clone command", "control"),
    Flow("WorkingTree", "Extractors", "Python source", "data"),
    Flow("Extractors", "PraxisHome", "scratch clone", "data"),
    Flow("CodingAgent", "TranscriptReader", "session transcript", "data"),
    Flow("CodingAgent", "AgentCheck", "agent processes", "data"),
    Flow("AgentCheck", "Recorder", "repository root", "control",
         source_refs=("praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce", "praxis.gitdiff:toplevel@8ec06a6fdbe03f140cb84201248234310de8bcdb65842f4c1fbb236d16c0a83e", "praxis.gitdiff:repo_id@8ec06a6fdbe03f140cb84201248234310de8bcdb65842f4c1fbb236d16c0a83e"),
         review_digest="0232339a6a05ca93f675a2453c7ed066f5a3c779327505df5553a40bf7ba5718"),
    # The item pool.
    Flow("ItemPool", "AgentCheck", "repository lookup", "control",
         source_refs=("praxis.pool:freeze@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a", "praxis.pool:show@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a", "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce"),
         review_digest="4e336631e22567e6cc8ac33d07bfd9d14372450fea2bae189457d6affdfc9d64"),
    Flow("ItemPool", "ComponentMap", "map derivation", "control",
         source_refs=("praxis.pool@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a", "praxis.components:derive@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2", "praxis.components:load_owner@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2", "praxis.components:to_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2"),
         review_digest="01ecacabd6e405f4f7bbe57ef83ebae823bb738249299015105a23b361312d05"),
    Flow("ItemPool", "Extractors", "import scan", "control",
         source_refs=("praxis.pool@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a", "praxis.extract:static_imports@d6ed8095f6dc9cda58ceee603412fb46d33c1a8e253d3bc49b53abd14c64c74a", "praxis.extract:reach@d6ed8095f6dc9cda58ceee603412fb46d33c1a8e253d3bc49b53abd14c64c74a"),
         review_digest="46e0bb6b27fbcbb4c099ee85c29c792e9197e8fac4c6123a9c19116cff5e16f8"),
    Flow("ItemPool", "Git", "status command", "control"),
    Flow("ItemPool", "Ledger", "map and items", "data",
         source_refs=("praxis.pool@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="ad711b68ac7de245e9ff1eb029236125d767d1e3b1be3109bbb766d2630eeea7"),
    Flow("Ledger", "ItemPool", "frozen map", "data",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.pool@52515a86d5c345f943958319c0abf270f6802096457af0056d09976aecd7a24a"),
         review_digest="66402dc57487db537b6c8976d7d935c8e928b606b305d12495a2e398a3d8b56a"),
    # The task registration.
    Flow("Owner", "TaskRegistration", "forecast answers", "data"),
    Flow("TaskRegistration", "AgentCheck", "repository lookup", "control",
         source_refs=("praxis.task:register@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88", "praxis.task:last@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88", "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce"),
         review_digest="5df2c3f246687acb457d42918e0edf2e14aa1bc4f00f02daad0f1b733867cfe2"),
    Flow("TaskRegistration", "ComponentMap", "map lookup", "control",
         source_refs=("praxis.task@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88", "praxis.components:from_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2", "praxis.components:product_components@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2"),
         review_digest="74a422c99f77f41a0d0230e314d54c83c98f77287ab2c11a33f30b8af572e8bf"),
    Flow("TaskRegistration", "Extractors", "import scan", "control",
         source_refs=("praxis.task@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88", "praxis.extract:static_imports@d6ed8095f6dc9cda58ceee603412fb46d33c1a8e253d3bc49b53abd14c64c74a"),
         review_digest="e91961d5e599cfa664433ccc6ddf3375b203aad52de45d7b918c4fba19e1f2a8"),
    Flow("TaskRegistration", "Ledger", "task rows", "data",
         source_refs=("praxis.task@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="8e44ac4fdb2f46895539271fe14ead0d746695b8144c2e965be8c0676ffcd3b4"),
    Flow("Ledger", "TaskRegistration", "items and outcomes", "data",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.task@c63157dd6bdb82a38dffbbf89f2eb8577c75d068fb9c5baeb5fab45e092f1d88"),
         review_digest="c76aca427e72539db9dcb9286ea8fe2c1b1600fc8ed47ee88974cbb5bb1aca1e"),
    # The probe.
    Flow("Owner", "Probe", "probe answers", "data"),
    Flow("Probe", "AgentCheck", "agent check", "control",
         source_refs=("praxis.probe:administer@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.unassisted:locate@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce", "praxis.unassisted:attached_agents@e0098ef9d9441ea9e8d4ee4691b9455fe259c4a2a3307565d22c6541c89ebfce"),
         review_digest="98f8b7d5f7abcf56c025dfcff0ffc9b8a423f82125873b4a52afb7885f38e902"),
    Flow("Probe", "ComponentMap", "map lookup", "control",
         source_refs=("praxis.probe@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.components:from_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2", "praxis.components:product_components@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2"),
         review_digest="2c67cec4f0632e4670035bcf5d503420b680e0edf96f246ba9f950564275d3e1"),
    Flow("Probe", "Extractors", "import scan", "control",
         source_refs=("praxis.probe@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.extract:static_imports@d6ed8095f6dc9cda58ceee603412fb46d33c1a8e253d3bc49b53abd14c64c74a", "praxis.extract:reach@d6ed8095f6dc9cda58ceee603412fb46d33c1a8e253d3bc49b53abd14c64c74a"),
         review_digest="9ffec8786690755aa2e8a0d1b2e3ae52c6764d3f52bcaa9e2d0621ee273561df"),
    Flow("Probe", "Scoring", "rating distances", "data",
         source_refs=("praxis.probe@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.scoring:pfnet@07dbed49c26d66abf2aea67bfbc50db67bb435f65681ad40ce0deb4f9a1eeaf0", "praxis.scoring:pfsim@07dbed49c26d66abf2aea67bfbc50db67bb435f65681ad40ce0deb4f9a1eeaf0"),
         review_digest="365b980b7b0f413d39a44dc2a1b9be3d142d20e5e98dae2a856db106339782f6"),
    Flow("Probe", "TranscriptReader", "transcript check", "control",
         source_refs=("praxis.probe@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.transcript:locate@e8e0a3a34038444eed2b2e311e431a9ed522347635084331d2a9be32446a6019", "praxis.transcript:read@e8e0a3a34038444eed2b2e311e431a9ed522347635084331d2a9be32446a6019"),
         review_digest="64123e3cca9f72dd1636b6daae17a3431a53a082d5a8ce8de37af5b8ab2a3116"),
    Flow("Probe", "Git", "status command", "control"),
    Flow("Probe", "Ledger", "probe responses", "data",
         source_refs=("praxis.probe@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf"),
         review_digest="691d2c88dcad89ffd2e0af16bc47e5c67a7e7fd7cc38f6f7426b500b1a2a62ae"),
    Flow("Ledger", "Probe", "due items", "data",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.probe@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75", "praxis.probe:due@1a60dcee8c11fc60f4aa7a91ded5b1b83c0a820a9d71afbdb571e6b9a3baaa75"),
         review_digest="4e7b1e3f72d6f59dff1b0db89744880a061b99c77457a1a8d6726418d1a07bd5"),
    # The report.
    Flow("Ledger", "Report", "measurement rows", "data",
         source_refs=("praxis.store:Store@ccd921da2a2ac602a30eadb63637772e2835a69032b0477126e804ff357e36bf", "praxis.report:scores@68c935a88d75d81366b4ce27f1ae783455e5e1885fbe240a35de077e194b9b78"),
         review_digest="ede0b515808ed6bb9ae5ee212d0a19d9e568bdc659e660f52ff09972afda90b2"),
    Flow("Report", "ComponentMap", "map lookup", "control",
         source_refs=("praxis.report@68c935a88d75d81366b4ce27f1ae783455e5e1885fbe240a35de077e194b9b78", "praxis.components:from_json@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2", "praxis.components:assign@14512efa5dc730de8c4d744478074e61dda64429fb74424506b90fda989803b2"),
         review_digest="d99ee18b8fd8f071e820f8f260a560e287bfaa8b3ab08d767909f8a91c900c65"),
    Flow("TranscriptReader", "Report", "prompt names", "data",
         source_refs=("praxis.transcript:read@e8e0a3a34038444eed2b2e311e431a9ed522347635084331d2a9be32446a6019", "praxis.transcript:Prompt@e8e0a3a34038444eed2b2e311e431a9ed522347635084331d2a9be32446a6019", "praxis.report@68c935a88d75d81366b4ce27f1ae783455e5e1885fbe240a35de077e194b9b78"),
         review_digest="812a6d858a82bb4a768eb92a76ba68f77a70a6d308d13956c46a700cc9cd843e"),
    Flow("Report", "Scoring", "forecasts and outcomes", "data",
         source_refs=("praxis.report@68c935a88d75d81366b4ce27f1ae783455e5e1885fbe240a35de077e194b9b78", "praxis.scoring:bss@07dbed49c26d66abf2aea67bfbc50db67bb435f65681ad40ce0deb4f9a1eeaf0", "praxis.scoring:decomposition@07dbed49c26d66abf2aea67bfbc50db67bb435f65681ad40ce0deb4f9a1eeaf0", "praxis.scoring:randomization_test@07dbed49c26d66abf2aea67bfbc50db67bb435f65681ad40ce0deb4f9a1eeaf0"),
         review_digest="6c7c5c5777a3f6822e86111f3785593acf05434511e4043a0ef5aa835224b9be"),
)
# fmt: on

FLOW_KINDS: tuple[str, ...] = ()

# The invariants and the import rules are in the contract file, map/contract.py.
MODEL = Model(
    canvas=(904, 984),
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
    "ItemPool": "the item pool",
    "TaskRegistration": "the task registration",
    "Probe": "the unassisted probe",
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
    ): "The command surface calls freeze for praxis components freeze and show for praxis components.",
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
    ): "The item pool loads the owner map or derives one, serializes it with to_json, and assigns paths with it.",
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
    ): "The item pool stores and freezes the map, writes the D1 items at epoch 0, and flags too_few_components.",
    (
        "Ledger",
        "ItemPool",
    ): "The ledger returns the frozen map, the D1 pairs that already have items, and the item counts.",
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
    ): "The ledger returns the frozen map, the unexposed items, the closed tasks with their outcomes, and the exposures, forecasts, and divergences of the last task.",
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
    ): "The probe stores structural, drift, decision, knockout, and Pathfinder results, and the agent_attached and abandoned flags.",
    (
        "Ledger",
        "Probe",
    ): "The ledger returns the frozen map, unanswered exposures, edge history, unprobed decisions, scenario runs, and the drift answers of the week.",
    # The report.
    (
        "Ledger",
        "Report",
    ): "The ledger returns the frozen map, tasks, outcomes, flagged tasks, forecasts, probe responses, D1 items, and edge history.",
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
    ): "The report calls bss, decomposition, corp_reliability, and randomization_test with forecasts and outcomes.",
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
