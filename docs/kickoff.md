# Kickoff: a measured skill monitor and delegation policy for coding-agent work

## Outcome

After this is built I will know, from measurements I did not grade, whether my unassisted ability on the skills I use with coding agents is rising, flat or falling, skill by skill; the agent I work with will run in a different mode (tutor, collaborator, delegate) depending on which skill a prompt touches and what I have demonstrated on it; and the whole thing will exist as a pre-registered protocol another person could run on themselves and produce comparable data. "Learning" means a change in unassisted performance over time. Nothing inferred from how I phrase prompts counts as learning on its own.

This is a new repository. Praxis (github.com/0xfauzi/praxis) is archived and is not a base, a dependency or a source of code. Telltale (/home/user/telltale) is the recorder and stays a separate repository; read its AGENTS.md first, its invariants apply to anything that touches it and are the model for this repo's own. explain-pr (/home/user/explain-pr) is a Claude Code skill and a candidate scorer for the explain-back drill.

## Standards, set before the first line of product code

1. This repo gets an AGENTS.md (CLAUDE.md is `@AGENTS.md`) carrying: telltale's invariants adapted verbatim where they apply (four durable shapes and two signatures; integration tests only, in one directory; standard library only in anything that runs beside the agent; every derived number is an Evidence with a claim class, written through one store module; unknown stays unknown, never zero-fill; a claim class is never upgraded; no global configuration is ever edited without the owner's explicit per-file consent, and the tool prints the snippet by default; capture fails open), plus the Ambition section from the archived praxis CLAUDE.md. Each invariant gets a hook or validator before it gets a sentence.
2. The shared pre-commit config used across the five repos (byte-identical `.pre-commit-config.yaml` plus `scripts/precommit/`) is copied in on day one. It enforces no em dashes, no mocks outside tests, complexity and file-length ratchets, secrets and shell checks.
3. Claim classes everywhere: observed, derived, comparative, associative, predictive. A regex over prompt text is associative at best and is never labelled with a verb like "learning". The words cause, impact and would are refused in any report unless the pre-registered decision rule was met.
4. The outside judge is built before the thing it judges. Drills are code that executes; neither I nor an LLM grades them where execution can.
5. `docs/preregistration.md` is committed before any drill data exists. It fixes the metrics, thresholds, held-out split, placebo and the decision rule for "the policy changed unassisted performance". Later edits to it must say why, in the diff.
6. Local only. The only bytes that leave the machine are drill items and compacted transcripts sent to the judge model I authorize, after redaction. The judge client lives in exactly one module, enforced by a hook as telltale does for its model stack.
7. Measure, do not estimate. Any number in code, a comment or a report that was not produced by running something is written as "needs measuring" with the command that would measure it.

## Architecture (five layers, one repo plus telltale)

- **Record** (telltale, unchanged except where named below): tool calls, file reads and edits, commands classified as test/lint/typecheck with exit status, compactions, per-request tokens, model, git snapshots. It never stores prompt or assistant text.
- **Ledger** (new): per-skill table of what I delegated, what I did by hand, what I explained back, and my unassisted rating. The taxonomy is extracted from my own sessions, not a syllabus.
- **Instrument** (new): drills generated from my own agent's output, administered with no AI attached, scored by execution, scheduled by spaced repetition, feeding a per-skill rating.
- **Policy** (new): a hook package that chooses tutor, collaborator or delegate per prompt from the ledger, and injects or defers accordingly.
- **Protocol** (new): the pre-registration, the instrument and an anonymized export schema, publishable so the n=1 design can become n=many.

## Phase 1: recorder gaps (changes to telltale, under its invariants and briefs process)

- Edit attribution. From telltale's git snapshots joined to PostToolUse Write/Edit payloads, split each working-tree diff into agent-authored hunks and human-authored hunks. Ambiguous stays ambiguous; nothing is attributed by default. Output: human-authored lines per session, observed.
- Confirm or refute with a real run whether Claude Code's OTel metric `claude_code.code_edit_tool.decision` (accept/reject per edit) reaches telltale's receiver, and store it if it does. It is the cheapest "did I read it" signal available.
- Codex: verify with a real Codex binary that hooks written in the shape Codex 0.150+ accepts (map of event to list of `{matcher?, hooks: [...]}`) fire for UserPromptSubmit and PreToolUse. Record the measured result in docs/experiments.
- Join key: the Claude session UUID (`observations.provider_session_id`). The new repo reads transcript text at analysis time directly from `~/.claude/projects` and `~/.codex/sessions` and never persists it; refuse, do not guess, when a session is present on one side only.

## Phase 2: the skill ledger

- From each session's transcript and telltale activities, extract the skills touched: libraries, language features, debugging techniques, architectural moves. Every ledger row cites the session and turn it came from. Prove cardinality at the boundary: a skill that cannot be tied to a turn is not recorded.
- Classify each user turn as substitution (asked for the solution) or complement (asked for the explanation, showed an attempt, asked why), following Lehmann, Cornelius and Sting (arXiv:2409.09047). Use the judge model with a fixed prompt and record its confidence; keep no regex layer.
- Per skill: delegated count, hand-written lines (from Phase 1), explain-backs, unassisted rating (Phase 3), and my declared intent for the skill (learning, maintaining, not wanted). The delegation rule is then a lookup: delegating a skill marked learning, or any skill with no unassisted demonstration, is the event Phase 5 fires on.

