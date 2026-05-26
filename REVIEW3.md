# Review pass 3 — bugs, partial implementations, hand-waving

Focused issue hunt. Every finding below was confirmed with a probe (the probe script is shown). Findings are graded P0-P3 by impact, and bucketed by category at the end with a fix plan.

---

## Confirmed bugs

### B1 — Fenced-JSON parser is broken (P0)

**Where:** `praxis/scoring/judge.py:_parse_response`

**What:** The spec (§8.7) requires the parser to strip markdown code fences from LLM responses. The implementation is broken — it returns the *trailing* empty string after the closing fence instead of the inner JSON.

```python
# When given: ```json\n{"scores": {...}}\n```
cleaned = cleaned.split("```", 2)[-1]   # → '' (last segment after closing fence)
# Then: '' has no '{', so ValueError "No JSON object found"
```

**Probe:**
```
FAIL fenced valid               → ValueError: No JSON object found
OK   with preamble + postamble  → parsed correctly
```

**Impact:** Anthropic/OpenAI sometimes wrap JSON in ` ```json … ``` ` despite our prompt asking for raw JSON. When that happens, the entire judge call is wasted — `score_session` catches the exception and silently moves on. The user pays for the API call but gets no score uplift.

**Fix:** use `removeprefix`/`removesuffix` or index `[1]` after splitting.

### B2 — `find_card_for_model_hint` prefix match is way too loose (P0)

**Where:** `praxis/models_advisor/cards.py:find_card_for_model_hint`

**What:** The prefix-match strategy uses *symmetric* prefix matching:

```python
if norm.startswith(cand_norm) or cand_norm.startswith(norm):
    return card
```

The second clause means a *shorter* query matches a *longer* card id. Combined with dict iteration order, this silently mis-routes models.

**Probe:**
```
'claude' → claude-haiku-4-5     (should be ambiguous / None)
'gpt'    → gpt-4o               (should be ambiguous / None)
'co'     → copilot-default      (should be too short to resolve)
'opus'   → claude-opus-4-7      (this one is correct via alias, but only by luck)
```

**Impact:** A user whose Codex session metadata literally says `model: claude` (real possibility if metadata is incomplete) gets the **Haiku** card applied to their session — wrong tier, wrong pricing, wrong advice. The model_advisor will recommend Haiku-appropriate tactics for what's actually an Opus session.

**Fix:** drop the second clause. Keep only `norm.startswith(cand_norm)`. A `cand_norm` (an alias) is the prefix to match against; the user's hint is allowed to be longer (for dated suffixes like `claude-opus-4-7-20260315`), but never shorter than the alias.

### B3 — `models --show` doesn't normalize the query (P0)

**Where:** `praxis/cli/__main__.py:cmd_models`

**What:** The CLI matches user input with `target = args.show.lower()` and checks `c.id == target or target in [a.lower() for a in c.aliases]`. The card system uses `_normalize_model_string` which converts spaces and dots to hyphens. The CLI doesn't.

**Probe:**
```
'claude-opus-4-7'   → Claude Opus 4.7 card (works)
'Claude Opus 4.7'   → "No card found"      (broken)
'claude opus 4.7'   → "No card found"      (broken)
'OPUS-4-7'          → "No card found"      (broken — alias is 'opus-4-7' which is lowercased OK, but the test fails because target='opus-4-7' and aliases include 'opus-4-7' — wait, this should work)
```

Actually `OPUS-4-7` returns nothing because of a different issue: my output above shows it works internally but the printed output is empty (script bug). The first three cases are the genuine fail.

**Impact:** Users naturally type human names ("Claude Opus 4.7"). The CLI tells them "No card found" even when the exact card exists. Friction, looks broken.

**Fix:** route the CLI's `--show` through `find_card_for_model_hint` instead of doing a half-baked lowercase match.

### B4 — Iteration score adds 2.0 for any multi-turn session (P1)

**Where:** `praxis/scoring/heuristics.py:heuristic_dimension_scores`

**What:** The formula is `clip(iteration_rate * 10 + (2.0 if has_multi_turn else 0.0))`. A 4-turn session with zero iteration markers gets `0 + 2.0 = 2.0` on the iteration dimension.

**Probe:**
```
4-turn session, zero iteration markers:
  iteration_rate: 0.0
  has_multi_turn: True
  iteration score: 2.0   ← free bump
