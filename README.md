# Praxis

Praxis measures how well you understand the systems your coding agents build for you, at the
level of components, and how well you anticipate and catch their work, with the agent
switched off, and chooses, per prompt, which mode the agent runs in: tutor, collaborator or
delegate. It records its own evidence through the agent's hooks, as line-hash snapshots of
the working tree: hashes and counts, never text.

Status: the recorder and the instrument of `docs/design/03-measurement.md` are built, and
the pilot has not started. At every prompt boundary, `praxis hook` gives each changed line
an author: the agent, the owner, the state before the session, or ambiguous. It also
records import edges and the scored segment of each task. The `praxis test` commands ask
questions with no agent attached. Deterministic extractors and the recorder's rows score
the answers, never tests. The write-it drill is withdrawn. There are no scores and no
claims about anyone yet.

## System map

The map of praxis's components, flows and sequences is written with
[systemap](https://github.com/0xfauzi/systemap) and checked on every pull request, where the
workflow posts the architecture delta as a comment. The published map is at
<https://0xfauzi.github.io/praxis/map/>; the same figures are in the repository at
[`docs/map/figures/system.svg`](docs/map/figures/system.svg) and
[`docs/map/figures/structure.svg`](docs/map/figures/structure.svg), and the model that
produces them is `map/model.py`, `map/journeys.py` and `map/contract.py`.

## Install, on the machine where the agents run

```
uv tool install git+https://github.com/0xfauzi/praxis   # or `uv sync` in a checkout
praxis show install claude   # shows the hook settings and writes no file
praxis install claude        # shows the change to ~/.claude/settings.json, asks, writes
praxis install codex         # the same for ~/.codex/hooks.json
praxis show status           # what is installed, recorded and due
```

Praxis writes no file outside this repository and `$PRAXIS_HOME` until you type yes on a
terminal. It keeps the previous bytes of each file that it writes under
`$PRAXIS_HOME/settings-backups/` (`$PRAXIS_HOME` is `~/.praxis` by default). `praxis remove
AGENT` removes the hook settings in the same way.

## Commands

Each command starts with a verb, and most commands then name an object. Run `praxis` with
no words to show this list.

| Command | What it does |
|---|---|
| `praxis show status` | Show the hooks, the ledger counts, the component maps, the tasks and the tests to do. |
| `praxis show install AGENT` | Show the hook settings that the agent needs. This command writes no file. |
| `praxis install AGENT` | Write the hook settings into the agent settings file after you type yes. |
| `praxis remove AGENT` | Remove the praxis hook settings from the agent settings file after you type yes. |
| `praxis make components [--write]` | Make a component map draft from the imports. With `--write`, write it to a file that you can edit. |
| `praxis set components [--reason REASON]` | Set the component map and the test items for this repository. |
| `praxis show components` | Show the set component map and the count of test items that are left. |
| `praxis start task` | Record a task before you write its prompt. A treatment task asks for your predictions. |
| `praxis show task` | Show the result of the last closed task. |
| `praxis test structure` | Answer questions about the imports between components, with no agent attached. |
| `praxis test changes` | Select the components that changed in the last week, with no agent attached. |
| `praxis test decisions` | Select the answer that you gave to a past agent question, with no agent attached. |
| `praxis test knockout` | Select the component that a scenario cannot run without, with no agent attached. |
| `praxis test pathfinder` | Give a number for how closely each pair of components is related, with no agent attached. |
| `praxis show scenarios` | Show each scenario in the scenario file and its last run. |
| `praxis run scenarios` | Run each scenario in a scratch clone and record the components that it enters. |
| `praxis show report` | Show the scores and their analysis for this repository. |
| `praxis show lines [--session ID] [--json]` | Show the line counts by author for each file at each hook event. |
| `praxis show sessions` | Show each session with its line counts at its last hook event. |
| `praxis hook` | Record one hook event from the agent. Only the agent runs this command. |

AGENT is `claude` or `codex`. `praxis --version` shows the version. `praxis hook` keeps its
name because it is the command that the agent settings files contain.

Owner-facing text (command names, help, prompts, refusals, results) is written to the
ASD-STE100 common rules; dictionary acceptance needs the official dictionary. The rules
are in `.claude/skills/systemap/references/language.md`.

## Glossary

| Term | Meaning |
|---|---|
| agent | A coding agent, Claude Code or Codex, that changes the repository for the owner. |
| owner | The person who uses praxis and whose understanding praxis measures. |
| session | One continuous period of work of a coding agent. |
| hook event | One run of `praxis hook` by the agent: at a session start, a prompt, a tool call or a stop. |
| ledger | The SQLite file `$PRAXIS_HOME/ledger.db`, which holds hashes, counts, identifiers and scores, and no line of code or prompt text. |
| component | A named set of repository paths that the glob rules of the component map give to it. |
| component map | The ordered glob rules that give each repository path one component; `praxis set components` fixes the map for a repository. |
| extractor | A versioned program that reads the repository and gives the correct answer to an item, for example X1 for static imports. |
| item | One closed question about a relation between two components, for example whether code in one component imports code in the other. |
| epoch | The period during which the correct answer to an item stays the same; a change of the answer starts a new epoch. |
| probe | The measurement that a `praxis test` command makes: the owner answers items with no agent attached. |
| task | A piece of agent work that the owner records with `praxis start task` before writing its prompt. |
| arm | The condition of a task, treatment or control: only a treatment task asks for predictions before the work. |
| segment | The scored part of a task: the agent work from its prompt to the first Stop after the agent changes a file. |
| scenario | A real operation of the system, as a command in the owner's scenario file, that praxis runs to see which components it enters. |
| scratch clone | A temporary `git clone --local` copy of the repository under `$PRAXIS_HOME/scratch/`, deleted when the run ends. |
| knockout | A test in which praxis selects one component at random, shows the scenarios that cannot run without it, and the owner names the component. |
| pathfinder | A test in which the owner rates how closely each pair of components is related, and praxis compares that network with the imports. |
| Brier score | The squared difference between a probability and the outcome, 0 or 1; a lower score is better. |

The standards are `AGENTS.md`, the plan `docs/kickoff.md`, the protocol
`docs/preregistration.md`, the designs `docs/design/01-recorder.md` and
`docs/design/03-measurement.md` (`docs/design/02-instrument.md` is kept as the rejected
first instrument), and the measurements behind them `docs/experiments/E01.md` (why praxis
records for itself) and `docs/experiments/E02.md` (why hashes suffice to find the agent's
work).

Everything in this repository was written after the first praxis was archived as
`praxis-v0`. That version inferred learning from the wording of prompts and never measured
unassisted capability. Nothing from it is carried forward as code.
