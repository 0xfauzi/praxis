# Review pass 2 — heuristics audit + continuous-learning design

Two threads. First: justify every heuristic in the codebase (or kill it). Second: take continuous learning seriously — Hermes-style memory adapted to a scheduled scorecard, not a live agent.

---

## Part 1 — every heuristic, justified or replaced

I went through the code and bucketed every non-LLM decision into three groups. The groups carry different design intent and should be treated differently.

### Group A — cheap baseline / no-keys fallback (this is the right design)

These exist so the tool produces a real, defensible score when the user has no API keys configured. Spec §1.2: "missing API keys → heuristic-only mode."

| Location | What it does | Could be LLM? | Why heuristic here is correct |
|---|---|---|---|
| `scoring/heuristics.py` — `_PLAN_MARKERS`, `_STRUCTURE_MARKERS`, `_VERIFY_MARKERS`, `_ITERATION_MARKERS`, `_PUSHBACK_MARKERS` (5 regex) | Per-turn pattern detection for the 5 rubric dimensions | Yes, an LLM would have higher recall and would catch paraphrases | Cost (one judge call per turn per session = thousands of calls/run), latency, and the no-keys requirement. Already overridden 70/30 by the judge when keys are present. |
| `scoring/heuristics.py` — `heuristic_dimension_scores` (`planning_density × 14`, `avg_context_richness × 10`, etc.) | Map raw signal density to 0-7 dimension scores | Yes, an LLM could rate each dimension directly | Same cost/latency. The constants are calibration — see Improvement I below. |
| `scoring/heuristics.py` — `avg_context_richness = 0.6 × length + 0.4 × code_block_share` | Reduce a turn to a 0-1 richness scalar | Yes — "rate this turn's context richness 0-1" | Cost, latency, determinism (identical runs return identical numbers). |
| `behavior/signals.py` — `_WHY_QUESTIONS`, `_COMPREHENSION_CHECKS`, `_EXPLANATION_REQUESTS`, `_PURE_DELEGATION`, `_OUTSOURCED_DEBUG`, `_OWN_ATTEMPT_MARKERS` (6 regex) + `_is_telegraphic` length check | Per-turn behavioral signal counting | Yes, and probably better — "does this turn show engagement vs. delegation?" is squarely LLM territory | Same cost concern, multiplied (every turn of every session, not just sampled). |
| `behavior/trajectory.py` — `assess_trajectory_heuristic` decision tree (slope > 0.02, avg_engagement ≥ 0.3, pure_delegator_rate > 0.4) | Pick a trajectory label from aggregate stats | LLM version (`assess_trajectory_with_llm`) is the primary path | Heuristic is the no-keys fallback. Thresholds are arbitrary; see Improvement IV below. |
| `models_advisor/advisor.py` — `_heuristic_fit` (tier=frontier ∧ avg_chars<200 → "over-using") | Heuristic model–task fit assessment | LLM version (`_llm_fit`) is the primary path | Heuristic is no-keys fallback. The 200-char threshold is a guess; see Improvement IV. |
| `scoring/coach.py` — `_FALLBACK_DRILLS` (static dict of 3 drills per dimension) + `_heuristic_coaching` (picks weakest 2) | Generate coaching with no keys | LLM version (`_llm_coaching`) is the primary path | No-keys fallback. But static drills get stale across many cycles — see Improvement III below. |

**These are the right design**, but they have one shared latent bug: the constants and thresholds were chosen by intuition. After the LLM judge has rated a meaningful corpus, we can back-calibrate them. See Improvement I.

### Group B — calibration constants (not really "heuristics" — they're knobs)

