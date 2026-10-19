# 03: measuring system understanding and supervisory judgment

Status: DESIGN, written 2026-10-19, before any data. It replaces
`docs/design/02-instrument.md` (write-it drills). That design was rejected by the owner on
two grounds, and both are binding here:

1. It measured code-level fluency (retyping a function body the agent wrote). That is the
   work the agent is for. The owner wants to keep understanding at the level of components
   and systems: what the parts are, how they connect, why they are shaped that way, what
   breaks when something changes, and when the agent's design is wrong.
2. It scored by the project's tests. Tests are code-level artifacts, often written by the
   agent itself (circular), they reward trial and error, and most design, infrastructure
   and data-model decisions have none. Nothing in this document uses a project test as a
   score, an item source or a symptom.

Nothing in this document is a claim about the owner. It fixes what will be observed, how it
is scored, what stays unknown, what would show the instrument measures the wrong thing, and
what must change in the repository first.

## Sources

Every number below carries one of these tags, or is marked "needs measuring" with the
measurement that settles it. Design choices (a probability, a count of items) are labelled
as choices, never as measurements.

- [LS] learning-science review (pretesting, calibration, structural knowledge, reliance).
  Primary sources are named inline.
- [SE] software-engineering review (comprehension, impact analysis, agent studies).
- [MS] measurement and statistics review (proper scoring, small-n power, n-of-1 design).
  Several of its numbers are derived by exact enumeration or closed-form arithmetic and say
  so.
- [RT] red-team review.
- [MX] hook mechanics, transcript structure and praxis code review, including two recorder
  defects found by running the installed hook.
- [CR] measurements made by the critic of the two draft designs on this machine, counts
  only, on the one local transcript (`~/.claude/projects/-home-user/6eb3e8a3-...jsonl`,
  Claude Code 2.1.302/2.1.303, an orchestration session, not one of the owner's coding
  sessions).
- "Measured here": run in this repository at commit 697b332 on 2026-10-19. The script that
  produced the pair-pool and reference numbers is to be committed as
  `experiments/E03/pair_pool.py` (section 13).

## 0. The task, rewritten

- Altitude. The outcome is not a score. It is that the owner keeps an accurate model of
  the systems their agents build and catches the agent when its design is wrong, without
  being re-tasked. Measurement exists to tell whether that is true and whether the
  intervention (predicting before the agent acts) helps.
- Depth. Prediction before action is two things that must be kept apart: an intervention
  with a known, item-specific effect (g = 0.54 on targeted material, g = 0.04 on anything
  else, St. Hilaire, Chan and Ahn 2024 [LS]) and an unvalidated measure. So the primary
  outcome is a separate, delayed, unassisted probe of system structure, scored by
  deterministic extractors, and the intervention is randomized per task.
- Range. Reliance research (decide first, score only disagreements), situation awareness
  (freeze probes), structural-knowledge assessment (Pathfinder), architecture recovery
  (reflexion models), single-case statistics (randomization tests, multiple baselines) and
  a red team each contribute a mechanism below.
- Belief. The hard version is a randomized, pre-registered, judge-free estimate on the
  owner's real repositories, with placebos, a known-groups check and a power calculation
  from measured inputs. Where that is out of reach (one small repository has too few
  items, section 3.5), the document says so and says what closes the gap.

## 1. The construct

### 1.1 What is measured

**System-level understanding.** How closely the owner's model of a system the agent built
for them matches the system as it is, answered unassisted and at a delay, at component
grain:

- which components exist and where their boundaries are;
- which components depend on which, directly and through others;
- which components run when a named operation of the system runs (runtime data flow);
- which components read or write which persistent data (data model), where an extractor
  for it exists;
- how that structure changed while the agent worked.

**Supervisory judgment**, in the parts that can be observed without a judge:

- anticipation: knowing, before the agent works, where its work will land;
- awareness: knowing, afterwards, what structural change the agent made;
- enforcement: noticing and acting when the agent breaks a boundary the owner declared.

### 1.2 What is explicitly not measured

- Code-level fluency: writing, retyping, fixing or recalling code, syntax or API names.
- Whether code passes tests. No project test is ever a ground truth, an item source or a
  symptom here, including tests the owner wrote.
- Self-rated understanding, as an outcome or as a validator. Explanatory self-ratings are
  inflated (Rozenblit and Keil 2002 [LS]) and relative metacomprehension accuracy is about
  +.24 (Prinz, Golke and Wittwer 2020 [LS]). Confidence appears only inside a probability
  that a proper scoring rule scores.
- Familiarity proxies as understanding: owner-authored lines, touch counts, recency. Before
  agents they explained about a quarter of the variance in self-rated knowledge (Fritz et
  al. 2010, R^2 = 0.25 [SE]), and agent-written code removes the basis of the inference
  (Wheeler 2026, argued, not tested [SE]). They appear here only as references that a
  measure of understanding must beat.
- Rationale and the correctness of a design choice. Only the chosen option is ever built,
  so the alternatives' outcomes are never observed (selective labels, Lakkaraju et al.
  2017 [MS]). Decision recall (section 4.6) tests memory of a structured decision, which
  is narrower, and is labelled that way. Rationale waits for a judge (section 4.9).
- "Will it take more than one attempt". The recorder has no attempt concept; its
  `reverted` column counts pre-session lines that are gone, not agent retries
  (`docs/design/01-recorder.md` [RT]). The item stays absent until an attempt is defined
  from observed rows and validated.
- The agent's habits as such. Knowing where this agent tends to edit is a model of the
  agent [RT]. The forecast (section 4.4) is scored only as skill over references that
  already know those habits.
- Anyone other than the owner.

## 2. The unit: the component

### 2.1 Definition

A component is a named set of repository-relative paths, defined by an ordered list of
glob rules in a versioned **component map**. The first matching rule assigns a path. A path
no rule matches belongs to `unmapped`, which is reported and never dropped or merged
(AGENTS.md invariant 4). `map_version = sha256(canonical JSON of the rules)[:16]` is
stamped on every derived row. A map defines nodes only: every edge comes from an extractor
(section 3), so the owner chooses granularity but never the answers.

### 2.2 Mechanical map (the default)

`praxis components derive` computes it from `git ls-files`, with no owner input.

1. Fixed classes first: `tests` (paths under `tests/` or `test/`, `test_*.py`,
   `*_test.py`), `docs` (`docs/`, `*.md`, `*.rst`), `config` (dotfiles, `pyproject.toml`,
   CI files, lock files). These are recorded in outcomes but are never forecast and never
   appear in structural items, because touching them reflects convention, not structure
   [RT, false improvement 2].
2. Product root: the Python package root if one exists (`src/` layout or top-level
   directories with `__init__.py`), otherwise the repository root.
3. Descend from the product root one level at a time. At depth d the components are the
   children at d (a module file, or a directory with everything under it). Choose the
   smallest d giving at least 5 product components; if d gives more than 30, use d - 1 and
   record a warning. Validity of structural-knowledge scores rose with the number of
   concepts from 5 to 30 (Trumpower, Sharara and Goldsmith 2010 [LS]). Fewer than 5 at
   every depth: the repository gets forecasts but no structural items, recorded as
   `too_few_components`, never padded.
4. A file directly in a directory that has components below it belongs to `<dir>/.`.

Measured here: the product root is `src/praxis`, and depth 1 gives 9 product components
(cli, doctor, drill, gitdiff, hooks, install, recorder, spans, store). Taking the
top-level package would give 1 component [MX]. In a flat package like this, depth-1
components are single files, so the forecast in section 4.4 is close to "which files will
the agent edit" [CR]. That is why an owner map that groups files into the owner's own
concepts is recommended before data (section 2.4, check V3), and why the forecast is never
primary.

### 2.3 Polyglot repositories

The map uses paths only and works for any language. The static extractor is Python-only
(`ast` is standard library). An item involving a component with no Python files is
`unknown` and is not generated; the runtime extractor (section 3.2) is language-neutral
at component grain only if the scenario's process is Python, and otherwise `unknown`. An
extractor for another language is added only after its own experiment.

