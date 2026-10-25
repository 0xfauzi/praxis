# 05: altitudes of understanding, and a seeded-fault criterion

Status: PROPOSED, needs owner decision. Written 2026-10-25, before any catch-task data. It
adds to `docs/design/03-measurement.md` and replaces nothing in it. Until the owner
decides, nothing here is built in `src/praxis`: the one artifact is experiment E14
(`experiments/E14/`, `docs/experiments/E14.md`), which measures whether the fault
generators work on real repositories and shows the owner nothing.

The owner asked for the right altitudes for understanding and whether praxis facilitates
them. The short answer, argued below: praxis measures one altitude of five, the
components and their connections; it has no criterion above that altitude, so it cannot
tell whether its primary outcome predicts the owner catching the faults that matter
most; and the cheapest criterion that keeps every invariant is a deterministic
seeded-fault catch task, which this document designs and E14 has started to measure.

Nothing in this document is a claim about the owner.

## Sources

Tags as in design 03 ([LS], [SE], [MS], [RT], [MX], "measured here"), plus:

- [E14] measured here by `experiments/E14/faults.py` on 2026-10-25, on praxis at
  844a207, telltale at ec6a9e4 and systemap at 02a496b.
- [LIT] a primary source named inline and cited from memory; not re-read for this
  document. No number is taken from a [LIT] source.
  Rasmussen 1985 (the abstraction hierarchy, IEEE Trans. SMC 15(2)); Vicente 1999
  (cognitive work analysis); Bainbridge 1983 (ironies of automation: a monitor cannot
  catch what it does not understand); Parasuraman and Manzey 2010 (complacency in
  automation use); Green and Swets 1966 (signal detection); DeMillo, Lipton and Sayward
  1978 (mutation analysis, the competent-programmer assumption); Budd and Angluin 1982
  (equivalence of a mutant is undecidable in general); Just et al. 2014 and Papadakis et
  al. 2018 (whether mutants stand in for real faults: correlated in the first, weakly
  once suite size is controlled in the second); Dolan-Gavitt et al. 2016 (LAVA:
  injected bugs at scale, and how they differ from organic ones).

## 0. The task, rewritten

- Altitude. The outcome is not a fifth measure. It is that praxis can say, for each
  altitude, whether the owner's model is accurate there, and steer the agent's mode
  toward the altitude where it is stale. That needs a criterion that sits above the
  component level and is graded without a judge, before any facilitation is built.
- Depth. A human-factors practitioner would start from the abstraction hierarchy and
  from the observation that supervisors fail at the purpose and constraint levels, not at
  the parts level [LIT Rasmussen, Bainbridge]. A test engineer would build the criterion
  by fault seeding, whose ground truth is known by construction, and would worry first
  about whether seeded faults resemble real ones [LIT Just, Papadakis]. A psychophysicist
  would insist on catch trials (clean decoys) and a fixed base rate, or "always wrong"
  scores perfectly [LIT Green and Swets]. The outside judge is the generator's own label,
  re-derived by an extractor on the mutant (E14), not anyone's opinion.
- Range. Supervisory control (abstraction hierarchy), fault seeding and mutation analysis,
  signal detection, situation awareness (design 03 S1), architecture conformance
  (design 03 S2), and a red team (section 8).
- Belief. The hard version grades every altitude deterministically on an arbitrary
  repository. That is out of reach for two altitudes, and section 1 says exactly where:
  constraints need machine-readable rules, and flows need owner-named scenarios. The rest
  is in reach, and E14 measured its supply.

## 1. The construct, per altitude

### 1.1 Five levels

Rasmussen's abstraction hierarchy describes one system at five levels, each the "why" of
the level below and the "how" of the level above [LIT Rasmussen 1985, Vicente 1999]. For a
repository an agent builds:

| Level | Name | What the owner's model holds | Example in praxis |
|---|---|---|---|
| L1 | purpose and constraints | what must stay true, and which part guarantees it | derived rows reach the ledger only through `store.py` |
| L2 | abstract function | flows and guarantees across parts: which parts a named operation passes through, and in which direction | the `hook-edit` operation passes from `recorder` into `store` |
| L3 | generalized function | the components and their connections | `extract` does not import `cli` |
| L4 | physical function | the mechanism inside one component | `scoring.snap` sends a value at a midpoint to a fixed side |
| L5 | physical form | the code text | the bound of one `range` call |

An agent's most expensive errors are at L1 and L2: a change that is locally correct and
breaks a guarantee or a flow. A supervisor catches those only with a model at that level
[LIT Bainbridge 1983]. L4 and L5 are the work the agent is for (design 03, preface).

### 1.2 Where praxis sits today

