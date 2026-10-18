"""The ledger: one SQLite file under $PRAXIS_HOME, and the only module that INSERTs.

AGENTS.md invariant 2: every derived number is an Evidence written through here, so the
CHECK constraints and the one constructor are the only way in. The
derived-writes-only-in-store pre-commit hook refuses an INSERT into these tables
anywhere else.

Nothing in this file stores a line of text from a repository, a prompt or a tool. Hashes
and counts only.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REDUCER_VERSION = "attribution-v1"
CLAIM_CLASSES = ("observed", "derived", "comparative", "associative", "predictive")
COVERAGES = ("full", "partial", "no_baseline")

Files = dict[str, Counter[str]]


def home() -> Path:
    return Path(os.environ.get("PRAXIS_HOME") or Path.home() / ".praxis")


SCHEMA = """
CREATE TABLE IF NOT EXISTS hook_events (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT,
  event       TEXT,
  tool_name   TEXT,
  tool_use_id TEXT,
  ts          TEXT NOT NULL,
  duration_ms REAL NOT NULL CHECK (duration_ms >= 0),
  outcome     TEXT NOT NULL CHECK (outcome IN ('ok', 'unobservable', 'error')),
  note        TEXT
) STRICT;
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
  delta       INTEGER NOT NULL CHECK (delta <> 0)
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
  claim_class     TEXT NOT NULL CHECK (claim_class IN
                    ('observed', 'derived', 'comparative', 'associative',
                     'predictive')),
  coverage        TEXT NOT NULL CHECK (coverage IN ('full', 'partial', 'no_baseline')),
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
CREATE TABLE IF NOT EXISTS repo_config (
  repo_id  TEXT PRIMARY KEY,
  test_cmd TEXT NOT NULL,
  ts       TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS items (
  item_id      TEXT PRIMARY KEY,
  repo_id      TEXT NOT NULL,
  created_ts   TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('write-it')),
  file         TEXT,
  qualname     TEXT,
  agent_lines  INTEGER CHECK (agent_lines IS NULL OR agent_lines >= 0),
  total_lines  INTEGER CHECK (total_lines IS NULL OR total_lines > 0),
  min_fraction REAL NOT NULL CHECK (min_fraction >= 0 AND min_fraction <= 1),
  validation   TEXT NOT NULL,
  claim_class  TEXT NOT NULL CHECK (claim_class = 'derived'),
  sources      TEXT NOT NULL CHECK (json_valid(sources) AND sources <> '[]'),
  reducer_version TEXT NOT NULL,
  assumptions  TEXT NOT NULL CHECK (json_valid(assumptions)),
  -- A refused drill records an item with no function; a function always has a file.
  CHECK ((file IS NULL) = (qualname IS NULL))
) STRICT;
CREATE TABLE IF NOT EXISTS results (
  id            INTEGER PRIMARY KEY,
  item_id       TEXT NOT NULL REFERENCES items (item_id),
  ts            TEXT NOT NULL,
  outcome       TEXT NOT NULL CHECK (outcome IN ('pass', 'fail', 'aborted', 'refused')),
  elapsed_s     REAL NOT NULL CHECK (elapsed_s >= 0),
  test_cmd_hash TEXT NOT NULL,
  claim_class   TEXT NOT NULL CHECK (claim_class = 'observed'),
  coverage      TEXT NOT NULL CHECK (coverage = 'full'),
  sources       TEXT NOT NULL CHECK (json_valid(sources) AND sources <> '[]'),
  reducer_version TEXT NOT NULL,
  warnings      TEXT NOT NULL CHECK (json_valid(warnings))
) STRICT;
"""

ITEM_ASSUMPTIONS = [
    "a function's lines are the agent's when their hashes match agent deltas recorded"
    " for this repository, so a line the owner retyped identically still counts as the"
    " agent's",
    "the tests the owner configured are the only judge of a body",
]


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


@dataclass(frozen=True)
class ItemRow:
    item_id: str
    repo_id: str
    created_ts: str
    kind: str
    file: str | None
    qualname: str | None
    agent_lines: int | None
    total_lines: int | None
    min_fraction: float
    sources: list[str]
    validation: str


@dataclass(frozen=True)
class ResultRow:
    item_id: str
    ts: str
    outcome: str
    elapsed_s: float
    test_cmd_hash: str
    sources: list[str]
    warnings: list[str]


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
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=2000")
        db.executescript(SCHEMA)
        self._db = db
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

    # -- writes ---------------------------------------------------------------------

    def hook_event(
        self,
        *,
        session_id: str | None,
        event: str | None,
        tool_name: str | None,
        tool_use_id: str | None,
        ts: str,
        duration_ms: float,
        outcome: str,
        note: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO hook_events (session_id, event, tool_name, tool_use_id, ts,"
            " duration_ms, outcome, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, event, tool_name, tool_use_id, ts, duration_ms, outcome, note),
        )

    def has_baseline(self, session_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM baselines WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        return row is not None

    def baseline(self, session_id: str, files: Files) -> None:
        rows = [
            (session_id, f, h, n) for f, c in files.items() for h, n in c.items() if n
        ]
        with self.db:
            # An empty tree still needs a baseline row to say "observed, nothing dirty":
            # absence of rows must not read as absence of a baseline.
            self.db.execute(
                "INSERT INTO baselines (session_id, file, line_hash, count)"
                " VALUES (?, ?, ?, ?)",
                (session_id, "", "", 1),
            )
            self.db.executemany(
                "INSERT INTO baselines (session_id, file, line_hash, count)"
                " VALUES (?, ?, ?, ?)",
                rows,
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
        with self.db:
            self.db.execute(
                "INSERT INTO pending (session_id, tool_use_id, ts, file, line_hash,"
                " count) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, tool_use_id, ts, "", "", 1),
            )
            self.db.executemany(
                "INSERT INTO pending (session_id, tool_use_id, ts, file, line_hash,"
                " count) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def pending_take(self, session_id: str, tool_use_id: str) -> Files | None:
        """The pre-snapshot for one tool call, consumed. None without a PreToolUse."""
        with self.db:
            rows = self.db.execute(
                "SELECT file, line_hash, count FROM pending"
                " WHERE session_id = ? AND tool_use_id = ?",
                (session_id, tool_use_id),
            ).fetchall()
            if not rows:
                return None
            self.db.execute(
                "DELETE FROM pending WHERE session_id = ? AND tool_use_id = ?",
                (session_id, tool_use_id),
            )
        return _files(rows)

    def agent_delta(
        self,
        session_id: str,
        tool_use_id: str,
        tool_name: str,
        ts: str,
        changes: list[tuple[str, str, int]],
    ) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO agent_deltas (session_id, tool_use_id, tool_name, ts,"
                " file, line_hash, delta) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (session_id, tool_use_id, tool_name, ts, f, h, d)
                    for f, h, d in changes
                ],
            )

    def attributions(self, rows: list[Attribution]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO attributions (session_id, ts, trigger, file, agent, human,"
                " preexisting, ambiguous, reverted, claim_class, coverage, sources,"
                " reducer_version, assumptions, warnings)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.session_id,
                        r.ts,
                        r.trigger,
                        r.file,
                        r.agent,
                        r.human,
                        r.preexisting,
                        r.ambiguous,
                        r.reverted,
                        r.claim_class,
                        r.coverage,
                        json.dumps(r.sources),
                        REDUCER_VERSION,
                        json.dumps(r.assumptions),
                        json.dumps(r.warnings),
                    )
                    for r in rows
                ],
            )

    def touch_session_repo(self, session_id: str, repo_id: str, ts: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO session_repos (session_id, repo_id, ts)"
            " VALUES (?, ?, ?)",
            (session_id, repo_id, ts),
        )

    def set_test_cmd(self, repo_id: str, test_cmd: str, ts: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO repo_config (repo_id, test_cmd, ts)"
            " VALUES (?, ?, ?)",
            (repo_id, test_cmd, ts),
        )

    def item(self, row: ItemRow) -> None:
        self.db.execute(
            "INSERT INTO items (item_id, repo_id, created_ts, kind, file, qualname,"
            " agent_lines, total_lines, min_fraction, validation, claim_class, sources,"
            " reducer_version, assumptions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'derived', ?, ?, ?)",
            (
                row.item_id,
                row.repo_id,
                row.created_ts,
                row.kind,
                row.file,
                row.qualname,
                row.agent_lines,
                row.total_lines,
                row.min_fraction,
                row.validation,
                json.dumps(row.sources),
                REDUCER_VERSION,
                json.dumps(ITEM_ASSUMPTIONS),
            ),
        )

    def result(self, row: ResultRow) -> None:
        self.db.execute(
            "INSERT INTO results (item_id, ts, outcome, elapsed_s, test_cmd_hash,"
            " claim_class, coverage, sources, reducer_version, warnings)"
            " VALUES (?, ?, ?, ?, ?, 'observed', 'full', ?, ?, ?)",
            (
                row.item_id,
                row.ts,
                row.outcome,
                row.elapsed_s,
                row.test_cmd_hash,
                json.dumps(row.sources),
                REDUCER_VERSION,
                json.dumps(row.warnings),
            ),
        )

    # -- reads ----------------------------------------------------------------------

    def test_cmd(self, repo_id: str) -> str | None:
        row = self.db.execute(
            "SELECT test_cmd FROM repo_config WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        return None if row is None else str(row[0])

    def agent_files(self, repo_id: str) -> list[str]:
        return [
            str(r[0])
            for r in self.db.execute(
                "SELECT DISTINCT d.file FROM agent_deltas d"
                " JOIN session_repos s ON s.session_id = d.session_id"
                " WHERE s.repo_id = ? ORDER BY d.file",
                (repo_id,),
            )
        ]

    def agent_hashes(self, repo_id: str, file: str) -> tuple[set[str], list[str]]:
        """Line hashes the agent added to one file across every session on the repo, and
        the sessions they came from. A hash the agent later removed (sum <= 0) is not
        the agent's any more."""
        hashes: set[str] = set()
        for h, total in self.db.execute(
            "SELECT d.line_hash, SUM(d.delta) FROM agent_deltas d"
            " JOIN session_repos s ON s.session_id = d.session_id"
            " WHERE s.repo_id = ? AND d.file = ? GROUP BY d.line_hash",
            (repo_id, file),
        ):
            if total > 0:
                hashes.add(str(h))
        sessions = [
            str(r[0])
            for r in self.db.execute(
                "SELECT DISTINCT d.session_id FROM agent_deltas d"
                " JOIN session_repos s ON s.session_id = d.session_id"
                " WHERE s.repo_id = ? AND d.file = ? ORDER BY d.session_id",
                (repo_id, file),
            )
        ]
        return hashes, sessions

    def item_rows(self) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        return list(self.db.execute("SELECT * FROM items ORDER BY created_ts"))

    def result_rows(self) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        return list(self.db.execute("SELECT * FROM results ORDER BY id"))

    def baseline_of(self, session_id: str) -> Files | None:
        rows = self.db.execute(
            "SELECT file, line_hash, count FROM baselines WHERE session_id = ?",
            (session_id,),
        ).fetchall()
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

    def attribution_rows(self, session_id: str | None = None) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        if session_id is None:
            return list(self.db.execute("SELECT * FROM attributions ORDER BY id"))
        return list(
            self.db.execute(
                "SELECT * FROM attributions WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        )

    def hook_event_rows(self, session_id: str | None = None) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        if session_id is None:
            return list(self.db.execute("SELECT * FROM hook_events ORDER BY id"))
        return list(
            self.db.execute(
                "SELECT * FROM hook_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        )

    def sessions(self) -> list[str]:
        return [
            r[0]
            for r in self.db.execute(
                "SELECT session_id FROM hook_events WHERE session_id IS NOT NULL"
                " GROUP BY session_id ORDER BY MIN(ts)"
            )
        ]


def _files(rows: list[tuple[str, str, int]]) -> Files:
    files: Files = {}
    for file, h, n in rows:
        if file == "" and h == "":
            # The marker row that tells an empty snapshot from no snapshot at all.
            continue
        files.setdefault(file, Counter())[h] += n
    return files