### 2.4 Owner override and freezing

- The owner may write `$PRAXIS_HOME/components/<repo_id>.toml`, read with `tomllib`:
  `[[component]]` tables with an `id` and `globs`. It lives under `$PRAXIS_HOME`, never in
  the owner's repository (invariant 8 [MX]).
- `praxis components freeze` records `map_version`, source (`mechanical` or `owner`) and a
  timestamp. No task or probe is accepted in a repository without a frozen map. Freezing
  refuses fewer than 5 product components, which blocks the coarse-map move [RT, playbook
  (g)].
- A change opens a new `map_version` and a segment break: scores are never pooled across
  versions. The change carries a reason **code** from a closed list (`new_module`,
  `regrouped`, `renamed`, `correction`), never free text (G11).
- The mechanical map is always scored alongside as a sensitivity analysis, and every
  forecast score is also computed at file grain, so a grain mismatch shows as a gap between
  grains, not a hidden penalty [RT, false decline 5].

## 3. Ground truth: an extractor family

Ground truth comes only from deterministic, versioned extractors over the repository, or
from the recorder. Each is a derived artifact, never an observation: automated
architecture recovery is known to be inaccurate (Lutellier et al. 2015 [SE]), which is why
each extractor answers a narrow, checkable relation and never "belongs together". Each
extractor has its own experiment with a decision rule written before the run (section 13).
Extractors read source at analysis or probe time in memory and keep only identifiers and
counts (invariant 5).

### 3.1 X1: static imports (required)

- A file edge f -> g exists if f has an `Import` or `ImportFrom` resolving (relative
  imports included) to a module in g. Lifted to components through the map.
- Each component edge carries its import-site count and whether every site is
  function-level (inside a `def`) or at least one is module-level. Measured here:
  `spans -> store` is function-level only, and `drill -> gitdiff` has 2 sites; a
  one-site function-level helper import is incidental reuse, not a boundary dependency
  [CR]. D1 items are stratified by these two attributes and reported per stratum.
- Reach: the transitive closure of the component graph.
- Blind spots counted per component: `importlib.import_module`, `__import__`, string
  plugin registries, and `if TYPE_CHECKING` imports (counted separately). Any of these
  makes items involving the component `partial`. Static graphs miss dynamic, configuration,
  SQL and infrastructure wiring [SE], so every row carries the assumption that an absent
  edge may be an extractor blind spot.

Measured here, X1 on `src/praxis`: 16 edges among 72 ordered pairs (base rate 0.222);
`store` is imported by 5 components and `cli` imports 5. Of the 56 non-edge pairs, 7 reach
their target through another component and 49 have no path at all.

### 3.2 X2: runtime component traces of owner-named scenarios (candidate)

The owner names real operations of their system as scenarios in
`$PRAXIS_HOME/scenarios/<repo_id>.toml` (an id and a command; for praxis, `praxis hook`
fed a recorded fixture payload). praxis runs each in a scratch clone (section 3.5) with a
standard-library `sys.setprofile` hook that records, per scenario, which components'
functions were entered and which component called into which. Stored: scenario id, a hash
of the command, component ids, call-edge counts. Not stored: the command text in the
ledger (it lives only in the owner's own file), any argument, any value. This covers
runtime data flow without a judge and without any test assertion. Both drafts treated
data-flow items as judge-only; a search of the standard library (`sys.setprofile`,
`sys.settrace`) shows they are not [CR]. Coverage, overhead and determinism need measuring
(E05).

### 3.3 X3: persistent data (candidate)

For SQLite: execute the repository's DDL in `sqlite3.connect(":memory:")` and read
`PRAGMA table_info` and `PRAGMA foreign_key_list`, giving tables and their references.
Table access per component: during X2 scenarios, `sqlite3.Connection.set_trace_callback`
attributes each executed statement's table names to the component on the call stack.
Stored: table names (identifiers) and counts, never statement text. Other stores (Postgres,
files, queues) are `unknown` until an extractor exists. Needs its own experiment (E06).

### 3.4 Edge history from the recorder

The recorder stores hashes, so it cannot rebuild a past import graph from its own rows, and
a past working tree is recoverable only where it was committed. Both drafts relied on
`git show` of committed trees, which misses uncommitted agent work. So the recorder gains
one duty (section 5): at each Stop, for Python files the segment changed, run X1 on those
files and store their edges keyed by the file's content hash, under the hook's time
budget. Over budget, that boundary's edges are `unobservable`, never guessed. Cost needs
measuring (E04, extending `experiments/E01/hook_cost.py`; current hook p50 71.5 ms and p95
92.3 ms [MX, `docs/design/01-recorder.md`]).

### 3.5 Scratch clones, item supply and the pool limit

Anything that must execute (X2, X3, the knockout probe, agent replay) runs in
`git clone --local` copies under `$PRAXIS_HOME/scratch/`, deleted when the run ends. A
`git worktree add` would write `.git/worktrees/` into the owner's repository, so it is not
used.

An **item** is (relation, A, B[, scenario or table]). Relations: D1 direct import, D2
reach-through, RT runtime call or execution, TB table write. The item supply is finite and
binding:

- Measured here: 72 ordered D1 pairs. D2 has 7 positives against 49 negatives, so a
  constant 0.1 forecast nearly aces it; D2 is reported but never pooled into the primary
  outcome without the R_layer reference beside it.
- An item is consumed by its first exposure (section 6). It is re-admitted as a new item
  only when its truth flips (a new epoch), which the recorder's edge history detects.
- At 4 items consumed per task (section 6.2 defaults), praxis's D1 pool alone lasts 18
  tasks (72 / 4, derived).
- `praxis components pool` reports the supply per repository and relation. The power
  simulation (section 6.6) takes the supply as an input, and a repository whose supply
  cannot reach the simulated count over the study is declared infeasible for the
  confirmatory analysis, not run underpowered.

## 4. The measures

Shared conventions:

- Every probability is on the grid {0.1, 0.3, 0.5, 0.7, 0.9}. On this grid the Brier score
  keeps honest reporting optimal (the nearest grid point is best for every one of 999
  beliefs except 2 exact-midpoint ties); the log score does not (31 of 999) [MS, derived].
  The log score is stored as a secondary number only, with the clip named in the
  assumptions field.
- Brier of one binary forecast: `BS = (p - o)^2`. A set forecast is one binary forecast per
  element, summed; this is strictly proper for the vector of marginals and does not reward
  getting the joint dependence right, which every row says [MS]. Jaccard, F1, precision and
  recall are descriptions only: they are not proper (the Jaccard-optimal report omits
  components believed at 0.35 each [MS, derived]). Jaccard of two empty sets is NULL.
- Skill: `BSS_ref = 1 - sum BS_owner / sum BS_ref` over the same forecasts, against a
  reference fitted only on data dated before each forecast. A reference that cannot yet be
  fitted gives BSS = NULL, never a fallback to another reference.
- Decomposition `BS = REL - RES + UNC` over the five grid values, with the Ferro and
  Fricker (2012) bias correction, the same estimator throughout [MS]. Reliability diagrams
  by CORP (isotonic regression by pool-adjacent-violators, standard library) with
  resampled consistency bands [MS]. Resolution is reported with calibration, never
  calibration alone, because always forecasting the base rate games calibration (Murphy
  1973 [LS, MS]).
- A diagram bin is interpreted only with at least 81 forecasts (the count that resolves a
  0.10 miscalibration at p = 0.7 to a 95 percent half-width); detecting it with 80 percent
  power needs about 165, roughly 400 to 800 forecasts over five bins [MS, derived].
- Within-task forecasts are correlated: `n_eff = n_tasks * m / (1 + (m - 1) * ICC)`; the
  ICC needs measuring [MS].
- Every derived row carries the Evidence fields as columns with CHECK constraints: claim
  class, coverage, a non-empty source list (session id and turn index, or extractor id and
  tree hash), reducer version, assumptions, warnings (invariant 2). There is no separate
  `evidence` table today (searched: `CREATE TABLE` in `src/praxis/store.py`); each derived
  table carries the columns.
- Unanswered, skipped, refused (agent attached), late and void are values with reasons,
  never 0 and never 0.5.

### 4.1 P1: delayed structural probe (primary outcome)

**(a) What the owner does.** Runs `praxis probe` in a terminal in the repository, whenever
they choose, with no agent attached: the `attached_agents` check moves from `drill.py` to
`unassisted.py`, an attached agent refuses the probe, and the refusal is recorded. praxis
shows due items one at a time with a visible clock. Each names a relation and its
components (with their globs) and asks one closed question on the grid, for example "Does
code in `recorder` import code in `gitdiff`?" or "When scenario `hook-edit` runs, does
`recorder` call into `store`?"

An item is due once its exposure (section 6) is at least L days old and at most L_max days
old. L = 7 days is a design choice following the one-week delayed test of Richland, Kornell
and Kao Exp 4 [LS]; L_max = 28 days is a design choice. The diagnostically best L needs
measuring (Q9). An item that passes L_max unprobed is `probe_missed`, and missingness by arm
is reported as a check on differential attrition.

**(b) Truth.** X1 on the working tree at probe time for D1 and D2; X2 and X3 on the HEAD
tree, cached by tree hash, for RT and TB. HEAD sha, tree hash, a dirty flag and the
extractor version are stored with each response. An item whose truth changed between its
exposure and its probe is flagged `truth_changed_since_exposure` and analysed separately:
it tests whether the owner noticed the change.

**(c) No feedback during the study.** P1 is the criterion, so no per-item answer is shown
at probe time; feedback would turn the criterion into memorizing an answer key. To limit
what wrong answers plant, a debrief at the end of each pre-registered block shows every
probed item's truth (the planting risk in the literature is for multiple-choice lures,
Roediger and Marsh 2005 [LS]; P1 items are binary). The owner can always read the code;
that is stated, not prevented.