| Measure (design 03) | Level | Note |
|---|---|---|
| P1 structural probe | L3 | import and scenario-call relations between components |
| S1 drift notice | L3 | change of L3 relations |
| F1 component forecast | L3 | where the agent's work lands |
| K knockout | L3 | a scenario failure is the cue, the answer is a component |
| C1 Pathfinder | L3 | relatedness of components |
| S2 boundary enforcement | L1 by declaration | the owner's own forbidden pairs, associative |
| S3 decision recall | none fixed | memory of a structured decision |
| S4 detection latency | none | removal of agent lines, any cause |

Every scored measure is at L3. V7 (convergence) checks P1 against K and C1, which are at
the same level, so a P1 that tracks only component layout passes V7. There is no
criterion above P1, and none below it that would show P1 is not generic code reading.

### 1.3 What can be graded, and where

"Gradable" has two parts here, and both must hold: the label of an item is fixed without
a judge or a project test (invariant 9), and a stimulus exists that is answerable from the
owner's model at that level and not by reading code (section 4).

| Level | Label without a judge | Code-free stimulus | On an arbitrary Python repository | Only a judge can grade |
|---|---|---|---|---|
| L1 | yes, by X4 (section 2.1), where a rule is machine-readable | yes | no: only where the repository declares machine-readable constraints (scoped pygrep hooks, DDL CHECK clauses, praxis S2 forbidden pairs, import contracts) | why a constraint exists; a prose invariant; a constraint nobody wrote down |
| L2 | yes, by X2 on owner-named scenarios | yes, weakest of the three (section 4.3) | no: only where the owner has named scenarios that run | flows no scenario runs; what data means |
| L3 | yes, by X1 | yes | yes, with a frozen map of 5 or more components | a component's role in words |
| L4 | yes, by construction and an AST-locality check | no: the function must be shown | yes, as a label; the stimulus is code | whether the mechanism is the right one |
| L5 | yes, by construction | no: the stimulus is the hunk | yes | nothing at this level |

Measured supply [E14]: L3, L4 and L5 are supplied on praxis, telltale and systemap,
with no recovery failure in 471 drawn items. L1 has faults on praxis (84 items) and
telltale (60) but only 4 and 3 decoys, so it is not supplied; on systemap it is zero,
because its 12 invariants are prose, its import rules are empty and it has no scoped hook
or DDL. L2 is labelled only on praxis, the one repository with a scenario file, and
there X2 labels 1 of 24 call drops a fault, so it is not supplied either. On telltale
and systemap the layer order alone answers every L3 item (section 5.1).

So L1 and L2 cannot be graded on an arbitrary repository, and this design does not
pretend otherwise: on a repository without machine-readable constraints, L1 is
`unknown` (invariant 4), never zero, and the report says which supply was missing. The
L4 and L5 items are not targets. They are the discriminant levels: a P1 that predicts
catching L5 slips as well as L3 faults is measuring care or code reading, not structure.

### 1.4 What is not measured

- Rationale, and whether a constraint is the right one. Only a judge could grade either
  (design 03, 4.9).
- Free recall or explanation of any level: owner input stays closed vocabulary
  (invariant 5).
- Real-fault detection as a score. S4 is associative and enters only as a second
  criterion (section 5.4).

## 2. Two extractors one level up

Each is a derived artifact under design 03 section 3's rules: it reads source or runs a
scenario in a scratch clone, keeps identifiers and counts, and carries a version.

### 2.1 X4: enforcement sites (L1)

X4 answers "which component enforces constraint C, and does any other component now do
what C forbids". It reads only rules a machine can check:

- A scoped pygrep hook in `.pre-commit-config.yaml` (`language: pygrep` with a `files`
  pattern): lines matching `entry` may appear only outside the scope. Its site is the
  product file outside the scope that holds matches; its resource is the pattern's last
  group (a table, a module). praxis has 2 (`judge-isolation`, which has no instance yet
  because `judge.py` does not exist, and `derived-writes-only-in-store`); telltale has
  3; systemap has 0 [E14].
