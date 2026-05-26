# Praxis v0.2 - The Coaching Spec

**Status:** Build target for v0.2.
**Last updated:** 2026-05-26.
**Audience:** Claude Code, implementing from scratch against the existing v0.1 codebase.

---

## 0. How to use this spec

You are evolving the existing `praxis` package. Most v0.1 modules stay; this spec replaces the user-visible surface and adds three new subsystems (moments, tasks, weekly cadence). Where the spec says **MUST**, it is a hard requirement. **SHOULD** is a strong preference. **MAY** is optional.

Where a design decision was made for a non-obvious reason, the reasoning is in the spec; follow it.

Decisions already locked (from the v0.2 planning conversation):

- Delivery surfaces: local HTML file plus a macOS notification on a weekly schedule, AND a `praxis week` CLI command. No email, no markdown export, no IDE extension.
- Task clustering: LLM-driven (Section 5). Project hint and timestamps are context, not the grouping rule.
- Comparison anchor: self-baseline only. No cohort telemetry. Defer that to v0.3+.
- Judge layer: required. No heuristic-only fallback path. API key required to use Praxis.
- No heuristic substitutes for LLM judgment anywhere in the product surface. Heuristics may still extract structured features (turn counts, prompt lengths, regex marker hits), but those features are inputs to LLM calls and to baseline statistics - they are not decision rules for what gets coached, judged, clustered, or surfaced.

### What the "no heuristics" principle does and does not cover

It covers anything where a rule would substitute for judgment: which sessions get coached, which moments lead the digest, how sessions cluster, what the suggested alternatives say.

It does not cover:

- **Statistical math on time series.** Slope significance bands and hysteresis thresholds (Section 7) are how you read noisy data honestly. The labels they produce (Learning, Drifting, etc.) are statistical facts, not LLM judgments. The LLM paraphrases the slope into prose; it does not invent the label.
- **Display significance gating.** Whether a delta of 0.2 on a /10 scale gets an arrow or a tilde (Section 8.3) is UX, not coaching.
- **Hard caps on surface area.** "Maximum 3 moments per digest" (Section 4.3) is a UX ceiling. The product would be worse if a chatty LLM convinced itself to surface 12 things.
- **Accountability metrics computed from data.** Whether last week's commitment was met (Section 6.3) MUST be computed from the data, not the LLM. Letting the LLM grade itself defeats the whole feedback loop. The LLM may write prose around the outcome; it does not decide the outcome.
- **Protocol-failure fallbacks.** When an LLM call returns malformed output twice, deterministic fallback paths exist (Sections 4.3.1, 5.3). These are graceful degradation, not heuristic substitution.

---

## 1. Charter & non-goals

### 1.1 Charter

v0.1 was a measurement engine. v0.2 is a coaching product. The success metric is **observable behavior change** between weekly digests, not "did we render a number."

Three things v0.2 must do that v0.1 does not:

1. **Tie advice to evidence.** Every coaching suggestion must point to a specific quoted span from a real transcript, with the dollar/time impact of the missed move.
2. **Compare this week to the user's own past.** Every metric in the digest must carry a delta vs. the rolling 90-day baseline (or, after 8+ weeks of data, week-over-week with a noise band).
3. **Land at a time the user is receptive.** Weekly, on a day they configure, via a macOS notification and an HTML file. Not on demand, not buried in a terminal scrollback.

### 1.2 Non-goals for v0.2

- Real-time / pre-prompt feedback (rejected: requires IDE or shell wrappers).
- Cohort percentile or shared baselines (deferred; requires telemetry infrastructure and a privacy review).
- Email delivery, Slack delivery, web dashboard (deferred).
- Cross-machine sync (the SQLite DB stays local).
- Non-macOS scheduling: Linux/Windows users will get the `praxis week` CLI and a printable systemd/Task Scheduler snippet, but the notification surface is macOS-only in v0.2.

---

## 2. The behavior change loop

The product runs in a loop with **one week** as the unit of iteration:

```
Sunday 18:00 (configurable)
   |
   v
[ scan window: last 7 days ] -- (judge cost-optimized, see Section 9)
   |
   v
[ identify moments: per dim, top 1-2 transcript spans where the user lost score ]
   |
   v
[ cluster sessions into tasks: one LLM call across the whole week ]
   |
   v
[ compute deltas vs. 90-day baseline + last week ]
   |
   v
[ render digest: trajectory headline -> top moment -> cost ledger -> tasks -> follow-up ]
   |
   v
[ macOS notification: "Your weekly Praxis read is ready" -> opens file://...html ]
   |
   v
[ next Sunday: did last week's "one thing to try" actually move the needle? ]
```

The loop is closed by **Section 6.3 (the follow-up panel)**: each digest carries forward one specific commitment from the previous week and reports whether the data shows it happened.

If we cannot close the loop, this is not a coaching product. It is v0.1 with a cron job.

---

## 3. Architecture changes at a glance