| Location | What it does | Could be LLM? | Why heuristic here is correct |
|---|---|---|---|
| `scoring/aggregate.py` — `JUDGE_WEIGHT = 0.7`, `HEURISTIC_WEIGHT = 0.3` | How much to trust the judge over the heuristic | No — this *is* the meta-decision | These are tunable parameters. Should be config knobs, not hardcoded. |
| `scoring/judge.py` — `MAX_TRANSCRIPT_CHARS = 12_000`, 1000-head + 800-tail per long turn | Prompt-budget cap before sending to the judge | No — this is the input prep | Anthropic/OpenAI token budgets, latency. Already the right move. |
| `orchestrator.py` — `max_new_scored = 50`, `since_days = 30` | Per-run budgets | No — these protect API spend | Already CLI flags; correct as defaults. |

These aren't heuristics in the "smart-guess" sense. They're configuration. The action item is to expose them more clearly (e.g. `praxis scan --judge-weight 0.6`) and to document the rationale.

### Group C — pure computation / presentation (LLM is inappropriate)

| Location | What it does | Could be LLM? | Why heuristic here is correct |
|---|---|---|---|
| `behavior/trajectory.py` — `_linear_slope` (least-squares regression) | Compute trajectory slope from a series | No | LLMs can't do float-precise stats reliably. Use math. |
| `models_advisor/advisor.py` — `_estimate_cost` (4 chars/token, 1.5× output, × pricing) | Arithmetic on published pricing × measured volume | No | Pure arithmetic. LLM would be slower with no upside. The two assumptions (chars/token and output multiplier) could be empirically calibrated though — see Improvement V. |
| `reports/html_report.py` — `_grade_label` (5 buckets at 8.5/7.0/5.5/4.0) | Map a numeric score to a grade chip | No (could but shouldn't) | Determinism matters here — same score yields same label across runs. Presentation logic. |

Clear-cut, no action needed.

### Group D — heuristics that *should* become LLM-mediated even when keys are present

This is where the audit found real improvement opportunities. These are heuristics today even when the user has API keys, but they probably shouldn't be.

| Location | What it does | What's the issue | Proposed change |
|---|---|---|---|
| `models_advisor/advisor.py` — `_summarize_tasks` keyword `if/elif` classification | Classify the first prompt of each session into 9 buckets (debugging, refactoring, code generation, ...) | First-match-wins ordering is arbitrary ("bug" in "I bug fixed this refactor" → debugging). Coarse, low signal. | **Batch-classify with the judge call.** Cache classification keyed by `stable_id` so it's once per session forever. Heuristic stays as the no-keys fallback. |
| `behavior/signals.py` — per-turn regex even when keys present | Compute engagement/delegation per session via regex regardless of API key availability | The judge already reads the transcript. Asking it to also output `engagement_rate`, `delegation_rate`, `is_pure_delegator` adds ~50 tokens to the response and gives a much better answer than the regex. | **Fold per-session behavioral signals into the existing `JudgeResult`.** Regex stays as no-keys fallback only. Trajectory math then uses the judge-derived signals when present. |

These two changes alone would noticeably improve quality without changing the user-visible architecture.

### Improvements proposed (ordered by leverage)

**I. Empirically calibrate the dimension-score multipliers.**
The `× 14`, `× 10`, `× 12` constants in `heuristic_dimension_scores` were picked to make heuristic scores roughly comparable to LLM scores. Once we have a sizable corpus where both ran, fit the multipliers via simple regression so the heuristic-only mode closer-tracks the LLM. New CLI command: `praxis calibrate` reads from `session_scores`, fits, writes the new constants into a small config JSON.

**II. Fold behavioral signal extraction into the judge call (when keys present).**
Add `behavioral_signals` to the `JudgeResult` schema. The judge already reads the full transcript; it can output a structured behavior block for ~50 extra tokens. Drop the regex pass for that session. Regex stays as the no-keys fallback path.

**III. Cached LLM-generated drill bank.**
Today's `_FALLBACK_DRILLS` ships with 3 hardcoded drills per dimension. After a few cycles with API keys, the system has accumulated many LLM-authored drills it has shown to the user. Cache them in `~/.praxis/drills_cache.json` keyed by dimension. The no-keys fallback pulls from the cache when available, falls back to the hardcoded set otherwise. Fresher coaching across many cycles without per-cycle LLM cost.

**IV. Re-derive the trajectory and fit thresholds.**
`slope > 0.02`, `avg_engagement ≥ 0.3`, `pure_delegator_rate > 0.4`, frontier-tier `chars < 200` — these were picked off the top of someone's head. Once we have ground truth (LLM-labeled trajectories on a real corpus), fit the thresholds. Until then, document them as "calibrated by intuition, awaiting empirical correction."

**V. Calibrate cost-estimate assumptions.**
`4 chars/token` and `1.5× output multiplier` are reasonable for English coding chat but won't match other workloads. After the first month of runs, sample a few sessions, count actual tokens (e.g. via the model's tokenizer), and refine the per-card multiplier. Or just expose it as a per-card `chars_per_token_override` field on `ModelCard`.

