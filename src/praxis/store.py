"""The ledger: one SQLite file under $PRAXIS_HOME, and the only module that INSERTs.

AGENTS.md invariant 2: every derived number is an Evidence written through here, so the
CHECK constraints (schema.py) and the one constructor per table are the only way in.
The derived-writes-only-in-store pre-commit hook refuses an INSERT into these tables
anywhere else. A writer's keywords are its columns (the TypedDicts in schema.py) and
become the row, so a signature is the schema it writes; nothing here stores a line of
text.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Unpack

from praxis.schema import CLAIMS, EV_COLS, SCHEMA_VERSION, migrate
from praxis.schema import SCHEMA as SCHEMA
from praxis.schema import Attribution as Attribution
from praxis.schema import Evidence as Evidence
from praxis.schema import item_id as item_id

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from praxis.schema import (
        Decision,
        DecisionResponse,
        Divergence,
        DriftResponse,
        Flag,
        HookEvent,
        ItemBatch,
        Knockout,
        PathfinderRun,
        Pointers,
        ProbeResponse,
        Scan,
        ScenarioRun,
        Task,
    )

REDUCER_VERSION = "attribution-v1"

Files = dict[str, Counter[str]]
Values = dict[str, object]
Rows = list[sqlite3.Row]


def home() -> Path:
    return Path(os.environ.get("PRAXIS_HOME") or Path.home() / ".praxis")


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: sqlite3.Connection | None = None

    def open(self) -> Store:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        # Hooks are separate processes and subagents run tools concurrently, so two
        # hooks can write at once. WAL plus a busy timeout is what lets both finish.
        db = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("PRAGMA foreign_keys=ON")
        self._db = db
        (version,) = db.execute("PRAGMA user_version").fetchone()
        if version != SCHEMA_VERSION:
            # Every statement is IF NOT EXISTS, so two first opens both succeed;
            # executescript commits any open transaction, so it runs before one opens.
            db.executescript(SCHEMA)
            with self._tx():
                migrate(db)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return self

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("store is not open")
        return self._db

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        # isolation_level=None is autocommit, where `with connection:` opens nothing:
        # a multi-row write is atomic only inside an explicit BEGIN. Nested use joins
        # the outer transaction.
        db = self.db
        if db.in_transaction:
            yield db
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def _insert(
        self, table: str, ev: Evidence | None, rows: Sequence[Values]
    ) -> list[int]:
        if not rows:
            return []
        names = list(rows[0])
        extra = () if ev is None else (CLAIMS[table], *ev.columns())
        columns = names + ([] if ev is None else ["claim_class", *EV_COLS])
        # The one string-built statement: the table is a literal at every call site
        # and the columns are the calling writer's own parameter names.
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)})"  # noqa: S608
            f" VALUES ({', '.join('?' * len(columns))})"
        )
        ids: list[int] = []
        with self._tx() as db:
            for r in rows:
                rowid = db.execute(sql, [*(r[c] for c in names), *extra]).lastrowid
                if rowid is None:
                    raise RuntimeError(f"{table}: no row id after INSERT")
                ids.append(rowid)
        return ids

    def _all(self, sql: str, *params: object) -> Rows:
        return list(self.db.execute(sql, params))

    def _one(self, sql: str, *params: object) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.db.execute(sql, params).fetchone()
        return row

    # -- the recorder ---------------------------------------------------------------

    def hook_event(self, **row: Unpack[HookEvent]) -> None:
        self._insert("hook_events", None, [dict(row)])

    def has_baseline(self, session_id: str) -> bool:
        sql = "SELECT 1 FROM baselines WHERE session_id = ? LIMIT 1"
        return self._one(sql, session_id) is not None

    def baseline(self, session_id: str, files: Files) -> None:
        rows = [
            (session_id, f, h, n) for f, c in files.items() for h, n in c.items() if n
        ]
        with self._tx() as db:
            # An empty tree still needs a baseline row to say "observed, nothing dirty":
            # absence of rows must not read as absence of a baseline.
            db.executemany(
                "INSERT INTO baselines (session_id, file, line_hash, count)"
                " VALUES (?, ?, ?, ?)",
                [(session_id, "", "", 1), *rows],
            )

    def pending_put(
        self, session_id: str, tool_use_id: str, ts: str, files: Files
    ) -> None:
        rows = [
            (session_id, tool_use_id, ts, f, h, n)
            for f, c in files.items()
            for h, n in c.items()
            if n
        ]
        with self._tx() as db:
            db.executemany(
                "INSERT INTO pending (session_id, tool_use_id, ts, file, line_hash,"
                " count) VALUES (?, ?, ?, ?, ?, ?)",
                [(session_id, tool_use_id, ts, "", "", 1), *rows],
            )

    def pending_take(self, session_id: str, tool_use_id: str) -> Files | None:
        """The pre-snapshot for one tool call, consumed. None without a PreToolUse."""
        key = (session_id, tool_use_id)
        with self._tx() as db:
            rows = db.execute(
                "SELECT file, line_hash, count FROM pending"
                " WHERE session_id = ? AND tool_use_id = ?",
                key,
            ).fetchall()
            db.execute(
                "DELETE FROM pending WHERE session_id = ? AND tool_use_id = ?", key
            )
        return _files(rows) if rows else None

    def agent_delta(
        self,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        ts: str,
        changes: list[tuple[str, str, int]],
        **pointers: Unpack[Pointers],
    ) -> None:
        call = {"session_id": session_id, "tool_use_id": tool_use_id,
                "tool_name": tool_name, "ts": ts, **pointers}  # fmt: skip
        rows = [{**call, "file": f, "line_hash": h, "delta": d} for f, h, d in changes]
        self._insert("agent_deltas", None, rows)

    def attributions(self, rows: list[Attribution]) -> None:
        with self._tx() as db:
            db.executemany(
                "INSERT INTO attributions (session_id, ts, trigger, file, agent, human,"
                " preexisting, ambiguous, reverted, claim_class, coverage, sources,"
                " reducer_version, assumptions, warnings)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.session_id, r.ts, r.trigger, r.file, r.agent, r.human,
                        r.preexisting, r.ambiguous, r.reverted, r.claim_class,
                        r.coverage, json.dumps(r.sources), REDUCER_VERSION,
                        json.dumps(r.assumptions), json.dumps(r.warnings),
                    )
                    for r in rows
                ],
            )  # fmt: skip

    def touch_session_repo(self, session_id: str, repo_id: str, ts: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO session_repos (session_id, repo_id, ts)"
            " VALUES (?, ?, ?)",
            (session_id, repo_id, ts),
        )

    def decision(self, *, ev: Evidence, **row: Unpack[Decision]) -> int:
        return self._insert("decisions", ev, [dict(row)])[0]

    def survival(self, session_ids: Sequence[str], probe: Counter[str]) -> Counter[str]:
        """How many of each probed line hash are the agent's in these sessions: deltas
        summed per (file, hash) across the sessions and clipped at zero, as
        agent_multiset does, then summed over files and intersected with ``probe`` as
        multisets (min of counts). Two identical lines are two lines."""
        if len(set(session_ids)) != len(session_ids):
            raise ValueError("a session id is listed twice")
        agent: Counter[str] = Counter()
        for h, total in self.db.execute(
            "SELECT line_hash, SUM(delta) FROM agent_deltas WHERE session_id IN"
            " (SELECT value FROM json_each(?)) GROUP BY file, line_hash",
            (json.dumps(list(session_ids)),),
        ):
            if total > 0:
                agent[h] += total
        return agent & probe

    # -- component maps, rules and edges --------------------------------------------

    def component_map_put(self, cmap_json: str, ts: str) -> str:
        """Store a map (components.to_json) and return its version. The same rules
        twice are one map; the same rules under another source are refused."""
        # Imported here: every hook opens a Store and must not pay for this module.
        from praxis.components import from_json

        cmap = from_json(cmap_json)
        rules = json.dumps([[r.id, *r.globs] for r in cmap.rules])
        self.db.execute(
            "INSERT INTO component_maps (repo_id, version, source, rules_json,"
            " warnings_json, created_ts) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (repo_id, version) DO NOTHING",
            (cmap.repo_id, cmap.version, cmap.source, rules,
             json.dumps(list(cmap.warnings)), ts),
        )  # fmt: skip
        stored = self._one(
            "SELECT source FROM component_maps WHERE repo_id = ? AND version = ?",
            cmap.repo_id,
            cmap.version,
        )
        if stored is None or stored[0] != cmap.source:
            raise ValueError("these rules are stored under another source")
        return cmap.version

    def component_map_freeze(
        self, repo_id: str, version: str, ts: str, *, reason: str | None = None
    ) -> None:
        """Freeze a stored map. Freezing another version supersedes the frozen one and
        needs a reason code; refreezing the frozen version changes nothing."""
        set_ = "UPDATE component_maps SET {} WHERE repo_id = ? AND version = ?"
        with self._tx() as db:
            now = self._frozen(repo_id)
            if now is not None and now["version"] == version:
                return
            if now is not None:
                if reason is None:
                    raise ValueError("a map change needs a reason code")
                db.execute(
                    set_.format("superseded_ts = ?"), (ts, repo_id, now["version"])
                )
            done = db.execute(
                set_.format("frozen_ts = ?, reason = ?"), (ts, reason, repo_id, version)
            )
            if done.rowcount != 1:
                raise KeyError(f"no stored map {version}")

    def _frozen(self, repo_id: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM component_maps WHERE repo_id = ?"
            " AND frozen_ts IS NOT NULL AND superseded_ts IS NULL",
            repo_id,
        )

    def component_map_frozen(self, repo_id: str) -> str | None:
        """The frozen map as components.to_json wrote it, or None."""
        row = self._frozen(repo_id)
        if row is None:
            return None
        doc = {
            "repo_id": row["repo_id"],
            "source": row["source"],
            "rules": json.loads(row["rules_json"]),
            "version": row["version"],
            "warnings": json.loads(row["warnings_json"]),
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    def boundary_rule_put(
        self, repo_id: str, map_version: str, src: str, dst: str, allowed: bool, ts: str
    ) -> None:
        row: Values = {"repo_id": repo_id, "map_version": map_version,
                       "src_component": src, "dst_component": dst,
                       "allowed": allowed, "ts": ts}  # fmt: skip
        self._insert("boundary_rules", None, [row])

    def boundary_rules(self, repo_id: str, map_version: str) -> Rows:
        return self._all(
            "SELECT * FROM boundary_rules WHERE repo_id = ? AND map_version = ?"
            " ORDER BY src_component, dst_component",
            repo_id,
            map_version,
        )

    def file_edges(
        self,
        *,
        scanned: Mapping[str, str],
        edges: Sequence[tuple[str, str, int, bool]],
        ev: Evidence,
        **scan: Unpack[Scan],
    ) -> None:
        """X1 at file grain for one scan. ``scanned`` maps every parsed file to its
        component; ``edges`` are (src_file, dst_component, sites, module_level), one
        per pair. A scanned file with no edge gets a row with a NULL destination."""
        seen = Counter((s, d) for s, d, _, _ in edges)
        if any(n > 1 for n in seen.values()):
            raise ValueError("an edge is listed twice; sum its sites first")
        bare = sorted(set(scanned) - {s for s, _ in seen})
        found = [*edges, *((f, None, None, None) for f in bare)]
        rows: list[Values] = [
            {**scan, "src_file": s, "src_component": scanned[s], "dst_component": d,
             "sites": n, "module_level": m}
            for s, d, n, m in found
        ]  # fmt: skip
        self._insert("file_edges", ev, rows)

    def edge_history(self, repo_id: str) -> Rows:
        sql = "SELECT * FROM file_edges WHERE repo_id = ? ORDER BY ts, id"
        return self._all(sql, repo_id)

    def scenario_run(
        self,
        *,
        components: Sequence[str],
        call_edges: Mapping[tuple[str, str], int],
        ev: Evidence,
        **run: Unpack[ScenarioRun],
    ) -> None:
        if len(set(components)) != len(components):
            raise ValueError("a component is listed twice")
        edges = sorted([s, d, n] for (s, d), n in call_edges.items())
        row: Values = {**run, "components_json": json.dumps(sorted(components)),
                       "call_edges_json": json.dumps(edges)}  # fmt: skip
        self._insert("scenario_runs", ev, [row])

    # -- tasks ----------------------------------------------------------------------

    def task_register(
        self,
        *,
        region: Sequence[str],
        bound: Sequence[str],
        related: Sequence[str],
        drawn: Sequence[str],
        **task: Unpack[Task],
    ) -> str | None:
        """One task and its exposures, atomically, against the frozen map only.
        Supersedes the repository's open task in the same transaction and returns its
        id, or None. An item listed twice is refused: exposure consumes it."""
        repo_id, ts = task["repo_id"], task["ts"]
        row: Values = {**task, "region_json": json.dumps(list(region)),
                       "status": "unbound"}  # fmt: skip
        roles = {"bound": bound, "related": related, "drawn_not_shown": drawn}
        exposed: list[Values] = [
            {"item_id": i, "task_id": task["task_id"], "ts": ts, "role": role}
            for role, ids in roles.items()
            for i in ids
        ]
        with self._tx():
            frozen = self._frozen(repo_id)
            if frozen is None or frozen["version"] != task["map_version"]:
                raise ValueError("a task needs the repository's frozen map")
            old = self.task_open(repo_id)
            if old is not None:
                self.task_void(old["task_id"], "superseded", ts)
            self._insert("tasks", None, [row])
            self._insert("exposures", None, exposed)
        return None if old is None else str(old["task_id"])

    def task_bind(
        self, task_id: str, session_id: str, prompt_id: str | None, ts: str
    ) -> None:
        self._move(
            "UPDATE tasks SET status = 'bound', bound_session_id = ?,"
            " bound_prompt_id = ?, bound_ts = ? WHERE task_id = ?",
            (session_id, prompt_id, ts, task_id),
        )

    def task_close(self, task_id: str, ts: str) -> None:
        self._move(
            "UPDATE tasks SET status = 'closed', closed_ts = ? WHERE task_id = ?",
            (ts, task_id),
        )

    def task_void(self, task_id: str, reason: str, ts: str) -> None:
        status = "superseded" if reason == "superseded" else "void"
        self._move(
            "UPDATE tasks SET status = ?, void_reason = ?, void_ts = ?"
            " WHERE task_id = ?",
            (status, reason, ts, task_id),
        )

    def _move(self, sql: str, values: tuple[object, ...]) -> None:
        # The triggers and the status CHECK refuse a second set or a wrong transition.
        if self.db.execute(sql, values).rowcount != 1:
            task_id = values[-1]
            raise KeyError(f"no task {task_id}")

    def task_open(self, repo_id: str) -> sqlite3.Row | None:
        """The unbound or bound task; a partial unique index keeps it to one."""
        return self._one(
            "SELECT * FROM tasks WHERE repo_id = ? AND status IN ('unbound', 'bound')",
            repo_id,
        )

    def task_last_closed(self, repo_id: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM tasks WHERE repo_id = ? AND status = 'closed'"
            " ORDER BY closed_ts DESC, ordinal DESC LIMIT 1",
            repo_id,
        )

    def task_rows(self, repo_id: str) -> Rows:
        sql = "SELECT * FROM tasks WHERE repo_id = ? ORDER BY ordinal"
        return self._all(sql, repo_id)

    def task_count(self, repo_id: str) -> int:
        row = self._one("SELECT COUNT(*) FROM tasks WHERE repo_id = ?", repo_id)
        return 0 if row is None else int(row[0])

    def exposures_of(self, task_id: str) -> Rows:
        return self._all(
            "SELECT e.item_id, e.role, e.ts, p.relation, p.src_component,"
            " p.dst_component, p.truth, p.epoch FROM exposures e"
            " JOIN probe_items p USING (item_id) WHERE e.task_id = ? ORDER BY e.id",
            task_id,
        )

    def forecast(
        self,
        task_id: str,
        rows: Iterable[tuple[str | None, float | None, str, str | None]],
        ts: str,
        *,
        ev: Evidence,
    ) -> None:
        """(component, p, kind, item_id) per row: a 'forecast' names a component, a
        'prequestion' an item. p None is shown and unanswered."""
        self._insert(
            "forecasts",
            ev,
            [
                {"task_id": task_id, "kind": kind, "component": component,
                 "item_id": item, "p": p, "ts": ts}
                for component, p, kind, item in rows
            ],
        )  # fmt: skip

    def forecasts_of(self, task_id: str) -> Rows:
        sql = "SELECT * FROM forecasts WHERE task_id = ? ORDER BY id"
        return self._all(sql, task_id)

    def outcome(
        self,
        task_id: str,
        rows: Iterable[tuple[str, bool, bool | None, int | None, int | None]],
        ts: str,
        *,
        ev: Evidence,
    ) -> None:
        """(component, o_first, o_final, edges_added, edges_removed) per component."""
        self._insert(
            "outcomes",
            ev,
            [
                {"task_id": task_id, "ts": ts, "component": c, "o_first": first,
                 "o_final": final, "edges_added": added, "edges_removed": removed}
                for c, first, final, added, removed in rows
            ],
        )  # fmt: skip

    def outcomes_of(self, task_id: str) -> Rows:
        sql = "SELECT * FROM outcomes WHERE task_id = ? ORDER BY component"
        return self._all(sql, task_id)

    # -- probes ---------------------------------------------------------------------

    def probe_items_put(
        self,
        *,
        rows: Iterable[tuple[str, str, str, bool, int, int | None, bool | None]],
        ev: Evidence,
        **batch: Unpack[ItemBatch],
    ) -> list[str]:
        """(relation, src, dst, truth, epoch, sites, module_level) per item; returns
        the item ids, derived from the key, so putting an item twice is refused."""
        key = (batch["repo_id"], batch["map_version"])
        items: list[Values] = [
            {**batch, "item_id": item_id(*key, rel, src, dst, epoch),
             "relation": rel, "src_component": src, "dst_component": dst,
             "truth": truth, "epoch": epoch, "sites": sites,
             "module_level": module_level}
            for rel, src, dst, truth, epoch, sites, module_level in rows
        ]  # fmt: skip
        self._insert("probe_items", ev, items)
        return [str(i["item_id"]) for i in items]

    def probe_items_unexposed(self, repo_id: str, map_version: str) -> Rows:
        """Items of the current epoch for their pair that no task has exposed."""
        return self._all(
            "SELECT p.* FROM probe_items p WHERE p.repo_id = ? AND p.map_version = ?"
            " AND NOT EXISTS (SELECT 1 FROM exposures e WHERE e.item_id = p.item_id)"
            " AND p.epoch = (SELECT MAX(q.epoch) FROM probe_items q"
            "  WHERE q.repo_id = p.repo_id AND q.map_version = p.map_version"
            "  AND q.relation = p.relation AND q.src_component = p.src_component"
            "  AND q.dst_component = p.dst_component) ORDER BY p.item_id",
            repo_id,
            map_version,
        )

    def probe_item(self, item_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM probe_items WHERE item_id = ?", item_id)

    def probe_response(self, *, ev: Evidence, **row: Unpack[ProbeResponse]) -> int:
        """p None is a skip. Only called after the attached-agent check found none."""
        values: Values = {**row, "skipped": row["p"] is None, "agent_attached": 0}
        return self._insert("probe_responses", ev, [values])[0]

    def drift_response(self, *, ev: Evidence, **row: Unpack[DriftResponse]) -> None:
        self._insert("drift_responses", ev, [dict(row)])

    def divergence(self, *, ev: Evidence, **row: Unpack[Divergence]) -> None:
        self._insert("divergences", ev, [dict(row)])

    def decisions_unprobed(self, repo_id: str) -> Rows:
        """Decisions with an option list, in sessions on this repository, that have no
        response yet. A session recorded on two repositories lists under both."""
        return self._all(
            "SELECT d.* FROM decisions d JOIN session_repos s USING (session_id)"
            " WHERE s.repo_id = ? AND d.option_count IS NOT NULL AND NOT EXISTS"
            " (SELECT 1 FROM decision_responses r WHERE r.decision_id = d.id)"
            " ORDER BY d.ts, d.id",
            repo_id,
        )

    def decision_response(
        self, *, ev: Evidence, **row: Unpack[DecisionResponse]
    ) -> None:
        """recalled_index None is a skip. An index outside the option list is refused
        here: the one check the schema cannot make across tables."""
        index = row["recalled_index"]
        found = self._one(
            "SELECT option_count FROM decisions WHERE id = ?", row["decision_id"]
        )
        count = None if found is None else found[0]
        if index is not None and not (count is not None and 0 <= index < count):
            raise ValueError("recalled index outside the decision's options")
        self._insert("decision_responses", ev, [{**row, "skipped": index is None}])

    def knockout(
        self,
        *,
        components_named: Sequence[str],
        ev: Evidence,
        **row: Unpack[Knockout],
    ) -> None:
        named = json.dumps(list(components_named))
        self._insert("knockouts", ev, [{**row, "components_named_json": named}])

    def pathfinder_run(self, *, ev: Evidence, **row: Unpack[PathfinderRun]) -> None:
        self._insert("pathfinder_runs", ev, [dict(row)])

    def flag(
        self, *, ts: str, code: str, detail: Mapping[str, object], **on: Unpack[Flag]
    ) -> None:
        row: Values = {**on, "ts": ts, "code": code,
                       "detail_json": json.dumps(dict(detail))}  # fmt: skip
        self._insert("flags", None, [row])

    # -- reads ----------------------------------------------------------------------

    def baseline_of(self, session_id: str) -> Files | None:
        rows = self._all(
            "SELECT file, line_hash, count FROM baselines WHERE session_id = ?",
            session_id,
        )
        return _files(rows) if rows else None

    def agent_multiset(self, session_id: str) -> tuple[Files, dict[str, list[str]]]:
        """(per-file agent line multiset, per-file tool_use_ids that contributed).

        Deltas sum per (file, hash); a sum below zero means the agent removed lines it
        never added relative to a snapshot, which cannot happen with paired pre/post
        snapshots and is clipped to zero rather than carried as a negative count.
        """
        files: Files = {}
        sources: dict[str, set[str]] = {}
        for file, h, total in self.db.execute(
            "SELECT file, line_hash, SUM(delta) FROM agent_deltas WHERE session_id = ?"
            " GROUP BY file, line_hash",
            (session_id,),
        ):
            if total > 0:
                files.setdefault(file, Counter())[h] = total
        for file, tool_use_id in self.db.execute(
            "SELECT DISTINCT file, tool_use_id FROM agent_deltas WHERE session_id = ?",
            (session_id,),
        ):
            sources.setdefault(file, set()).add(tool_use_id)
        return files, {f: sorted(ids) for f, ids in sources.items()}

    def attribution_rows(self, session_id: str | None = None) -> Rows:
        if session_id is None:
            return self._all("SELECT * FROM attributions ORDER BY id")
        sql = "SELECT * FROM attributions WHERE session_id = ? ORDER BY id"
        return self._all(sql, session_id)

    def hook_event_rows(self, session_id: str | None = None) -> Rows:
        if session_id is None:
            return self._all("SELECT * FROM hook_events ORDER BY id")
        sql = "SELECT * FROM hook_events WHERE session_id = ? ORDER BY id"
        return self._all(sql, session_id)

    def sessions(self) -> list[str]:
        return [
            r[0]
            for r in self._all(
                "SELECT session_id FROM hook_events WHERE session_id IS NOT NULL"
                " GROUP BY session_id ORDER BY MIN(ts)"
            )
        ]


def _files(rows: Iterable[Sequence[Any]]) -> Files:
    files: Files = {}
    for file, h, n in rows:
        if file == "" and h == "":
            # The marker row that tells an empty snapshot from no snapshot at all.
            continue
        files.setdefault(file, Counter())[h] += n
    return files