**(d) Response deadline.** Each item has a deadline set in the Stage 1 pre-registration
from the pilot's response-time distribution. `grep import` answers any D1 item in seconds
[CR], so a late answer is `slow` and excluded from the primary analysis; undetectable
lookup is an assumption on every row.

**(e) References** (all fitted on data before the item):

- R_base: the current graph's density per relation, one p for every item.
- R_layer: knows only a topological layer order of the earlier graph; forward pairs get
  the earlier forward edge rate snapped to the grid, backward pairs 0.1. Measured here:
  BS 0.130 against R_base's 0.173, BSS 0.248. A two-rule hub forecaster (`store` is
  imported by everything, `cli` imports everything) scores BSS 0.106. So BSS against
  R_base mostly measures layering, learned once.
- R_fam: for pair (A, B), an isotonic (PAV) function, fitted on earlier probe outcomes, of
  the share of the owner's bound segments in a trailing window that touched both A and B.
  NULL until fitted.

**(f) Scoring.** BS per item and per set; BSS against each reference; REL, RES, UNC;
CORP. The confirmatory statistic is a randomized arm contrast (section 6.3), not a level.

**(g) Claim class, coverage.** Truth: derived. BS: derived. BSS and every arm contrast:
comparative. No row is labelled "understands" or "learned"; report text says "accuracy on
structural items about components bound to tasks", no more (invariant 3). Coverage:
`full` (both components fully parsed or traced), `partial` (a blind-spot marker or parse
failure), `unknown` (not generated).

**(h) Stored.** `probe_items`: item id, repo id, map version, relation,
A, B, scenario or table id, epoch, stratum, exposure task id and condition. `probe_responses`:
item id, p, seconds, deadline met, HEAD sha, tree hash, dirty flag, truth, site count,
function-level flag, coverage, the attached-agent result. Read and discarded: source files,
traces.

**(i) Minimums.** No P1 number for a repository with fewer than 5 product components.
Weekly counts and coverage are always reported; scores only once the Stage 2 minimums from
the power simulation are met.

### 4.2 S1: drift-notice probe (awareness of structural change)

A freeze probe in the SAGAT family, which has the strongest sensitivity evidence in the
situation-awareness literature: about 94 percent across 243 studies (Endsley 2021 [LS]).