**VI. Make the JUDGE/HEURISTIC blend configurable.**
Expose `--judge-weight` as a CLI flag for `scan`. Default 0.7. Power users may want 1.0 (trust the judge entirely) or 0.5 (skeptical).

These are not blocking for v0.1, but they should be on the v0.2 list.

---

## Part 2 — does the system need memory? Judgment call.

The previous goal mentioned "continuously learn like the Hermes agent." Hermes is the current reference design for memory-equipped agents (Nous Research, 2026): bounded core memory files (`MEMORY.md`, `USER.md`), three memory actions (`add`/`replace`/`remove`), consolidation at 80% full, skill capture from completed tasks, prefetch/sync loop, prompt-injection scanning. Letta/MemGPT does a similar three-tier OS-inspired thing (core/archival/recall). Both target **live agents the user talks to continuously**.

**The scorecard is not that.** It's a scheduled batch job that reads logs and writes a report. Asking "does the scorecard need Hermes-style memory" is asking the wrong question. The right question is: *what failure modes does the system have today that memory would fix?*

### What memory the system already has

| Memory | Storage | Purpose | Working? |
|---|---|---|---|
| Session deduplication | `session_scores` SQLite table | Idempotent re-runs (spec §1.2) | Yes — proven by smoke test |
| Daily consolidation cache | `daily_consolidations` SQLite table | One-coaching-per-day, not per-run | Yes |
| Run log | `run_log` SQLite table | Audit trail of when scans ran | Yes |
| Model card cache | In-memory after first JSON load | Avoid re-reading cards each call | Yes |
| Built-in cards on disk | `data/builtin_cards/*.json` | Ship-once, update manually | Yes |

This is the right amount of memory for an idempotent batch tool. The system *persists state* across runs (which is what "memory" technically means at the system level). It does **not** need agentic memory layers, runtime tool calls, or per-turn prefetch — there are no turns. It runs to completion and exits.

### Failure modes I considered, with the verdict

For each candidate "system memory" layer, the question is: what would the system get wrong without it, and how often?

| Candidate memory | What it would fix | Failure rate today | Verdict |
|---|---|---|---|
| Model card freshness tracking ("when did vendor pricing last change vs. when we last verified") | Stale cost figures | Pricing changes ~quarterly per vendor; user verifies via per-card source URL | **No.** The `pricing_last_verified` field on each card already exposes this. Adding active "go check for changes" memory would require a card-refresh CLI, which is out of scope per the user's earlier "manual is OK" call. |
| Research citation freshness ("Sarkar 2025 still current?") | Stale rubric evidence | Papers don't change once published | **No.** Content problem, not memory. Manual content update if a stronger paper appears. |
| API failure memory ("Anthropic was down last time, try OpenAI first") | Wasted retry on known-bad provider | API outages are minutes-to-hours; the next scheduled run will catch the recovery | **No.** The judge already fails over within a single run. Cross-run memory would over-fit to transient state. |
| Daemon resume state ("crashed at session 32 of 50, pick up there") | Lost work on crash | Idempotence means re-running is cheap — pick up where we left off automatically | **No.** Solved by the existing `has_session()` check; crash recovery is free. |
| **Heuristic ↔ judge calibration history** | Heuristic-only mode (no API keys) stays calibrated to whatever the judge says is true on this user's data | Heuristic-only mode produces scores that drift from the judge's view; we have no idea by how much | **Yes — this is the one form of system memory worth adding.** See below. |