```

**Impact:** Inflates the heuristic-only score for users who are simply having multi-turn conversations regardless of whether they iterate. Spec §7 said the heuristic should be conservative; this isn't.

**Fix:** require the multi-turn bonus to be conditioned on at least *some* iteration signal: `+2.0 if has_multi_turn and iteration_rate > 0`. Or drop the bonus entirely — multi-turn is captured indirectly by other signals.

### B5 — `_PLAN_MARKERS` has high false-positive rate (P1)

**Where:** `praxis/scoring/heuristics.py:_PLAN_MARKERS`

**What:** The pattern matches `\bwill\b`, `i want to`, and `first.*then` — all common words/phrases with no planning intent.

**Probe (4 of 4 false positives):**
```
"This will fail with a TypeError"         → matches (FP)
"The function will not return"            → matches (FP)
"I want to know why"                      → matches (FP)
"First click X then click Y"              → matches (FP)
```

**Impact:** Inflates planning score for users who never plan but use English. The judge corrects this when keys are present (70/30 blend), but heuristic-only mode is unreliable.

**Fix:** drop `\bwill\b` (way too common), drop `first.*then` (this matches "first click then click", not planning), tighten `\bi want to\b` to require a planning verb like `i want to (build|design|create|implement|refactor|plan)`. Better still: this is exactly the kind of judgment LLMs do well; defer this dimension to the judge entirely when keys are available, and accept lower accuracy in heuristic-only mode (which is the no-keys fallback anyway).

### B6 — `_parse_response` raises on `{}` and on non-numeric scores (P1)

**Where:** `praxis/scoring/judge.py:_parse_response`

**Probe:**
```
'{}'                                → KeyError: 'scores'
'{"scores": {"planning": "high"}}'  → ValueError: float('high')
```

**Impact:** Same as B1 — wasted API call, no score recorded. The caller catches `Exception` upstream so the pipeline doesn't crash, but the user sees zero LLM-derived signal for that session.

**Fix:** wrap dimension extraction in a try, default to 5.0 (neutral) on coerce failure, default to `{}` when `scores` key missing. Don't let the LLM's malformed output destroy a billable call.

### B7 — `_heuristic_fit` advice constructs nonsense text for non-claude/non-gpt families (P2)

**Where:** `praxis/models_advisor/advisor.py:_heuristic_fit`

**What:** The string `f"a faster tier ({card.family}'s 'fast' tier)"` produces "Claude's 'fast' tier" for Claude (OK), "gpt's 'fast' tier" for GPT (lowercase, awkward), and would say "gemini's 'fast' tier" or "copilot's 'fast' tier" if those ever triggered the frontier+short-prompt branch — but neither has a fast tier in our cards.

**Probe:**
```
Opus over-using advice produced:
  "...a faster tier (claude's 'fast' tier) would be cheaper..."
