# Kickoff: a measured skill monitor and delegation policy for coding-agent work

## Outcome

After this is built I will know, from measurements I did not grade, whether my unassisted understanding of the systems my agents build (their components, how they connect, how the agent changed them) and my ability to anticipate and catch the agent's work are rising, flat or falling, repository by repository and component by component; the agent I work with will run in a different mode (tutor, collaborator, delegate) depending on which components a prompt touches and what I have demonstrated on them; and the whole thing will exist as a pre-registered protocol another person could run on themselves and produce comparable data. "Learning" means a change in delayed, unassisted structural-probe accuracy over time (P1, docs/design/03-measurement.md). Nothing inferred from how I phrase prompts counts as learning on its own.

This is a new repository. The first praxis is archived as `praxis-v0` and is not a base, a dependency or a source of code. Telltale is a separate repository whose invariants were the model for this repo's own; it was evaluated as the recorder and failed the attribution requirement (`docs/experiments/E01.md`), so praxis records its own evidence and telltale is at most a read-only import source. explain-pr is a Claude Code skill and a candidate scorer for the judge-dependent teach-back (J1).

## Standards, set before the first line of product code

1. This repo gets an AGENTS.md (CLAUDE.md is `@AGENTS.md`) carrying: telltale's invariants adapted verbatim where they apply (four durable shapes and two signatures; integration tests only, in one directory; standard library only in anything that runs beside the agent; every derived number is an Evidence with a claim class, written through one store module; unknown stays unknown, never zero-fill; a claim class is never upgraded; no global configuration is ever edited without the owner's explicit per-file consent, and the tool prints the snippet by default; capture fails open), plus the Ambition section from the archived praxis CLAUDE.md. Each invariant gets a hook or validator before it gets a sentence.
2. The shared pre-commit config used across the five repos (byte-identical `.pre-commit-config.yaml` plus `scripts/precommit/`) is copied in on day one. It enforces no em dashes, no mocks outside tests, complexity and file-length ratchets, secrets and shell checks.
3. Claim classes everywhere: observed, derived, comparative, associative, predictive. A regex over prompt text is associative at best and is never labelled with a verb like "learning". The words cause, impact and would are refused in any report unless the pre-registered decision rule was met.
4. The outside judge is built before the thing it judges. Probes are scored by deterministic extractors over the repository and by the recorder; no project test is ever a score, and neither I nor an LLM grades what an extractor can.
5. `docs/preregistration.md` is committed in two stages: Stage 1 before any task, probe or forecast row exists, Stage 2 before the first randomized task. It fixes the metrics, thresholds, held-out split, placebo and the decision rule for "the policy changed unassisted performance". Later edits to it must say why, in the diff.
6. Local only. The only bytes that leave the machine are teach-back answers and compacted transcripts sent to the judge model I authorize, after redaction. The judge client lives in exactly one module, enforced by a hook as telltale does for its model stack.
7. Measure, do not estimate. Any number in code, a comment or a report that was not produced by running something is written as "needs measuring" with the command that would measure it.

## Architecture (five layers, one repo)

- **Record** (`praxis hook`, `docs/design/01-recorder.md`): line-hash multiset snapshots of the working tree before and after every mutating tool call and at every prompt boundary, from which agent, human, pre-existing and ambiguous line counts are derived per file. Hashes and counts only; no prompt, assistant or file text is ever stored. Telltale's per-request tokens, compactions and verification runs can be joined read-only on the session id when its store is present.
- **Ledger** (new): per-skill table of what I delegated, what I did by hand, what I explained back, and my P1 score on the components keyed to each skill. The taxonomy is extracted from my own sessions, not a syllabus.
- **Instrument** (new): delayed structural probes about the components my agents built (imports, runtime traces of named scenarios, schemas), answered with no AI attached and scored by deterministic extractors; a per-task randomized prediction step before the agent acts; drift, boundary-enforcement and decision-recall measures (docs/design/03-measurement.md).
- **Policy** (new): a hook package that chooses tutor, collaborator or delegate per prompt from the ledger, and injects accordingly.
- **Protocol** (new): the pre-registration, the instrument and an anonymized export schema, publishable so the n=1 design can become n=many.