- CHECK clauses in DDL, read at runtime: the module is imported in the scratch clone and
  every module-level string holding `CREATE TABLE` is executed in
  `sqlite3.connect(":memory:")`, so DDL built by f-strings and helpers is read too
  (praxis's `schema.SCHEMA` is one f-string; a literal-only reader found 0 tables) [E14].
- praxis S2 forbidden pairs (design 03, 4.3) and systemap `ImportRule` contracts, once
  declared. Both are empty today in the repositories measured [E14].
- Counted and never read as truth: systemap `Invariant` entries. They are prose, and
  their `governs` lists were written by whoever wrote the map (in praxis, an agent), so
  using them as truth would let a model grade what an extractor should (invariant 9).

X4 enables an L1 probe beside P1, called PC: "Which component is the only one that may run
`INSERT` on `forecasts`?", closed over component ids, multi-category Brier against the
uniform reference, truth from X4 at probe time. PC is cheap; it is proposed here and
built only if the owner accepts this design.

### 2.2 X2-delta: path changes across commits (L2)

For each owner-named scenario, the components entered and the call edges, compared
between two trees: edges added, edges removed, components no longer entered. Stored as
scenario id, tree hashes and component-pair ids with signs. Not stored: anything X2 does
not already exclude.

A working tree at a segment boundary cannot be rebuilt later (the recorder keeps hashes,
design 03 3.4), and running scenarios inside a hook would exceed its time budget
(invariant 7). So X2-delta runs between committed trees, when the owner runs
`praxis run scenarios` or on a schedule, never inside a hook. It enables an L2 drift probe
in the S1 family ("Since last week, does `hook-edit` still pass from `recorder` into
`store`?") and labels L2 catch items (section 3). Determinism: E14 traced each of the 4
praxis scenarios twice on the base tree and the two traces were equal in all 4 [E14].

## 3. The criterion: a seeded-fault catch task (SF)

### 3.1 The item

An SF item is (level, operator, component ids, label, card). The generator applies one
operator once, to one file, in a scratch clone (`git clone --local` under
`$PRAXIS_HOME/scratch/`, no remote, deleted when the item has been checked and its card
built). The owner's working tree and `.git` are only read by `git clone` (invariant 5).
The label is fixed by construction and confirmed by re-extraction on the mutant; an item
whose re-extraction disagrees is discarded and counted as a generator defect.

Every fault operator has a decoy operator of the same card form whose label is "not
wrong" by construction (section 4.4).

### 3.2 Generators per level

| Level | Fault operator | Decoy operator | Label fixed by | Kept invisible by |
|---|---|---|---|---|
| L1 | `c_relocate`: a statement a scoped hook allows only in its site file, copied into a new private function in an in-scope file of another component | `c_relocate_in_site`: the same statement into a new private function in the site file | X4: the hook's in-scope matches rise in exactly the target file (fault) or do not change (decoy) | parse; import; the new function is never called, so nothing runs |
| L1 control | `c_drop_check`: one CHECK clause removed from DDL, only when its text is once in the source and once in the runtime DDL | none (recognition control, section 4.4) | X4: that table's CHECK count drops by one, every other table unchanged | parse; import; the DDL still executes in memory |
| L2 | `f_drop_call`: one bare call statement into another component, in a function, becomes `pass` | the same operator | X2 after the mutation: fault when a deterministic scenario's component set or call-edge set changes, decoy when none does; a second trace must agree | parse; import; every scenario's exit code unchanged |
| L3 | `g_add_edge`: an import of a module of B, inside a new private function in a file of A, for a pair with no X1 edge, where B already reaches A (a new component cycle) | the same, where B does not reach A and the two are not already in one cycle | X1 on the touched file: the base edges plus exactly (A, B); the cycle status from the base reach | parse; import (a function-level import never runs at import time) |
| L4 | `m_negate_if`; `m_flip_compare` (`<` to `<=` and back, `>` to `>=` and back) | `m_mirror_compare` (`a < b` to `b > a`, pure operands only); `m_swap_branches` (`if c: X else: Y` to `if not (c): Y else: X`) | AST: exactly the planted function (and its enclosing functions) differs; X1 and hook matches unchanged | parse; import; mutations sit in function bodies |
| L5 | `l_shift_int`: an int literal k of at least 1 used as an index, a slice bound or a `range` argument becomes k + 1 | `l_rename_local`: one local name renamed throughout its function, where no nested scope, f-string, `match`, `locals`, `eval` or `exec` could see it | as L4 | as L4 |

Choices behind the table, each from E14's trials (E14.md, "What we did"):

- L3 places the import inside a function for faults and decoys alike. At module level, 4
  of 6 trial back edges on praxis broke the import with a circular-import error. That
  makes the fault visible to anyone who runs the code and makes placement a label leak.
  E14 still makes and import-tests the module-level form on every L3 item, as data on
  how often an agent's back edge would surface by itself.
- L2 cannot fix its label before the mutation. The guess "the component pair is a call
  edge in a base trace" was wrong for 3 of 3 trial faults: the dropped call site was not
  the one the scenario ran. So the label is X2's, and the recovery check is that a second
  trace of the mutant agrees.
- `c_drop_check` requires the clause to be unique in the runtime DDL. In a trial, 1 of 3
  praxis items dropped clauses in several tables: 12 of the 64 clauses whose text is
  unique in `schema.py` sit in a fragment several tables share.

Generators planned and not yet in E14: an L1 relocation across an S2 forbidden pair (an
import added from A into B where the owner declared A to B forbidden), and an L1
relocation of a table write (X3, design 03 3.3) into a component that X3 shows never
writes that table.

### 3.3 Item supply and cost

An item is consumed by its first exposure, like P1 items (design 03 3.5), at its level's
grain: L1 (constraint, component, resource), L2 a call site, L3 an ordered component pair,
L4 and L5 a function. E14 measured the supply per level per repository and the wall time
per item (E14.md). Items are generated before a sitting, never while the owner waits.

## 4. Presentation: what the owner sees

### 4.1 The contamination problem

The level of an item is the level of its stimulus, not of its fault. Show the diff and
every item becomes a code-reading item: a relocated `INSERT` is caught by anyone who
reads the line, whether or not they know the constraint, so L1 to L3 collapse into L5.
Show nothing and there is nothing to judge. The design rule that follows:

**An item at level k shows the change's effect at level k and nothing below it. Whether
that effect is wrong is decided by a fact at level k that the card does not carry and the
owner's model does or does not.**

Two checks make the rule testable before any owner sees an item:

- Sufficiency: the label is a deterministic function of the card and the base system's
  truth at level k. L1: wrong iff the card's component is not the X4 site of that
  operation and resource. L3: wrong iff the base X1 graph has a path from the card's
  `depends_on` to its component. L2: wrong iff a named scenario's X2 path changes. E14
  confirms each label from the extractor, so sufficiency holds by construction for L1
  and L3; for L2 it holds only relative to the named scenarios (section 8, R13).
- Necessity: a responder that sees the card and knows nothing about the system scores at
  the reference. V12 (section 6.3) runs scripted card-only responders on each generated
  pool before a sitting: one that reads the template id and every field except the
  component ids, one that matches component names against the resource words, and one
  that flags code artefacts at L4 and L5. Any of them above R_base on a level suspends
  that level's items until the generator is fixed.

### 4.2 The card

Every card has the same frame: "Change 7 of 24. While working on <intent>, the agent made
this change. The change was made in a scratch copy and never in your repository." The
last sentence is true (section 3.1) and limits the false structural beliefs a fault card
could plant (R15). Then one effect line, or a code display, by level:

| Level | Effect shown | Example |
|---|---|---|
| L1 | component, operation, resource (from X4) | "`task` now runs `INSERT` on `forecasts`." |
| L1 control | table, from X4 | "One rule on what table `probe_responses` accepts was removed." |
| L2 | caller component, callee component, caller function name (from the AST and X2) | "In `recorder`, `_snap` no longer calls into `store`." |
| L3 | the new dependency (from X1) | "`extract` now depends on `cli`." |
| L4 | the whole function after the change, with its qualified name, no diff, no highlight | the function's text |
| L5 | the changed hunk before and after, two lines of context | the hunk |

`<intent>` is a template chosen per operator family, identical for faults and decoys of
that family, from three phrasings by a seeded draw: for L1 "keeping related work
together", for L2 "removing a call that looked redundant", for L3 "reusing an existing
helper", for L4 "simplifying a condition", for L5 "tidying a bound or a name". Each is
the kind of reason an agent gives, and each is as true of the decoy as of the fault.

L4 and L5 show code because no code-free stimulus exists at those levels (section 1.3).
The code is read from the scratch clone at display time and discarded with it; no row
holds it, and the integration suite plants a canary in the mutant and checks every byte
under `$PRAXIS_HOME` after the sitting (invariant 5).

### 4.3 L2 is the weakest card

The L2 card names a function, which is an L4 identifier, because without a call site the
card is ambiguous. Whether that call site is on a named operation's path is knowledge at
the boundary of L2 and L4. L2 is therefore reported, never pooled with L1 or L3, and its
association with P1 is read as exploratory.

### 4.4 The answer, the base rate and the decoys

The answer, closed vocabulary only (invariant 5):

1. "How likely is it that this change is wrong?" One grid probability
   {0.1, 0.3, 0.5, 0.7, 0.9}, the grid on which the Brier score keeps honest reporting
   optimal (design 03, section 4).
2. If p is at least 0.5, a reason code: `breaks_a_rule`, `breaks_an_operation`,
   `wrong_dependency`, `wrong_logic`, `slip`, `cannot_say`. The codes map to L1 to L5;
   the confusion between the planted level and the named one is reported, descriptive.

No location is asked: a single-effect card names its component, so a location answer
would be trivial. A bundled card (three effects, one possibly faulty, answered by index)
would make location meaningful and is an open question (Q10) because it triples decoy use.

Base rate. Each item is a fault with probability 0.5, by a seeded Bernoulli draw, and the
owner is told "about half of these changes are wrong". Without decoys, "always 0.9" scores
a Brier of 0.01; with an announced 0.5, it scores 0.41 against 0.25 for a constant 0.5
(derived: 0.5 x 0.01 + 0.5 x 0.81). A Bernoulli draw, not an exact half per sitting, so the
owner cannot reason from a count of earlier answers. Hit rate and false-alarm rate at
p of at least 0.7 are reported beside the Brier score, descriptive [LIT Green and Swets].

Decoys have the card form of their faults: same template, same fields, same display
length class. The L1 control `c_drop_check` has no decoy; its card is close to
self-evidently wrong, so it is a recognition control (an attention check expected near
ceiling), reported apart and never in a level comparison.

### 4.5 Why templates and not agent summaries

A real agent summary of a planted change needs a model call: that is the judge path
(invariant 6), it is not deterministic, and its candour varies, so a summary could leak
the label in one item and hide it in the next. Templates hold candour fixed across
faults and decoys. The cost is realism (R1). A later judge-dependent variant could have a
model write the summary with the label still fixed by construction, provided V12's
card-only responders, with a judge among them, stay at R_base.

### 4.6 Deterministic generation, no model

Every card field is a pure function of the operator, the extractor delta on the mutant and
the frozen component map; the intent phrasing is a seeded draw. The display at L4 and L5
is the mutant's own text. Nothing in the card path calls a model, reads a project test
or stores code.

## 5. Scoring and analysis

### 5.1 Per item and per level

- Brier per item, `BS = (p - y)^2` with y = 1 for a fault. Unanswered, skipped, late
  and refused (agent attached) are values with reasons, never 0 and never 0.5 (design 03,
  section 4).
- Per level: mean BS, the Murphy decomposition with the Ferro and Fricker correction once
  a bin reaches design 03's minimum, and hit and false-alarm rates at p of at least 0.7.
- References, each fitted before the item: R_base, a constant 0.5 (the announced rate),
  BS 0.25. For L3 also R_layer, which says 0.9 when the new dependency points up the
  base X1 layer order and 0.1 otherwise. E14 measured what R_layer already knows: it
  scores 0.01 on every L3 fault (a back edge always points up) and misses only on
  layer-incongruent decoys, of which praxis has 73 of 273 and telltale and systemap 0
  [E14]. So L3 skill beyond layering, BSS against R_layer, is computed only on items
  drawn half from incongruent decoys, and today only praxis supplies them. Elsewhere L3
  is reported against R_base with the label "includes layer order".
- The L1 control is reported as a hit rate only, beside L1, and enters no comparison.

### 5.2 Criterion validity: does P1 predict catching, and at which level (V10)

The unit is the item. For each SF item at level k, the predictor is the owner's P1 Brier
score on the P1 items that involve the card's components (A, or A and B), from the most
recent probe before the sitting, fitted on data dated before the item. The outcome is the
SF item's Brier score. Per level, the statistic is the Spearman correlation between the
two over the level's items, with a permutation null that shuffles P1 scores across
components (20000 permutations, seed fixed in Stage 2). An item whose components have no
P1 score has no predictor and is left out and counted, never imputed (invariant 4).

Predictions, written before data:

- P1 against SF L3: positive (the same level; a convergent check that, unlike V7, uses
  an outcome the owner did not rehearse).
- P1 against SF L1: the substantive question. Positive means component-structure
  knowledge goes with catching constraint faults; null means praxis's primary outcome
  is silent about the faults that cost most.
- P1 against SF L5: no larger than P1 against SF L3. Larger means P1 tracks care or code
  reading (the discriminant check).
- L2 and L4 are reported, exploratory (section 4.3, and L4's stimulus is code).

Which level's understanding goes with catching constraint faults is asked within SF: per
sitting, the correlation over components between SF L1 and each other level's SF score,
descriptive, because the per-component counts in one sitting are small.

Power, derived: with 24 items at a level, a one-sided test at 0.05 has 80 percent power
for a correlation of about 0.5 (Fisher z: ((1.645 + 0.842) / atanh 0.5)^2 + 3 = 23.5);
a correlation of 0.3 needs about 68. At the section 6.1 counts, 24 items take four
sittings at L3 and six at L1. An underpowered null is reported as uninformative, as V7 is.

### 5.3 Decision rule, fixed before data [proposed, needs owner decision]

- SF-1. "P1 predicts catching planted changes at level k" may be written only if the
  one-sided permutation p for level k is below 0.05 and the 90 percent cluster-bootstrap
  interval (clusters: sittings) of SF-k BSS against R_base lies above zero. Otherwise
  the report says "no evidence that P1 predicts catching at level k", with the item
  count and the correlation the count could detect.
- SF-2. If the P1 against SF L5 estimate is at least the P1 against SF L3 estimate, every
  P1 trend in the report carries the warning "P1 tracks code reading as much as
  structure (SF-2)".
- SF-3. A level is "stale" for the owner when its SF BSS against R_base has an interval
  that includes zero while L3's lies above zero. A stale level is a proposal for
  facilitation (section 7), never a policy input until invariant 10's admission.
- The words cause, impact and would stay refused unless a rule here was met, and the
  report names the rule (invariant 3).

### 5.4 Against real faults: S4 (V11)

Planted faults are not the agent's faults (R1). The second criterion is the owner's
record with real ones: S4's share of agent lines in a component removed in the same
session (design 03, 4.5). Per component, the Spearman correlation between SF L1 to L3
BSS (pooled, items whose card names the component) and the S4 same-session removal share
over the same weeks. Prediction: positive. A null has two readings, named on every row:
the planted faults do not resemble the ones the owner meets, or S4 measures refactors
and changed minds, which it also counts. A second check uses S4's events directly: the
agent changes the owner reverted are classified by the extractors (X4 change, X2 path
change, X1 edge change, else local), giving the level mix of real catches, compared with
the planted mix (Q6).

### 5.5 Claim classes

| Quantity | Class |
|---|---|
| SF label (by construction, confirmed by an extractor) | derived |
| SF item BS, hit and false-alarm rates | derived |
| SF BSS against R_base or R_layer | comparative |
| P1 to SF association (V10), SF to S4 association (V11) | associative |

No row says "understands", "would catch" or "catches the agent's errors". Report text:
"accuracy on planted changes at level k, in a scratch copy", no more (invariant 3).

## 6. How it enters the protocol

### 6.1 The one sitting

Preregistration 1.3.2's sitting gains an SF block after the P1 items and the validation
set, with the same attached-agent check (refused and recorded) and a per-item deadline:
60 seconds, provisional, replaced from the sitting's response times by the rule P1 uses.
The block, a design choice: 4 L1 faults or decoys where L1 is supplied, 4 L2 where it is
labelled, 6 L3, 4 L4, 4 L5, and 2 L1 controls: 24 items. The seconds per item are not
known (Q1); at 30 seconds each the block is 12 minutes. If the sitting with the block
exceeds an hour, the block moves to a second sitting, never shortened by dropping a
level. A level that a repository does not supply is absent and reported as not supplied.

The sitting is Stage 1: its SF rows feed V10 and V12 and never a confirmatory test
(preregistration 1.9). In the study, an SF sitting every 4 weeks (a design choice;
supply against consumption is Q11).

### 6.2 Never practice

- No per-item feedback, ever, during the study. A debrief would teach the generator,
  which is the main threat to the criterion (R3). The owner sees per-level aggregates at
  the end of a pre-registered block of sittings.
- SF items never appear in `praxis start task`, forecasts, prequestions, tutor-mode
  context, S1, or any drill. They are generated from their own pool and consumed on
  exposure.
- An L3 or L1 pair named on an SF card is flagged `sf_exposed` for P1 for L_max days and
  analysed apart, because a fault card describes a dependency that does not exist (R15).

### 6.3 Preregistration cells [proposed, needs owner decision]

- 1.3.2: the SF block above, and what it supplies: SF deadlines, seconds per item, the
  V12 card-only baselines on the generated pool, and the first V10 cross-section.
- 1.4: base rate 0.5, announced; items per level per sitting as in 6.1; sitting interval
  4 weeks; SF deadline 60 seconds, provisional.
- 1.7, new rows: V10 (criterion validity, section 5.2; threshold: rule SF-1; plan: SF-1
  to SF-3); V11 (S4 convergence, section 5.4; not a gate; a powered null is reported as
  one); V12 (card-only baselines, section 4.1; threshold: no baseline's BSS against
  R_base above zero on a level; plan: that level's items are suspended until the
  generator is fixed and V12 passes again).
- 1.8, new rows: V10 null at L1 with L3 positive means "P1 is silent about constraint
  faults", and the report says so beside every P1 number; SF-2 met means the P1 warning;
  V12 failed means the level is suspended.

## 7. Facilitation consequences

- Measure first. SF-3 names a stale level; nothing is targeted before it does.
- Forecasts: when L1 is stale and X4 supplies constraints, `praxis start task` may add one
  closed question, "which of these rules could this task touch?" over X4 constraint ids,
  in the treatment arm under the same binding rule as F1 (design 03, 6.2). When L2 is
  stale, the same over named scenarios.
- Tutor mode: the `additionalContext` for a prompt in tutor mode may ask the agent to
  state, before editing, the X4 constraints or the scenarios its change touches, at the
  stale level. It carries constraint and scenario ids only, never an SF item, a score or
  a result (design 03, G10).
- Admission: a per-level profile chosen from SF is a behavioural signal. It enters a
  policy decision only after it predicts P1 on held-out weeks and survives the placebo
  (invariant 10). Until then this section is a proposal.

## 8. Red team

Each threat names what would detect it. Where E14 already measured something, it says so.

| # | Threat | What would detect it |
|---|---|---|
| R1 | Planted faults are unlike the agent's faults. The operators are textbook one-statement mutations; agent faults are larger, coherent, multi-file and plausible. Mutant detection tracks real-fault detection only weakly once confounds are controlled [LIT Papadakis] | V11 (SF against S4); the level mix of the owner's real reverted agent changes against the planted mix (Q6); the size profile (files, statements, components touched) of real agent faults against planted ones |
| R2 | Items are recognisable by artefact: a new function named `_e14_relocated`, `pass` where a call was, `not (...)` with redundant brackets, a local renamed `x_`. At L4 and L5 the code is shown, so an artefact gives the label away | V12's artefact responder on the generated displays; the production generator draws names from the module's own vocabulary and minimises brackets, and V12 must pass before a sitting |
| R3 | The owner learns the generator. The owner reads this document and approves the operators | per-operator accuracy within a level (an owner who knows `m_negate_if` beats `m_flip_compare` in the same functions); SF trend across sittings with P1 flat; the operator is never on the card, and every family has a same-form decoy |
| R4 | Level is confounded with difficulty: a level near floor or ceiling shows no association whatever the owner knows | per-level hit and false-alarm rates; V10 is read only for levels whose hit rate is inside 0.1 to 0.9; rank statistics only |
| R5 | Decoys are poor. L1 decoys are all in the site component, so "the component that owns tables is fine" answers them by name; and there are only 4 (praxis) and 3 (telltale) [E14] | V12's name-matching responder; Q2 (a second L1 decoy operator); until then L1 decoys are reused across sittings and the reuse count is reported |
| R6 | A decoy is not equivalent: `b > a` differs from `a < b` when the operands' comparison methods disagree; a rename is seen by `globals()`-style dynamic access the blocker list misses | X2 on decoy mutants where scenarios exist (any trace or exit change marks the decoy non-equivalent); Q12 |
| R7 | A fault is not wrong: a negated condition in dead code, a bound never reached, a back edge the owner would accept. Labelling an inert change wrong penalises a correct owner | X2 on fault mutants where scenarios exist marks inert faults (Q4); the acyclicity norm for L3 is stated on every row, and S2 forbidden pairs replace it where declared |
| R8 | Cost: generation is slow or the sitting waits | E14: 95.0, 77.5 and 76.6 seconds for whole runs of 188, 163 and 120 items with enumeration; items are generated before the sitting, never during it |
| R9 | Constraint supply: most repositories declare no machine-readable constraint, so L1 is absent where it matters | E14: systemap has 0; praxis and telltale have 2 and 3 scoped hooks, one of praxis's with no instance; `praxis show components` would print L1 supply per repository, and absent supply is `unknown`, never 0 |
| R10 | Circularity: the owner wrote the hooks, so knowing the constraint is near-certain and L1 sits at ceiling | per-level ceiling check (R4); L1 then measures noticing, which is stated, not knowing |
| R11 | Card vocabulary leaks: "runs `INSERT`", "imports `urllib.request`" sound risky to anyone | V12's keyword responder with a fixed list of risky words, run on each pool |
| R12 | Reactivity: an SF sitting makes the owner more vigilant in real work, which moves S4 | S4 in the week after each sitting against the week before, descriptive |
| R13 | L2 labels are relative to named scenarios: a decoy that changes no named path may break an unnamed one, and an owner who knows that is marked wrong | scenario coverage (share of product components entered) printed beside every L2 number; L2 is never pooled (section 4.3) |
| R14 | One person: 24 items per level detect only a correlation near 0.5 | power stated before data (section 5.2); an underpowered null is reported as uninformative |
| R15 | SF contaminates P1: a fault card says "`extract` now depends on `cli`", which is false, and the owner may later believe it | the card says the change was never made; `sf_exposed` pairs are analysed apart in P1 for L_max days (section 6.2); P1 accuracy on exposed against unexposed pairs |
| R16 | The owner's tree is written, or code text persists | E14's `git status --porcelain` check on the clone after every item; the owner's tree is only read by `git clone`; a canary planted in each mutant, then the byte scan of `$PRAXIS_HOME` (invariant 5) |
| R17 | Layer order answers L3: on telltale and systemap every L3 decoy is layer-congruent, so a responder who knows only the layering scores 0.010 [E14] | BSS against R_layer on incongruent decoys only (section 5.1), which only praxis supplies today |
| R18 | L2 is near empty at component grain: 1 fault in 24 praxis call sites [E14] | Q3 (a function-grain trace); until it lands, L2 is not in the sitting |

## 9. Open questions (each needs measuring)

| # | Question | Measurement that settles it |
|---|---|---|
| Q1 | Seconds per SF item, per level | The one sitting's timestamps (E07's runner, extended to the SF block) |
| Q2 | An L1 decoy operator with supply: candidates are the same operation on a resource no constraint guards, or the guarded statement moved within the site component to a second file | E14 rerun with the operator, decoy supply per repository |
| Q3 | Whether a function-grain X2 (caller function to callee component, `co_qualname` in the tracer) labels L2 call drops as faults in useful numbers | E14 L2 rerun on praxis with the finer trace: the fault share among the 24 call sites |
| Q4 | Share of inert faults at L4 and L5 | X2 on fault mutants where scenarios exist (praxis): faults whose traces and exit codes are all unchanged |
| Q5 | L1 supply on the owner's own repositories | E14 on each repository the owner names; `praxis show components` once built |
| Q6 | The level mix of the agent changes the owner really reverted | S4 events classified by X4, X2, X1 and AST deltas between the snapshot before and after the reverted change (a later E15) |
| Q7 | Floor and ceiling per level | The one sitting's hit and false-alarm rates |
| Q8 | SF reliability | Split-half (odd and even items) per level across sittings; items cannot be re-shown, so no test-retest |
| Q9 | Card-only baseline scores | V12 on each generated pool, before the first sitting |
| Q10 | Whether a bundled card (three effects, one possibly faulty) makes location informative at an acceptable cost | A sitting with half the L3 items bundled: seconds per item and location accuracy |
| Q11 | Sitting interval against supply | E14 supply and consumption per sitting; the binding level (L1 decoys today) sets it |
| Q12 | Mirror and rename decoy equivalence rate | X2 on decoy mutants on praxis: decoys whose traces or exit codes change |
| Q13 | Whether telltale and systemap have layer-incongruent pairs under an owner map, where the mechanical map has none | `layer_rule.py` against the owner's frozen map once it exists |
| Q14 | Why 2 of 39 drawn `c_drop_check` removals broke DDL execution | Inspect the two removals in a scratch clone; fix the comma rule in `ddl.py` |

## 10. Repository changes, by file (proposed; nothing is built until the owner decides)

| File | Change |
|---|---|
| `src/praxis/sites.py` (new) | X4 from `experiments/E14/sites.py` and `ddl.py`, versioned `x4-v1`; identifiers and counts only |
| `src/praxis/faults.py` (new) | Generators and label recovery from E14, scratch clones only, items generated before a sitting |
| `src/praxis/extract.py` | X2-delta between two trees; the function-grain trace if Q3 succeeds |
| `src/praxis/probe.py` | `praxis test review` (a neutral name, design 03 G22): the SF block, the attached-agent check, deadlines, the canary |
| `src/praxis/schema.py`, `store.py` | `sf_items` (item id, repo id, map version, level, operator id, component ids, label, generator and extractor versions, tree hash, card fields as identifiers) and `sf_responses` (item id, p on the grid, reason code from the closed set, seconds, deadline met, attached-agent result), each with the Evidence columns and CHECKs; no TEXT column for code or owner input |
| `src/praxis/report.py` | Per-level BS and BSS, V10, V11, V12, rules SF-1 to SF-3 |
| `tests/integration/test_review.py` (new) | Drives the installed binary against a real repository with a recorded responder; checks the owner tree's hash and `.git` are unchanged, the canary is absent from `$PRAXIS_HOME`, and every label is recovered |
| `docs/preregistration.md` | The section 6.3 cells, as an amendment that says why |

## 11. Design record

- Rejected: showing the diff (it turns every item into code reading, section 4.1);
  free-text explanations graded by a judge (invariants 5 and 6; it waits for design 03
  4.9); project tests as labels or detectors (invariant 9); real agent faults as the only
  criterion (their labels are unknown, so S4 stays associative and second); SF as
  practice (it would train the generator into the owner and spend the criterion);
  model-written faults or summaries (not deterministic, and on the judge path);
  module-level L3 placement and pair-guessed L2 labels (E14 trials).
- How this differs from the mutation localisation design 03 dropped (section 14): that
  probe scored code-level faults by the names of the project tests they failed. SF uses
  no test, fixes labels by construction and re-extraction, spans five levels, and shows
  no code below L4.
- What the owner must decide: whether to build SF (sections 3 to 6); whether L4 and L5,
  which show code, belong in a praxis sitting at all, given that design 03 refuses
  code-level fluency as an outcome (here they are discriminant levels only, never
  outcomes); and whether the sitting may grow past an hour or SF takes its own sitting.