| Module | v0.1 state | v0.2 change |
|---|---|---|
| `praxis/scanners/` | Claude, Codex, Copilot | Unchanged. |
| `praxis/scoring/heuristics.py` | 6-dim heuristic scoring | **Removed.** No heuristic scoring path in v0.2. Feature extraction (turn count, prompt length) survives in a renamed `features.py` and is used only for baseline statistics and rendering metadata, never as a substitute for LLM judgment. |
| `praxis/scoring/judge.py` | Per-session LLM judge | Extended to emit **moments** (Section 4). New compressed-transcript path (Section 9). |
| `praxis/scoring/aggregate.py` | Profile snapshot from scores | Adds week-bucketed aggregation (Section 8). |
| `praxis/scoring/coach.py` | Generic drill bank by weakest dim | **Replaced.** Coach now selects moments + ties drills to them. |
| `praxis/behavior/` | Per-session signals + slope trajectory | Slope replaced by a weekly-bucketed model with a noise band (Section 7). |
| `praxis/models_advisor/` | Per-model fit + cost estimate | Cost becomes a first-class dimension in the digest (Section 10). |
| `praxis/storage/` | 3 tables: session_scores, daily_consolidations, run_log | **Schema migration** (Section 14): add `moments`, `tasks`, `weekly_digests`, `follow_ups`. Drop `daily_consolidations`. |
| `praxis/reports/` | terminal.py + html_report.py | **Both rewritten** around the weekly digest layout (Section 6). |
| `praxis/orchestrator.py` | Daily idempotent scan + score + render | **Replaced by `run_weekly()`**; daily flow is deleted. |
| `praxis/cli/` | scan / rubric / models / status / install-daemon | Replaced surface (Section 12). |
| `~/.praxis/config.toml` | Did not exist | New, holds user-configurable schedule + display preferences (Section 12.2). |

---

## 4. The Moments engine - transcript-tied evidence

This is the most important new subsystem. The product's claim to be a *coach* lives or dies here.

### 4.1 What a moment is

A `Moment` is a structured pointer to a specific span of a real transcript where one rubric dimension dropped, with a concrete suggested alternative.

```python
@dataclass(frozen=True)
class Moment:
    moment_id: str                  # sha256(session.stable_id + dim_key + turn_index)[:16]
    session_stable_id: str
    dim_key: str                    # one of the 6 rubric keys
    turn_index: int                 # which turn the lapse occurred at (0-indexed user turns)
    quoted_excerpt: str             # <= 240 chars, the user's own words (or assistant's, if it shows the missed verification)
    why_it_lost_score: str          # <= 180 chars, one specific sentence
    suggested_alternative: str      # <= 220 chars, what to do next time
    dollar_impact_estimate: float | None   # see Section 10; None if not estimable
    minutes_impact_estimate: int | None    # rough wall-time cost if model went off-track
    severity: Literal["minor", "moderate", "major"]
    created_at: datetime
```

### 4.2 How moments are produced

Moments come from the judge, not from heuristics. The judge prompt (Section 9.2) is extended to return, alongside scores and rationales:

```json
"moments": [
  {
    "dim_key": "verification",
    "turn_index": 4,
    "quoted_excerpt": "<exact span from the transcript, <= 240 chars, copied verbatim>",
    "why_it_lost_score": "Accepted the SQL migration without checking what tables it touched.",
    "suggested_alternative": "Ask: 'list every table this migration writes to, before you run it.'",
    "severity": "moderate"
  }
]
```

The judge is instructed to emit **at most one moment per dim per session**, and only when it actually saw a specific coachable lapse in the transcript. The judge decides this, not a score threshold. The prompt says: "Do not emit a moment for a dim where you have nothing specific to coach on. A score of 5 with no specific lapse is not a moment; a score of 7 with one clearly avoidable mistake is. Use your judgment." This caps the moment volume at ~6 per session in the worst case, ~1-2 in the typical case, but the cap comes from what was actually present in the session, not from a numerical floor.

### 4.3 How moments are selected for the weekly digest

A formula picking the headline moment would be a heuristic substitute for the most important coaching judgment in the digest. v0.2 uses one LLM call to make this choice.

After all moments for the week are emitted and validated (substring check, redaction), one selector call sees all candidate moments and chooses which to surface:

Input prompt:

```
You are picking the single most coachable moment from a person's AI usage this week. You will see N moment candidates, each with:
  - dim_key
  - quoted_excerpt
  - why_it_lost_score
  - suggested_alternative
  - severity (minor / moderate / major)
  - session_started_at (so you know recency)
  - dollar_impact_estimate (may be null)
  - whether this same suggested_alternative was flagged in any of the previous 3 weeks (recurrence_count)

Choose:
  1. one headline_moment_id - the single moment most worth opening the week's digest with. Weigh severity, how concrete the alternative is, recurrence (a pattern that keeps happening is more worth coaching than a one-off), and dollar impact.
  2. up to two supporting_moment_ids - other moments worth showing in the "weakest dim" panel. Prefer moments on different dims from the headline.
  3. a one-sentence headline_reason explaining why you picked the headline moment.

Return JSON:
{
  "headline_moment_id": "...",
  "headline_reason": "<one sentence>",
  "supporting_moment_ids": ["...", "..."]
}
```

Cheap-tier model from the user's primary provider. Cost: ~$0.005/week. Total moments rendered in the digest: 3 maximum (one headline + up to two supporting). This is a deliberate UX ceiling - the product fails if it floods the user with 12 things to fix - and stays as a hard cap regardless of how many moments the LLM is tempted to surface.

### 4.3.1 Failure modes considered

