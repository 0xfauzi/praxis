"""The ledger's DDL and closed vocabularies. store.py executes it and holds every INSERT
(the derived-writes-only-in-store hook); this module holds no write.

No TEXT column holds a line of a repository, a prompt, a tool's output or anything the
owner typed: ids, hashes, timestamps, closed-vocabulary codes and JSON of those. Where
the schema can refuse more (hex hash columns, codes, the probability grid, one claim
class per table) it does. Measurement rows are append-only, enforced by triggers; a
task's and a map's transition columns are each set once. A CHECK passes on NULL, which
is why a nullable column needs no IS NULL branch.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

CLAIM_CLASSES = ("observed", "derived", "comparative", "associative", "predictive")
COVERAGES = ("full", "partial", "no_baseline")
EVIDENCE_COVERAGES = ("full", "partial", "unobservable")
GRID = (0.1, 0.3, 0.5, 0.7, 0.9)
MAP_REASONS = ("new_module", "regrouped", "renamed", "correction")
VOID_REASONS = (
    "superseded", "map_changed", "hook_error", "recorder_gap", "head_not_descendant",
    "no_segment",
)  # fmt: skip
# Design 03 section 10 (G1-G22) and the 4.4 f flags they cite. G4 agent_previewed, G8
# late_prediction, G12 is deadline_met on the response, G15 unmapped_paths, G16
# version_change, G17 abandoned, G20 the three decision voids, G21 zero_forbidden; the
# rest are the 4.4 f exclusions, the 4.1 a probe_missed, the attached-agent refusal and
# the 2.2 too_few_components.
FLAG_CODES = (
    "agent_previewed", "late_prediction", "owner_answered_question",
    "non_human_turn_in_segment", "owner_edited_in_segment", "interrupted",
    "recorder_gap", "codex_unrecorded", "version_change", "empty_segment",
    "agent_attached", "probe_missed", "transcript_changed", "transcript_swept",
    "reversed", "zero_forbidden", "too_few_components", "unmapped_paths", "abandoned",
)  # fmt: skip
POINTERS = ("prompt_id", "turn_id", "permission_mode", "agent_id", "cli_version")
# One claim class per derived table; the CHECK and the INSERT both read it from here.
CLAIMS = {
    "file_edges": "derived", "scenario_runs": "derived", "forecasts": "observed",
    "outcomes": "derived", "probe_items": "derived", "probe_responses": "observed",
    "drift_responses": "observed", "divergences": "derived", "decisions": "observed",
    "decision_responses": "observed", "knockouts": "observed",
    "pathfinder_runs": "derived",
}  # fmt: skip
EV_COLS = ("coverage", "sources", "reducer_version", "assumptions", "warnings")


def _in(values: Sequence[object]) -> str:
    return "(" + ", ".join(repr(v) for v in values) + ")"


def _hex(column: str, *lengths: int) -> str:
    # NULL passes a CHECK, so the same clause serves nullable and NOT NULL columns.
    sizes = ", ".join(map(str, lengths))
    return f"CHECK ({column} NOT GLOB '*[^0-9a-f]*' AND length({column}) IN ({sizes}))"


def _array(column: str, non_empty: bool = False) -> str:
    # CASE, not AND: json_type raises on malformed JSON and AND does not short-circuit.
    tail = f" AND json_array_length({column}) > 0" if non_empty else ""
    return (
        f"CHECK (CASE WHEN json_valid({column}) THEN json_type({column}) = 'array'"
        f"{tail} ELSE 0 END)"
    )


def _evidence(table: str) -> str:
    return f"""claim_class TEXT NOT NULL CHECK (claim_class = '{CLAIMS[table]}'),
  coverage TEXT NOT NULL CHECK (coverage IN {_in(EVIDENCE_COVERAGES)}),
  sources TEXT NOT NULL {_array("sources", non_empty=True)},
  reducer_version TEXT NOT NULL CHECK (reducer_version <> ''),
  assumptions TEXT NOT NULL {_array("assumptions")},
  warnings TEXT NOT NULL {_array("warnings")}"""


def _bit(column: str, null: bool = False) -> str:
    return f"{column} INTEGER{'' if null else ' NOT NULL'} CHECK ({column} IN (0, 1))"


_REPO = f"repo_id TEXT NOT NULL {_hex('repo_id', 16)}"
_MAP = f"map_version TEXT NOT NULL {_hex('map_version', 16)}"
_MAP_FK = "FOREIGN KEY (repo_id, map_version) REFERENCES component_maps"
_P = f"p REAL CHECK (p IN {_in(GRID)})"
_SHA = _hex("head_sha", 40, 64)
_TREE_HEX = f"TEXT {_hex('tree_hash', 40, 64)}"
# Design 03 4.1 (b): X2 truth is for the HEAD tree its scratch clone ran, so a run
# names that tree; only a run no Python process traced may lack it.
_TREE = f"{_TREE_HEX} CHECK (tree_hash IS NOT NULL OR outcome = 'not_python')"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS hook_events (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT,
  event       TEXT,
  tool_name   TEXT,
  tool_use_id TEXT,
  ts          TEXT NOT NULL,
  duration_ms REAL NOT NULL CHECK (duration_ms >= 0),
  outcome     TEXT NOT NULL CHECK (outcome IN ('ok', 'unobservable', 'error')),
  note        TEXT,
  prompt_id TEXT, turn_id TEXT, permission_mode TEXT, agent_id TEXT, cli_version TEXT,
  head_sha    TEXT {_SHA}
) STRICT;
-- Every snapshot event reads its session's rows: E04 measured 30 to 57 ms added per
-- event at 200k rows without this index.
CREATE INDEX IF NOT EXISTS hook_events_session ON hook_events (session_id);
CREATE TABLE IF NOT EXISTS baselines (
  session_id TEXT NOT NULL,
  file       TEXT NOT NULL,
  line_hash  TEXT NOT NULL,
  count      INTEGER NOT NULL CHECK (count > 0)
) STRICT;
CREATE INDEX IF NOT EXISTS baselines_session ON baselines (session_id);
-- Scratch, not an observation: a PreToolUse snapshot waiting for its PostToolUse. The
-- one table rows are deleted from.
CREATE TABLE IF NOT EXISTS pending (
  session_id  TEXT NOT NULL,
  tool_use_id TEXT NOT NULL,
  ts          TEXT NOT NULL,
  file        TEXT NOT NULL,
  line_hash   TEXT NOT NULL,
  count       INTEGER NOT NULL CHECK (count > 0)
) STRICT;
CREATE INDEX IF NOT EXISTS pending_key ON pending (session_id, tool_use_id);
CREATE TABLE IF NOT EXISTS agent_deltas (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL,
  tool_use_id TEXT NOT NULL,
  tool_name   TEXT NOT NULL,
  ts          TEXT NOT NULL,
  file        TEXT NOT NULL,
  line_hash   TEXT NOT NULL,
  delta       INTEGER NOT NULL CHECK (delta <> 0),
  prompt_id TEXT, turn_id TEXT, permission_mode TEXT, agent_id TEXT, cli_version TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS agent_deltas_session ON agent_deltas (session_id);
CREATE TABLE IF NOT EXISTS attributions (
  id              INTEGER PRIMARY KEY,
  session_id      TEXT NOT NULL,
  ts              TEXT NOT NULL,
  trigger         TEXT NOT NULL,
  file            TEXT NOT NULL,
  agent           INTEGER NOT NULL CHECK (agent >= 0),
  human           INTEGER CHECK (human IS NULL OR human >= 0),
  preexisting     INTEGER CHECK (preexisting IS NULL OR preexisting >= 0),
  ambiguous       INTEGER NOT NULL CHECK (ambiguous >= 0),
  reverted        INTEGER CHECK (reverted IS NULL OR reverted >= 0),
  claim_class     TEXT NOT NULL CHECK (claim_class IN {_in(CLAIM_CLASSES)}),
  coverage        TEXT NOT NULL CHECK (coverage IN {_in(COVERAGES)}),
  sources         TEXT NOT NULL CHECK (json_valid(sources) AND sources <> '[]'),
  reducer_version TEXT NOT NULL,
  assumptions     TEXT NOT NULL CHECK (json_valid(assumptions)),
  warnings        TEXT NOT NULL CHECK (json_valid(warnings)),
  -- Unknown stays unknown: the two counts a baseline separates are NULL together with
  -- the coverage that says why, never one without the other.
  CHECK ((coverage = 'no_baseline') = (human IS NULL AND preexisting IS NULL
                                        AND reverted IS NULL))
) STRICT;
CREATE INDEX IF NOT EXISTS attributions_session ON attributions (session_id);
-- Which repository a session worked in, by hashed toplevel. Written on every snapshot.
CREATE TABLE IF NOT EXISTS session_repos (
  session_id TEXT NOT NULL,
  repo_id    TEXT NOT NULL,
  ts         TEXT NOT NULL,
  PRIMARY KEY (session_id, repo_id)
) STRICT;
CREATE TABLE IF NOT EXISTS component_maps (
  {_REPO}, version TEXT NOT NULL {_hex("version", 16)},
  source TEXT NOT NULL CHECK (source IN ('mechanical', 'owner')),
  rules_json TEXT NOT NULL {_array("rules_json", non_empty=True)},
  warnings_json TEXT NOT NULL {_array("warnings_json")},
  created_ts TEXT NOT NULL, frozen_ts TEXT, superseded_ts TEXT,
  reason TEXT CHECK (reason IN {_in(MAP_REASONS)}),
  PRIMARY KEY (repo_id, version),
  CHECK (frozen_ts IS NOT NULL OR (superseded_ts IS NULL AND reason IS NULL))
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS component_maps_one_frozen ON component_maps (repo_id)
  WHERE frozen_ts IS NOT NULL AND superseded_ts IS NULL;
CREATE TABLE IF NOT EXISTS boundary_rules (
  {_REPO}, {_MAP}, src_component TEXT NOT NULL, dst_component TEXT NOT NULL,
  {_bit("allowed")}, ts TEXT NOT NULL,
  PRIMARY KEY (repo_id, map_version, src_component, dst_component),
  CHECK (src_component <> dst_component), {_MAP_FK}
) STRICT;
-- One row per (scan, file, destination); a scanned file with no edge is one row with a
-- NULL destination, so "parsed, no imports" and "not parsed" stay apart.
CREATE TABLE IF NOT EXISTS file_edges (
  id INTEGER PRIMARY KEY, session_id TEXT, ts TEXT NOT NULL, {_REPO}, {_MAP},
  src_file TEXT NOT NULL CHECK (src_file <> ''), src_component TEXT NOT NULL,
  dst_component TEXT, sites INTEGER CHECK (sites > 0), {_bit("module_level", True)},
  extractor_version TEXT NOT NULL, {_evidence("file_edges")},
  CHECK ((dst_component IS NULL) = (sites IS NULL)),
  CHECK ((sites IS NULL) = (module_level IS NULL)),
  CHECK (dst_component IS NOT src_component), {_MAP_FK}
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS file_edges_once
  ON file_edges (repo_id, map_version, ts, src_file, ifnull(dst_component, ''));
CREATE INDEX IF NOT EXISTS file_edges_repo ON file_edges (repo_id, ts);
CREATE TABLE IF NOT EXISTS scenario_runs (
  id INTEGER PRIMARY KEY, {_REPO}, {_MAP}, scenario_id TEXT NOT NULL,
  command_hash TEXT NOT NULL {_hex("command_hash", 16)}, ts TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'failed', 'timeout', 'not_python')),
  exit_code INTEGER, duration_ms REAL NOT NULL CHECK (duration_ms >= 0),
  components_json TEXT NOT NULL {_array("components_json")},
  call_edges_json TEXT NOT NULL {_array("call_edges_json")},
  extractor_version TEXT NOT NULL, tree_hash {_TREE}, {_evidence("scenario_runs")},
  CHECK (CASE outcome WHEN 'ok' THEN exit_code IS 0
    WHEN 'timeout' THEN exit_code IS NULL WHEN 'not_python' THEN exit_code IS NOT NULL
    ELSE exit_code IS NOT 0 END), {_MAP_FK}
) STRICT;
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, {_REPO}, {_MAP},
  ordinal INTEGER NOT NULL CHECK (ordinal > 0),
  ts TEXT NOT NULL, arm TEXT NOT NULL CHECK (arm IN ('treatment', 'control')),
  seed_hash TEXT NOT NULL {_hex("seed_hash", 64)},
  region_json TEXT NOT NULL {_array("region_json")},
  stratum TEXT NOT NULL CHECK (stratum IN ('region', 'uniform', 'empty')),
  status TEXT NOT NULL CHECK (status IN
    ('unbound', 'bound', 'closed', 'superseded', 'void')),
  bound_session_id TEXT, bound_prompt_id TEXT, bound_ts TEXT, closed_ts TEXT,
  void_reason TEXT CHECK (void_reason IN {_in(VOID_REASONS)}), void_ts TEXT,
  UNIQUE (repo_id, ordinal), {_MAP_FK},
  CHECK ((bound_session_id IS NULL) = (bound_ts IS NULL)),
  CHECK ((void_reason IS NULL) = (void_ts IS NULL)),
  CHECK (CASE status
    WHEN 'unbound' THEN bound_ts IS NULL AND closed_ts IS NULL AND void_ts IS NULL
    WHEN 'bound' THEN bound_ts IS NOT NULL AND closed_ts IS NULL AND void_ts IS NULL
    WHEN 'closed' THEN bound_ts IS NOT NULL AND closed_ts IS NOT NULL
      AND void_ts IS NULL
    WHEN 'superseded' THEN void_reason IS 'superseded' AND closed_ts IS NULL
    ELSE void_reason IS NOT NULL AND void_reason <> 'superseded' END)
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS tasks_one_open ON tasks (repo_id)
  WHERE status IN ('unbound', 'bound');
CREATE TABLE IF NOT EXISTS probe_items (
  item_id TEXT PRIMARY KEY {_hex("item_id", 16)}, {_REPO}, {_MAP},
  relation TEXT NOT NULL CHECK (relation IN ('D1', 'D2', 'reach')),
  src_component TEXT NOT NULL, dst_component TEXT NOT NULL, {_bit("truth")},
  truth_ts TEXT NOT NULL, extractor_version TEXT NOT NULL,
  epoch INTEGER NOT NULL CHECK (epoch >= 0), sites INTEGER CHECK (sites > 0),
  {_bit("module_level", True)}, {_evidence("probe_items")},
  UNIQUE (repo_id, map_version, relation, src_component, dst_component, epoch),
  CHECK (src_component <> dst_component),
  CHECK ((sites IS NULL) = (module_level IS NULL)),
  CHECK (sites IS NULL OR (truth = 1 AND relation = 'D1')), {_MAP_FK}
) STRICT;
-- An item is consumed by its first exposure: UNIQUE (item_id) is the consumption.
CREATE TABLE IF NOT EXISTS exposures (
  id INTEGER PRIMARY KEY, item_id TEXT NOT NULL UNIQUE REFERENCES probe_items,
  task_id TEXT NOT NULL REFERENCES tasks, ts TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('bound', 'related', 'drawn_not_shown'))
) STRICT;
-- p NULL is a component or item shown and left unanswered, never a default.
CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks,
  kind TEXT NOT NULL CHECK (kind IN ('forecast', 'prequestion')),
  component TEXT, item_id TEXT REFERENCES probe_items, {_P}, ts TEXT NOT NULL,
  {_evidence("forecasts")},
  CHECK ((kind = 'forecast') = (component IS NOT NULL)),
  CHECK ((kind = 'forecast') = (item_id IS NULL))
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS forecasts_once
  ON forecasts (task_id, kind, ifnull(component, item_id));
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, ts TEXT NOT NULL,
  component TEXT NOT NULL, {_bit("o_first")}, {_bit("o_final", True)},
  edges_added INTEGER CHECK (edges_added >= 0),
  edges_removed INTEGER CHECK (edges_removed >= 0), {_evidence("outcomes")},
  UNIQUE (task_id, component),
  CHECK ((edges_added IS NULL) = (edges_removed IS NULL))
) STRICT;
-- agent_attached is a constant: a probe answered with an agent attached is refused
-- (a flags row), so the database refuses a response that says otherwise.
CREATE TABLE IF NOT EXISTS probe_responses (
  id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES probe_items,
  task_id TEXT REFERENCES tasks, ts TEXT NOT NULL, {_P}, {_bit("skipped")},
  seconds REAL NOT NULL CHECK (seconds >= 0), lag_days REAL CHECK (lag_days >= 0),
  {_bit("deadline_met")}, {_bit("truth_at_probe")}, {_bit("truth_changed")},
  agent_attached INTEGER NOT NULL CHECK (agent_attached = 0),
  head_sha TEXT {_SHA}, tree_hash TEXT {_hex("tree_hash", 40, 64)},
  {_bit("dirty", True)}, extractor_version TEXT NOT NULL,
  {_evidence("probe_responses")},
  CHECK ((skipped = 1) = (p IS NULL)),
  CHECK ((task_id IS NULL) = (lag_days IS NULL))
) STRICT;
CREATE TABLE IF NOT EXISTS drift_responses (
  id INTEGER PRIMARY KEY, {_REPO}, {_MAP},
  week TEXT NOT NULL CHECK (week GLOB '[0-9][0-9][0-9][0-9]-W[0-5][0-9]'),
  component TEXT NOT NULL, {_bit("said_changed", True)}, {_bit("did_change", True)},
  ts TEXT NOT NULL, {_evidence("drift_responses")},
  UNIQUE (repo_id, map_version, week, component), {_MAP_FK}
) STRICT;
CREATE TABLE IF NOT EXISTS divergences (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT REFERENCES tasks,
  ts TEXT NOT NULL, {_REPO}, {_MAP}, src_component TEXT NOT NULL,
  dst_component TEXT NOT NULL, {_bit("rule_allowed")}, {_bit("noticed", True)},
  {_evidence("divergences")}, CHECK (src_component <> dst_component), {_MAP_FK}
) STRICT;
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, tool_use_id TEXT NOT NULL,
  tool_name TEXT NOT NULL CHECK (tool_name IN ('AskUserQuestion', 'ExitPlanMode')),
  prompt_id TEXT, ts TEXT NOT NULL, option_count INTEGER CHECK (option_count > 0),
  chosen_index INTEGER, options_hash TEXT {_hex("options_hash", 64)},
  {_evidence("decisions")}, UNIQUE (session_id, tool_use_id),
  CHECK ((option_count IS NULL) = (options_hash IS NULL)),
  CHECK (chosen_index IS NULL
    OR (option_count IS NOT NULL AND chosen_index >= 0 AND chosen_index < option_count))
) STRICT;
CREATE TABLE IF NOT EXISTS decision_responses (
  id INTEGER PRIMARY KEY, decision_id INTEGER NOT NULL REFERENCES decisions,
  ts TEXT NOT NULL, recalled_index INTEGER CHECK (recalled_index >= 0),
  {_bit("skipped")}, lag_days REAL NOT NULL CHECK (lag_days >= 0),
  {_evidence("decision_responses")},
  CHECK ((skipped = 1) = (recalled_index IS NULL))
) STRICT;
CREATE TABLE IF NOT EXISTS knockouts (
  id INTEGER PRIMARY KEY, {_REPO}, {_MAP}, ts TEXT NOT NULL,
  component_knocked TEXT NOT NULL, scenario_id TEXT,
  components_named_json TEXT NOT NULL {_array("components_named_json")},
  {_bit("hit")}, {_evidence("knockouts")}, {_MAP_FK}
) STRICT;
CREATE TABLE IF NOT EXISTS pathfinder_runs (
  id INTEGER PRIMARY KEY, {_REPO}, {_MAP}, ts TEXT NOT NULL,
  n_components INTEGER NOT NULL CHECK (n_components > 1),
  pfsim REAL CHECK (pfsim BETWEEN 0 AND 1), pfsim_minus_chance REAL,
  {_evidence("pathfinder_runs")}, {_MAP_FK}
) STRICT;
CREATE TABLE IF NOT EXISTS flags (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, repo_id TEXT {_hex("repo_id", 16)},
  session_id TEXT, task_id TEXT REFERENCES tasks,
  code TEXT NOT NULL CHECK (code IN {_in(FLAG_CODES)}),
  detail_json TEXT NOT NULL
    CHECK (CASE WHEN json_valid(detail_json) THEN json_type(detail_json) = 'object'
      ELSE 0 END),
  CHECK (coalesce(repo_id, session_id, task_id) IS NOT NULL)
) STRICT;
"""


