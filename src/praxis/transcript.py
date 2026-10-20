"""Analysis-time reader of a Claude Code transcript (`~/.claude/projects/*/<id>.jsonl`).

It returns identifiers, timestamps, counts and hashes. The one exception is `names`: the
whole-word identifiers and path-like tokens of a prompt or assistant text, which exist
only so a caller can intersect them with component ids and repo paths. They are kept out
of repr and must never be stored, logged or printed. A set is right for them because
prompt_named is a presence test; nothing else here collapses duplicates.

`index` is the 0-based line number in the file, the turn index a derived row cites.
Assistant entries carry no promptId (measured on 2.1.303), so assistant text and tool
calls take the promptId of the latest main-thread user entry that carried one. A line
that is not a JSON object of the expected shape becomes a warning naming its index; only
a blank line is passed over. A missing `origin` is the reason code `absent`, never
`human`. Exceptions carry a reason code and nothing read from the file.

ExitPlanMode is read as a two-option decision, approve (0) or reject (1), from the
tool result's `is_error`; that mapping needs a recorded rejected plan to confirm (E08).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NON_IDENT = re.compile(r"[^A-Za-z0-9_]+")
_PATHISH = re.compile(r"[A-Za-z0-9_./\-]+")
_DOTTED = re.compile(r"[A-Za-z0-9_]\.[A-Za-z0-9_]")
_SESSION_ID = re.compile(r"[A-Za-z0-9-]+")

Decision = tuple[int, int | None, str]
Outcome = tuple[bool | None, object]  # (is_error, toolUseResult or None if shared)


class TranscriptError(Exception):
    """One reason code: unreadable, ambiguous, bad_session_id."""


@dataclass(frozen=True)
class Prompt:
    index: int
    prompt_id: str | None
    ts: str
    origin_kind: str
    names: frozenset[str] = field(repr=False)


@dataclass(frozen=True)
class AssistantText:
    index: int
    prompt_id: str | None
    ts: str
    names: frozenset[str] = field(repr=False)
    ended_turn: bool | None  # None: the entry recorded no stop_reason


@dataclass(frozen=True)
class ToolUse:
    index: int
    tool_use_id: str
    name: str
    prompt_id: str | None
    ts: str
    option_count: int | None
    chosen_index: int | None
    options_hash: str | None
    # The lowercased suffix of the file the call names ("" for a name with none);
    # None when its input names no file. The path itself is not kept.
    suffix: str | None = None


@dataclass(frozen=True)
class Session:
    session_id: str
    path: str
    prompts: tuple[Prompt, ...]
    assistant: tuple[AssistantText, ...]
    tools: tuple[ToolUse, ...]
    warnings: tuple[str, ...]


def options_hash(labels: Sequence[str]) -> str:
    """sha256 of the option labels in offered order; the recorder must use this too."""
    canon = json.dumps(list(labels), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def names(text: str) -> frozenset[str]:
    words = {w for w in _NON_IDENT.split(text) if _IDENT.fullmatch(w)}
    paths = {tok.removeprefix("./").rstrip(".-/") for tok in _PATHISH.findall(text)}
    return frozenset(words | {p for p in paths if "/" in p or _DOTTED.search(p)})


def locate(session_id: str, claude_projects: Path) -> Path | None:
    if not _SESSION_ID.fullmatch(session_id):
        raise TranscriptError("bad_session_id")
    found = sorted(claude_projects.glob(f"*/{session_id}.jsonl"))
    if len(found) > 1:
        raise TranscriptError("ambiguous")
    return found[0] if found else None


def read(path: Path) -> Session:
    try:
        raw = path.read_bytes()
    except OSError:
        raise TranscriptError("unreadable") from None
    reader = _Reader()
    for index, line in enumerate(raw.split(b"\n")):
        if line.strip():
            reader.line(index, line)
    return reader.session(path)


def _blocks(message: object) -> list[dict[str, Any]] | None:
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list) and all(isinstance(b, dict) for b in content):
        return content
    return None


def _text(blocks: list[dict[str, Any]]) -> str | None:
    texts = [b.get("text") for b in blocks if b.get("type") == "text"]
    if not all(isinstance(t, str) for t in texts):
        return None
    return "\n".join(t for t in texts if isinstance(t, str))


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


class _Reader:
    def __init__(self) -> None:
        self.prompts: list[Prompt] = []
        self.assistant: list[AssistantText] = []
        self.uses: list[tuple[int, str, str, str | None, str, object]] = []
        self.results: dict[str, list[Outcome]] = {}
        self.warnings: list[str] = []
        self.prompt_id: str | None = None

    def line(self, index: int, line: bytes) -> None:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            self.warnings.append(f"malformed_line:{index}")
            return
        if not self._entry(index, entry):
            self.warnings.append(f"unexpected_shape:{index}")

    def _entry(self, index: int, entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        kind = entry.get("type")
        if kind not in ("user", "assistant") or entry.get("isSidechain") is True:
            return True
        blocks = _blocks(entry.get("message"))
        ts = _str(entry.get("timestamp"))
        if blocks is None:
            return False
        if kind == "user":
            return self._user(index, entry, blocks, ts)
        return ts is not None and self._assistant(index, entry["message"], blocks, ts)

    def _tool_results(
        self, entry: dict[str, Any], results: list[dict[str, Any]]
    ) -> bool:
        """toolUseResult belongs to one block; with several it names none of them."""
        shared = len(results) > 1
        for block in results:
            tid = _str(block.get("tool_use_id"))
            if tid is None:
                return False
            is_error = block.get("is_error")
            outcome = entry.get("toolUseResult") if not shared else None
            err = is_error if isinstance(is_error, bool) else None
            self.results.setdefault(tid, []).append((err, outcome))
        return True

    def _user(
        self,
        index: int,
        entry: dict[str, Any],
        blocks: list[dict[str, Any]],
        ts: str | None,
    ) -> bool:
        pid = _str(entry.get("promptId"))
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            if not self._tool_results(entry, results):
                return False
            self.prompt_id = pid if pid is not None else self.prompt_id
            return True
        text = _text(blocks)
        if ts is None or text is None:
            return False
        origin = entry.get("origin")
        kind = _str(origin.get("kind")) if isinstance(origin, dict) else None
        self.prompt_id = pid
        self.prompts.append(Prompt(index, pid, ts, kind or "absent", names(text)))
        return True

    def _assistant(
        self, index: int, message: dict[str, Any], blocks: list[dict[str, Any]], ts: str
    ) -> bool:
        text = _text(blocks)
        uses = [
            (
                index,
                _str(b.get("id")),
                _str(b.get("name")),
                self.prompt_id,
                ts,
                b.get("input"),
            )
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        if text is None or any(u[1] is None or u[2] is None for u in uses):
            return False
        if any(b.get("type") == "text" for b in blocks):
            stop = message.get("stop_reason")
            ended = None if stop is None else stop == "end_turn"
            self.assistant.append(
                AssistantText(index, self.prompt_id, ts, names(text), ended)
            )
        self.uses.extend((i, str(t), str(n), p, s, x) for i, t, n, p, s, x in uses)
        return True

    def session(self, path: Path) -> Session:
        tools = []
        for index, tid, name, pid, ts, tool_input in self.uses:
            decided: Decision | None = None
            if name in ("AskUserQuestion", "ExitPlanMode"):
                decided = _decision(name, tool_input, self.results.get(tid, []))
                if decided is None:
                    self.warnings.append(f"decision_unscorable:{index}")
            k, chosen, digest = decided or (None, None, None)
            suffix = _suffix(tool_input)
            tools.append(ToolUse(index, tid, name, pid, ts, k, chosen, digest, suffix))
        return Session(
            path.stem,
            str(path),
            tuple(self.prompts),
            tuple(self.assistant),
            tuple(tools),
            tuple(self.warnings),
        )


def _suffix(tool_input: object) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path", tool_input.get("notebook_path"))
    return PurePosixPath(path).suffix.lower() if isinstance(path, str) else None


def _decision(
    name: str, tool_input: object, outcomes: list[Outcome]
) -> Decision | None:
    """None when the call's shape cannot be scored; an absent result is chosen None."""
    if not isinstance(tool_input, dict):
        return None
    outcome = outcomes[0] if len(outcomes) == 1 else None
    if name == "ExitPlanMode":
        plan = _str(tool_input.get("plan"))
        if plan is None:
            return None
        rejected = outcome[0] if outcome is not None else None
        return 2, (None if rejected is None else int(rejected)), options_hash([plan])
    return _ask(tool_input.get("questions"), outcome)


def _ask(questions: object, outcome: Outcome | None) -> Decision | None:
    if not (isinstance(questions, list) and len(questions) == 1):
        return None
    question = questions[0]
    options = question.get("options") if isinstance(question, dict) else None
    if not isinstance(options, list) or question.get("multiSelect") is not False:
        return None
    labels = [o.get("label") if isinstance(o, dict) else None for o in options]
    if not labels or not all(isinstance(lab, str) for lab in labels):
        return None
    result = outcome[1] if outcome is not None else None
    answers = result.get("answers") if isinstance(result, dict) else None
    answer = (
        answers.get(question.get("question")) if isinstance(answers, dict) else None
    )
    chosen = labels.index(answer) if labels.count(answer) == 1 else None
    return len(labels), chosen, options_hash([str(lab) for lab in labels])
