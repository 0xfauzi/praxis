# Praxis: Reposition to Coach

> Plan dated 2026-05-27. Persisted ahead of execution per `~/.claude/CLAUDE.md` guidance on complex features.

## Summary

Praxis is being repositioned from a retrospective AI-usage scorecard into an AI usage coach. The product becomes a four-step loop the user lives inside:

```
Commit (Mon)  ->  Cue (every session)  ->  Reflect (post-session)  ->  Review (next Mon)
```

The existing engine (scanners, judge, rubric, follow-up math, trajectory, baseline) is the evidence layer that keeps the coaching honest. What's changing is the user-facing surface: four new verbs, one rename, integration glue into Claude Code / Codex CLI / Copilot, and a README rewrite that drops the "scorecard" framing entirely.

Ship the full loop in one release.

---

## 1. Why this design

### The gap between scorecard and coach

A retrospective dashboard produces awareness but rarely produces behavior change. Real coaching, in the behavior-change literature, has five elements. Citations validated 2026-05-27; see notes on effect-size accuracy below:

| Element | Reference | Notes |
|---|---|---|
| Goal commitment (user articulates one specific change) | Locke & Latham (2002), *American Psychologist* 57(9); Gollwitzer & Sheeran (2006), *Advances in Experimental Social Psychology* 38, "Implementation Intentions and Goal Achievement" | The Gollwitzer & Sheeran result is **d = 0.65 across 94 independent tests on goal attainment** (not behavior in general). Bieleke et al. 2024 (642 tests) is the more recent meta-synthesis. |
| Just-in-time cue (prompt at the moment of behavior) | Nahum-Shani et al. (2018), *Annals of Behavioral Medicine* 52(6), "Just-in-Time Adaptive Interventions" | The framework is canonical. The "proximity beats intensity" effect is **modest in current evidence** (2024 meta-analysis of 23 JITAI studies: g ≈ 0.15). Frame as motivating, not proven. |
| Practice with feedback | Ericsson, Krampe & Tesch-Romer (1993), *Psychological Review* 100(3); Hattie & Timperley (2007), *Review of Educational Research* 77(1) | Ericsson's claims are bounded by Macnamara, Hambrick & Oswald (2014) finding deliberate practice explains only 4-26% of variance depending on domain. Hattie & Timperley's original d ≈ 0.79 was revised down to **d = 0.48 across 435 studies** by Wisniewski, Zierer & Hattie (2020), *Frontiers in Psychology* 10:3087. Cite both. |
| Self-reflection (user names what happened) | Kolb (1984) experiential learning; Schraw / Flavell metacognition | Less contentious. |
| Visible loop closure (user sees "I said X, here is what happened") | Ryan & Deci (2000), *American Psychologist* 55(1) self-determination theory; Carver & Scheier control theory | SDT replicates well; cite Ryan & Deci 2000 as canonical, Ryan & Deci (2017) book for depth. |

**Frameworks we explicitly do NOT invoke:** ego depletion / willpower depletion (Hagger et al. 2016 multilab replication, N=2,141 across 23 labs, found d=0.04 - essentially null); the "10,000 hours" popularization of Ericsson; Fogg's B=MAP as a tested causal model (it's a design heuristic, not validated theory). The Praxis differentiator is evidence over claims, so we keep the citations honest.

**Fogg B = MAP** is the right framing for our cue layer **as a design heuristic**: cite Fogg (2009), *Persuasive Technology Conference Proceedings* (ACM), not the *Tiny Habits* book. Wood & Rünger (2016), *Annual Review of Psychology* 67, "Psychology of Habit," is the academic anchor for habit formation; Duhigg's "cue, routine, reward" is popular metaphor, not Wood's framework (her terms: context, repetition, reward).

Praxis today does practice-with-feedback well (the judge layer) and has the structural kernel of loop closure (the `follow_up` engine computes outcome from pure data, no LLM in the loop). The three missing pieces are agency, mid-session cue, and self-reflection.

This plan adds those three pieces using infrastructure that mostly already exists:

  - The headline moment with `suggested_alternative` is the seed of the commitment.
  - The `follow_ups` table already carries baseline + measured + outcome.
  - The `shell-nudge` surface already proves an external tool can install ambient reminders.
  - Claude Code, Codex CLI, and Copilot all expose either `SessionStart` hooks or auto-loaded instruction files. Verified via documentation research on 2026-05-27.

### Decisions taken on 2026-05-27

  - Reposition fully ("coach-first everywhere"). No `praxis week` alias. The word "scorecard" is removed from positioning.
  - Agency-first: the user picks the commitment from three system-generated suggestions (or free-text).
  - `praxis install-coach` auto-detects each tool and prompts per-tool ("install for Claude Code? [Y/n]").
  - Ship the whole loop in one release.

---

## 2. The four-verb loop

### `praxis commit`

Run at the start of a week (or any time the user wants to change focus). Shows three suggestions plus a free-text option:

```
This week's focus options:

  1. Before debugging, paste the error + your expected output.
     (from this week's headline moment - recurring pattern, 3x in last 4 weeks)

  2. Ask "what would change your mind?" on high-stakes claims.
     (your weakest dim: verification, currently 4.8/10)

  3. State the goal + the 'done when' criterion before turn one.
     (your weakest dim: planning, currently 5.1/10)

  4. Keep last week's commitment: "..."   [if applicable]

  5. Write your own (free text)

Pick one [1-5]:
```

Suggestion sources (in priority order):

  1. The current week's selected headline moment's `suggested_alternative`.
  2. The two weakest dimensions' canonical drill from `coach.py`'s drill bank.
  3. The prior week's commitment, if it was in flight and not yet closed.

Persists into `follow_ups` with new fields: `user_chosen=True`, `display_text` (the verbatim string the user picked, hard-capped at 280 chars).

**Re-commit semantics (mid-week).** If `praxis commit` is invoked when an active pending commitment already exists for the current ISO week, the prompt offers an explicit replace path:

```
You already have an active commitment this week:
  "<existing display_text>"

  [r]eplace      Pick a new focus (the existing one is marked 'superseded' and kept in history)
  [k]eep         No change; the existing commitment stays active
  [c]ancel       Exit without writing
```

On `replace`: the existing row's `outcome` becomes `superseded`, its `superseded_by` is set to the new row's `id`, and the new commitment is inserted with `outcome='pending'`. Both rows are preserved so the weekly review can show the full intra-week history. Each weekly run still closes only the most-recent active commitment (per the partial-unique index defined in Section 4).

### `praxis nudge`

The single command that knows the active commitment and prints it in a tool-appropriate format. Called by hooks and by the shell-nudge fallback. One `--format` flag with three values:

  - `--format claude-code` -- emits the JSON shape the SessionStart hook expects.
  - `--format codex` -- emits the JSON shape the Codex hook expects.
  - `--format text` (default) -- one-line stdout for the shell-nudge.

Internal throttling: `~/.praxis/.last_nudge` records the last fire time per surface. If a surface fires more than once per 30 minutes (configurable), the second call returns empty. Prevents commitment fatigue when the user opens many short sessions in succession.

### `praxis reflect`

A 30-second post-session check-in. Opt-in via the Claude Code `Stop` hook (Codex equivalent when shipping):

```
That session ran 4 turns. Quick check-in?

  Did you focus on: "Before debugging, paste the error + your expected output."
    [y]es / [n]o / [p]artial / [s]kip

  One line - what got in the way, or what helped?
  >
```