def _refuse(table: str, op: str, when: str = "") -> str:
    name = f"{table}_{op.split()[0].lower()}_{'once' if when else 'never'}"
    return (
        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {op} ON {table}"
        f"{' WHEN ' + when if when else ''}"
        f" BEGIN SELECT RAISE(ABORT, '{table}: {op.lower()} refused'); END;\n"
    )


def _once(*columns: str) -> str:
    return " OR ".join(
        f"(OLD.{c} IS NOT NULL AND NEW.{c} IS NOT OLD.{c})" for c in columns
    )


_APPEND_ONLY = ("boundary_rules", "exposures", "flags", *CLAIMS)
_FIXED_TASK = "task_id, repo_id, map_version, ordinal, ts, arm, seed_hash, region_json"
_TASK_MOVE = (
    "OLD.status IN ('superseded', 'void') OR (OLD.status = 'closed'"
    " AND NEW.status <> 'void') OR (OLD.status = 'bound' AND NEW.status = 'unbound')"
)
SCHEMA += "".join(_refuse(t, "UPDATE") for t in _APPEND_ONLY)
SCHEMA += "".join(
    _refuse(t, "DELETE") for t in (*_APPEND_ONLY, "tasks", "component_maps")
)
SCHEMA += _refuse("tasks", f"UPDATE OF {_FIXED_TASK}, stratum")
SCHEMA += _refuse(
    "tasks",
    "UPDATE",
    f"{_TASK_MOVE} OR "
    + _once("bound_session_id", "bound_prompt_id", "bound_ts", "closed_ts", "void_ts"),
)
SCHEMA += _refuse(
    "component_maps", "UPDATE OF repo_id, version, source, rules_json, warnings_json"
)
SCHEMA += _refuse("component_maps", "UPDATE", _once("frozen_ts", "superseded_ts"))
# Stored as PRAGMA user_version: an open skips the DDL while the schema is unchanged.
SCHEMA_VERSION = int(hashlib.sha256(SCHEMA.encode()).hexdigest()[:7], 16)