## Phase 3: the instrument (built and baselined before Phases 4 and 5 start)

- `drill` command. Three item types generated from the last N days of my own agent's output:
  - write-it: a function the agent wrote is removed; I rewrite it against the project's own tests.
  - fix-it: a bug is planted in a diff the agent produced that week; I find it; scored by the project's tests.
  - explain-it: I explain a diff I accepted; scored blind by a second model against a rubric, with my identity and the week hidden. Prototype the scorer with explain-pr.
- No AI during a drill: the harness checks for attached agent processes and refuses to score if one is present; the refusal is recorded.
- Scoring by execution for write-it and fix-it. Scheduling by FSRS (fall back to SM-2 only if FSRS cannot be implemented without a dependency). Per-skill rating by Glicko-2, starting unrated, never defaulted.
- Drill cost is measured. If the daily drill exceeds ten minutes on median, redesign before continuing.
- Sanity set: the five areas of the only comparable tool found (Atrophy, open source, 2026: syntax recall, debugging, code reading, API memory, decomposition), so there is one external reference point.
- Baseline: two weeks of drills with no policy and no coaching text. Phases 4 and 5 do not start until it exists.

## Phase 4: calibration (starts with zero behavioural signals and admits only those that earn it)

- Candidate signals: substitution rate, human-authored lines, read-before-edit ratio, test runs per edit epoch, time between agent edit and my next edit, anything else telltale observes. Fit on the first half of weeks, predict unassisted ratings on the second half. Report held-out correlation per signal with confidence intervals. A signal below the pre-registered threshold is not reported anywhere, under any label.
- Placebo: repeat on shuffled week labels. Anything that survives the placebo is noise and is removed.
- The gap series: assisted output quality (project tests passing in-session, agent-authored lines accepted) against unassisted rating, per skill. Diverging series are the finding; name the skills.

## Phase 5: the policy, run as an n-of-1 trial

- Per prompt, the hook package looks up the skills the prompt touches and chooses a mode:
  - tutor: UserPromptSubmit injects context instructing the agent to give hints and ask for my attempt before producing code (the Bastani et al. PNAS 2025 design: hints-only GPT did no exam harm, answer-giving GPT cut exam scores 17%); PreToolUse defers Edit on files in that skill until I have written an attempt.
  - collaborator: the agent proceeds, and the prompt gets one line naming the skill and offering an explain-back afterwards.
  - delegate: nothing injected.
  The mode never blocks a prompt outright and never alters output bytes or exit codes.
- Randomize the policy on and off by week. Pre-registered primary outcome: unassisted rating on skills the policy fired for, policy weeks against control weeks. Secondary: substitution rate, human-authored lines, drill completion. Minimum eight weeks before any claim.
- Weekly review leads with the gap series and the trial status. It contains no causal language unless the pre-registered rule was met. It records my self-report (did I follow the mode this week) beside the data, because the gap between those two is itself a signal.

## Phase 6: the protocol

- Publish the pre-registration, the instrument and the export schema so someone else can run the same eight weeks on their own machine and produce rows comparable to mine. Anonymized export is opt-in and strips paths, identifiers and code.
- State plainly in the README what n=1 can show (within-person change under a randomized policy) and what it cannot (an effect size for anyone else).

## Range: fields to run as parallel subagents before Phase 3 design is final, with a red team

- Learning science: item generation, retrieval practice, spacing, transfer vs recall. Verified sources: Bastani et al. PNAS 2025; Shen and Tamkin arXiv:2601.20245; Roediger and Karpicke 2006; Bjork 1994; Liu et al. arXiv:2604.04721; Fan et al. BJET 2025; Lehmann et al. arXiv:2409.09047.
- Psychometrics: Glicko-2 on sparse heterogeneous items; minimum items before a rating is shown; item difficulty drift when items are generated from one person's code.
- Clinical trial design: n-of-1 and interrupted time series; carryover between weeks; what eight weeks of one participant can and cannot show.
- Human-computer interaction: interruption cost; when an in-prompt cue is ignored or resented; measuring whether tutor mode changes behaviour or just adds friction.
- Software measurement and git forensics: edit attribution, hunk-level provenance, what is unattributable in principle.
- Security and privacy: what drill items and compacted transcripts leak to the judge provider; redaction before send; what the export schema must strip.
- Red team: assume the system is built. Find the three ways I would game it, the three ways it would mislabel me, and the way it makes me worse (drills that train for the drill; tutor mode that teaches me to phrase around it). Each finding becomes a test or a pre-registered exclusion.

## Belief

The target is the hard version: a per-skill unassisted rating, a held-out validated predictor, a completed eight-week randomized n-of-1 result, and a protocol another person can run. Not a dashboard. Ship the ugly end-to-end proof first: one skill, one write-it drill, one tutor-mode cue, one week, all wired through telltale, before generalizing anything. Try edit attribution and drill generation in parallel; whichever stalls, report the stall with the measurement that shows it. Stop only when every phase has run against my real data and the pre-registered judge has returned a result. Then report the closest result, the gap to the target, and what would close it.

## Before starting

Rewrite this through the four lenses in the Ambition section, show me the few-line version, and name the access you need that you do not have: the repository name and its creation, my real `~/.claude` and `~/.codex` session files, a Codex binary for the hook test, a judge API key, telltale write access under its briefs process, and the owner's per-file consent before any hook is installed into global settings.