## Phase 1: the recorder (built here; see docs/design/01-recorder.md)

- `praxis hook`: one command for every hook event, exit 0 whatever happens. SessionStart takes the baseline snapshot; PreToolUse and PostToolUse around Edit, Write, MultiEdit, NotebookEdit and Bash take the pre and post snapshots whose difference is the agent's delta; UserPromptSubmit and Stop take the boundary snapshot that is attributed against the agent multiset and the baseline. Per-file counts of agent, human, pre-existing, ambiguous and reverted lines are written as Evidence rows with claim class derived.
- Judge first: the integration suite drives the installed binary with payloads in the recorded Claude Code 2.1.257 shape against a real git repository, with the expected counts written before each scenario runs. Garbage on stdin, no git repository, a repository with no commits, binary and oversize files all exit 0 and record why nothing was attributed.
- `praxis show install claude` and `praxis show install codex` print the hook blocks; `praxis install <agent>` merges one into the named file after showing the change and taking a yes on a terminal, keeps the previous bytes under `$PRAXIS_HOME`, and refuses without a terminal; `praxis remove <agent>` undoes it the same way. `praxis show status` reports what is installed and recorded, read-only.
- Codex: verify with a real Codex binary that the printed shape fires for the six events. Record the measured result in docs/experiments. Needs the owner's machine.
- Transcript join: the session id in the hook payload is the transcript's filename stem under `~/.claude/projects`. Text is read at analysis time and never persisted; refuse, do not guess, when a session has ledger rows and no transcript or the reverse.

## Phase 2: the skill ledger

- From each session's transcript and recorder rows, extract the skills touched: libraries, language features, debugging techniques, architectural moves. Every ledger row cites the session and turn it came from. Prove cardinality at the boundary: a skill that cannot be tied to a turn is not recorded.
- Classify each user turn as substitution (asked for the solution) or complement (asked for the explanation, showed an attempt, asked why), following Lehmann, Cornelius and Sting (arXiv:2409.09047). Use the judge model with a fixed prompt and record its confidence; keep no regex layer.
- Per skill: delegated count, hand-written lines (from Phase 1), explain-backs, P1 on the components keyed to the skill (Phase 3), and my declared intent for the skill (learning, maintaining, not wanted). The delegation rule is then a lookup: delegating a skill marked learning, or any skill with no unassisted demonstration, is the event Phase 5 fires on.
- Skills are keyed to components of the frozen component map where the policy needs them (docs/design/03-measurement.md section 2).

## Phase 3: the instrument (built and baselined before Phases 4 and 5 start)

- Superseded on 2026-10-19 by docs/design/03-measurement.md. The write-it, fix-it and explain-it items, scoring by project tests, FSRS scheduling and Glicko-2 ratings are withdrawn: write-it and fix-it measure code-level fluency and are scored by tests the agent often wrote; Glicko-2 cannot estimate one-off item difficulty with one respondent.
- First, the recorder fixes in 03 section 5 (apply_patch, base-sha diffing, prompt_id and turn_id, edges at Stop). No outcome data before them.
- Then `praxis show components`, `praxis start task`, the `praxis test` commands and `praxis show task`, with the extractor experiments E03 to E08 run before their extractors are trusted. E09 (agent replay, V4) and E10 (runtime imports against X1, V2) are not run: X1 static-import validity is assumed, with dynamic imports counted as blind spots, and the published SWE-bench run-to-run variation stands as the predictability prior (docs/preregistration.md).
- No AI during a probe: the attached-agent check is kept and refusals are recorded.
- Cost is measured. If the daily median of task plus probe time exceeds ten minutes, redesign before continuing.
- Pilot under the Stage 1 pre-registration, then the validity checks and the falsification table in 03 section 8, as Stage 1 fixes them. Phases 4 and 5 do not start until they pass.
- explain-it survives only as the judge-dependent teach-back J1, outside the primary path, once a judge key exists.

## Phase 4: calibration (starts with zero behavioural signals and admits only those that earn it)

