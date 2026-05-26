# Implementation review — Praxis v0.1.0

A section-by-section audit of the implementation against the original spec doc (kept on disk as a historical artifact), calling out (a) places where the implementation diverges from the spec, and (b) places where the spec itself is hand-wavy or underspecified.

## Verdict at a glance

| Area | Faithful? | Notes |
|---|---|---|
| Section 3 file tree | Yes (with brand variation) | All 28 files exist. Package named `praxis`, CLI named `praxis`, storage at `~/.praxis/`. Spec uses `ai_scorecard` / `~/.ai-scorecard/`. Intentional brand override, consistent across README + pyproject. |
| Section 4 data model | Yes | `Session.stable_id`, `Provider`/`Role` enums match spec exactly. |
| Section 5 scanners | Yes + safety env vars | All scanners parse defensively, filter zero-turn sessions, honor sandboxed root dirs via `PRAXIS_*` env vars (not in spec — added for safe testing). |
| Section 6 rubric | Yes | Seven dimensions, weights sum to 1.0, evidence + exemplar populated. |
| Section 7 heuristics | Yes | Marker patterns, formulas, ceiling at 7.0, fit fixed at 5.0. |
| Section 8 judge | Yes | System prompt now matches spec 8.5 text including voice + edge cases section. |
| Section 9 aggregate | Mostly | `_blend`, `_weighted_overall`, `ProfileSnapshot.from_scores` all match. Minor: spec says to **dedupe** standouts/failures across the last 5 judged sessions; the impl appends without dedupe. Fixed in this review pass. |
| Section 10 behavior | Yes | Signals + trajectory match. LLM prompt now uses spec 10.3.5 wording. |
| Section 11 model advisor | Yes (with extensions) | All 8 built-in cards present. Now extended with `pricing` field (not in spec — see new work below). |
| Section 12 storage | Yes | Three tables, exact schema, ProfileStore API. Honors `PRAXIS_HOME` for sandboxing. |
| Section 13 coach | Yes | All 7 dimensions have fallback drills. LLM prompt updated to spec 13.4 text. |
| Section 14 voice + design | Yes | HTML uses cream/terracotta/Libre Baskerville, no bold, generous whitespace. |
| Section 15 orchestrator | Yes | RunSummary, `_gather_sessions`, `run()` with all flags; tz-aware datetimes throughout. |
| Section 16 reports | Yes (polished beyond spec) | Terminal rewritten with grade label, trajectory slopes, color-coded fit glyphs. HTML rendered + verified visually. No double-encoded entities. |
| Section 17 CLI | Yes | All 6 subcommands respond to `--help`; `scan --no-judge` completes end-to-end. |
| Section 18 packaging | Yes | `pyproject.toml` works with uv; `pipx install .` would work too. |
| Section 19 testing | Yes | 36 tests across 6 files; smoke test produces expected output. |

## Real divergences (now fixed)

### 1. `ProfileSnapshot.from_scores` dedupe

**Spec 9.5:** "take the last 5 sessions that have a non-None judge_result, take their first standout/failure each, **dedupe**, cap at 5"

**Was:** appended without dedupe, so the same standout from two sessions would appear twice.

**Now:** deduped while preserving order, then capped at 5.

### 2. Synthetic data generator was destructive

`tests/make_synthetic_data.py` called `shutil.rmtree(~/.claude/projects)` on import, which would delete the user's real Claude Code history. Now: writes to a sandbox at `./synthetic_chat_home/` by default, with an opt-in `--in-real-home` flag.

### 3. CLI naming

The implementation uses `praxis` (package, CLI, and `~/.praxis/` storage) consistently. The spec text uses `ai_scorecard` / `ai-scorecard` / `~/.ai-scorecard/`. Renamed product-side to `praxis` per the final naming decision.

## Places where the spec hand-waved (and how this implementation handled it)

### A. Datetime tz handling

The spec doesn't specify whether `Session.started_at` is tz-aware or naive. Mixing the two raises `TypeError`. The implementation now produces tz-aware UTC for every code path — both parsed JSON timestamps and the `path.stat().st_mtime` fallback. `datetime.utcnow()` (deprecated in Python 3.12+) was replaced with `datetime.now(timezone.utc)`.

