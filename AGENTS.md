# Praxis

Praxis measures, with the agent switched off, how well its owner understands the systems
their coding agents build for them at the level of components (what the parts are, how they
connect, how the agent changed them) and how well they anticipate and catch the agent's
work; and it chooses, per prompt, which mode the agent runs in: tutor, collaborator or
delegate. It records its own evidence through the agent's hooks
(`docs/design/01-recorder.md`); telltale, a separate repository, is an optional read-only
import source and never a dependency (`docs/experiments/E01.md` is the measurement behind
that decision). The first praxis (archived as `praxis-v0`) inferred learning from the
wording of prompts and never measured unassisted capability; nothing from it is a base, a
dependency or a source of code here. The recorder is designed in
`docs/design/01-recorder.md`, the measurement in `docs/design/03-measurement.md` (which
replaces the rejected `docs/design/02-instrument.md`), and everything else in
`docs/kickoff.md` until a design document replaces it; the study protocol is
`docs/preregistration.md`. This file holds the invariants and the commands. Enforceable
rules live in hooks and validators, not in prose.

## Commands

- `uv sync` - install or refresh the environment. Installs the `dev` group and nothing else.
- `uv run pytest -m integration` - the suite. Every test is an integration test and there is
  one test directory; see the invariants below.
- `uv run mypy .` - strict typing, the same config CI runs. Never weaken the config to make
  code pass.
- `uv run ruff check --fix . && uv run ruff format .` - lint then format, in that order.
- `uv run deptry .` - dependency hygiene. One package, one dependency list, one run.
- `pre-commit run --all-files` - everything fast. Hooks auto-fix, so re-stage and retry on
  the first failure.

## Architecture invariants (violating any is a bug, not a style choice)

1. **Five layers, and the recorder records only what the others need.** Record, Ledger,
   Instrument, Policy, Protocol. The recorder is `praxis hook`: command hooks that snapshot
   the working tree as a multiset of line hashes relative to the session's base commit,
   before and after every mutating tool call and at every prompt boundary, and record the
   import edges of the Python files a segment changed, so that attribution falls out of one
   mechanism for every tool. It stores hashes, counts and identifiers, never lines. Any
   other recorder's store (telltale's included) is opened read-only through one function in
   `store.py` and never written.
2. **Every derived number is an Evidence, and it is written through store.py.** Evidence
   carries its claim class (observed, derived, comparative, associative, predictive), its
   coverage, its non-empty source list, its reducer version, its assumptions and its
   warnings. The `derived-writes-only-in-store` hook keeps the INSERT statements for the
   derived tables in `store.py`, so the CHECK constraints and the one constructor are the
   only way in.
3. **A claim class is never upgraded.** A regex over prompt text is associative at best and
   is never labelled with a verb like learning. A report may narrow a claim or drop it.
   The words cause, impact and would are refused in any report unless the pre-registered
   decision rule was met, and the report says which rule.
4. **Unknown stays unknown. Never zero-fill, never default.** A missing value is `None`
   through the model, the ledger and the report. A component with no probe result is
   unscored, not scored at a starting number, and a forecast left blank is unanswered, never
   filled with a default probability; a reference forecaster that cannot yet be fitted gives
   NULL skill, never a fallback reference. "0 delegations" and "delegation was not
   observable for this session" are different statements that the coverage field keeps
   apart.
5. **No prompt, assistant or code text is ever persisted.** Transcripts are read at analysis
   time from where the agent wrote them and the store holds only what was derived, with
   its source cited as session id and turn index. The identifiers that name a thing (a
   repo-relative path, a function's qualified name, a repository's hashed root) are
   stored; no line of anyone's code is. Code that must execute for a measurement (an
   extractor scenario, a knockout, an agent replay) runs in a scratch clone under
   `$PRAXIS_HOME` that is deleted when the run ends; the owner's working tree and `.git` are
   never written. Owner input is closed vocabulary (grid probabilities, component ids,
   option indices, reason codes); free recall typed into praxis is displayed and discarded
   and not even its length is kept. The integration suite reads the bytes of every file
   under `$PRAXIS_HOME`, with planted canaries, to enforce the absence.
6. **Standard library only beside the agent, and the judge client lives in one module.**
   `dependencies` in pyproject.toml is empty and stays empty. The one network client, to the
   judge model, is `src/praxis/judge.py`; the `judge-isolation` hook refuses
   `urllib.request` anywhere else. The redaction that runs before a send lives beside it.
7. **Hooks fail open and change nothing.** A praxis hook never blocks a prompt, never alters
   the agent's output bytes or exit code, exits 0 whatever happened, and gives up silently
   at its timeout. Its only channels are `additionalContext` and, at the Stop that closes a
   registered task's scored segment, one `terminalSequence` notice from a closed vocabulary,
   which Claude Code writes to the owner's terminal and never to the agent. A PreToolUse
   `defer` is not a channel: Claude Code ignores it in interactive sessions.
8. **No global configuration is ever edited without the owner's per-file consent.**
   `~/.claude/settings.json`, `~/.codex/config.toml` and everything else outside this
   repository and `$PRAXIS_HOME` are read-only by default. `praxis setup` PRINTS the
   snippet; `--apply` writes exactly one named file after an interactive yes and refuses
   when stdin is not a TTY.