Throttling: only fires when the session crossed a meaningful threshold (>= 2 turns and >= 60s elapsed). Persists into a new `session_reflections` table.

### `praxis review`

The renamed digest. Masthead leads with commitment status, not the score. Underneath, the existing rubric / trajectory / baseline / per-model panels are unchanged.

```
PRAXIS - Week of 2026-W22

Your focus this week:
  "Before debugging, paste the error + your expected output."

How it went:
  Sessions:    7 (vs. 5 last week)
  You said:    4 yes / 1 partial / 2 no
  Data says:   verification_rate 0.62 -> 0.78  (improved)
  Gap:         your self-report and the data agree this week.

Next:
  Outcome: improved. Lock it in for one more week, or pick a new focus?
  [k]eep / [n]ew / [d]igest

[full digest below...]
```

When self-report and data disagree, the closing paragraph names it as a curiosity, not a verdict ("Interesting gap - the data shows verification dropped while you reported sticking to the focus. What do you think happened?"). The judge writes this paragraph under a constrained prompt that forbids accusatory framing.

**Interactive vs non-interactive.** The masthead's trailing prompt (`[k]eep / [n]ew / [d]igest`) only renders when stdin is a TTY (`sys.stdin.isatty()`). When invoked from cron / launchd / scripts (where `--notify` and `install-weekly` keep firing the digest), the prompt is omitted entirely and the command exits 0 after rendering. An explicit `--non-interactive` flag forces the non-prompting path even from a TTY for predictable scripting:

  - `[k]eep`: writes a new `follow_ups` row keeping the same `display_text` for next week (re-activates the commitment for the new ISO week).
  - `[n]ew`: invokes `praxis commit` inline (re-entry into Section 2's selection prompt).
  - `[d]igest`: dismiss the prompt and return; the rest of the digest is already on stdout.

The legacy `install-weekly` LaunchAgent invocation (`praxis review --notify --non-interactive`) is the canonical non-TTY runner.

### `praxis install-coach` (the bootstrapper)

Detects each tool's presence and prompts per-tool. Detection:

  - Claude Code: `~/.claude/settings.json` or `~/.claude/projects/` exists.
  - Codex CLI: `~/.codex/` exists.
  - Copilot: any VS Code workspace storage has Copilot chat artifacts (already detected by the existing scanner).

For each tool present, prompt:

```
Found Claude Code. Install the Praxis coaching hook? [Y/n]:
```

On yes, install the appropriate surface (see Section 5).

Flags:

  - `--yes` -- assume yes for every prompt (scripted installs).
  - `--all` -- install for every tool whether or not detected.
  - `--tool claude|codex|copilot` -- only that one.

Sister command: `praxis uninstall-coach`, symmetric.

---

## 3. CLI surface changes

### Renames

| Old | New | Notes |
|---|---|---|
| `praxis week` | `praxis review` | No alias. "Coach-first everywhere" decision. |
| (internal) `cmd_week` | `cmd_review` | Rename the function too. |
| (internal) `run_weekly` | unchanged | The internal concept is still a weekly run; only the verb changes. |
| (internal) `WeeklyRunSummary` | unchanged | Same reasoning. |

### Additions

| Verb | Purpose |
|---|---|
| `praxis commit` | Pick this week's focus |
| `praxis nudge` | Print active commitment (consumed by hooks; can be run manually) |
| `praxis reflect` | Post-session check-in |
| `praxis install-coach` | Set up cue surfaces for detected AI tools |
| `praxis uninstall-coach` | Tear down cue surfaces |

### Unchanged (secondary)

`scan`, `re-score`, `baseline`, `history`, `show`, `report`, `open`, `last`, `status`, `rubric`, `follow-up`, `models`, `config`, `install-weekly`, `uninstall-weekly`, `shell-nudge`, `install-shell-nudge`, `uninstall-shell-nudge`. These still work; they just stop being the headliners in `--help` and the README.

**`shell-nudge` semantics post-rename:** the existing shell-startup `shell-nudge` continues to fire, but its emitted snippet now delegates to `praxis nudge --format text` instead of carrying its own hard-coded text. This means the shell-nudge surface shares the same throttle state (`~/.praxis/.last_nudge`) as the SessionStart hooks -- a single shell startup that fires after a Claude Code SessionStart hook just fired stays silent within the 30-min window. The shell-nudge is therefore a fallback for users who don't have hooks installed (e.g., Copilot-only users), not a separate competing surface.

`praxis --help` rewrites: lead with the loop verbs (`commit`, `nudge`, `reflect`, `review`, `install-coach`). Secondary verbs under a "More" section.

---

## 4. Schema changes

### `follow_ups` table additions

```sql
ALTER TABLE follow_ups ADD COLUMN user_chosen INTEGER NOT NULL DEFAULT 0;
ALTER TABLE follow_ups ADD COLUMN display_text TEXT;
ALTER TABLE follow_ups ADD COLUMN superseded_by INTEGER REFERENCES follow_ups(id);
```

  - `user_chosen` distinguishes a user-picked commitment from the system-default headline-moment derivation.
  - `display_text` is the verbatim string the user sees in nudges. When `user_chosen=0`, defaults to `commitment_text` (the existing field). Free-text user-supplied display_text is hard-capped at **280 characters** (matches Twitter's limit and fits the SessionStart hook payload + Copilot instruction file budget); free-text longer than 280 is rejected with a re-prompt during `praxis commit`.
  - `superseded_by` enables the mid-week re-commit flow (Section 2). When the user replaces a still-pending commitment, the prior row's `outcome` becomes `superseded` and its `superseded_by` points to the new row's `id`. The `Outcome` literal in `praxis/follow_up.py` adds `"superseded"` alongside the existing `improved | unchanged | worse | pending`.
  - **Weekly uniqueness:** the migration also adds a partial-unique index that ensures at most one *active* (`outcome IN ('pending')` and `superseded_by IS NULL`) row per `week_iso`. This is what makes `session_reflections.commitment_week_iso` resolvable to a single active commitment per week (see FK note below).

```sql
CREATE UNIQUE INDEX idx_follow_ups_one_active_per_week
  ON follow_ups(week_iso) WHERE outcome = 'pending' AND superseded_by IS NULL;
```

### New `session_reflections` table

```sql
CREATE TABLE session_reflections (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_stable_id        TEXT NOT NULL,
    follow_up_id             INTEGER NOT NULL REFERENCES follow_ups(id),
    self_report              TEXT NOT NULL CHECK (self_report IN ('yes', 'no', 'partial', 'skip')),
    note                     TEXT,
    created_at               TEXT NOT NULL
);
CREATE INDEX idx_reflections_follow_up ON session_reflections(follow_up_id);
```

  - One row per session reflection. The FK now targets `follow_ups(id)` (guaranteed unique via PRIMARY KEY) rather than `week_iso` (which is no longer unique once the supersede mechanism allows multiple rows per week). The architect flagged the prior FK target on `week_iso` as broken; this is the fix.
  - Aggregated per `follow_up_id` to compute "you said: 4 yes / 1 partial / 2 no" in the review masthead.

### Migration

A new `praxis/storage/migrations/` directory with numbered SQL files (e.g., `0001_add_follow_up_columns.sql`, `0002_create_session_reflections.sql`). `profile_store.py` runs them on init, tracked via a `schema_migrations` table.

**Transactional semantics (per migration):** each `.sql` file runs inside `BEGIN; ... COMMIT;`. The `INSERT INTO schema_migrations (version, applied_at) VALUES (...)` row goes inside the same transaction so the version is recorded if-and-only-if the migration succeeded. On any exception, the connection issues `ROLLBACK` and re-raises; subsequent invocations re-attempt that version. Idempotent only at the granularity of "fully applied" -- partially applied migrations are impossible by construction.

```python
# Sketch (praxis/storage/migrations/runner.py)
for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
    version = path.stem  # e.g. "0001_add_follow_up_columns"
    if version in already_applied:
        continue
    try:
        conn.execute("BEGIN")
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                     (version, datetime.now(timezone.utc).isoformat()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
```

---

## 5. Integration architecture

### One source of truth, three surfaces

`praxis nudge` is the only code path that reads the active commitment from the DB. Every integration surface calls it (or, for static surfaces, is rewritten by a separate command):

```
                  +----------------+
                  | follow_ups DB  |
                  +-------+--------+
                          |
                          v
                  +----------------+
                  | praxis nudge   |  <-- the only reader
                  +-------+--------+
                          |
        +-----------------+-----------------+
        |                 |                 |
        v                 v                 v
   Claude Code        Codex CLI         Copilot
   SessionStart       SessionStart      ~/.copilot/instructions/
   hook (dynamic)     hook (dynamic)    praxis-commitment.md (static,
                                         rewritten on praxis commit)
```

### Claude Code

`praxis install-coach claude-code` (or the auto-detected path) writes to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{
      "matcher": "*",
      "_praxisManaged": true,
      "hooks": [{
        "type": "command",
        "command": "praxis nudge --format claude-code"
      }]
    }],
    "Stop": [{
      "matcher": "*",
      "_praxisManaged": true,
      "hooks": [{
        "type": "command",
        "command": "praxis reflect --session-end --non-interactive-fallback"
      }]
    }]
  }
}
```

The hook installer must:

  - Read the existing settings.json (if any) and merge, not overwrite.
  - **Use the `_praxisManaged: true` sentinel key on every block Praxis adds** (strict JSON forbids comments). Uninstall removes only entries with this key; pre-existing user-authored hooks at the same event name are preserved. Claude Code ignores unknown keys at this level, so the sentinel is safe to ship.
  - Validate the resulting JSON before atomic-write (tempfile + `os.replace`).

`praxis nudge --format claude-code` returns a JSON envelope:

```json
{
  "hookSpecificOutput": {
    "additionalContext": "[Praxis] This week's focus: <commitment text>"
  }
}
```

The text appears as injected context at session start. No banner, no popup, no interruption.

Reference: https://code.claude.com/docs/en/hooks-guide.md

### Codex CLI

Same shape, into `~/.codex/hooks.json`:

```json
{
  "hooks": [
    {
      "event": "SessionStart",
      "command": "praxis nudge --format codex"
    },
    {
      "event": "Stop",
      "command": "praxis reflect --session-end --non-interactive-fallback"
    }
  ]
}
```

`praxis nudge --format codex` returns the Codex-shape JSON:

```json
{
  "hookSpecificOutput": {
    "additionalContext": "[Praxis] This week's focus: <commitment text>"
  }
}
```

Reference: https://developers.openai.com/codex/hooks

### Copilot (VS Code + JetBrains)

Copilot has no session-lifecycle hooks for non-extension tooling, so the cue is file-based. Research on 2026-05-28 confirmed two important constraints:

  1. **Auto-loaded paths are governed by `chat.instructionsFilesLocations` in VS Code user `settings.json`.** A file dropped into `~/.copilot/instructions/` is *not* guaranteed to load without that path being explicitly enabled (the VS Code docs contradict themselves on the default state).
  2. **Filenames must end in `*.instructions.md`** -- not `.md`. VS Code only auto-loads files matching that pattern.

The installer behavior reflects both. Primary surface (per-repo, confirmed auto-load behavior):

  - `.github/copilot-instructions.md` at the workspace root. **Verified**: VS Code auto-loads this with zero settings changes. Praxis maintains a managed block inside an existing or new file:

    ```markdown
    <!-- praxisManaged:begin -->
    This week's focus: <commitment text>
    (Praxis - your AI usage coach. Run `praxis review` to see how it's going.)
    <!-- praxisManaged:end -->
    ```

    `install-coach copilot` asks per-repo before writing (opt-in, to avoid surprising people on shared repos).

Optional surface (user-level, cross-workspace):

  - File path: the installer writes `<vscode-user-data>/prompts/praxis-commitment.instructions.md` where `<vscode-user-data>` is the platform-specific VS Code user-data folder (`~/Library/Application Support/Code/User/prompts/` on macOS, `~/.config/Code/User/prompts/` on Linux, `%APPDATA%\Code\User\prompts\` on Windows). This is the directory VS Code's "New Instructions File... -> User Data Folder" command writes to and which Copilot Chat reads by default for any file matching `*.instructions.md`.
  - In addition, the installer ensures the user-level `settings.json` includes that directory in `chat.instructionsFilesLocations: { "<path>": true }`. This step is gated behind an explicit prompt because it modifies user-level VS Code settings; the installer never silently edits user settings.json.

Both surfaces are rewritten whenever `praxis commit` runs (or the active commitment changes). The instruction file content is the same on both surfaces; only the path varies.

References:
  - https://code.visualstudio.com/docs/copilot/customization/custom-instructions
  - https://code.visualstudio.com/docs/copilot/reference/copilot-settings
  - https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot

### Throttling logic

`praxis nudge` reads `~/.praxis/.last_nudge` (JSON, keyed by surface + cwd hash). If the same surface fired within the last 30 minutes from the same project, returns empty. Configurable via `~/.praxis/config.toml`:

```toml
[nudge]
throttle_minutes = 30
```

### Self-report submission (the `Stop` hook path)

**Hook payload contract** (verified 2026-05-28 against the Claude Code hooks docs). The Stop hook command reads a JSON object from stdin. The fields Praxis uses:

  - `session_id` (string, always present) -- the stable session ID, used as `session_reflections.session_stable_id` (note: the existing Praxis scanners store this same ID with the `claude_` prefix; the Stop hook payload is the un-prefixed Claude-internal form).
  - `transcript_path` (string, always present) -- absolute path to the JSONL transcript for the session. Used to count turns + elapsed time without re-scanning the projects directory.
  - `cwd` (string, always present) -- working directory; used to scope the throttle and to detect which project the session belonged to.
  - `hook_event_name` (string, always `"Stop"`) -- sanity check.

The Codex equivalent has the same general shape (`session_id`, `cwd`, `hook_event_name`) per the OpenAI Codex hooks docs. When `praxis reflect --session-end` runs, it accepts either shape via duck-typing on the JSON keys.

**Fallback when fields are missing.** If stdin is empty, the JSON is malformed, or required fields are absent, `praxis reflect --session-end` writes a one-row marker into `session_reflections` with `self_report='skip'` and `note='hook payload missing or unparseable'`, then exits 0. The hook never blocks the AI tool's exit on a bad payload.

When the Stop hook fires `praxis reflect --session-end`, the command:

  1. Reads stdin (with 100ms read timeout); parses JSON; extracts `session_id` and `transcript_path`.
  2. Stat-reads the transcript file to count user turns and compute elapsed time; bails silently (writes a `skip` row with `note='session too short'`) if turns < 2 or elapsed < 60s.
  3. If stdin is a TTY: opens the interactive 2-question prompt in the same terminal. Otherwise (the Stop hook is non-interactive by default): launches the prompt in a **detached child process** (see below). If the parent terminal has already closed, the child writes a `skip` row with `note='parent terminal closed'` instead of attempting to attach to a missing TTY.

**Detached process mechanism.** The cross-platform launch uses `subprocess.Popen` with `start_new_session=True` (which on POSIX calls `setsid()` to detach from the controlling terminal; on Windows, sets `CREATE_NEW_PROCESS_GROUP`). The detached `praxis reflect` process inherits no stdio from the parent and opens its own from `/dev/tty` (POSIX) or `CONIN$` / `CONOUT$` (Windows). If neither is available (parent was a non-terminal context like a daemon), the child takes the same `skip` path described above. **We do not attempt to spawn new Terminal.app / iTerm / Ghostty windows** -- terminal emulator integration is fragile and out of scope; users on non-TTY contexts get their reflection skipped and recorded as such.

**Hook timing budget.** The Stop hook itself returns within 5 seconds (configurable via `~/.praxis/config.toml [reflect] hook_timeout_seconds`). Everything beyond stdin parse and the detached launch runs in the child; the parent hook never blocks the AI tool's exit. Per the Claude Code hook docs, exit code 0 is "non-blocking, continue normally" -- the only way to block a Stop is exit code 2, which Praxis never returns.

---

## 6. README + masthead rewrite

### README structure

```
# Praxis

> Your AI usage coach.

Praxis helps you pick one specific behavior to improve, nudges you in-session
through Claude Code and Codex, and shows you what changed.

## The loop

1. **Commit** -- `praxis commit`. Pick one focus for the week from three suggestions.
2. **Cue** -- Praxis injects your focus into every Claude Code / Codex session.
3. **Reflect** -- A 30-second check-in after each session (optional).
4. **Review** -- `praxis review`. See what changed.

## Install

[unchanged install instructions]

After install:
    praxis install-coach   # wire up Claude Code / Codex / Copilot

## What's under the hood

Praxis scores your AI usage against six research-backed dimensions. That
scoring is the evidence layer that keeps coaching honest. When you commit
to "verify more," the scoring tells you whether you actually did.

[rubric table unchanged]

[Shen & Tamkin / Sarkar / OpenRouter / Anthropic research grounding unchanged]
```

### Help text rewrites

`praxis --help` leads with the loop verbs. Secondary verbs grouped under a "More" header.

### Masthead rewrite (`praxis review` terminal output)

Already described in Section 2.

### Files touched

  - `README.md` -- full rewrite of the lead sections.
  - `praxis/cli/__main__.py` -- new verbs, rename, help reorganisation.
  - `praxis/reports/digest_terminal.py` -- masthead change.
  - `praxis/reports/digest_html.py` -- masthead change.
  - `praxis/reports/adapter.py` -- carries the new commitment + reflection summary.
  - `praxis/orchestrator.py` -- `run_weekly` adds commitment/reflection rollup; `_step_render` receives them.
  - `praxis/follow_up.py` -- `build_follow_up` learns about user_chosen + display_text.
  - `praxis/scoring/coach.py` -- the existing canned-drills bank powers the suggestion list for `praxis commit`.

---

## 7. Build order (within one release)

We ship all of it together, but the internal build order matters for testability:

1. **Schema migrations** (1 day). Add `user_chosen`, `display_text` to `follow_ups`; create `session_reflections`; migration runner. The richer behavioral signals from Section 11 piggy-back on the existing `signals_json` column on `session_scores` (the orchestrator already persists `BehavioralSignals` there via `extract_signals(...)`); no new signals table is required.
2. **Signal layer expansion** (3 days). Implement the ~15 new behavioral signals from Section 11 in `praxis/behavior/signals.py` and the augmentation/automation classifier in `praxis/behavior/aug_auto.py`. Each new signal ships with regex + unit tests against fixture sessions; the LLM classifier is a single cheap-tier call gated by API key.
3. **`praxis commit`** (2 days). New command, suggestion source plumbing, free-text path, DB writes. Suggestion sources now include the new signals (e.g., low specification-artifact rate -> "open your next session with a spec block").
4. **`praxis nudge`** (1 day). Reader + three output formats; throttling.
5. **`praxis install-coach` / `uninstall-coach`** (3 days). Tool detection, hook installers for Claude Code + Codex, file injector for Copilot, idempotent merging, marker-based uninstall.
6. **`praxis reflect`** (2 days). Interactive prompt, threshold gating, async detached prompt for the Stop hook path.
7. **`praxis review` (rename + masthead)** (2 days). Rename command + function; masthead rewrite in both renderers; commitment + reflection rollup; gap-detection prose.
8. **Report panels** (3 days). Add the new panels from Section 12 to both HTML and terminal digest renderers: behavioral patterns, augmentation/automation balance, cadence, repeat-task radar, verification calibration, specification adoption, context engineering depth, knowledge-gap distribution, tool/agent ladder.
9. **README + help rewrite** (1 day).
10. **End-to-end smoke test on all three tools** (1 day). Real Claude Code session and real Codex session: verify the cue appears at SessionStart, throttling works, uninstall cleans up. Real Copilot session: verify the instruction file is rewritten on `praxis commit` and Copilot Chat picks it up. Plus a fixture-based pass: the new signals fire on the curated session corpus.

Estimated total: ~19 working days, with parallelisable sections (signals + report panels are independent of the loop verbs). Single-developer real-time: ~4 weeks. The signals + report work is what grew the scope from the original 13-day estimate.

---

## 8. Out of scope (the over-complication traps)

Per the 2026-05-27 discussion, explicitly NOT building:

  - A `coach me now` chatbot mode. The product is the loop, not a conversation.
  - Gamification (streaks, badges, levels). Extrinsic motivators displace intrinsic ones.
  - Pre-prompt nudges (intercepting every prompt). Too intrusive; session-start is the right cadence.
  - AI-generated personalised commitments (LLM picks for the user). The user picks; the system suggests.
  - Standalone practice/drill mode. The session IS the drill.
  - Daily push notifications. Once-per-session ambient (the SessionStart hook + the shell-nudge fallback) is enough.
  - A web dashboard or mobile app. Local-first is a differentiator.
  - Direct CLAUDE.md / AGENTS.md writes (vs. hook-based dynamic injection). Hooks survive upstream file edits; direct writes don't.
  - A `praxis week` alias for `praxis review`. "The product is the loop."

---

## 9. Failure modes and nth-order effects

### Failure modes

| Mode | Mitigation |
|---|---|
| Hook fatigue (cue fires on micro-sessions, becomes noise) | Throttling via `.last_nudge`; default 30 min; configurable. |
| Self-report dishonesty (user says yes without doing it) | Feature, not bug -- the data-vs-report gap is the coaching moment. Don't validate. |
| Stop hook blocks AI tool exit | Hard 5s timeout in the hook; interactive prompt runs in detached process. |
| Copilot's static instruction file gets stale | `praxis commit` rewrites it on every commit-change. |
| Reflection survey decay (user starts skipping by week 4) | Rotate the question phrasing; occasionally skip ("nice rhythm -- no check-in this week"). |
| The headline-moment-as-default-commitment trap | Option 1 in `praxis commit` is "keep last week's" when one is active and unfinished. |
| Praxis as a config-spammer (settings.json pollution) | Always use the `_praxisManaged: true` sentinel key (strict JSON forbids comments); uninstall removes only entries with that key. Validate merged JSON before atomic write. |
| Goodhart on the metric (user spams "what's the source?" without engagement) | The judge already detects superficial verification; commitment outcome is the dim mean (qualitative judge score), not a raw count. |
| Onboarding cliff (new user, no commitment, hooks installed) | `praxis nudge` returns empty when no commitment; hooks are silent until first commit. |
| Cross-tool gap (Copilot users don't get hook-based dynamic updates) | Documented; static file is refreshed on every commit; works in practice. |

### Nth-order effects

Positive:
  - The cue becomes a habit anchor: "every session starts with my focus." Over weeks, internalises. Wendy Wood's habit research backs this for cue-anchored repeated behaviors.
  - The self-report vs. data gap teaches calibrated self-perception. Arguably the most important meta-skill for AI-assisted work, since you need it to know when to verify.
  - The product shifts from oracle to mirror. Praxis stops telling you the answer and starts asking you to notice. That is the actual definition of coaching.

Watch-out:
  - The cue could become wallpaper. Mitigation: rotate the framing weekly using a fresh quote from the latest headline moment.
  - Over-anchoring on one focus, neglecting other dimensions. Mitigation: the digest still scores all dims; focus is for change, not for evaluation.
  - If reflection becomes mandatory, completion rates collapse. Keep opt-in and skippable.

---

## 10. Open questions to revisit during execution

These were not blocking decisions but will surface during build:

  1. **Throttle default.** 30 minutes is a guess. Need to measure actual session frequency for power users; might need 60 min or per-project tuning.
  2. **Reflection threshold.** 2 turns + 60s is a guess. Real telemetry from the first week of dogfooding will tell us.
  3. ~~**Free-text commitment validation.**~~ Resolved 2026-05-28: hard reject above 280 chars (Section 4). Iterate after dogfooding if 280 turns out to be too tight.
  4. **Multi-project commitments.** If the user works in three projects this week, do they share one commitment or have per-project ones? v1 = global only. Per-project as v2.
  5. **Gap-detection prose constraint.** What's the prompt that prevents the closing paragraph from sounding accusatory? Needs to be drafted and tested.
  6. **Calendar week vs. ISO week vs. rolling 7-day for the commitment window.** Today the digest is ISO-week. Keep that; commitments align to ISO-week starts.
  7. **Augmentation/automation classifier cost.** One cheap-tier LLM call per session. At current pricing, probably ~$0.001 per session. Worth confirming on a real week's volume before shipping.
  8. **Signal calibration window.** New signals will need ~2 weeks of dogfooding to calibrate thresholds (e.g., what counts as a "spec artifact"? Does a one-line goal qualify?). v1 ships with conservative thresholds; tighten after.
  9. **Comparative anchors in the report.** Some panels (cadence, augmentation share) become much more useful with a benchmark ("you are in the 70th percentile of high-adopters"). We have no benchmark dataset today. v1 ships with absolute numbers + literature anchors only; community-aggregated percentiles are a v2 conversation about privacy.

---

## 11. Rubric and behavioral signal additions (research-driven)

This section was added 2026-05-27 after a thorough literature review of 2025-2026 primary sources. The work below is what makes the Praxis report independently useful as a behavioral-analytics product (Section 12), and what gives `praxis commit` a richer suggestion bank.

### Rubric: stays at 6 dimensions, descriptions refined

The current rubric (planning, context, iteration, tools, fit, verification) holds up against the new evidence. We deliberately do **not** add a 7th dimension: rubric stability is itself a feature (users build mental models around the dimensions, and weekly comparisons require stability). What we change:

  - **Planning** dimension description and exemplar refined to explicitly mention **specification artifacts** (markdown specs, acceptance criteria, "done when" statements). Evidence: Josh Woodward (Google I/O 2026 Dialogues, May 19, 2026, "we haven't written PRDs in months -- we write specifications in markdown that models can pick up and execute directly"); GitHub SpecKit (Sep 2025); Sean Grove "The New Code" (AI Engineer World's Fair, June 2025); Anthropic Claude Code best practices on plan mode; Andrej Karpathy retracting "vibe coding" as "passe" in mid-2025 in favour of "partial autonomy."
  - **Context** dimension description anchored to the strongest 2025 empirical finding on prompt quality: arXiv 2501.11709, "Towards Detecting Prompt Knowledge Gaps for Improved LLM-guided Issue Resolution." N=433 dev-ChatGPT conversations from GitHub issue threads. Effective conversations had knowledge gaps in **12.6% of prompts**; ineffective ones in **44.6%**. Three predictors: specificity, contextual richness, clarity. The paper's four gap categories (Missing Context, Missing Specifications, Multiple Context, Unclear Instructions) become per-signal categorisations in `signals.py`.
  - **Verification** dimension reframed around calibrated trust rather than blanket verification. Evidence: Stack Overflow Developer Survey 2025 (N ~= 49k; trust dropped to 29%, -11pp YoY; 66% frustration with "almost right but not quite"; 45% report debugging AI code takes longer); Sonar/Stack Overflow surveys (96% don't fully trust; 38% say reviewing AI code takes *more* effort than reviewing humans'); automation-bias literature (erroneous machine-recommended advice followed at 26% higher rate; reliability paradox -- consistently-correct automation reduces error detection from ~75% to ~30%); CodeRabbit (AI code generates ~1.7x more issues). The dimension's exemplar shifts from "ask for sources" to "verify proportional to blast radius."
  - **Tools** dimension description extends to the Anthropic skills/hooks/subagents ordering (Skills first, hooks for determinism, subagents for context isolation or parallelism) and OpenAI's harness-engineering guidance. Evidence: Anthropic Agent Skills launch (Oct 2025); OpenAI Codex harness engineering posts.
  - **Iteration** description anchors to the CodeChat finding: quality climbs over turns (Java docs +14.7%, Python imports +3.7% over 5 turns), and the highest-quality multi-turn pattern is **explicit error-identification before the fix request**.

No dimension weights change. No keys change. Existing scoring is backwards-compatible.

### Signals: ~15 new patterns added to `signals.py`

The current `signals.py` has 7 patterns (3 engagement, 3 atrophy, 1 independence + telegraphic). The new signals roughly double the surface area, each traceable to a primary source. Each is a regex or a single LLM-judge sub-call; no new infrastructure.

| Signal | Pattern | Primary source |
|---|---|---|
| **Specification artifact** | Session opens with a structured spec block (`## Goal`, `## Constraints`, `## Approach`, `Acceptance criteria`, `Done when:`) before implementation prompts begin. | Woodward (Google I/O 2026 Dialogues); SpecKit (github.com/github/spec-kit); Sean Grove "The New Code"; Anthropic plan-mode best practices |
| **Error-naming follow-up** | User explicitly identifies what went wrong (`the issue is`, `I see that`, `the error says`, `what is happening is`) before asking for a fix. | arXiv 2509.10402 CodeChat: most effective multi-turn pattern |
| **Knowledge-gap signature (4 sub-types)** | Missing Context / Missing Specifications / Multiple Context confusion / Unclear Instructions. Negative cues: short with no context; contradictory; multiple unrelated requests in one turn. | arXiv 2501.11709 (44.6% vs 12.6% gap rate) |
| **Iterative refinement** | Turn N+1 explicitly references and refines turn N's response (`actually, instead of X`, `close but`, `let's try a different approach`). | arXiv 2509.10402 CodeChat; Sarkar 2025 |
| **Plan-mode invocation** | User invokes plan mode (Claude Code), asks for a plan-before-code, or uses an explicit "explore -> plan -> code -> commit" structure. | Anthropic Claude Code best practices |
| **Scaffolding artifact** | User references or edits CLAUDE.md, AGENTS.md, copilot-instructions.md, Custom-GPT instructions, or skill/subagent files within the session. | OpenAI ChatGPT usage paper (Projects 19x YoY); Anthropic Agent Skills |
| **TDD / test-first marker** | "let me write a test first," "I'll write the failing test," "let's add a test for this case." | Anthropic best practices ("TDD is the single strongest pattern") |
| **Recipe pattern** | Prompt follows a Recipe shape: ingredients (inputs/files), steps, expected output. | arXiv 2506.01604 |
| **Context-and-instructions pattern** | Prompt provides a context block AND explicit instructions, in that order. | arXiv 2506.01604 |
| **Verification depth (graded)** | Beyond "did you check sources," distinguish: source-check / test-run / spot-check / blanket-accept. | Sonar; SO 2025; automation-bias lit |
| **Repeat-task signature** | Substantively similar requests recurring across sessions (semantic similarity, not exact match). Marks "should this be a skill?" | OpenAI power-user pattern; Anthropic Skills guidance |
| **Cadence: high-adopter pattern** | Longitudinal: most weekdays show >=1 substantive session over 3+ weeks. | arXiv 2509.19708 (high-adopters +61%; low-adopters -11%) |
| **Augmentation vs automation classification** | Per-session LLM classifier (single cheap-tier call): learning/feedback session vs delegate-and-accept session. | Anthropic Economic Index methodology, Jan + Mar 2026 reports |
| **Code-comprehension turn** | User asks AI to explain code they wrote, asks "what would break this," or asks for security/perf review of the diff. | 9to5Google on Google's code comprehension interview; counterweight to "don't read code" folklore |
| **Tool/agent ladder progression** | User moves up the ladder: single-turn Q&A -> multi-step request -> hooks/skills/subagents within a project. | Anthropic Skills/hooks ordering; OpenAI harness engineering |

### Implementation notes

  - `BehavioralSignals` dataclass gains a field per signal (counts and rates). Backwards-compatible: existing 7 fields stay, new fields default to 0.
  - Regex signals ship with unit tests against curated fixture sessions: 5 positive + 5 negative per signal. The fixtures double as the "what does great look like" exemplar bank for `praxis commit`.

### Augmentation vs automation classifier (concrete spec)

The classifier was flagged by the architect for being load-bearing but under-specified. Concrete v1:

  - **Module**: `praxis/behavior/aug_auto.py`
  - **Model tier**: Claude Haiku 4.5 when `ANTHROPIC_API_KEY` is set, else `gpt-5-mini`. (The same primary-provider rule the moment selector already uses.)
  - **When it runs**: in the existing pass-1 path, alongside the rubric judge; one extra cheap-tier call per session. Skipped silently when no API key is available; the report panel degrades to "classifier unavailable" instead of erroring.
  - **System prompt** (verbatim shape; exact wording subject to calibration in the dogfooding window from Section 10 Q8):

    ```
    You are classifying one AI coding session by mode. Given a transcript,
    decide whether the user was primarily augmenting their own work or
    automating it.

    Augmentation: user is learning, asking why, comparing options, asking
    for explanations, verifying claims, taking notes, refining their own
    thinking. Quality signals: questions about how/why, comprehension
    checks, explicit "I want to understand X."

    Automation: user is delegating work and accepting outputs with minimal
    engagement. Patterns: short imperative requests ("fix this," "write
    me X"), accepting first drafts, no verification or explanation
    questions.

    Mixed: substantial elements of both within the session.

    Return JSON only.
    ```

  - **Output schema** (strict):

    ```json
    {
      "classification": "augmentation" | "automation" | "mixed",
      "confidence": 0.0-1.0,
      "rationale": "<one sentence>"
    }
    ```

  - **Persistence**: stored on the session_scores row in a new `aug_auto_classification` TEXT column (one of the three literals) plus `aug_auto_confidence` REAL. Aggregation across the week feeds the "Augmentation vs automation balance" panel (Section 12).
  - **Cost estimate**: ~$0.001 per session at current Haiku 4.5 pricing for typical session sizes. Open question 7 in Section 10 confirms this against real volume.

### Knowledge-gap classifier (concrete spec)

  - **Module**: lives inside `praxis/behavior/signals.py` (regex-based for v1; no LLM call needed for the four-category cut).
  - **Output**: a dict on `BehavioralSignals`:

    ```python
    knowledge_gaps: dict[str, int] = {
        "missing_context": int,        # prompts asking about code without including it
        "missing_specs": int,          # prompts without acceptance criteria / done-when
        "multiple_context": int,       # prompts mixing unrelated tasks in one turn
        "unclear_instructions": int,   # ambiguous "do something with X" prompts
    }
    ```

  - Counted **per user-turn within the session**, summed across the session, then aggregated per week for the report panel. Each value is a count, not a probability.

### What gets pruned

The Shen & Tamkin (2026) framing is preserved (it's the strongest anchor for engagement vs atrophy), but its rough cut (engagement vs delegation vs independence) is now one layer among several. The newer literature supports a richer taxonomy. The existing `is_pure_delegator` boolean stays for backwards compatibility; new code prefers the augmentation/automation classifier.

---

## 12. The report as a standalone product

The user explicitly asked that the report be useful independent of the coaching loop. This section captures that positioning.

### Framing

"AI Usage Telemetry" -- a personal data product about how you actually work with AI. Think Strava for AI usage, not "scorecard." Standalone usefulness comes from showing patterns the user can't see themselves, with literature anchors so the patterns mean something.

The coaching loop layered on top is the active product. The report is the evidence + reflection surface that also stands on its own, and is what the user opens via `praxis review`.

### New panels (additions to the existing rubric + trajectory + baseline + per-model panels)

| Panel | What it shows | Literature anchor |
|---|---|---|
| **Behavioral patterns** | Counts of the ~15 signals from Section 11, with up to two concrete excerpts each ("you used the error-naming pattern 12 times this week"). | Each signal's primary source. |
| **Augmentation vs automation balance** | Share of sessions classified as learning/feedback vs delegate-and-accept. Comparison anchor: Anthropic Economic Index reports ~52% augmentation industry-average on Claude.ai. | Anthropic Economic Index Jan + Mar 2026. |
| **Cadence panel** | Sustained-use vs binge pattern. Most-weekdays-active streak. Position on the high-adopter / low-adopter spectrum from arXiv 2509.19708 (sustained intentional use beats sporadic). | arXiv 2509.19708. |
| **Repeat-task radar** | Tasks done 3+ times this period -- candidates for skills/sub-agents. Surfaces concrete cost savings ("you typed variants of 'add a unit test for X' 12 times -- a skill could reclaim ~30 min/week"). | OpenAI ChatGPT usage paper (Projects 19x YoY); Anthropic Skills. |
| **Verification calibration** | Detected verify-effort breakdown (source-check / test-run / spot-check / blanket-accept) vs. session count. | Sonar; SO 2025; automation-bias lit. |
| **Specification adoption** | Share of sessions that opened with a spec block vs. cold prompting. Trend over time. | Woodward (Google I/O 2026); SpecKit; Sean Grove. |
| **Context engineering depth** | Are you using CLAUDE.md / AGENTS.md / Projects / Custom GPTs / Skills? DORA 2025 lists context engineering as a top-7 AI capability. | DORA 2025; Anthropic Agent Skills; OpenAI Projects. |
| **Knowledge-gap distribution** | Which of the four arXiv-2501.11709 gap categories (Missing Context / Missing Specifications / Multiple Context / Unclear Instructions) appeared most in your prompts. Directly coachable. | arXiv 2501.11709. |
| **Tool / agent ladder** | Where you are on the single-turn -> agentic spectrum. The ladder rungs are Anthropic-consensus: prompt-only -> tools-on -> skills -> hooks -> subagents. | Anthropic Skills/hooks/subagents guidance; OpenAI harness engineering. |
| **Cost-effectiveness ratio (refined)** | Already exists in cost ledger; promote: "you spent $X on Opus this week for tasks Haiku could have done = $Y overspend." | METR/MIT/OpenAI on model-task fit; "Glass Slipper" retention from OpenRouter. |

### Standalone-use scenarios

The report should be useful in three modes without the coaching loop active:

  1. **"Where am I now?"** -- a snapshot for a user who has not yet committed to any focus. Reads as descriptive analytics. The masthead degrades gracefully when no commitment exists: skips the commitment block and leads with the strongest pattern from the week.
  2. **"How am I trending?"** -- the 30/60/90-day view. Trajectory + cadence + augmentation-share over time. This is the "Strava for AI usage" experience.
  3. **"What should I try?"** -- the report surfaces concrete coachable items even outside the loop. A reader who picks up the report once is given 2-3 specific patterns to try, each anchored to a citation, each detectable so progress can be measured.

### Where this lives in the codebase

  - `praxis/behavior/signals.py` -- ~15 new signal extractors (regex + LLM where needed).
  - `praxis/behavior/aug_auto.py` -- new module for the augmentation/automation classifier (LLM call).
  - `praxis/behavior/cadence.py` -- new module for the longitudinal cadence panel.
  - `praxis/behavior/repeat_task.py` -- new module for the repeat-task detector. **v1 implementation: text-overlap on the existing `praxis/scoring/clustering.py` output** (extends the existing `cluster_sessions` step rather than adding an embedding dependency). The detector compares user-turn first-sentences across the week's clustered tasks and flags any task whose first sentence has >= 70% token overlap with 2+ other tasks. Semantic similarity over user-turn embeddings is deferred to v2 (would require a new dependency on either Voyage AI or a local model; defers cleanly to keep the "local-first" Section 8 constraint).
  - `praxis/reports/digest_html.py` and `digest_terminal.py` -- new panel renderers.
  - `praxis/reports/adapter.py` -- carries the new panel inputs into the renderers.

### What this is NOT

  - Not a public dataset / community leaderboard. Comparative anchors stay literature-based or local-history-based until the privacy story is sorted (see Section 10, open question 9).
  - Not a real-time stream. The report still refreshes once per `praxis review` invocation.
  - Not vendor-comparative. We do not rank Claude vs GPT vs Gemini in the report; we surface user-by-user model-task fit only.

---

## 13. Definition of done

  - All four loop verbs ship and work end-to-end.
  - `praxis install-coach` succeeds on a Mac with Claude Code, Codex CLI, and VS Code Copilot installed; uninstall returns the system to prior state.
  - A user can complete a full Commit -> Cue -> Reflect -> Review cycle in one week.
  - The README does not contain the word "scorecard." `praxis week` is gone.
  - All ~15 new behavioral signals (Section 11) fire correctly against curated fixture sessions; new report panels (Section 12) render in HTML and terminal.
  - Existing tests still pass; new tests cover commit, nudge, reflect, install-coach, the masthead, every new signal, and the augmentation/automation classifier.
  - Schema migration runs cleanly on a real prior-version `~/.praxis/profile.db`.
  - Citation accuracy: every claim in the README and report panel labels has a primary-source URL on file (Section 14), and the citation-validation corrections from 2026-05-27 (Section 1, notes column of the five-elements table) are reflected.

---

## 14. Primary sources (cited evidence)

This is the authoritative list of primary sources Praxis grounds its claims in. Every README or report-panel reference should map back to one of these. Updates require evidence; no claim ships without a source.

### Behavior-change frameworks (validated 2026-05-27)

  - Fogg, B.J. (2009), "A Behavior Model for Persuasive Design," *Proceedings of the 4th International Conference on Persuasive Technology*, ACM Article 40. https://www.behaviormodel.org/
  - Gollwitzer, P.M. & Sheeran, P. (2006), "Implementation Intentions and Goal Achievement: A Meta-Analysis of Effects and Processes," *Advances in Experimental Social Psychology* 38, 69-119. d = 0.65 across 94 independent tests on goal attainment. https://cancercontrol.cancer.gov/sites/default/files/2020-06/goal_intent_attain.pdf
  - Bieleke et al. (2024), "The When and How of Planning: Meta-Analysis of the Scope and Components of Implementation Intentions in 642 Tests." (Most recent meta-synthesis; supersedes Gollwitzer & Sheeran 2006 for current effect estimates.) URL TBD -- locate the journal record before quoting effect sizes from this paper in the README.
  - Nahum-Shani, I. et al. (2018), "Just-in-Time Adaptive Interventions (JITAIs) in Mobile Health: Key Components and Design Principles for Ongoing Health Behavior Support," *Annals of Behavioral Medicine* 52(6), 446-462. https://academic.oup.com/abm/article/52/6/446/4733473
  - JITAI meta-analysis (2024), N=2,563 across 23 studies, g ~= 0.15. https://pmc.ncbi.nlm.nih.gov/articles/PMC12481328/
  - Michie, S., van Stralen, M.M., West, R. (2011), "The Behaviour Change Wheel: A New Method for Characterising and Designing Behaviour Change Interventions," *Implementation Science* 6:42. https://link.springer.com/article/10.1186/1748-5908-6-42
  - Locke, E.A. & Latham, G.P. (2002), "Building a Practically Useful Theory of Goal Setting and Task Motivation: A 35-Year Odyssey," *American Psychologist* 57(9), 705-717. https://www-2.rotman.utoronto.ca/facbios/file/09%20-%20Locke%20&%20Latham%202002%20AP.pdf
  - Ryan, R.M. & Deci, E.L. (2000), "Self-Determination Theory and the Facilitation of Intrinsic Motivation, Social Development, and Well-Being," *American Psychologist* 55(1), 68-78. https://www.apa.org/research-practice/conduct-research/self-determination-theory
  - Wood, W. & Rünger, D. (2016), "Psychology of Habit," *Annual Review of Psychology* 67, 289-314. https://www.annualreviews.org/content/journals/10.1146/annurev-psych-122414-033417
  - Hattie, J. & Timperley, H. (2007), "The Power of Feedback," *Review of Educational Research* 77(1), 81-112; revised by Wisniewski, Zierer & Hattie (2020), *Frontiers in Psychology* 10:3087, d = 0.48 across 435 studies. https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2019.03087/full
  - Ericsson, K.A., Krampe, R.T., Tesch-Romer, C. (1993), "The Role of Deliberate Practice in the Acquisition of Expert Performance," *Psychological Review* 100(3), 363-406. https://gwern.net/doc/psychology/writing/1993-ericsson.pdf
  - Macnamara, B.N., Hambrick, D.Z., Oswald, F.L. (2014), *Psychological Science* -- deliberate practice explains 4-26% of variance depending on domain. https://journals.sagepub.com/doi/abs/10.1177/0956797614535810

### AI-assisted development research (primary sources, 2025-2026)

  - Becker et al. (2025), "Measuring the Impact of Early-2025 AI on Experienced Open-Source Developer Productivity," METR. arXiv 2507.09089. https://arxiv.org/abs/2507.09089 -- N=16 OSS devs, within-subjects RCT, **-19% with AI** on mature repos.
  - MIT Management Science (2025), 3 RCTs, N=4,867 devs, **+26% task completion**, juniors gain more than seniors. https://pubsonline.informs.org/doi/10.1287/mnsc.2025.00535
  - Anthropic Economic Index, "Economic primitives," Jan 2026 report. https://www.anthropic.com/research/anthropic-economic-index-january-2026-report
  - Anthropic Economic Index, "Learning curves," Mar 2026 report. https://www.anthropic.com/research/economic-index-march-2026-report -- N ~= 1M Claude.ai conversations, augmentation 52% / automation 45%.
  - Shen & Tamkin (2026), "How AI Impacts Skill Formation," arXiv 2601.20245 -- N=52 dev RCT learning Trio; AI group scored 17pp lower on comprehension quiz.
  - "Towards Detecting Prompt Knowledge Gaps for Improved LLM-guided Issue Resolution" (Jan 2025), arXiv 2501.11709 -- N=433 conversations. **44.6% vs 12.6% gap rate** for ineffective vs effective conversations. Specificity / Contextual Richness / Clarity. https://arxiv.org/abs/2501.11709
  - "Exploring Prompt Patterns in AI-Assisted Code Generation" (Jun 2025), arXiv 2506.01604 -- Recipe + Context-and-Instructions patterns rank highest. https://arxiv.org/abs/2506.01604
  - "Developer-LLM Conversations: An Analysis of Interactions and Generated Code Quality" (CodeChat) (Sep 2025), arXiv 2509.10402 -- N=82,845 conversations; multi-turn quality climbs over 5 turns. https://arxiv.org/abs/2509.10402
  - "Intuition to Evidence: Measuring AI's True Impact on Developer Productivity" (Sep 2025), arXiv 2509.19708 -- N=300, 12 months. **High-adopters +61%; low-adopters -11%.** https://arxiv.org/abs/2509.19708
  - "The Fast and Spurious" (2026, FSE'26 companion), arXiv 2510.24265 -- N=415; GenAI gains offset by review/verification load. https://arxiv.org/abs/2510.24265
  - GitClear, "AI Copilot Code Quality 2025." 211M LOC analysis. https://www.gitclear.com/ai_assistant_code_quality_2025_research
  - DORA 2025, "State of AI-Assisted Software Development." N ~= 5,000 practitioners. https://cloud.google.com/resources/content/2025-dora-ai-assisted-software-development-report
  - Stack Overflow Developer Survey 2025. N ~= 49k; 84% AI use, 29% trust (-11pp YoY). https://survey.stackoverflow.co/2025/ai/
  - JetBrains State of Developer Ecosystem 2025. N ~= 23k; 85% AI use, **44% workflow-integrated**. https://blog.jetbrains.com/research/2025/10/state-of-developer-ecosystem-2025/
  - OpenAI, "ChatGPT usage and adoption patterns at work" (2025). Power-users 17x more coding messages; Projects 19x YoY. https://cdn.openai.com/pdf/3c7f7e1b-36c4-446b-916c-11183e4266b7/chatgpt-usage-and-adoption-patterns-at-work.pdf
  - METR, "Measuring AI Ability to Complete Long Tasks" (2025). 100% on <4 min, <10% on >4h. https://metr.org/blog/2025-03-19-measuring-ai-ability-to-complete-long-tasks/
  - SWE-Bench Pro (Sep 2025), arXiv 2509.16941 -- long-horizon failures concentrate on instruction-following. https://arxiv.org/abs/2509.16941

### Vendor / industry guidance (use as design heuristics, not evidence)

  - Sundar Pichai keynote, Google I/O 2026 (May 19, 2026). 75% new Google code is AI-generated and human-approved. 8.5M monthly Gemini developers. https://blog.google/innovation-and-ai/sundar-pichai-io-2026/
  - Josh Woodward (VP, Google Labs), I/O 2026 Dialogues panel (filmed May 19, recap published ~May 22, 2026). "We haven't written PRDs in months -- we write specifications in markdown that models can pick up and execute directly." Cites Stitch's open-source DESIGN.md format. https://blog.google/innovation-and-ai/technology/ai/io-2026-dialogues-recap/
  - Anthropic Agent Skills launch (Oct 2025). https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
  - Anthropic Claude Code best practices. https://code.claude.com/docs/en/best-practices
  - GitHub SpecKit (open-sourced Sep 2, 2025). https://github.com/github/spec-kit + https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/
  - Sean Grove (OpenAI), "The New Code," AI Engineer World's Fair, June 2025. https://www.classcentral.com/course/youtube-the-new-code-sean-grove-openai-467279 (talk listing; locate the full transcript or video before citing in the README).
  - Karpathy on "vibe coding" -- coined Feb 2025 (https://x.com/karpathy/status/1886192184808149383), retracted by mid-2025 ("vibe coding is passe," https://thenewstack.io/vibe-coding-is-passe/).
  - OpenRouter State of AI 2025 (100T tokens analyzed). Existing Praxis citation.
  - Google Cloud, "Five Best Practices for Using AI Coding Assistants." https://cloud.google.com/blog/topics/developers-practitioners/five-best-practices-for-using-ai-coding-assistants

### Claims we explicitly DO NOT repeat

  - "Google's best AI-native developers don't read code" -- not in any verifiable Google primary source. The closest authentic claim is Woodward's specs-not-PRDs statement; the "don't read code" framing is third-party paraphrase, and Pichai's keynote contradicts it (75% AI-generated, human-approved). Treat as vendor-adjacent folklore.
  - "AI tools make developers 55% faster" -- GitHub Octoverse / Copilot self-reports without published methodology. Cite Octoverse 2025 for adoption scale only, not effectiveness.
  - "10,000 hours of deliberate practice creates expertise" -- Macnamara 2014 caps variance-explained at 4-26%. Cite Ericsson for the framework, not the popularization.
  - "Willpower is a depletable resource (ego depletion)" -- Hagger et al. 2016 multilab replication, N=2,141 across 23 labs, found d=0.04. Do not invoke.

---

## 15. Tracking

This plan persists the decisions and research taken on 2026-05-27. When executing:

  - Update this file in place if a decision changes.
  - Reference section numbers in commits ("PLAN.md S5: Claude Code hook installer"; "PLAN.md S11: spec-artifact signal").
  - Open questions in S10 get resolved inline as they're decided.
  - New primary sources added during execution go into S14 with a URL and a one-line role for them in the rubric/signals.
