# Pre-registration

Status: DRAFT. No drill result exists yet. This file must be complete, with every "needs
measuring" resolved or explicitly deferred with a reason, before the first drill result is
stored (AGENTS.md invariant 11). After that point, every edit to this file says why in the
same commit, and the amendment log at the end records it.

## 1. Question

Does running the agent in a per-skill mode (tutor, collaborator, delegate) chosen from the
owner's measured unassisted capability change that capability, compared with weeks in
which the agent runs with no mode chosen?

## 2. Design

- Single participant (the owner). Within-person, randomized by ISO week: policy on or
  policy off, allocation drawn once per week from a seeded generator whose seed is
  committed here before week 1. Needs deciding: block randomization in pairs so that no
  run of more than two identical weeks occurs.
- Baseline: two consecutive weeks of drills with the policy off and no coaching text of
  any kind, before randomization starts.
- Minimum duration: eight randomized weeks after the baseline. No claim of any kind is
  made before the eighth week closes.
- Carryover: a skill drilled in a policy-on week is still drilled in the following week.
  Needs deciding: whether week-boundary results are excluded from the primary analysis as
  a carryover guard, and the measurement that would show carryover exists.

## 3. Outcomes

Primary: the per-skill unassisted rating (Glicko-2) on skills for which the policy fired
at least once, compared between policy-on and policy-off weeks. Rating is computed from
drill results scored by execution only (write-it, fix-it). explain-it results are
secondary until their scorer's agreement with execution-scored items is measured.

Secondary, in this order and no others:

1. Substitution rate: share of user turns classified as substitution by the judge, per
   week.
2. Human-authored lines per week, from the recorder's attribution rows (docs/design/01-recorder.md).
3. Drill completion rate: items offered against items completed, per week.
4. Self-report agreement: the owner's weekly "did I follow the mode" answer against the
   hooks' record of modes fired.

## 4. Metrics and definitions

- Skill: a ledger row with a name and at least one citation (session id, turn index).
- Unassisted: the drill harness found no attached agent process and the item was
  completed within its wall-clock cap. Needs measuring: the cap, from the baseline
  distribution of completion times.
- Rating: Glicko-2 with the parameters fixed here before week 1. Needs deciding: tau,
  the rating period (one week), and the minimum number of scored items before a rating
  is reported at all. Unrated is reported as unrated, never as a number.

## 5. Thresholds and decision rule

Needs measuring, from the two baseline weeks, before randomization begins:

- The week-to-week standard deviation of each primary and secondary metric with the
  policy off. The minimum detectable change is stated in those units.
- The decision rule: the policy is said to have changed unassisted performance only if
  the mean per-skill rating change in policy-on weeks exceeds that in policy-off weeks by
  more than the pre-stated threshold, under the permutation test in section 7, at the
  pre-stated alpha. Needs deciding: the threshold in rating points and the alpha, written
  here before week 1.

Any report that meets the rule may use the words "changed" and "under this rule". The
words cause, impact and would remain refused regardless (AGENTS.md invariant 3).

## 6. Signal admission (calibration)

A behavioural signal may enter any report or policy decision only if:

- Fit on the first half of available weeks, it predicts the held-out second half's
  per-skill rating with a correlation whose confidence interval excludes zero. Needs
  deciding: the interval level and the minimum number of held-out weeks.
- It fails to predict ratings on shuffled week labels (the placebo in section 7).

Admission is a commit that cites both results. A signal that later fails on new weeks is
removed by a commit that cites the failure.

## 7. Placebo and permutation

- Placebo: every calibration and every primary analysis is repeated with week labels
  shuffled (seeded, 1000 draws, seed committed here). A result that is not distinguishable
  from the shuffled distribution is reported as such.
- Permutation test for the primary outcome: the observed policy-on minus policy-off
  difference against the distribution under random relabelling of weeks.

## 8. Exclusions, fixed before data

- Weeks with fewer than N sessions are excluded from the primary analysis. Needs
  measuring: N, from the baseline weeks' session counts.
- Drill items whose project tests fail on the untouched code are void and are logged.
- A drill completed with an agent attached is void and is logged, and the refusal counts
  against drill completion rate.

## 9. What this design can and cannot show

It can show whether this participant's unassisted ratings moved differently in policy-on
weeks than in policy-off weeks, under the rule above. It cannot show an effect size for
anyone else, and it cannot separate the policy's effect from the effect of being measured.
The protocol in section 10 is how the second limitation is addressed over time.

## 10. Protocol

The instrument, this file and an anonymized export schema (strips paths, identifiers and
code; keeps per-week metrics and ratings) are published so that another person can run
the same baseline and eight weeks on their own machine and produce comparable rows.

## Amendment log

| Date | Section | Change | Why |
|---|---|---|---|
| 2026-10-18 | all | Skeleton created, before any data. | Phase 0. |