- Candidate signals: substitution rate, human-authored lines, read-before-edit ratio, test runs per edit epoch, time between agent edit and my next edit, anything else the recorder observes. Fit on the first half of weeks, predict the P1 structural probe score on the second half. Report held-out correlation per signal with confidence intervals. A signal below the pre-registered threshold is not reported anywhere, under any label.
- Placebo: repeat on shuffled week labels. Anything that survives the placebo is noise and is removed.
- The gap series: agent-authored lines accepted and components changed per week against P1 per component; no project test enters either series. Diverging series are the finding; name the components.

## Phase 5: the policy, run as an n-of-1 trial

- Per prompt, the hook package looks up the components the prompt touches and chooses a mode:
  - tutor: UserPromptSubmit injects context instructing the agent to give hints and ask for my attempt before producing code (the Bastani et al. PNAS 2025 design: hints-only GPT did no exam harm, answer-giving GPT cut exam scores 17%). An attempt gate needs redesign: PreToolUse defer is ignored in interactive Claude Code.
  - collaborator: the agent proceeds, and the prompt gets one line naming the skill and offering an explain-back afterwards.
  - delegate: nothing injected.
  The mode never blocks a prompt outright and never alters output bytes or exit codes.
- Randomize the policy with an allocation whose smallest attainable p is below the pre-registered alpha (unrestricted 4-of-8 weeks, at least 12 pair-blocked weeks, or a multiple baseline across repositories); 8 pair-blocked weeks cannot reach 0.05. Pre-registered primary outcome: P1 on components the policy fired for. Secondary: substitution rate, human-authored lines, probe completion. No claim before the pre-registered minimum.
- Weekly review leads with the gap series and the trial status. It contains no causal language unless the pre-registered rule was met. It records my self-report (did I follow the mode this week) beside the data, because the gap between those two is itself a signal.

## Phase 6: the protocol

- Publish the pre-registration, the instrument and the export schema so someone else can run the same pilot and trial on their own machine and produce rows comparable to mine. Anonymized export is opt-in and strips paths, identifiers and code.
- State plainly in the README what n=1 can show (within-person change under a randomized policy) and what it cannot (an effect size for anyone else).

## Range: fields to run as parallel subagents before Phase 3 design is final, with a red team

- Learning science: item generation, retrieval practice, spacing, transfer vs recall. Verified sources: Bastani et al. PNAS 2025; Shen and Tamkin arXiv:2601.20245; Roediger and Karpicke 2006; Bjork 1994; Liu et al. arXiv:2604.04721; Fan et al. BJET 2025; Lehmann et al. arXiv:2409.09047.
- Psychometrics: resolved in docs/design/03-measurement.md: proper scoring on a fixed grid with skill over mechanical references replaces Glicko-2.
- Clinical trial design: n-of-1 and interrupted time series; carryover between weeks; what eight weeks of one participant can and cannot show.
- Human-computer interaction: interruption cost; when an in-prompt cue is ignored or resented; measuring whether tutor mode changes behaviour or just adds friction.
- Software measurement and git forensics: edit attribution, hunk-level provenance, what is unattributable in principle.
- Security and privacy: what teach-back answers and compacted transcripts leak to the judge provider; redaction before send; what the export schema must strip.
- Red team: assume the system is built. Find the three ways I would game it, the three ways it would mislabel me, and the way it makes me worse (probes that train for the probe; tutor mode that teaches me to phrase around it). Each finding becomes a test or a pre-registered exclusion.

## Belief

The target is the hard version: a per-component delayed unassisted probe score, a held-out validated predictor, a completed randomized n-of-1 result, and a protocol another person can run. Not a dashboard. Ship the ugly end-to-end proof first: one repository, one registered task through the fixed recorder, one delayed structural probe scored by X1, one tutor-mode cue, one week, all wired through the recorder, before generalizing anything. Try edit attribution and probe generation in parallel; whichever stalls, report the stall with the measurement that shows it. Stop only when every phase has run against my real data and the pre-registered judge has returned a result. Then report the closest result, the gap to the target, and what would close it.

## Before starting

Rewrite this through the four lenses in the Ambition section, show me the few-line version, and name the access you need that you do not have: the repository name and its creation, my real `~/.claude` and `~/.codex` session files, a Codex binary for the hook test, a judge API key, and the owner's per-file consent before any hook is installed into global settings.