def migrate(db: sqlite3.Connection) -> None:
    """Add the pointer and tree_hash columns to a ledger created before they existed
    and drop the rejected drill's tables (repo_config holds an owner-typed test
    command, which invariant 5 forbids the ledger to keep). Runs inside open()'s BEGIN
    IMMEDIATE, so two hooks cannot both add a column."""
    # secure_delete zeroes the freed pages: a DROP alone leaves the text in the file.
    db.execute("PRAGMA secure_delete = ON")
    for legacy in ("results", "items", "repo_config"):  # results references items
        db.execute(f"DROP TABLE IF EXISTS {legacy}")
    pointers = dict.fromkeys(POINTERS, "TEXT")
    # A run stored before the column existed has no tree hash, so the not_python rule
    # can be added only to a table that holds no run.
    stored = db.execute("SELECT 1 FROM scenario_runs LIMIT 1").fetchone()
    tree = _TREE_HEX if stored else _TREE
    added = {
        "hook_events": {**pointers, "head_sha": f"TEXT {_SHA}"},
        "agent_deltas": pointers,
        "scenario_runs": {"tree_hash": tree},
    }
    for table, columns in added.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns.items():
            if column not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


@dataclass(frozen=True)
class Evidence:
    """The Evidence fields a derived row carries beside its claim class, which each
    table fixes. No field has a default: an empty source list is the database's to
    refuse, never this class's to fill."""

    coverage: str
    sources: Sequence[str]
    reducer_version: str
    assumptions: Sequence[str]
    warnings: Sequence[str]

    def __post_init__(self) -> None:
        # A bare str is a Sequence[str]: "ab" would store as ["a", "b"].
        if any(isinstance(v, str) for v in self._lists()):
            raise TypeError("sources, assumptions and warnings are lists, not str")

    def _lists(self) -> tuple[Sequence[str], ...]:
        return (self.sources, self.assumptions, self.warnings)

    def columns(self) -> tuple[str, ...]:
        s, a, w = (json.dumps(list(v)) for v in self._lists())
        return (self.coverage, s, self.reducer_version, a, w)


