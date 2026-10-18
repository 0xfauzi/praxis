# Praxis

Praxis measures what you can do on the skills you use with coding agents when the agent is
switched off, and chooses, per prompt and per skill, which mode the agent runs in: tutor,
collaborator or delegate. It records its own evidence through the agent's hooks, as line-hash
snapshots of the working tree: hashes and counts, never text.

Status: the recorder and the first item type of the instrument. `praxis hook` attributes
every line in the working tree to the agent, the owner, the state before the session, or
ambiguous, at every prompt boundary. `praxis drill` strips the body of a function the agent
wrote, in place and with no agent attached, and scores what the owner writes by the
project's own tests. No ratings, no scheduling, no claims about anyone yet.

## Install, on the machine where the agents run

```
uv tool install git+https://github.com/0xfauzi/praxis   # or `uv sync` in a checkout
praxis setup claude --apply    # shows the change to ~/.claude/settings.json, asks, writes
praxis setup codex --apply     # the same for ~/.codex/hooks.json
praxis doctor                  # what is installed, recorded and pending
```

Nothing is written outside this repository without a yes typed on a terminal, and the
previous bytes of any file written are kept under `~/.praxis/settings-backups/`.
`praxis setup <agent> --remove` takes praxis out again the same way.

The standards are `AGENTS.md`, the plan `docs/kickoff.md`, the protocol
`docs/preregistration.md`, the designs `docs/design/01-recorder.md` and
`docs/design/02-instrument.md`, and the measurements behind them `docs/experiments/E01.md`
(why praxis records for itself) and `docs/experiments/E02.md` (why hashes suffice to find
the agent's work).

Everything in this repository was written after the first praxis was archived as
`praxis-v0`. That version inferred learning from the wording of prompts and never measured
unassisted capability. Nothing from it is carried forward as code.
