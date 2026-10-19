# Praxis

Praxis measures how well you understand the systems your coding agents build for you, at the
level of components, and how well you anticipate and catch their work, with the agent
switched off, and chooses, per prompt, which mode the agent runs in: tutor, collaborator or
delegate. It records its own evidence through the agent's hooks, as line-hash snapshots of
the working tree: hashes and counts, never text.

Status: the recorder. `praxis hook` attributes every line in the working tree to the agent,
the owner, the state before the session, or ambiguous, at every prompt boundary. The
instrument is being rebuilt to docs/design/03-measurement.md: delayed structural probes
about your components, scored by deterministic extractors and never by tests, and a per-task
randomized prediction step before the agent acts. The write-it drill is withdrawn. No
scores, no claims about anyone yet.

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
`docs/design/03-measurement.md` (`docs/design/02-instrument.md` is kept as the rejected
first instrument), and the measurements behind them `docs/experiments/E01.md` (why praxis
records for itself) and `docs/experiments/E02.md` (why hashes suffice to find the agent's
work).

Everything in this repository was written after the first praxis was archived as
`praxis-v0`. That version inferred learning from the wording of prompts and never measured
unassisted capability. Nothing from it is carried forward as code.