### The one form of memory worth adding: a calibration loop

The system stores, for every scored session, **both** the heuristic dimension scores and (when API keys were present) the judge dimension scores. That paired data is already on disk — it just isn't being used.

A calibration loop would:

1. On a periodic trigger (e.g. once every 7 days, or on a manual `praxis calibrate` invocation), read all rows from `session_scores` that have both `heuristic_scores_json` and `judge_result_json` populated.
2. For each rubric dimension, fit a simple linear correction: `judge_score ≈ a + b × heuristic_score`. Two parameters per dimension, seven dimensions, fourteen numbers total.
3. Persist those coefficients to `~/.praxis/calibration.json`.
4. The `heuristic_dimension_scores` function reads `calibration.json` if present and applies the correction before returning. No keys required at scoring time.

**Why this counts as system-level memory:** the system uses past observations of its own behavior to improve its future behavior on the same user's data. It's small (14 numbers), bounded, self-contained, and doesn't require an LLM at scoring time. It also addresses real Improvements I and IV from Part 1.

**Why this is the *only* memory layer worth adding now:** it's the only candidate above with a non-trivial failure rate (heuristic drift from judge is unmeasured but almost certainly nonzero) and a clean fix (one regression, fourteen numbers).

Everything else on the candidate list either:
- Solves a non-existent problem (daemon resume, API failover memory)
- Would require infrastructure out of scope (active model-card refresh, telemetry-driven personalization)
- Is more honestly a content-update task than a memory problem (research citations)

### What I am NOT proposing

- ❌ Long-running daemon with in-memory state. The CLI batch model is correct.
- ❌ Cross-user data pooling. The system is single-user by design.
- ❌ A `MEMORY.md` / `USER.md` profile file the LLM curates each cycle. This was over-engineering. The user's chat history *is* the durable record; the report is the synthesis. Adding a separate "user memory file" duplicates state for marginal benefit.
- ❌ Skill capture from high-scoring sessions. Same reason — the sessions themselves are the record; we don't need an extracted markdown distillation living in a parallel store.
- ❌ Coaching ledger to "avoid repeating advice." Coaching is regenerated from the same weakest dimensions each cycle; if it repeats, it's because the dimension hasn't moved. That's information, not a bug. The honest message ("you've been weakest on verification for three weeks") emerges naturally from the snapshot history we already store, not from a separate ledger.

### Concrete recommendation

Add **one** new piece of system memory: `~/.praxis/calibration.json`, written by a `calibrate` subcommand or auto-refreshed weekly during consolidation when at least 20 paired (heuristic, judge) observations exist. Apply at scoring time inside `heuristic_dimension_scores`. Document the math in the README.

Everything else: leave alone. The system already has the right amount of memory for what it does.

---

## Summary

- **Heuristics audit (Part 1):** all current heuristics fit one of three valid buckets (cheap no-keys fallback, calibration knob, pure computation). Several should be empirically calibrated (Improvements I/IV/V) and two should defer to the judge when keys are present (II/III). No heuristic should be deleted.
- **System memory (Part 2):** the system already persists what it needs to (session dedup, consolidation cache, run log, card cache). The one form of memory worth adding is a **heuristic ↔ judge calibration store** that lets the no-keys mode track the judge's view of this user's data over time. Everything else on the candidate list either solves a non-problem or is content-update work disguised as memory.