- Weekly, inside `praxis probe`, praxis shows k items: true changed edges (pairs whose X1
  or X2 truth changed during the past week, from the recorder's edge history) mixed with
  foils (unchanged pairs matched on co-touch familiarity bucket). The owner gives, per
  item, a grid probability that it changed this week. k and the mixing share are design
  choices; drift supply needs measuring (Q8).
- Scored by summed binary Brier, weighted by inverse sampling probability so the weighted
  mean estimates Brier over all pairs. Reference: R_drift, the share of pairs that changed,
  per familiarity bucket.
- Feedback follows the response here (it is not the criterion; noticing structural change
  is the skill to train), so S1 is also an intervention, applied equally in both arms.
- Claim class: derived; skill comparative. Labelled "awareness of structural change",
  situation-awareness level 1 at structural grain.

### 4.3 S2: boundary enforcement (reflexion model, associative)

After Murphy, Notkin and Sullivan 1995 [SE].

- The owner declares, once per map version, a closed-vocabulary rule per ordered pair of
  product components: `allowed`, `forbidden`, or nothing (`undeclared`, the unknown value;
  never defaulted to either). Frozen and versioned like the map.
- After each segment, the recorder's edge history lists divergences the agent introduced:
  a new edge on a `forbidden` pair, attributed to an agent delta in the segment.
- Outcome per divergence: `caught` if, before the session ends, the edge is gone again or
  the owner logged an amendment (`praxis components allow A B`, a decision row); otherwise
  `unnoticed`. praxis says nothing about divergences during the session; it reports them
  in `praxis last` afterwards (both arms).
- Reported: divergences per week, share caught, and the share of declared pairs that are
  `forbidden` (a model with nothing forbidden is vacuous and is flagged).
- Claim class: associative. Assumptions on every row: an edge can be removed for reasons
  unrelated to noticing; an owner can accept a divergence deliberately without logging it.
- This is a normative use of reflexion models. The SE review proposes reflexion as a
  descriptive measure (stated edges against actual edges). Scoring a normative
  declaration against actual edges penalizes the owner for the agent's violations, so
  descriptive accuracy stays in P1 and S2 scores only enforcement.

### 4.4 F1: component forecast (the candidate instrument, treatment arm only)

**(a) What the owner does.** In `praxis task`, treatment arm only, before writing the
prompt: praxis draws r product components at random (r a design choice fixed from the cost
measurement, E07) and the owner gives a grid probability for each that the agent will
change it in the scored segment. A blank stays `unanswered`. There is no keystroke that
fills unmarked components with a value (that is a default by another name, invariant 4).
Summed binary Brier stays proper per component, so sampling components is sound, and it
cuts friction [MS].

**(b) Truth.** Two outcomes, kept apart because one change request has no single actual
impact set (Arnold and Bohner 1993 [SE]):

- `o_first(c)`: c was touched (at least one agent delta, positive or negative, on a file
  mapped to c) in the scored segment (section 5.3). Calls flagged as commits are excluded.
- `o_final(c)`: files mapped to c hold net agent lines at the session's last boundary,
  relative to the session's base commit (section 5.1).

When they differ and the owner predicted `o_final`, that is a supervisory event, not a miss
[SE]. A segment with zero footprint is counted for coverage and excluded from Brier.

**(c) References.**

- R1 (headline): `p(c | s) = (k_cs + 1) / (n_cs + 2)`, where s is whether the bound prompt
  names c (a path, a module name or a component id, matched as a whole path or whole
  identifier, never a substring: `cli` is not in `client`, `store` is not in `restore`
  [CR]), n_cs counts earlier bound segments in this repository with that s, and k_cs those
  in which c was touched. The +1/+2 prior is a design choice. The prompt is read from the
  transcript at analysis time and discarded; only the boolean is stored. The owner forecast
  before writing the prompt, so R1 knows more than the owner did: beating it means knowing
  more than the prompt gave away [RT, false improvement 1]. A history-only predictor
  already reaches about 26 percent recall and 30 percent precision on files (Zimmermann et
  al. 2004 [SE]).
- Secondary, never chosen after seeing results: R0 (base rate per component) and R_last
  (0.9 for components touched in the previous bound segment, 0.1 otherwise).

**(d) Reported.** BS; BSS against R1, R0, R_last; REL, RES, UNC; CORP; recall and precision
at p >= 0.5 as descriptions (the documented human failure is predicting too small a change
set, Lindvall and Sandahl 1998, recall 0.76 and precision 0.96 as reported second-hand and
unreconciled with a one-in-three figure [SE]); `o_first` and `o_final` separately; at
component and file grain; stratified by R1's expected touched count, never by observed
size [MS, RT].

**(e) Claim class.** BS derived, BSS comparative. Labelled "calibration on forecasts of
agent work". It is not labelled system understanding unless V8 passes, F1 skill converges with P1 per
repository per week (V7), and a commit cites the result. Calibration training transfers weakly or not at all (Lichtenstein and
Fischhoff 1980; Martin and Mandel 2025, N = 610 and 871; Jorgensen and Gruschke 2009
[LS]), so a rising F1 is evidence about F1 only.

**(f) Exclusion and flags** (computed at analysis time from the transcript, stored as
booleans with session id and turn index):

- `agent_previewed`: in the same session, within a short window before registration (the
  window set from the pilot distribution of registration-to-prompt gaps), an assistant text
  block named, as a whole path or identifier, a component the owner then forecast at 0.7 or
  0.9, or a read-only planning exchange (plan mode, ExitPlanMode, or an end_turn with no
  mutating call) occurred. Both drafts' broader rules exclude nearly everything: 25 of 44
  main-thread assistant text blocks name a praxis module (22 as whole words), and 7 of 18
  human prompts were answered by an end_turn with no mutation [CR].
- `late_prediction`: the forecast is not strictly before the bound prompt's first assistant
  entry. Rejected, not scored as wrong.
- `owner_answered_question`, `non_human_turn_in_segment`, `owner_edited_in_segment`,
  `interrupted`, `recorder_gap` (a mutating PreToolUse without PostToolUse, or an
  `unobservable` or `error` hook row), `codex_unrecorded` (before the apply_patch fix),
  `version_change` (agent CLI version or model differs from the previous task: a new
  analysis segment [RT, Bansal et al. 2019]).
- The Stage 1 pre-registration fixes a maximum exclusion rate. If the pilot exceeds it,
  flagged tasks form a sensitivity stratum instead of being excluded.

**(g) Placebo.** Forecasts shuffled among tasks within a repository (seeded): the true
pairing's BSS_R1 must exceed the 95th percentile of the shuffled distribution. A forecaster
who knows only which components are generally busy passes the R1 check and fails this one.

**(h) Stored.** Task id, arm, sampled component ids, p per component, `o_first` and
`o_final` (derived rows), `prompt_named` per component, flags, map version. Read and
discarded: the bound prompt text and transcript structure (a 5.84 MB transcript parsed with
`json` in 38.1 ms [MX]).

### 4.5 S4: detection latency (associative, descriptive)

No owner input. Among agent line hashes added in any segment and later removed within a
pre-registered window W_d, the share removed in the same session, per week, with a binomial
interval; and among removals, the share by owner lines against by the agent under a later
prompt. Removal is detected by multiset lookup in later snapshots (the E02 mechanism, with
multisets). Associative: removal also follows refactors and changed requirements, stated on
every row; the share of tasks touching more than one product component is printed beside
it. No owner-facing calibration is ever computed from this (see the dropped M4, section
14).

### 4.6 S3: decision recall (conditional on supply)

- Source: only structured decisions. AskUserQuestion (`questions[].options[].label` and the
  answers) and ExitPlanMode (the approved plan) [MX]. At PostToolUse the recorder stores a
  pointer: session id, tool_use_id, tool name, option count k, chosen index, sha256 of the
  canonical option labels. Free-text option lists are excluded: telling offered options
  from steps needs a judge (of 44 assistant text blocks, 9 had numbered lines and 0 had
  option labels [MX]).
- Administration in `praxis probe`: free recall first (the owner types what was decided and
  why; the text is displayed and discarded, and nothing about it is kept except the boolean
  `recall_attempted`); then the real options in a seeded shuffle, with a grid probability
  per option; then immediate corrective feedback. Guessing first is what builds memory
  (g = 0.65 with guessing against 0.22 without [LS]) and feedback limits lure intrusion
  (Butler and Roediger 2008 [LS]).
- Void with reason: hash mismatch (`transcript_changed`), file gone (`transcript_swept`),
  or the decision no longer holds in the tree (`reversed`, by multiset survival of the
  following prompt's agent hashes; the survival threshold needs measuring). Quizzing a dead
  decision trains recall of a mistake [RT].
- Window: between L days and 30 days after the decision, because Claude Code deletes
  transcripts older than `cleanupPeriodDays`, default 30 [MX, RT]. Raising it means
  editing `~/.claude/settings.json`, offered only through `praxis setup --apply` with
  per-file consent (invariant 8). Codex has no documented AskUserQuestion equivalent
  [MX]: `unobservable`.
- Scoring: multi-category Brier over the k options, with the 1/k floor printed beside it.
  Derived. Labelled "memory of a structured decision", never "judgment". Items are drawn
  by seeded draw from all eligible decisions, never chosen by the owner, and labelled
  gameable (a reread of the transcript cannot be detected under relatime [RT]).
- Supply: 0 AskUserQuestion and 0 ExitPlanMode calls among 241 tool uses in the one local
  transcript [MX]. If the pilot yields fewer than the Stage 1 minimum, S3 is counts only.

### 4.7 K: knockout localisation (validation criterion only)

A convergent check from a different method, with no tests and no code-level fault. In a
scratch clone, praxis makes every public name of one randomly drawn component raise
`RuntimeError("praxis knockout")` on use, runs the owner's named scenarios (X2), and shows
only which scenario ids failed. The owner gives a grid probability per component that it
was knocked out. Truth: the drawn component. Reference: uniform 1/m, the true prior.
Multi-category Brier. Used only in V7. A weakness, stated: where scenario names map
one-to-one onto components the probe is a lookup; scenarios are therefore named by the
owner for operations, not modules, and the share of scenarios that execute more than one
component is reported. The owner's working tree is never written.

### 4.8 C1: Pathfinder map (convergent only)

Every P weeks (a design choice; cost needs measuring), the owner rates every unordered pair
of product components for relatedness, 1 to 9 (36 pairs for praxis's 9 components,
measured here), converted to distance d = 10 - rating. The owner's network is
PFnet(r = infinity, q = n - 1), computed by a Floyd-Warshall pass with max in place of sum
(standard library). Similarity to referents N_imp (an undirected link wherever X1 has an
edge) and N_co (co-change above the median pair) by neighbourhood closeness. The closeness
formula was written from memory in the draft and must be checked against Goldsmith and
Davenport before it is frozen (Q13); until then C1 is not computed. A node whose
neighbourhood union is empty is NULL. Whether code-derived referents stand in for expert
referents is open: the literature validated against experts (Acton, Johnson and Goldsmith
1994 [LS]), and concept maps share little variance with other tests (r = .31 [LS]).
Derived; never an outcome.

### 4.9 Judge-dependent measures (not built until a judge exists)

Each needs `src/praxis/judge.py` and the redaction beside it (invariant 6). There is no API
key today, and none enters the primary path.

- J1: teach-back of a component's role and failure modes, scored blind against a rubric.
  An explanation gate cut later unassisted maintenance failures from 77 to 39 percent in
  novices (Sankaranarayanan 2026, N = 78 [SE]).
- J2: decision items from free-text option lists.
- J3: classifying owner redirections as design-level or cosmetic. Explicit user correction
  resolved 91.49 percent of visible agent misalignments (Tang et al. 2026 [SE]).

Data-flow items are no longer in this list: X2 covers them without a judge.

## 5. Recorder prerequisites (first, before any outcome data)

Any outcome computed before these changes is invalid, because two measured defects
mislabel exactly the quantities F1, S2 and S4 read [MX].

### 5.1 Fixes

- Codex edits: add `apply_patch` to `MUTATING_TOOLS` (`recorder.py` line 17). Measured by
  running the installed hook with a payload built from the Codex docs: an apply_patch edit
  was attributed to the owner [MX]. A payload recorded from a real Codex binary is still
  needed.
- Commits: diff against the session's base commit (`git diff <base_sha>`), recorded at
  SessionStart, instead of HEAD (`gitdiff.py` lines 101-111). An agent's `git commit` then
  produces no delta. Measured: today an agent commit reads as the agent deleting its own
  lines and touching every committed file [MX]. Excluding commit calls instead loses real
  edits in `sed -i ... && git commit -am` [CR]. Record HEAD at every snapshot; a HEAD move
  to a non-descendant (checkout, rebase) makes later boundaries `unobservable`; a
  fast-forward outside any tool call (an owner pull) sets coverage `partial` with the
  warning `head_moved_outside_tool`. This removes recorder assumption 3 ("lines are
  compared relative to HEAD"). Needs its own experiment (E03b).
- Duplicates: delete `Store.agent_hashes` (lines 426-448), which returns a set and so flags
  every identical line (invariant 13 [MX]; E02 notes the blank-line inflation). Survival
  lookups (S3, S4) use a multiset lookup.

### 5.2 New fields and duties

- Store `prompt_id` (Claude), `turn_id` (Codex), `permission_mode`, `agent_id` and the
  agent CLI version where the payload carries it, on `hook_events` and `agent_deltas`.
  prompt_id matches the transcript's `promptId` and tool_use_id matches `tool_use.id` [MX].
- At each Stop: X1 edges of Python files the segment changed (section 3.4), under budget.
- At PostToolUse for AskUserQuestion and ExitPlanMode: the decision pointer (S3).
- At the Stop that closes a registered task's scored segment: one read of `tasks` and the
  feedback notice (section 5.4).

### 5.3 The scored segment

Measured on the one local transcript: of 18 human prompts, 7 were followed by no mutating
call before the next human prompt, 5 by Bash only, and 6 by any Edit or Write [CR]. A
segment that ends at the next human prompt is therefore often empty. Definition:

- It starts at the bound prompt and ends at the first Stop (not SubagentStop) after at
  least one mutating agent delta. Owner replies to an agent question in between do not end
  it; they set `owner_answered_question`. Non-human turns inside it set
  `non_human_turn_in_segment` (UserPromptSubmit also fires on scheduled tasks, subagent
  reports and peer messages, and the payload does not mark typed prompts; the transcript's
  `origin.kind` does [MX]).
- The empty-segment rate per week is reported. Representativeness of the 7/5/6 split needs
  measuring on the owner's coding sessions.

### 5.4 Feedback channel

The pretesting benefit depends on prompt feedback (Hays, Kornell and Bjork 2013 [LS]),
though delayed feedback still worked for trivia (Kornell 2014 [LS]), so latency is a
measured covariate. Both drafts relied on the next `praxis task` alone, which can be hours
later or never. Claude Code hooks accept a `terminalSequence` output field (OSC 0, 1, 2, 9,
99, 777 or BEL) that Claude Code itself writes to the owner's terminal, interactive only;
"a successful hook's stdout is never shown in the transcript" (`code.claude.com/docs/en/hooks`,
JSON output table and "Emit terminal notifications"; read 2026-10-19). So at the Stop that
closes a registered task's segment, in both arms, the hook may emit one notification from a
closed vocabulary ("praxis: feedback ready"), with no component id or other content. This
needs an amendment to invariant 7 and to the no-stdout promise in `hooks.py` lines 1-8. It
never alters agent output bytes or exit code. Before use, an integration test on 2.1.303
asserts that the transcript holds no praxis string after such a Stop. Codex support needs
measuring; until then Codex feedback is `praxis last` only. `systemMessage` is not used:
whether it reaches model context needs verifying.

## 6. Elicitation, intervention and control

### 6.1 `praxis task`

1. Before writing a prompt for a piece of work, the owner runs `praxis task` in a terminal
   in the repository. The name is neutral on purpose: the same ritual in both arms should
   not cue silent prediction in control tasks [CR].
2. It refuses without a frozen map.
3. It draws the task's bound items by the pre-task rule (6.2), then the arm from a
   generator seeded with the committed seed, the repo id and the task ordinal.
   P(treatment) = 0.5 is a design choice that maximizes contrast for a fixed task count.
4. Treatment: the owner enters the F1 forecast over r sampled components and answers the
   k_e bound items as prequestions (grid probabilities). Control: "registered", nothing
   asked.
5. One append-only row with a timestamp. A second `praxis task` while one is unbound voids
   the first as `superseded`. Rows are never edited.

`praxis task` is a command, not a hook: its failure blocks nothing, and the prompt then
counts as unregistered in the coverage denominator.

Why this mechanism [MX]: a UserPromptSubmit cue puts the request, and so the answer, in the
agent's context (sycophancy toward stated user beliefs is documented across five
assistants, Sharma et al. 2024 [RT]); a prediction typed into the prompt always reaches the
agent, since UserPromptSubmit cannot rewrite the prompt; and PreToolUse `defer` is ignored
in interactive Claude Code. A separate command is the only option where the agent never
sees the prediction and the words are the owner's by construction.

### 6.2 The pre-task binding rule (identical in both arms)

- Region: the h product components with the highest trailing touch rate in this
  repository (h = 3, a design choice). With no history, the region is empty.
- k_e bound items: drawn uniformly from unexposed items (current epoch) with at least one
  component in the region; if fewer remain, filled from a uniform stratum over all
  unexposed items. Stratum recorded.
- k_r related-untested items: unexposed items sharing exactly one component with a bound
  item. Never asked before the work and never shown in feedback, in either arm.
- k_p drawn-not-shown items: uniform over unexposed items, recorded, never asked or shown.
- Defaults k_e = 2, k_r = 1, k_p = 1 are design choices; their cost and the pool they
  consume need measuring (E07, section 3.5).
- After the segment, whether each bound item's components were actually touched is
  recorded. It is a post-randomization variable, so analyses conditioned on it are
  exploratory only.

This answers the fatal flaw in draft A, whose exposure pairs were random and unrelated to
the work [CR]: the items are drawn where the agent is working, the same way in both arms.
It does not make them task-specific; a rule that reads the prompt cannot run before the
prompt exists.

### 6.3 Arms, feedback and estimands

Feedback, after the segment, through the notification and `praxis last` (also printed at
the start of the next `praxis task`), the same in both arms: the components touched
(`o_first`, and later `o_final`), the structural changes the agent made (edges added or
removed, from the edge history), any S2 divergences, and the truth of the k_e bound items.
Treatment also sees its own forecasts and prequestion answers against the outcome,
high-confidence misses first (the hypercorrection effect, Butterfield and Metcalfe 2001
[LS]). Feedback never goes through agent context.

So the contrast is predict-then-see against see-only, the pretest against extended-study
design (Richland, Kornell and Kao 2009 highlighted the tested concepts in the control
condition to separate testing from attention [LS]).

Estimands, each on first delayed P1 probes:

- **Primary, specific:** `Delta_spec = mean BS(bound items, control tasks) - mean
  BS(bound items, treatment tasks)`, items with unchanged truth, deadline met.
- **Secondary, near transfer (randomized):** the same contrast on k_r related-untested
  items. The literature predicts about g = 0.17 for related untested questions [LS].
- **Secondary, untargeted (randomized):** the same contrast on k_p drawn-not-shown items.
  The literature predicts about g = 0.04 [LS].
- **Pipeline placebo:** drawn-not-shown against never-drawn items, within arm, must not
  differ. A difference means sampling, scheduling or analysis leaks, and every P1 contrast
  is suspended until it is found.
- **Fidelity:** the F1 forecast and prequestion completion rates, and feedback latency.

Test: a randomization test that re-draws arms under the actual allocation rule, with the
per-task mean BS as the unit (respecting within-task correlation), Monte Carlo with a
committed seed and draw count, exact enumeration when there are 70 or fewer allocations
[MS]. One-sided. alpha is a design choice for Stage 1; this document proposes 0.05.

The bundle is estimated as a bundle: forecast plus prequestions. Separating them needs a
later factorial variant (Q10).

### 6.4 The general effect: multiple baseline across repositories

Task-level randomization cannot estimate a general effect, because the owner's model of
the system is shared across arms [CR]. Weekly on/off designs assume effects that wear off,
and learning does not (Kravitz et al. 2014 [MS]). The design for an irreversible effect is
a multiple baseline across repositories with randomized start points (Koehler and Levin
1998 [MS]):

- In each of at least 3 repositories, tasks are registered from the start, but all are
  control until that repository's randomly drawn start week; from then on, task-level
  randomization at P(treatment) = 0.5.
- Outcome: BS on never-exposed P1 items per repository per week, level change at the start
  point, by the Koehler-Levin randomization test over start points.
- Spillover check: bound-item BS of control tasks after the start against bound-item BS
  of the (all-control) registered tasks before it; bound items are drawn by the same rule
  before the start. Once predicting is a habit, the owner may predict silently in control tasks,
  biasing the specific contrast toward zero [CR].
- If the owner actively works in fewer than 3 repositories (needs measuring, Q1), the
  general effect is descriptive only.

### 6.5 The weekly mode trial

The tutor, collaborator and delegate trial in `docs/preregistration.md` uses P1 as its
outcome. Its current design (8 weeks, pair-blocked) has 16 allocations and a smallest
one-sided p of 0.0625, so it cannot reach 0.05; unrestricted 4-of-8 gives 0.0143 and 12
pair-blocked weeks give 0.0156 [MS, derived]. Stage 2 must pick an allocation that can
reach its alpha, or the multiple-baseline design. Tutor mode's "PreToolUse defers Edit"
does not work interactively [MX] and needs redesign. The mode trial waits until the
instrument passes section 8.

### 6.6 Power, written down before the data

- The pretesting effect sizes are on proportion correct for other material; their size in
  Brier units for this owner needs measuring.
- Arithmetic as an illustration (normal approximation, two-sided 0.05, independent items,
  derived here): 24 first probes per arm give power 0.46 for g = 0.54 and 0.16 for
  g = 0.28 [CR]; 80 percent power needs about 54 items per arm for g = 0.54 and about 200
  for g = 0.28. At k_e = 2 that is about 54 and 200 registered tasks in total, and about 108
  and 400 consumed bound items, against praxis's 72 D1 pairs.
- So a conceptual-size effect is out of reach on one repository of praxis's size with
  imports alone. What closes the gap: more relations (X2, X3), truth-flip re-admission,
  pooling across repositories, and the measured registration rate. The Stage 2
  pre-registration sets the counts from simulation through the exact test, using measured
  per-item BS SD, ICC, between-week SD, lag-1 autocorrelation, registration rate, skip rate
  and pool supply [MS]. Until measured, the priors are the derived bounds on calibrated
  per-item Brier variance: at most 1/16 at fixed p, about 5/64 with variation across items
  [MS].

## 7. Fail open and privacy

- Hooks keep exiting 0 whatever happens. New hook work (edges at Stop, the decision
  pointer, the notification read) runs inside the existing fail-open path; any exception
  becomes a `hook_events` row with outcome `error`, and the affected task or boundary is
  later void with its reason. The added latency needs measuring (E04).
- Owner input is closed vocabulary: grid probabilities, component ids, option indices,
  reason codes, `allowed`/`forbidden`. No TEXT column holds owner input. Free recall in S3
  is displayed and discarded, not even its length stored. GitGuardian counted a roughly
  3.2 percent secret-leak rate in Claude Code commits against a roughly 1.5 percent
  baseline [RT], which is why no free-text field exists.
- The transcript reader (`transcript.py`) returns derived structures only; its exceptions
  are rendered without their arguments.
- The integration absence check is extended from `ledger.db` to every file under
  `$PRAXIS_HOME`, with canary strings planted in a fixture transcript, a fixture source
  file and a scenario command, a forced reader exception, and an assertion that no canary
  appears in any byte.

## 8. Validity plan (the pilot, run under the Stage 1 pre-registration)

No statement about the owner's understanding is made from any measure until these checks
have run and the results are committed. Each threshold and each consequence is fixed in
Stage 1 before the pilot's first row.

- **V1, reliability.** Split-half (odd and even items, Spearman-Brown) reliability of P1 BS
  per week; test-retest on a validation-only set of items probed twice, 7 days apart,
  excluded from every confirmatory analysis.
- **V2, extractor validity.** Compare X1 component edges with importer-to-importee pairs
  recorded by a `builtins.__import__` wrapper (or a `sys.meta_path` finder) during the
  owner's real scenarios, cache hits included. `python -X importtime` cannot do this: it
  prints a module only on first import, so a direct import already satisfied elsewhere
  never appears under its second importer [CR]. Function-level imports that never execute
  are their own class, not divergences. Components with real divergences are `partial`
  permanently.
- **V3, map agreement.** The owner labels one week of real changes by hand at their own
  conceptual grain; agreement with the mechanical map is measured [RT]. Low agreement is a
  reason to freeze an owner map before data, never to change the map afterwards.
- **V4, agent predictability ceiling.** Replay 10 to 20 real prompts 3 to 5 times each in
  scratch clones at the same commit and model and measure the mean pairwise Jaccard of
  touched component sets [RT, A2]. SWE-bench run-to-run coefficient of variation was 15.2
  percent for Claude 4.5 Sonnet and 47.0 percent for Llama-3.1-70B (Mehta 2026 [RT]);
  footprint variance here is unknown. Needs the owner's tokens and consent.
- **V5, understanding against familiarity (the discriminating check).** Classify each D1
  item, on data before it, by edge or no edge and by co-touch above or below the median.
  Congruent: edge with high co-touch, or no edge with low co-touch. Incongruent: the other
  two. Half the bound and held-out items are drawn from incongruent pairs (a design
  choice). A familiarity-driven owner is accurate on congruent items and below R_base on
  incongruent ones. The diagnostic is BSS against R_fam on incongruent items: R_fam is wrong
  there by construction, so beating it cannot come from exposure. This applies the
  reliance-literature rule that agreement cases are uninformative (Schemmer et al. 2023
  [LS]). Also report BSS against R_layer on incongruent items.
- **V6, known groups within a repository, authorship held constant.** Across repositories,
  authorship and familiarity move together, so a pure familiarity measure passes a
  between-repository check [CR]. Within one repository: components the owner wrote that the
  agent later restructured (recorder evidence) against owner-written components the agent
  did not touch. The prediction, written before the run: P1 BSS against R_layer is higher on
  the second. A between-repository check (one the owner designed against one an agent built
  recently, chosen by the human share of attributed lines, not by impression) is run too,
  as necessary but not sufficient.
- **V7, convergence.** Per component, P1 accuracy, K localisation accuracy and (once Q13 is
  settled) C1 closeness to N_imp are rank-correlated; per repository and week, F1 BSS_R1
  is correlated with P1 BSS. Its power with 5 to 30 components is
  low and is computed from pilot variance first; an underpowered null is reported as
  uninformative. Comprehension measures are often uncorrelated with each other (evidence
  profiles 2023; Arvan et al. 2026 [SE]), so a real null is possible and is reported as one.
- **V8, F1 skill and placebos.** BSS_R1 interval excludes zero, and the task-shuffle placebo
  (4.4 g) passes.
- **V9, feasibility.** Pool supply per repository, registration rate, skip rate and cost
  against the kickoff's ten-minute daily budget.

### 8.1 Falsification table (written before the pilot)

| Result | Conclusion | Consequence |
|---|---|---|
| V5: incongruent BSS against R_fam has an interval including zero while congruent accuracy is high | P1 tracks familiarity | No report may call P1 understanding; P1 is redesigned before Stage 2 |
| V6: no difference within a repository | P1 does not track system knowledge | Same as above |
| V1 below the Stage 1 floor | P1 too unreliable | No P1 trend is reported |
| Pipeline placebo differs from zero | The pipeline leaks | All contrasts suspended |
| V8: BSS_R1 indistinguishable from zero, or shuffle placebo fails | F1 measures nothing beyond prompt and history [RT, A1a] | F1 stays an intervention only; its scores are not reported |
| V4 below the Stage 1 agreement threshold | Footprints are mostly agent noise | F1 is reported as fidelity only |
| V9: simulated minimum detectable effect larger than g = 0.28 equivalent at the measured supply | The study cannot detect a plausible conceptual effect | Add repositories or relations before Stage 2, or declare the confirmatory study infeasible |
| V2: static precision or recall below the Stage 1 floor in a component | X1 truth unreliable there | That component's items are `partial` and excluded from the primary |

## 9. Pre-registration in two stages

Invariant 11 requires thresholds before the first result is stored. Both drafts collected
probe and forecast data before freezing [CR]. So:

1. **Stage 1 (pilot), committed before the first task, probe or forecast row:** what the
   pilot collects and for how long; the committed script
   (`experiments/prereg/derive.py`) that maps pilot data deterministically to every
   parameter (L if changed, L_max, deadlines, W, W_d, the survival threshold, cut
   points, R1 burn-in, the exclusion cap, minimum counts, the simulated
   minimum detectable effect); every V threshold and the conditional plan it triggers; and
   the rule that pilot data never enters a confirmatory test. The first pilot week is format
   practice and is never used as a floor [RT, false improvement 5].
2. **Stage 2 (confirmatory), generated by that script and committed before the first
   randomized task:** the outcomes, estimands, allocation, tests, alpha, seeds, exclusions
   and decision rules.

## 10. Gaming and reactivity defences

"test" means an integration test or a CHECK constraint; "prereg" a pre-registered rule.

| Id | Threat ([RT] unless noted) | Rule | Enforced by |
|---|---|---|---|
| G1 | Prompts name the files | F1 reported only as skill over R1; `prompt_named` stored | prereg; CHECK: an F1 report row cites its reference |
| G2 | Agent gets predictable | R1 history term; P1 never asks about agent behaviour; fixed classes not forecast | report code; test |
| G3 | Owner registers only easy work | Arm drawn by praxis inside registered tasks; registration coverage and registered against unregistered footprint size reported | prereg; report |
| G4 | Asking the agent first | `agent_previewed` (4.4 f); arm drawn after, so pre-registration behaviour cannot differ by arm | test with a recorded transcript fixture |
| G5 | Regression to the mean, format learning | First pilot week is practice; nothing selected for starting low | prereg |
| G6 | Exploiting the scoring rule | Brier on a fixed grid; set-overlap descriptive only; resolution always beside calibration | CHECK: p in the grid |
| G7 | Steering the agent toward the forecast | Primary outcome P1 does not depend on the segment's content; F1 scores the first segment only, with flags | prereg; assumptions field |
| G8 | Backfilling | Append-only rows; `late_prediction` rejected | test: a task registered after a prompt binds to the next one |
| G9 | Coarse map | Mechanical default, at least 5 components, frozen, file grain always reported | `praxis components freeze` refuses fewer than 5 |
| G10 | Forecast or task leaks to the agent | No praxis path writes `additionalContext` for tasks; the Stop notice is closed vocabulary | test: hook stdout empty except the allowlisted notice, no component id in any hook output, no praxis string in the transcript |
| G11 | Free text leaks secrets or code | Closed vocabulary; no TEXT column for owner input | schema; test |
| G12 | Lookup during a probe | One item at a time, clock, deadline, `slow` excluded | probe code; test |
| G13 | Rereading transcripts before S3 | Seeded draw; labelled gameable | report label |
| G14 | Harder tasks over time | P1 probes fixed items; F1 stratified by R1 expected count | report |
| G15 | Repository growth, stale map | New paths `unmapped` until a logged version; share reported | CHECK: map version on every row |
| G16 | Model or CLI change | `version_change` opens a segment | test |
| G17 | Friction makes forecasts a reflex | Median seconds and abandonment recorded from day one; redesign above the ten-minute daily median; forcing functions are liked less (Bucinca et al. 2021 [LS]) | doctor; report |
| G18 | Optimizing for predictability over design | Cross-component task share and S2 reported; falling cross-component share with rising F1 flagged | report |
| G19 | Reader leaks (tracebacks, caches) | Canary check over all of `$PRAXIS_HOME` | test |
| G20 | Lures planted by S3 options | Free recall first, immediate feedback, reversed decisions void | probe code; test |
| G21 | Vacuous boundary model | Forbidden share reported; zero forbidden flagged | report |
| G22 | Silent prediction in control tasks | Neutral command name; spillover check (6.4) | prereg |

## 11. Claim classes at a glance

| Quantity | Class |
|---|---|
| Extractor truth (X1, X2, X3), `o_first`, `o_final` | derived |
| BS of any item or forecast | derived |
| BSS, every arm contrast, multiple-baseline change | comparative |
| S2 caught share, S4 detection share | associative |

No computed number is labelled observed, and nothing is labelled causal.

## 12. Repository changes, by file

| File | Verdict | Change and reason |
|---|---|---|
| `src/praxis/recorder.py` | change | `apply_patch` in `MUTATING_TOOLS`; store prompt_id, turn_id, permission_mode, agent_id, CLI version; HEAD per snapshot and HEAD-move classification; X1 edges at Stop for changed Python files; decision pointer at PostToolUse; the segment-closing read and notice; drop assumption 3 |
| `src/praxis/gitdiff.py` | change | Diff against the session base sha; `git rev-parse HEAD` per snapshot under the existing 2 s timeout; `unobservable` with no commits |
| `src/praxis/hooks.py` | change | Extract new payload fields; amend the no-stdout docstring (lines 1-8) to allow only the allowlisted Stop notice |
| `src/praxis/install.py` | change | Add `AskUserQuestion\|ExitPlanMode` to the PostToolUse matchers; Codex matchers already alias apply_patch [MX] |
| `src/praxis/store.py` | change | Delete `repo_config`, `items` (write-it), `results`, `ITEM_ASSUMPTIONS`, `ItemRow`, `ResultRow`, `set_test_cmd`, `item`, `result`, `test_cmd`, `agent_files`, `agent_hashes`, `item_rows`, `result_rows`. Add `component_maps`, `boundary_rules`, `file_edges`, `scenario_runs`, `tasks`, `forecasts`, `outcomes`, `probe_items`, `exposures`, `probe_responses`, `drift_responses`, `divergences`, `decisions`, `decision_responses`, `knockouts`, `flags`, each with the Evidence columns and CHECKs (claim class, non-empty sources, p in the grid, coverage values, no TEXT column for owner input); a multiset survival lookup |
| `src/praxis/drill.py` | delete | Move `Repo`, `locate`, `attached_agents` and its `/proc` and `ps` helpers (lines 33-111) to `unassisted.py`; delete the rest (tests_pass, validate, backups, restore, the drill flow, lines 113-350) |
| `src/praxis/spans.py` | delete | Exists to find write-it candidates and carries the set defect |
| `src/praxis/unassisted.py` | new | Repository location and the attached-agent check |
| `src/praxis/components.py` | new | Map derivation, override, freeze, pool, `map_version` |
| `src/praxis/extract.py` | new | X1 with blind-spot counts and site attributes; X2 and X3 behind their experiments; scratch clones |
| `src/praxis/task.py` | new | `praxis task`, the binding rule, arm draw, forecast entry, `praxis last` |
| `src/praxis/probe.py` | new | P1, S1, S3, K and C1 administration, deadlines, debriefs |
| `src/praxis/transcript.py` | new | Analysis-time reader returning derived structures only |
| `src/praxis/report.py` | new | References, scores, decomposition, CORP, randomization tests, refusing below minimums |
| `src/praxis/cli.py` | change | Replace the `drill` verb (lines 52-65, 80-81, 86-103) with `task`, `last`, `probe`, `components`, `report` |
| `src/praxis/doctor.py` | change | Replace item and result counts (lines 74-75) and the backup check (lines 95-98) with maps, tasks, binding rate, probes, void reasons; import from `unassisted` |
| `.pre-commit-config.yaml` | change | Line 325 pattern: drop `repo_config\|items\|results\|ratings`, add the new tables |
| `tests/integration/test_drill.py` | delete | Replaced by `test_task.py`, `test_probe.py`, `test_components.py`, `test_extract.py`, driving the installed binary against real repositories with recorded payloads, each with the break-the-fix check |
| `tests/integration/test_recorder.py` | extend | apply_patch, agent commit with no delta, prompt_id, non-human turn, edges at Stop, the notice, the canary check over `$PRAXIS_HOME` |
| `fixtures/` | extend | Recorded Codex payloads, AskUserQuestion and ExitPlanMode payloads, 2.1.303 payloads beside 2.1.257 [MX] |
| `docs/design/02-instrument.md` | keep as record | Status line: rejected, why, replaced by this file |
| `experiments/` | new | E03 pair pool and map depth; E03b base-sha diffing; E04 hook cost of the new duties; E05 X2 traces; E06 X3 schemas; E07 task and probe cost; E08 structured-decision supply; E09 agent replay (V4); E10 extractor against runtime imports (V2) |

## 13. Open questions (each needs measuring)

| # | Question | Measurement that settles it |
|---|---|---|
| Q1 | Registered tasks per week, coverage of human prompts that led to mutations, and repositories actively used | Pilot counts of `tasks` against UserPromptSubmit rows with `origin.kind = human` followed by mutating deltas; distinct repo ids per week |
| Q2 | Seconds per `praxis task`, per probe item, per C1 administration; abandonment | Stored timestamps over the pilot (E07) |
| Q3 | Structured decisions per month | Tool-name counts over `~/.claude/projects/*/*.jsonl` and Codex rollouts (E08) |
| Q4 | Item supply per repository and relation | `praxis components pool` on each repository; consumption from Q1 |
| Q5 | X1 precision and recall at component level | V2 (E10) |
| Q6 | R1 burn-in length | R1's leave-one-out Brier against earlier segment count |
| Q7 | Footprint stability for identical prompts | V4 (E09) |
| Q8 | Weekly drift supply for S1 and divergence supply for S2 | Edge-set differences in the recorder's edge history |
| Q9 | Best lag L; does probing contaminate later probes | L = 2 against L = 7 days on validation-only items; first against repeat probe accuracy |
| Q10 | Forecast against prequestions as separate components of the bundle | A later factorial variant |
| Q11 | W, W_d, the survival threshold, the binding window, deadlines, the exclusion cap | Pilot distributions through `derive.py` |
| Q12 | Codex payloads (apply_patch, failed commands), transcript location, terminalSequence support | A real Codex binary (kickoff Phase 1) |
| Q13 | The exact Pathfinder closeness formula | Read Goldsmith and Davenport before computing C1 |
| Q14 | Lag-1 autocorrelation prior | Read Shadish et al. 2013 (doi:10.3758/s13428-012-0282-1), then replace with the owner's value |
| Q15 | Empty-segment rate on coding sessions | Segment definition 5.3 over the owner's transcripts |
| Q16 | Hook latency added by edges at Stop and the notice read | E04, extending `experiments/E01/hook_cost.py` |
| Q17 | X2 overhead, determinism and coverage; X3 coverage | E05, E06 |
| Q18 | Whether terminalSequence output leaves the transcript untouched on 2.1.303; whether systemMessage reaches the model | One recorded session each |
| Q19 | ICC of forecasts within a task; per-item BS SD; between-week SD | Pilot |
| Q20 | Brier-unit size of the pretesting effect for this owner | Pilot prequestion items against shown-only items, exploratory |
| Q21 | Lindvall and Sandahl's primary recall figure | Read JSS 43(1):19-27 |
| Q22 | Any published study scoring human predictions of an agent's edit locations | Targeted search of 2026 CHI and CSCW; the SE review found none |

## 14. Design record

This design merges two drafts under a critic's verdict.

- Kept from draft A: measure separated from intervention; structural probes with direct
  and reach items; the familiarity lure split and R_fam; Pathfinder as convergent only;
  detection latency as descriptive; extractor validation and map agreement; explicit or
  blank forecasts; the `o_first` and `o_final` split; closed vocabulary and the canary check;
  the falsification table.
- Kept from draft B: the neutral `praxis task` command; binding as an analysis-time join
  with prompt_id and turn_id; R1 conditioned on prompt-named components; drift items with
  inverse-probability weights; no feedback on criterion items; task-shuffle and relabelling
  placebos; stratification by R1's expected count; the mechanical map always scored
  alongside, and file grain; multi-category Brier for decision recall.
- Dropped: A's random exposure pairs as the primary (unrelated to the work); A's
  failure-undone forecast (the owner controls what is undone, so a calibrated owner keeps
  bad work after predicting 0.1 [RT, Perdomo et al. 2020]); B's footprint recall as the
  primary (episodic memory of what the agent touched, with extra encoding in one arm and
  the answer one `git log --stat` away); B's mutation localisation (code-level faults
  surfaced by project test names); B's 0.1 keystroke; B's 200-character reason; the
  free-recall character count.
- Added: task-bound pairs with a randomized primary; a multiple-baseline general effect;
  boundary enforcement; the extractor family beyond imports; R_layer; sampled forecast
  components; the segment definition that skips question turns; the terminalSequence
  notice; two-stage pre-registration; base-sha diffing; response deadlines; edge history at
  Stop; scratch clones instead of worktrees; the knockout probe in place of mutation
  localisation.

Where a research finding pulls against the critic's merge plan, and what this design does:

1. Segment definition. The learning-science and measurement reviews say to score only
   outcomes the owner does not control after predicting, ending at the next owner prompt.
   The critic lets owner replies to agent questions stay inside the segment. This design
   follows the critic for F1, because otherwise most segments are empty [CR], but flags
   `owner_answered_question`, runs a pre-registered sensitivity analysis without those
   tasks, and makes the primary outcome (P1) independent of segment content.
2. Reflexion models. The SE review proposes them as a descriptive measure of the owner's
   model; the critic uses them normatively. This design keeps them normative (S2) and keeps
   descriptive accuracy in P1, because scoring a normative declaration against actual edges
   penalizes the owner for the agent's violations.
3. Feedback through additionalContext. The learning-science review suggests it as one
   immediate channel; it reaches the agent, so it is not used. The critic's terminalSequence
   channel was checked against the Claude Code hooks documentation before adoption.
4. Feedback on criterion items. The review's lure finding argues for feedback; the critic
   argues against it on P1. Lures are a multiple-choice risk, P1 items are binary, so P1
   gets block-end debriefs and S3 keeps immediate feedback.
5. Who chooses the predicted prompts. The reviews prefer a hook-chosen random sample of
   prompts. A hook cannot obtain a prediction before a prompt without cueing the agent or
   blocking [MX], so the owner registers tasks, praxis draws the arm, and registration
   coverage is reported as selection (G3).