9. **Probes are answered unassisted and scored by deterministic extractors, never by
   tests.** A structural, drift, decision or knockout item is answered with no agent process
   attached (the harness refuses and records the refusal) and scored against the repository
   as a versioned extractor reads it (imports, runtime component traces of owner-named
   scenarios, schemas) or against the recorder's rows. No project test is a score, an item
   source or a symptom, whoever wrote it. Neither the owner nor a model grades what an
   extractor can grade; anything only a judge can score waits for the judge and stays out of
   the primary path.
10. **The ledger starts with zero behavioural signals.** A signal is admitted to any report
    or policy decision only after it has predicted the primary structural probe score (P1 in
    `docs/design/03-measurement.md`) on held-out weeks and survived the placebo named in
    `docs/preregistration.md`. Admission is a commit that cites the result.
11. **Pre-registration precedes data, in two stages.** Stage 1 of `docs/preregistration.md`
    is committed before the first task, probe or forecast row is stored: what the pilot
    collects, the committed script that turns pilot data into every parameter, each validity
    threshold and the plan it triggers, and the rule that pilot data never enters a
    confirmatory test. Stage 2, generated by that script, fixes metrics, thresholds, the
    held-out split, the placebo and the decision rule before the first randomized task. A
    later edit to either says why in the same commit.
12. **Integration tests only, in one directory.** `tests/integration/` is the whole test
    tree. `tests/integration/conftest.py` refuses to run while a test file exists anywhere
    else, and the `tests-live-under-integration` hook refuses to commit one. The hooks are
    the system: a hook tested with a stubbed payload or a stubbed git is a test of the stub,
    so the suite drives the installed `praxis` binary with recorded payloads against a real
    repository.
13. **Duplicate is not one.** Attribution and every survival lookup match multisets: two
    identical lines are two lines, and a line the agent added that the diff no longer holds
    is ambiguous, never folded into human. Content-identical lines from two authors are
    indistinguishable by construction and the assumptions field says so on every row.

## Style

- Python 3.12, uv only, pyproject.toml only. No emoji anywhere. No em dashes anywhere: a
  pre-commit hook blocks them, so use a hyphen, a colon or a full stop.
- Deep modules, thin interfaces. Match the idioms of the module you are editing.
- Comments state constraints the code cannot show, and nothing else.
- Never guess a number. Measure it, or write "needs measuring" and say what would measure
  it.

## Constraint intake

New rule or constraint? Enforce it first: a schema, a validator, a pre-commit hook or a CI
check. This file takes only what cannot be enforced. When it grows, convert lines into
hooks and cut them.

## Ambition

For any non-trivial task, rewrite it through the four lenses below before starting, show me
the rewritten version in a few lines, then do that version. Skip this for lookups and small
edits.

### Altitude: work on the outcome, not the request

- Ask what I want to happen after this task is done. Aim at that.
- If my request is a means to an end, name the end and say whether a better means exists.
- Prefer durable workflows that keep the outcome true without me re-tasking you.
- If owning the outcome end to end needs access, data, machines or permissions you don't
  have, ask for them before starting.

### Depth: do it the way the best person in the field would

Answer these four questions first:

1. What would a top professional do here that I didn't ask for?
2. What raw material would they start from instead of what I gave you?
3. What specific methods and tools would they use?
4. What, other than you or me, would show the result is right?

Then:

- Use the field's real instruments (databases, statistical models, profilers, protocol
  analysers, test harnesses, formal methods). Changing vocabulary is not changing method.
- Go to the raw data and try to reproduce or break my stated beliefs.
- Once you are deep in the data, produce something new: a finding, model or signal nobody
  had. Show that it holds on data it has not seen.
- Build an outside judge before the solution: a test, harness or tool you can run for
  immediate feedback. Never grade your own work.

### Range: bring in fields the task doesn't obviously need

- Restate the task's nouns as verbs. For example, a review deck is "getting busy people to
  believe a cause-and-effect story and decide under uncertainty in 30 minutes".
- List the fields those verbs touch, including unexpected ones, and include a red team.
- Use what each field does, not how it talks. Never role-play.
- Run independent fields as parallel subagents.

### Belief: set the hard version and keep going

- Raise the goal. If I ask for 5 seconds, aim for 200 ms. If I ask for two users, design
  for ten.
- Treat hard goals as solvable. When something looks impossible, find who has done
  something similar and work from there.
- Fail fast, try several routes in parallel, and ship an ugly end-to-end proof early.
- Do not stop at the first obstacle. Stop only when every route has been tried and the
  judge has given its result. Then report the closest result, the gap, and what would
  close it.

## Verification (none of these can be enforced by a hook)

- **A test that passes against the un-fixed code is not a test.** Break the fix, watch the
  test fail, restore it.
- **Before writing that something is missing, unspecified, impossible or out of scope,
  search for it and cite the search.**
- **Absence is not zero and duplicate is not one.** `set`, `dict`, `.get()`, `continue` and
  `return {}` over untrusted structure all convert "I cannot tell" into a confident answer.
  Prove cardinality once, at the boundary, and refuse there.