### B. Where do BUILTIN_CARDS actually live?

Spec 11.1.2 says cards are "versioned JSON" and users can drop JSON files in `~/.praxis/model_cards/`. The spec doesn't say whether built-in cards are Python objects or shipped JSON files. The original implementation put them in Python.

**This review moves them to JSON** under `praxis/data/builtin_cards/*.json`, loaded once and cached. Benefits:
- Matches the spec's "versioned JSON" framing
- Lets users replace or update cards without touching Python
- Makes "where did this come from" answerable (per-file `sources` URLs)

### C. Cost information

**Not in the spec at all.** The user explicitly asked for this in the second goal. The implementation now extends `ModelCard` with:
- `pricing.input_per_million_usd`
- `pricing.output_per_million_usd`
- `pricing.last_verified` (ISO date)
- `pricing.source_url`
- `pricing.notes`

Prices are populated with publicly known values *as of January 2026*. Each card includes a `pricing.source_url` pointing to the vendor's official pricing page so the user can verify before relying on the dashboard for budgeting. Models with no published per-token pricing (Copilot, which is subscription-based) have `pricing: null`.

### D. Sandboxed scanning

The spec implies scanners always read from the user's real `~/.claude` etc. There's no mention of how to test the pipeline without touching real data. The implementation adds env-var overrides:
- `PRAXIS_CLAUDE_ROOT` → override Claude root
- `PRAXIS_CODEX_HOME` → override Codex parent dir (alongside `CODEX_HOME` from the official Codex CLI)
- `PRAXIS_HOME` → override the scorecard storage home

This is essential for safe development and unit testing.

### E. LLM judge defensive parsing

Spec 8.7 says "Find first `{` and last `}`; parse the substring as JSON." This works for clean responses but can produce invalid JSON if the model emits a leading example object. The implementation does what the spec says; the LLM is constrained to JSON output via the system prompt, and (on OpenAI) via `response_format={"type": "json_object"}`. If a model produces malformed JSON anyway, `score_session` catches the exception and tries the other provider.

### F. Trajectory window vs all-time

The orchestrator computes the trajectory and per-model profiles on *this run's scanned sessions* (inside the `since_days` window), not on the full DB history. This matches spec section 15.4 step 11–13, but is worth flagging: if you scan with `--since-days 7` you'll get a 7-day trajectory, not an all-time one. The spec doesn't surface this clearly.

### G. Per-model "common tasks" classification

Spec 11.2.3 lists 9 task categories with keyword groups. The implementation uses `if/elif` matching — first match wins. Order matters: a prompt that contains both "bug" and "refactor" gets classified as `debugging`. The spec doesn't address ordering.

### H. Heuristic dimension scoring "fit" baseline

Spec 7.5 fixes `fit = 5.0` at the heuristic level because model–task fit can't be judged from a single session. This is correct, but the consequence is that for heuristic-only runs (no judge), `fit` contributes 12% × 5.0 = 0.6 points to the overall score regardless of input. A user scanning with `--no-judge` will always see `Model-task fit: 5.0`. Worth noting in the README.

## New work added in this review pass

1. **Pricing fields on ModelCard** — input/output per million USD, last_verified, source URL, notes.
2. **JSON cards shipped with the package** under `praxis/data/builtin_cards/`, loaded once and cached at module level.
3. **Cost rendering** — terminal and HTML show pricing per model; advisor surfaces "Estimated cost" based on the user's actual prompt-character volume.
4. **Cost-aware over-using guidance** — when a frontier model is being over-used, the advice now includes the dollar delta vs. a cheaper tier.
5. **Download / verify docs** — each card's source URL is rendered in `models --show`. README documents where the cards come from and how to refresh them.
6. **Dedupe fix in ProfileSnapshot.from_scores**.

## Things explicitly out of scope (per user direction)

- Automated model-card download / refresh CLI command (manual update is acceptable for v0.1.0)
- Pulling pricing from a live API
- Tracking historical price changes over time
- Anything in spec section 21 (no OAuth, no web UI, no real-time watcher)