def item_id(
    repo_id: str, map_version: str, relation: str, src: str, dst: str, epoch: int
) -> str:
    key = json.dumps([repo_id, map_version, relation, src, dst, epoch])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Attribution:
    session_id: str
    ts: str
    trigger: str
    file: str
    agent: int
    human: int | None
    preexisting: int | None
    ambiguous: int
    reverted: int | None
    coverage: str
    sources: list[str]
    assumptions: list[str]
    warnings: list[str] = field(default_factory=list)
    claim_class: str = "derived"


# The columns a writer takes as keywords, one TypedDict per table: Store's writers are
# `**row: Unpack[...]`, so a signature is the row it writes and a column left out is
# NULL in the database, never a default.
class Pointers(TypedDict, total=False):
    prompt_id: str | None
    turn_id: str | None
    permission_mode: str | None
    agent_id: str | None
    cli_version: str | None


class HookEvent(Pointers):
    session_id: str | None
    event: str | None
    tool_name: str | None
    tool_use_id: str | None
    ts: str
    duration_ms: float
    outcome: str
    note: str | None
    head_sha: NotRequired[str | None]


class Decision(TypedDict):
    session_id: str
    tool_use_id: str
    tool_name: str
    prompt_id: str | None
    ts: str
    option_count: int | None
    chosen_index: int | None
    options_hash: str | None