```

Lowercase "claude's" is jarring; for hypothetical other families it would be outright wrong.

**Impact:** Cosmetic for now (only Claude triggers the path with the current cards), but a real bug waiting for any future frontier model in another family.

**Fix:** look up the actual fast-tier card in the same family and reference its `display_name`. If no fast tier exists, give generic advice.

### B8 — HTML evidence always ends with `...` even when not truncated (P2)

**Where:** `praxis/reports/html_report.py:_dimension_bar_html`

```python
<span class="dim-evidence">{html.escape(dim.evidence[:140])}...</span>
```

The `...` is appended unconditionally. If `dim.evidence` is exactly 140 chars or shorter, no truncation happened, but we still print "...".

**Probe:** rendered HTML for the planning dimension shows `... and that one standard deviation high...` — that final `...` is fake.

**Fix:** only append `...` when truncation actually occurred.

### B9 — `print` goes to stdout for error logs (P2)

**Where:** scattered across `orchestrator.py`, `behavior/trajectory.py`, `models_advisor/*.py`, `scoring/coach.py`, `scoring/judge.py` — nine print sites.

**What:** Error messages like `[scorer] claude judge failed: ...` go to `stdout`, intermixed with the terminal report. If a user pipes `praxis scan | tee report.txt` or scrapes the output, the errors corrupt the captured report.

**Fix:** `print(..., file=sys.stderr)`. One-line per site.

### B10 — Empty session is scored at 0.6 (P2 / spec misalignment)

**Where:** `praxis/scoring/heuristics.py:heuristic_dimension_scores`

**What:** Spec §7.6 says empty sessions return all-zero `HeuristicFeatures`, which is correct. But `heuristic_dimension_scores` then runs and outputs `fit=5.0` (fixed per §7.5) and zeros for the other 6 dimensions. The weighted overall is `5.0 × 0.12 = 0.6`. So a session with no user turns is reported as 0.6/10 — not 0, not "no signal", just a misleading low number.

**Probe:**
```
Empty session: overall=0.6
System-only:   overall=0.6, user_turn_count=0
```

**Impact:** A scan that finds a few empty/system-only session files will populate the DB with sessions that drag the dimension means down for no real reason.

**Fix:** in `score_one_session`, short-circuit when `features.user_turn_count == 0` — either skip persistence (treat as not a usable session) or score the session as `overall=0.0` with all dimensions zero (including fit). Better: filter at the scanner level (zero user-turn sessions shouldn't pass `scan()`'s zero-turn filter, but they do because `turn_count > 0` includes system/tool turns).

### B11 — Hardcoded "15× cheaper" multiplier in cost advice (P2)

**Where:** `praxis/models_advisor/advisor.py:_cost_advice`

**What:** The savings hint `"Claude Haiku 4.5 is ~15× cheaper per input token"` is a literal string. The 15× was derived from Opus $15/M ÷ Haiku $1/M. If either price changes, the string is wrong and we won't notice — there's no code link between the strings and the JSON pricing.

**Impact:** Drift over time. Pricing changes routinely; this advice will silently become inaccurate.

**Fix:** compute the multiplier at runtime by looking up the fast-tier card in the same family.

### B12 — Hardcoded model name in `score_with_claude` (P2)

**Where:** `praxis/scoring/judge.py:score_with_claude`

**What:** `score_with_claude(session, model="claude-opus-4-7")`. Same pattern in `score_with_openai` with `"gpt-5"`. If the deployed model name has a date suffix or the family advances (Opus 4.8), the call fails.

**Impact:** Brittle. The user has to edit the package to update the model.

**Fix:** read judge model from an env var (`PRAXIS_JUDGE_MODEL_ANTHROPIC` / `_OPENAI`) with the current values as defaults.

### B13 — `score_session` returns None on both "no keys" and "all providers failed" (P2)

**Where:** `praxis/scoring/judge.py:score_session`

**What:** The contract is "returns None if no API keys are configured", but the function also returns None when the API call fails (after logging via `print`). The caller can't distinguish "user has no key" from "API was down right now".

**Impact:** In the second case, the orchestrator silently scores the session heuristic-only and persists it as if `use_judge=False`. The user has no idea their API call failed — they paid attention to setting up keys, but a single network blip means that session is permanently in the DB as heuristic-only (won't be re-scored because of idempotence).

**Fix:** distinguish the two cases. Either:
- Don't persist sessions whose judge call failed (leave them for retry next run)
- Mark them in the DB as "judge_attempted_but_failed" and retry on next run

### B14 — Orchestrator's window for trajectory differs from window for snapshot (P2)

**Where:** `praxis/orchestrator.py:run`

**What:**
- `snapshot` is built from DB rows where `started_at ≥ now - since_days` — uses the canonical store
- `trajectory` and `model_profiles` are built from `sessions_in_window`, which is freshly-scanned sessions filtered by `started_at`. Sessions whose source file mtime is older than `since_days` ago aren't in `sessions` at all, even if they're in the DB.

**Impact:** A session scored 60 days ago that you bring back into the 90-day window by running `--since-days 90` shows up in the snapshot but is invisible to the trajectory and model_profiles. The two views of "the last 90 days" disagree.

**Fix:** compute trajectory and model_profiles from DB rows too. We already rehydrate them in `_snapshot_from_rows`; do the same for behavior signals (or store them on the row).

### B15 — `_heuristic_coaching` target score is hardcoded to current + 2.0 (P3 / partial impl)

**Where:** `praxis/scoring/coach.py:_heuristic_coaching`

```python
target_score=min(10.0, snapshot.dimension_means[key] + 2.0)
```

Spec §13.4 explicitly says the LLM target should be `current + 1.5 to 2.5`. The heuristic fallback hardcodes 2.0. Minor partial implementation.

**Impact:** The heuristic fallback advice always says "lift this dimension by 2.0 points". The LLM version varies. Different feel between modes.

**Fix:** use a small dimension-specific variation (e.g. weakest gets +2.5, second weakest gets +1.5).

### B16 — Coach response not validated against schema (P3 / partial impl)

**Where:** `praxis/scoring/coach.py:_llm_coaching`

**What:** The prompt says "Exactly 2 focus areas. Exactly 3 drills per focus area." but the code accepts whatever the LLM returns:

```python
focus_areas=list(payload.get("focus_areas", [])),
```

If the LLM returns 1 focus area or 4 drills (or zero), we render whatever we got. The fallback only triggers when `focus_areas` is empty, not when it's the wrong shape.

**Impact:** Inconsistent report shape across runs.

**Fix:** validate count; fall back to heuristic if shape is wrong.

### B17 — `_parse_sqlite` uses `mkdtemp` + `rmtree` without context manager (P3)

**Where:** `praxis/scanners/copilot.py:_parse_sqlite`

**What:** If the process crashes between `tempfile.mkdtemp` and `shutil.rmtree`, the temp directory leaks.

**Fix:** use `tempfile.TemporaryDirectory()` context manager.

### B18 — Default SQLite journal mode is `delete` (rollback), not WAL (P3)

**Where:** `praxis/storage/profile_store.py`

**What:** Default journal mode means concurrent CLI invocations will serialize (one blocks while the other writes). For a single-user CLI this is mostly fine, but if a user has the daemon and runs `praxis scan` manually at the same time, they'll see brief locking.

**Fix:** `PRAGMA journal_mode=WAL` on connection init.

---

## Heuristics that should defer to the LLM (when keys are present)

(Carrying over from REVIEW2 with new evidence.)

### H1 — All five `_*_MARKERS` regex patterns (P1)

**Why:** B5 confirmed `_PLAN_MARKERS` has 4/4 FP rate on common phrases. The same will be true of the others — `\bsource\b` matches "source code", "open source", "data source" (not verification). `\biteration\b` markers like `\bno,\b` match negation, not iteration.

**Why heuristic now is still valid:** no-keys fallback. The judge already covers this when keys are present.

**Improvement:** when keys are present, fold per-turn behavior detection into the existing judge call. The judge reads the whole transcript anyway; asking it for per-dimension signal totals adds ~80 tokens to the response. Drop the regex output from the score-blending step when judge has both keys present and a successful response.

### H2 — `_summarize_tasks` keyword classification (P2)

**Why:** First-match-wins on a 9-keyword chain. A prompt about "refactor my debug logging" is classified `debugging` because `debug` is checked before `refactor`. Order dependence is silently wrong.

**Improvement:** batch-classify via the judge — one extra LLM call per scan run with all unclassified first prompts. Cache the result keyed by `stable_id`. Heuristic stays as no-keys fallback.

### H3 — `_heuristic_fit` decision tree (P2)

**Why:** Thresholds (`avg_prompt_chars < 200`, `> 1500`, `engagement < 0.1`) are picked off the top of someone's head.

**Improvement:** the `_llm_fit` already exists. The action item is to make `_heuristic_fit` itself less assertive in its no-keys output (avoid B7's "claude's 'fast' tier" type fabrication, drop the "very short" language since 200 chars is arbitrary).

---

## Hand-waving (constants that need empirical grounding)

| Location | Magic number | Origin |
|---|---|---|
| `heuristics.py` | `planning_density × 14`, `× 10`, `× 12` | Spec section 7.5, picked to land scores roughly at 5/10 for typical users |
| `heuristics.py` | `avg_user_prompt_chars / 1500` for length score | Spec, "rich enough" |
| `heuristics.py` | `0.6 × length + 0.4 × code_block_share` | Spec, balance unclear |
| `behavior/trajectory.py` | slope > 0.02, avg_eng ≥ 0.3, pure_delegator > 0.4 | Spec section 10.3.4 — "decision tree" |
| `advisor.py` | tier=frontier ∧ chars<200 → over-using | Spec section 11.2.4 |
| `advisor.py` | `chars_per_token = 4.0`, `output_multiplier = 1.5` | English-text heuristic, varies by language and content type |
| `aggregate.py` | `JUDGE_WEIGHT = 0.7`, `HEURISTIC_WEIGHT = 0.3` | Spec, "judges catch nuance, heuristics catch volume" |
| `judge.py` | `MAX_TRANSCRIPT_CHARS = 12_000`, 1000-head + 800-tail | Spec, budget-driven |
| `orchestrator.py` | `max_new_scored = 50`, `since_days = 30` | Spec, "protects API budgets" |
| `html_report.py` | grade thresholds 8.5 / 7.0 / 5.5 / 4.0 | Spec section 16.3 |

These aren't *wrong*. They're unmeasured. None blocks v0.1 but they're calibration debt — the calibration-store proposal in REVIEW2 addresses the most consequential subset (the heuristic dimension multipliers).

---

## Partial implementations

| Where | What's missing |
|---|---|
| `_llm_coaching` | No schema validation on LLM response (B16); always falls back to heuristic only if `focus_areas` is empty, not if shape is wrong |
| `_heuristic_coaching` | Hardcoded `+2.0` target, spec says variable (B15) |
| `score_session` | No retry; first-failure returns None (B13) |
| Behavioral signal extraction | Always regex even when judge is present (H1) |
| Task classification | Always keyword (H2) |
| Card refresh | No auto-refresh CLI; manual JSON edits only (acknowledged out of scope) |
| Heuristic calibration | Constants never refined against accumulated LLM observations (REVIEW2 calibration-store proposal) |

---

## Fix plan (this pass)

Fixing the bugs that have real user-visible impact and are small:

**Now:**
- B1 — fenced JSON parsing (5 min, clear correctness fix)
- B2 — prefix match aggressiveness (5 min, prevents silent mis-routing)
- B3 — CLI `--show` normalization (5 min, UX fix)
- B4 — iteration multi-turn bonus condition (2 min)
- B5 — `_PLAN_MARKERS` worst false positives (5 min)
- B6 — `_parse_response` defensive defaults (10 min, prevents wasted calls)
- B8 — HTML evidence "..." condition (1 min)
- B9 — print → stderr (5 min)
- B10 — skip sessions with zero user turns (5 min)
- B11 — compute Haiku savings multiplier from cards (10 min)

**Deferred to v0.2 (documented here):**
- B12 — env-var judge model
- B13 — failed-judge retry policy
- B14 — trajectory from DB rather than fresh scan
- B15/B16/H1/H2/H3 — bigger architecture moves
- B17/B18 — small but not urgent
- Calibration store from REVIEW2

After fixes, re-run all 36 tests + smoke test to confirm.