- **The selector picks a moment that isn't in the candidate list.** Validate against the input set. If invalid, re-prompt once. If still invalid, fall back to picking the most-recent `major`-severity moment, or if no major moments exist, the most-recent moderate. This is a graceful-degradation fallback for LLM protocol failure, not a heuristic substitute.
- **The selector always picks the most recent moment, regardless of severity.** Catch this in the same 4-week telemetry path as the judge calibration check (§9.6). If recency bias becomes structural, sharpen the calibration instruction in the prompt.
- **Only one candidate moment exists this week.** Skip the selector call entirely; that moment is the headline by default.

### 4.4 Failure modes considered

- **The judge invents a quote.** Mitigation: after parsing the judge response, every `quoted_excerpt` MUST be verified to be a substring of the actual session transcript (whitespace-normalized). If verification fails, the moment is discarded and a `[scorer] moment failed substring check` log line is emitted. Do not fall back to a fuzzy excerpt. A wrong quote is worse than no quote.
- **Excerpts contain secrets.** A user transcript may include API keys, passwords, or PII pasted into a prompt. Mitigation: before persisting any moment, run a redaction pass that masks anything matching common secret regexes (Anthropic, OpenAI, AWS, GitHub PATs, JWT-like, anything with ">= 20 alnum + `_-` after `key`/`token`/`secret`/`password`). Replace with `[REDACTED]`. This is non-negotiable - the moment will be re-rendered into an HTML file the user opens in a browser.
- **The same moment recurs week after week.** This is actually a feature: it surfaces persistent habits. But the digest should detect repetition (same `moment_id`-prefix or same `(dim_key, suggested_alternative)` pair appearing 3+ weeks in a row) and escalate the framing: "This is the 4th week we've flagged verification. Worth scheduling 30 min to write a personal checklist."

---

## 5. Task clustering - LLM-driven

### 5.1 Why not heuristic clustering

cwd + temporal proximity is a tempting rule because it is cheap and obvious. It is also wrong as the primary grouping mechanism. Two examples:

- Tuesday morning I debug an auth migration in `~/code/api`. Tuesday afternoon I switch to a totally separate UI bug in the same repo. cwd + 6h merges these into one "task". They are two tasks.
- Wednesday I work on the same feature spread across three repos (frontend, backend, infra). cwd splits this into three "tasks". It is one task.

Heuristic clustering will be wrong in both directions, and the user will see it and lose trust. v0.2 commits to LLM judgment about whether sessions belong together. Project hint and timestamps are passed as context to the model, not consulted as decision rules.

### 5.2 The clustering call

Once per weekly digest, after all sessions in the window are scored, one LLM call clusters them.

Input prompt:

```
You are reading short summaries of N AI-coding sessions from one person's week. Group them into "tasks" - clusters of sessions that share a single underlying goal, even if they span repos or days. Two sessions belong together if a knowledgeable colleague would describe them as part of the same piece of work. Two sessions are separate tasks if the colleague would describe them as different work, even in the same repo within an hour.

For each session you will see:
  - session_id
  - first user turn (truncated to 400 chars)
  - project_hint (filesystem path or "none")
  - started_at (ISO timestamp)

Return JSON in this exact shape:
{
  "tasks": [
    {
      "label": "<3-7 words, e.g. 'auth migration debugging' or 'deckgen UI polish'>",
      "task_type": "<one of: debugging, refactoring, building_new, planning, learning, research, ops, other>",
      "session_ids": ["<id>", "<id>", ...],
      "rationale": "<one sentence saying why these belong together>"
    }
  ]
}

Every session_id from the input MUST appear in exactly one task. A single-session task is fine.
```

Send all N sessions in one call. The cheap-tier model from the user's primary provider (Haiku if Anthropic, gpt-5-mini if OpenAI). Truncate each first-user-turn to 400 chars before sending. For a week of 50 sessions this is ~25K input tokens, well within context.

### 5.3 Validation

The returned JSON MUST satisfy:

- Every input session_id appears in exactly one task. If any are missing or duplicated, the response is rejected and the LLM is re-prompted once with the validation error. If the second attempt also fails, fall back to **one task per session**, deterministically labeled from the first 5 words of the session's first user turn. (This is a graceful degradation, not a heuristic substitute - it only fires on LLM protocol failure.)
- Labels reject as invalid if they exceed 60 chars or contain the literal strings "I", "you", "the user", "the assistant".
- `task_type` must be one of the eight enumerated values.

### 5.4 Cost

One LLM call per week. For 50 sessions at ~25K input + ~3K output tokens on a cheap-tier model: <$0.10/week. The product can afford this.

### 5.5 What the digest does with tasks

The "by task" panel shows the user's top 3 tasks of the week (ranked by total session count, with total cost as a tiebreaker), with:

- Task label + task_type
- Number of sessions
- Total cost
- Mean overall score on this task
- Worst dim on this task

This answers "where did my week go" in plain English.

### 5.6 Failure modes considered

- **The LLM invents a session_id that wasn't in the input.** Mitigation: validate every returned session_id against the input set. Invented IDs are rejected, triggering the re-prompt path in 5.3.
- **The LLM lumps everything into one task to be safe.** Mitigation: if the response has exactly one task and N > 6 sessions, re-prompt with explicit instruction to split when the work is genuinely distinct. If the second attempt is also one task, accept it - the user genuinely had a single-focus week.
- **The LLM splits everything into singletons to be safe.** Mitigation: similar re-prompt path if every task has exactly one session AND N > 8 sessions. Accept after retry.
- **Sessions with no cwd hint.** They get sent to the LLM with `project_hint: "none"`. The LLM must group based on content alone, which is the right behavior - this is a non-coding session or a Codex session without cwd metadata, and content is the honest signal.

---

## 6. Weekly digest layout

This is what the user actually sees. The order is the priority order: the eye lands on trajectory first, the moment second, the cost ledger third.

### 6.1 HTML digest sections, in order

```
+----------------------------------------------------------+
|  PRAXIS  -  Week of May 18-24, 2026                      |
|                                                          |
|  [Trajectory headline + confidence band]                  |  Section 7
|                                                          |
|  THIS WEEK'S MOMENT                                       |  Section 4.3
|  -----------                                              |
|  "<quoted excerpt from a real transcript>"                |
|                                                          |
|  Why this lost score: <one sentence>                      |
|  Next time, try: <one sentence>                           |
|  Estimated cost of this lapse: $X / Y minutes             |
|                                                          |
|  COST LEDGER                                              |  Section 10
|  -----------                                              |
|  This week: $X (vs $Y baseline) [over / under / on]      |
|  Biggest line: Opus on <task_label> ($A on N sessions)   |
|  Sonnet could have handled M of those, saving ~$B        |
|                                                          |
|  WHERE THE WEEK WENT                                      |  Section 5.3
|  -----------                                              |
|  1. <task_label>  -  N sessions  -  $A  -  worst: X      |
|  2. <task_label>  -  N sessions  -  $A  -  worst: X      |
|  3. <task_label>  -  N sessions  -  $A  -  worst: X      |
|                                                          |
|  THE SIX DIMENSIONS                                       |  Section 8
|  -----------                                              |
|  Planning      6.8  (baseline 5.4, +1.4)  [bar + delta]  |
|  Context       5.9  (baseline 6.2, -0.3)                 |
|  Iteration     4.2  (baseline 4.5, -0.3)                 |
|  ...                                                      |
|                                                          |
|  FOLLOW-UP FROM LAST WEEK                                 |  Section 6.3
|  -----------                                              |
|  Last week we asked you to <commitment text>.            |
|  This week's data shows: <improved / unchanged / worse>  |
|                                                          |
|  ONE THING TO TRY NEXT WEEK                               |
|  -----------                                              |
|  <single sentence drawn from this week's headline moment>|
|                                                          |
+----------------------------------------------------------+
```

The HTML uses the existing v0.1 visual language (cream, terracotta, Libre Baskerville). Do not redesign the look. Redesign the **content**.

### 6.2 The terminal digest (`praxis week`)

Same content order, ANSI-rendered, fits in <80 columns. The trajectory headline, the headline moment, and the follow-up are the three sections that MUST render in the terminal. The cost ledger and "where the week went" MAY render in a compact form. The full six-dim panel SHOULD be a footer.

### 6.3 The follow-up panel

This is the section that closes the loop. Each weekly digest persists one commitment in the `follow_ups` table:

```python
@dataclass
class FollowUp:
    week_iso: str               # "2026-W21"
    dim_key: str                # which dim the commitment targets
    commitment_text: str        # "ask 'list every table this migration writes' before running migrations"
    target_metric: str          # "verification_rate", "delegation_rate", or "<dim>_dim_mean"
    baseline_value: float       # the value as of digest generation
    measured_value: float | None  # filled in next week
    outcome: Literal["improved", "unchanged", "worse", "pending"]
```

The commitment text comes from the previous week's headline moment's `suggested_alternative`.

The outcome is computed next week by comparing the same metric in the new week's data. "Improved" means `measured_value > baseline_value + 0.5` (or the inverse for delegation_rate, which we want lower). "Worse" means `measured_value < baseline_value - 0.5`. Otherwise "unchanged."

This is the closed loop. Without this section, v0.2 is not a coaching product.

---

## 7. Trajectory as headline

### 7.1 What changes vs. v0.1

v0.1 fit a least-squares line to per-session engagement/delegation rates. Two problems:

- The unit is sessions, not time. A burst of 8 sessions in one day weights that day 8x.
- No confidence interval. A noisy 6-point fit produces dramatic-looking slopes.

v0.2 fixes both.

### 7.2 The new trajectory model

- Bucket all sessions in the last 90 days into **weekly buckets** (ISO weeks).
- For each weekly bucket, compute the mean of each signal (engagement_rate, delegation_rate, independence_rate, plus the 6 dim scores).
- Fit a least-squares line over the weekly means.
- Compute the standard error of the slope. If `|slope| < 1.5 * stderr`, the trajectory is **flat / noisy**, regardless of sign.

Trajectory labels:

| Condition | Label |
|---|---|
| Engagement slope significantly up, delegation slope significantly down | **Learning** |
| Engagement slope flat, delegation slope significantly down | **Growing autonomy** |
| Engagement slope flat, delegation slope flat | **Steady** |
| Engagement slope flat or down, delegation slope significantly up | **Drifting** |
| Engagement slope significantly down, delegation slope significantly up | **Atrophying** |
| Fewer than 4 weekly buckets with data | **Reading** (the v0.1 placeholder, retained) |

"Significantly" means `|slope| >= 1.5 * stderr` AND `|slope| >= 0.05` per week.

### 7.3 The headline copy

For each label, generate a single sentence that names the specific behavior shift, not just the slope direction:

- Learning: "Engagement up 0.18/wk, delegation down 0.11/wk over 8 weeks. You're investing more cognition per session, not less."
- Drifting: "Delegation up 0.14/wk over 6 weeks while engagement held flat. You're shipping more, but checking less."
- Steady: "No significant movement on either axis over 5 weeks. Habit is locked in - good or bad."

The sentence is LLM-generated, with the slope numbers and bucket count as inputs. Truncate to 180 chars.

### 7.4 Failure modes considered

- **Trajectory whiplash.** A bad week swings the label. Mitigation: require the new label to differ from the previous week's label by a stronger threshold (2.0 * stderr instead of 1.5 *) before the label changes. This adds hysteresis.
- **Sparse weeks.** A week with one session has a near-meaningless mean. Mitigation: weeks with fewer than 2 sessions are excluded from the fit but still rendered in the cost ledger.

---

## 8. 90-day baseline and week-over-week

### 8.1 Two anchors, in priority order

The baseline panel for each dimension shows two numbers:

1. **90-day rolling baseline** (mandatory): mean of all session scores in the last 90 days, excluding the current week. This is the user's "normal."
2. **Last week's mean** (optional, only after 2+ weeks of data): for spotting week-on-week swings.

The displayed delta is **this week minus 90-day baseline**. The HTML report shows last-week as a faded secondary annotation; the terminal omits it.

### 8.2 Why 90 days and not 30

30 days is too short - it tracks recent practice change too eagerly, so a user who improved is constantly beating their own baseline by 0.1 and shown a misleading green arrow. 90 days is long enough to be a real anchor but short enough to reflect the user's current era.

### 8.3 Significance gating

Deltas smaller than 0.3 on a /10 scale are rendered as `~` (unchanged). Only deltas >= 0.3 are rendered with up/down arrows. This avoids selling noise as signal.

### 8.4 Failure modes considered

- **The user has < 14 days of data.** No baseline exists. Render baseline as `--` and skip deltas. Show "Baseline forming. Come back in 2 more weeks for week-over-week."
- **Outlier sessions skew the baseline.** A 50-turn marathon session weights heavily. Mitigation: when computing the baseline, cap each session's contribution by clipping its `overall` at [1.0, 9.0] and weighting all sessions equally regardless of length. Length already shows up in the cost ledger.

---

## 9. Judge cost optimization

v0.1 cost ~$0.20 per session, ~25s per session. For weekly digests covering 30-100 sessions, that is $6-20 and 12-40 minutes per Sunday. Not acceptable.

v0.2 brings the cost down without using heuristics to decide which sessions matter. Every session gets LLM judgment. The optimization is in HOW that judgment is produced, not in WHICH sessions get it.

### 9.1 Two-pass judging

```
[ all sessions in window ]
   |
   v
[ pass 1: batched cheap-tier judge ]
   - 5 sessions per LLM call
   - cheap model (haiku-4-5 / gpt-5-mini)
   - compressed transcripts (Section 9.2)
   - emits: scores, rationales, moments candidates,
            and a self-flag "confidence: low|medium|high"
   |
   +---> sessions with confidence >= medium: scores accepted as final
   |
   +---> sessions with confidence == low: pass 2
                                            |
                                            v
                                          [ pass 2: frontier judge ]
                                          - one session per LLM call
                                          - frontier model (opus-4-7 / gpt-5)
                                          - same compressed transcript
                                          - re-scores from scratch (does not see pass 1)
```

Pass 1 always runs on every session. There is no skip path. A session the cheap model judged as "high confidence, overall 8.5" is accepted as final, not because heuristics said it was easy, but because the cheap model itself said it was easy and was specific about why.

Pass 2 runs on whatever pass 1 flagged. We expect this to be ~15-30% of sessions in a typical week. The frontier judge is told nothing about pass 1's output. It judges fresh. We use pass 2's scores, rationales, and moments.

### 9.2 The compressed-transcript path

For all judged sessions (both passes), compress the transcript before sending:

- Keep every user turn verbatim. The user's behavior is what we score.
- Replace assistant turns with a short summary: first 200 chars + "...[+N more chars, M tool calls]" if longer.
- Drop tool-result blocks entirely. Keep only the tool-call name + first 80 chars of arguments.

Effect: a 50-turn session that was ~80K input tokens compresses to ~12K. The moments excerpts (Section 4.4 substring check) still verify against the full transcript stored locally; the judge never sees the full one.

### 9.3 The confidence self-flag

The judge prompt is extended to require a `confidence` field on every session's output:

```json
{
  "session_id": "...",
  "scores": { ... },
  "rationale": { ... },
  "moments": [ ... ],
  "confidence": "low | medium | high",
  "confidence_reason": "<one short sentence>"
}
```

Calibration instruction inside the prompt:

- **high**: every dim has clear signal, no contradictions, transcript is long enough to ground each rationale.
- **medium**: most dims have clear signal but one or two are weak. Default to this when uncertain about an individual dim.
- **low**: transcript was ambiguous, very short, or the cheap model felt out of depth (e.g., a deep-architecture session where the cheap model could not assess fit). Triggers pass 2.

This is itself an LLM judgment, not a rule. The cheap model is being asked, in plain language, "do you want a second opinion." The product trusts that answer.

### 9.4 Pipeline ordering and batching mechanics

The weekly pipeline runs in this order:

```
scan -> cluster (one LLM call, Section 5.2) -> pass 1 batched -> pass 2 frontier on low-confidence -> moments validation -> digest render
```

Clustering runs BEFORE judging. The cluster assignment is then used as a batching constraint: pass 1 batches MUST NOT contain two sessions from the same task, to avoid the model anchoring across related sessions. This is a batching mechanic, not a decision about coaching content. If a single task has more sessions than can be spread across the available batches, the extras roll into batches alone or with non-task-mates only.

Within each batch, session order is randomized.

Each pass 1 call returns an array of N session objects matching the input order. Validate: if the response has the wrong number of objects, re-prompt once. If still wrong, fall back to one-session-per-call for that batch's sessions.

### 9.5 Cost target

- Pass 1: <$0.01 per session amortized (5 sessions × ~$0.005 each per batched call).
- Pass 2: <$0.08 per session via compression (down from $0.20).
- Escalation rate target: 15-30% of sessions go to pass 2.
- Aggregate target: <$0.03 average per session, <$1.50 per weekly digest for the median user.

### 9.6 Failure modes considered

- **The cheap model is uniformly over-confident**, marking every session "high" and never escalating. The frontier never gets called. We catch this in two ways: (a) on weekly runs, log the pass-1 confidence distribution; if "high" exceeds 90% over a 4-week rolling window, the prompt is auto-tuned (the calibration instruction in 9.3 is sharpened) and the user is shown a "calibration was off; re-tuned" line in the next digest. (b) `praxis week --frontier-only` forces every session through pass 2 for one-time sanity checking.
- **The cheap model is uniformly under-confident**, marking every session "low" and escalating everything. Costs balloon. Same telemetry catches this; if "low" exceeds 70% over 4 weeks, fall back to a stricter low-confidence definition in the prompt.
- **Pass 1 and pass 2 disagree wildly** on a session that did escalate. This is the expected case - pass 2's output wins. But the disagreement itself is signal about prompt calibration; persist both score sets in `session_scores` with a `judge_pass` column.
- **Batched judging produces correlated outputs across the batch.** Mitigated by random shuffle and the same-task exclusion in 9.4. Also: each session's prompt block starts with `--- session ${session_id} ---` to give the model a clean reset between sessions in the batch.

---

## 10. Cost-effectiveness as a first-class signal

v0.1 reports cost as a footnote. v0.2 promotes it.

### 10.1 The cost ledger panel

Always shown in the digest, between the headline moment and the task breakdown. Contents:

- **This week's spend**: total estimated USD across all priced models.
- **Baseline**: 90-day rolling weekly mean.
- **Biggest line**: which (model, task) pair drove the spend.
- **Tier-fit callout**: among Opus/GPT-5 sessions, how many had user_turn_count <= 3 AND avg_prompt_chars <= 200. Those are likely over-tier. Estimate the savings if those had run on Sonnet/gpt-5-mini.

### 10.2 The dollar_impact_estimate on moments

Each `Moment` carries an optional dollar impact. Compute it as:

- For a verification moment where the user accepted a bad output: estimate = sum of token costs of the next 2 turns after the lapse (the work the user had to redo).
- For a fit moment (over-tier): estimate = (frontier_cost - cheaper_tier_cost) for the session.
- For an iteration moment: estimate = token cost of the assistant's response that the user accepted prematurely.
- For planning/context/tools: usually None. We do not invent a number.

The estimate is conservative and rough. The HTML rendering MUST include the disclaimer "rough estimate from token volume + tier pricing" once at the bottom of the digest. Do not present these as invoiced costs.

### 10.3 What this enables

The digest can now say: **"This week's lapses cost you about $14 in wasted tokens. The biggest single one was the auth migration session on Wed: you accepted a 200-line SQL block without checking it touched the audit_log table."**

That sentence is what changes behavior. The 5.5/10 is not.

---

## 11. What gets cut

| Cut | Reason |
|---|---|
| The overall /10 in the masthead | Not actionable. Still computed and persisted; only de-promoted from the headline. The trajectory line is the new headline. |
| Daily consolidation table | Replaced by `weekly_digests`. |
| Heuristic-only mode | Locked-in decision. API keys required. The CLI errors with a clear message if no Anthropic/OpenAI key is set. |
| `--no-judge` flag | Removed. |
| `praxis install-daemon` (the daily one) | Removed. Replaced by `praxis install-weekly` (Section 12.4). |
| `praxis scan` as the primary verb | Becomes a hidden / advanced verb. The primary verb is `praxis week`. `praxis scan` still exists for power users who want to force a non-weekly refresh, but it does not render a report. |
| The 7th (structured output) dim | Already cut in v0.1.1. Stays cut. |

---

## 12. CLI surface

### 12.1 Commands

```
praxis week                         # primary verb. Generates this week's digest from current data, renders HTML + terminal.
praxis week --week 2026-W21         # render a past week.
praxis week --dry-run               # compute everything but do not write to DB or files.
praxis week --frontier-only         # force every session through pass 2 (frontier) for one-time sanity check.
praxis week --explain-judging       # print the pass-1 confidence distribution and which sessions escalated.
praxis re-score <session_stable_id> # force a fresh frontier judge of a specific session.
praxis scan                         # power-user: just scan and score, no digest. Used by the cron job.
praxis baseline                     # print the current 90-day baseline panel (no judge calls).
praxis follow-up                    # print the current follow-up commitment status without rendering the full digest.
praxis history                      # list past weekly digests with their dates and headline labels.
praxis show <week_iso>              # open a past digest's HTML.
praxis status                       # unchanged from v0.1.
praxis rubric                       # unchanged from v0.1.
praxis models                       # unchanged from v0.1.
praxis install-weekly               # set up the macOS launchd job (Section 13).
praxis uninstall-weekly             # remove it.
praxis config                       # print or edit ~/.praxis/config.toml.
```

### 12.2 Config file

`~/.praxis/config.toml`:

```toml
[schedule]
day = "sunday"           # any weekday name
hour = 18                # 0-23 local time
minute = 0

[scan]
since_days = 7
max_new = 200

[judge]
primary_provider = "anthropic"   # "anthropic" or "openai"
frontier_model = "claude-opus-4-7"
cheap_model = "claude-haiku-4-5"
# OpenAI fallback used automatically if anthropic credit / errors

[notification]
enabled = true           # macOS only; ignored elsewhere
sound = "default"

[privacy]
redact_secrets = true    # MUST default true (Section 4.4)
```

The config file is created on first run with these defaults. `praxis config` edits it via `$EDITOR`. `praxis config --get schedule.day` and `--set` are the programmatic forms.

### 12.3 Exit codes

- 0: success
- 1: generic error
- 2: no API key configured (after the v0.2 lock-in)
- 3: no sessions found in the window
- 4: cron / launchd installation failed

### 12.4 `praxis install-weekly` on macOS

Generates and loads a LaunchAgent plist at `~/Library/LaunchAgents/co.praxis.weekly.plist` that runs `praxis week --notify` at the configured day/time. The `--notify` flag post-runs `osascript -e 'display notification ...'` and writes the HTML to `~/.praxis/weeks/<iso_week>.html`.

On non-macOS, the command prints (and saves to `~/.praxis/install-weekly-snippet.txt`) the equivalent systemd timer or Task Scheduler XML, and exits cleanly without scheduling anything itself. Tell the user to install it.

---

## 13. Delivery surfaces

### 13.1 The HTML file

Written to `~/.praxis/weeks/2026-W21.html` (one file per ISO week). The file is fully self-contained: all CSS inline, no external resources, opens by double-click. `praxis show 2026-W21` runs `open ~/.praxis/weeks/2026-W21.html` (macOS) or the equivalent.

The `latest.html` symlink in `~/.praxis/` always points to the most recent week's file.

### 13.2 The macOS notification

Triggered only when running with `--notify` (set by the launchd job). Uses `osascript`:

```
display notification "Open ~/.praxis/latest.html to read." with title "Praxis weekly read is ready" sound name "default"
```

Notifications are best-effort: if `osascript` fails (notifications disabled, etc.), do not fail the run. Log it.

The notification text MUST include the headline trajectory label if the digest produced one, so the user gets the gist without opening: e.g., "Praxis: Drifting this week. Open for the detail."

### 13.3 The terminal digest

`praxis week` without `--notify` prints to stdout. ANSI colors, no notification, no HTML file regeneration (the HTML file is only written by the scheduled run unless `--write-html` is passed). This is the form factor for "I want to check on demand."

### 13.4 What is NOT delivered in v0.2

- Email. Deferred. Adding it later means adding one `notifier.py` module that reads from `~/.praxis/weeks/<iso>.html` and sends via the user's chosen SMTP/Resend. The HTML file is the single source of truth, by design.
- Markdown export. Deferred.
- Web dashboard. Deferred.

---

## 14. Storage schema migration

v0.1 has three tables: `session_scores`, `daily_consolidations`, `run_log`.

v0.2 changes:

```sql
-- KEEP, unchanged
CREATE TABLE session_scores ( ... );
CREATE TABLE run_log ( ... );

-- DROP after migrating any in-flight data
DROP TABLE daily_consolidations;

-- NEW
CREATE TABLE moments (
    moment_id TEXT PRIMARY KEY,
    session_stable_id TEXT NOT NULL,
    dim_key TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    quoted_excerpt TEXT NOT NULL,
    why_it_lost_score TEXT NOT NULL,
    suggested_alternative TEXT NOT NULL,
    dollar_impact_estimate REAL,
    minutes_impact_estimate INTEGER,
    severity TEXT NOT NULL CHECK (severity IN ('minor','moderate','major')),
    created_at TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_moments_session ON moments(session_stable_id);
CREATE INDEX idx_moments_created ON moments(created_at);

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,             -- sha256 of sorted member session ids[:16]
    label TEXT NOT NULL,
    task_type TEXT NOT NULL,
    project_hint TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    session_count INTEGER NOT NULL,
    total_cost_estimate_usd REAL,
    label_source TEXT NOT NULL CHECK (label_source IN ('llm','fallback'))   -- 'fallback' is the protocol-failure singleton path from Section 5.3
);
CREATE TABLE task_members (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    session_stable_id TEXT NOT NULL,
    PRIMARY KEY (task_id, session_stable_id)
);

CREATE TABLE weekly_digests (
    week_iso TEXT PRIMARY KEY,            -- "2026-W21"
    generated_at TEXT NOT NULL,
    trajectory_label TEXT NOT NULL,
    trajectory_headline TEXT NOT NULL,
    headline_moment_id TEXT REFERENCES moments(moment_id),
    cost_total_usd REAL,
    cost_baseline_usd REAL,
    snapshot_json TEXT NOT NULL,          -- full ProfileSnapshot as JSON
    html_path TEXT
);

CREATE TABLE follow_ups (
    week_iso TEXT PRIMARY KEY,
    dim_key TEXT NOT NULL,
    commitment_text TEXT NOT NULL,
    target_metric TEXT NOT NULL,
    baseline_value REAL NOT NULL,
    measured_value REAL,
    outcome TEXT NOT NULL CHECK (outcome IN ('improved','unchanged','worse','pending'))
);
```

Migration is one-shot: on first v0.2 run, detect v0.1 schema, run the CREATEs, DROP the daily table, write a `schema_version` row to `run_log` with value `2`. No data is lost from `session_scores`.

---

## 15. Acceptance criteria

A v0.2 build is shippable when all of the following are true. Each is testable.

### 15.1 Functional

1. `praxis install-weekly` on macOS creates a launchd job that fires `praxis week --notify` on the configured day.
2. `praxis week` produces an HTML file at `~/.praxis/weeks/<iso>.html` and a terminal render.
3. The headline of the digest is the trajectory label + one sentence, NOT the /10.
4. The "this week's moment" section contains a verbatim substring from a real transcript (validated by the substring check in 4.4).
5. The follow-up panel in week N shows the outcome of the commitment from week N-1, computed from data, not LLM judgment.
6. Cost ledger shows this week, the 90-day weekly baseline, and a tier-fit savings estimate.
7. Tasks panel groups sessions and labels each cluster.
8. No `<synthetic>` strings, no raw API keys, no obvious PII appears in any rendered digest.
9. The CLI exits with code 2 and a clear message if no API key is configured.
10. The `weekly_digests`, `moments`, `tasks`, `task_members`, `follow_ups` tables exist after first run.

### 15.2 Performance

1. On a typical 30-session week, `praxis week` completes in <120 seconds wall time.
2. On the same week, total LLM spend is <$2.
3. Pass 1 confidence distribution on a 4-week corpus of the spec author's history shows: 15-30% escalation to pass 2, with neither "high" >90% nor "low" >70% on any single week. (If outside this band, the prompt calibration needs another pass before shipping.)

### 15.3 Quality

1. Across 50 manually-reviewed moments on the spec author's own history, >= 90% have correct substring excerpts (the substring check passes).
2. >= 80% of those moments have suggested alternatives that the author would describe as "specific enough to act on" in a blinded review.
3. Trajectory labels are stable: the label does not change between two consecutive weeks on the same data unless one of the underlying slopes crosses the hysteresis threshold from Section 7.4.

---

## 16. Out of scope for v0.2

- Email, Slack, browser, IDE.
- Cohort or shared baselines.
- Cross-machine sync.
- Pulling live pricing.
- Re-judging older sessions when the rubric changes (the rubric is locked at v0.2; if it changes again, v0.3 will spec the migration).
- Custom dimensions / user-defined rubric.
- Multi-user / team-level digests.
- Anything in v0.1's "out of scope" section.

---

## Appendix A: Failure modes I want the implementer to actively think about

When implementing this spec:

1. The moments substring check is non-negotiable. If you're tempted to relax it for "near-matches," do not. Add the failed case to the test suite instead.
2. Secret redaction runs BEFORE the moment is persisted and BEFORE it's rendered. Two redactions are fine, zero is not.
3. The follow-up panel must use data, not the LLM, to decide whether the commitment was met. The LLM may write the prose around the outcome, but the outcome itself is computed.
4. The launchd job MUST be debuggable. Log to `~/.praxis/logs/weekly-<iso>.log` with stderr included.
5. Trajectory hysteresis exists for a reason. Do not "simplify" it out.
6. No heuristic substitute decides what gets coached, judged, or surfaced. Every session goes through LLM judgment (at minimum pass 1). Feature extraction is fine; feature-driven gating is not.
7. If the schema migration step fails, exit with a non-zero code and do NOT continue with a half-migrated DB. Print the path of a backup.

---

## Appendix B: Mapping to PM-review concerns

| PM concern | Section that addresses it |
|---|---|
| 1. Advice generic, data isn't | 4 (Moments engine) |
| 2. No feedback loop | 6.3 (Follow-up panel) + 13 (Weekly cadence) |
| 3. Unit of analysis wrong | 5 (Task clustering) |
| 4. Trajectory is footnote | 7 (Trajectory as headline) |
| 5. No comparison anchor | 8 (90-day baseline) |
| 6. Judge economics | 9 (Two-pass cheap-then-frontier + compression) |
| Cut the /10 masthead | 11 |
| Cut the daily daemon | 11 + 12.4 |
| Cost as first-class signal | 10 |