class MapRow(TypedDict):
    repo_id: str
    map_version: str
    ts: str


class Scan(MapRow):
    session_id: str | None
    extractor_version: str


class ScenarioRun(MapRow):
    scenario_id: str
    command_hash: str
    outcome: str
    exit_code: int | None
    duration_ms: float
    extractor_version: str
    tree_hash: str | None


class Task(MapRow):
    task_id: str
    ordinal: int
    arm: str
    seed_hash: str
    stratum: str


class ItemBatch(TypedDict):
    repo_id: str
    map_version: str
    truth_ts: str
    extractor_version: str


class ProbeResponse(TypedDict):
    item_id: str
    task_id: str | None
    ts: str
    p: float | None
    seconds: float
    lag_days: float | None
    deadline_met: bool
    truth_at_probe: bool
    truth_changed: bool
    head_sha: str | None
    tree_hash: str | None
    dirty: bool | None
    extractor_version: str


class DriftResponse(MapRow):
    week: str
    component: str
    said_changed: bool | None
    did_change: bool | None


class Divergence(MapRow):
    session_id: str
    task_id: str | None
    src_component: str
    dst_component: str
    rule_allowed: bool
    noticed: bool | None


class DecisionResponse(TypedDict):
    decision_id: int
    ts: str
    recalled_index: int | None
    lag_days: float


class Knockout(MapRow):
    component_knocked: str
    scenario_id: str | None
    hit: bool


class PathfinderRun(MapRow):
    n_components: int
    pfsim: float | None
    pfsim_minus_chance: float | None


class Flag(TypedDict, total=False):
    repo_id: str | None
    session_id: str | None
    task_id: str | None
