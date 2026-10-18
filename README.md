# Praxis

Praxis measures what you can do on the skills you use with coding agents when the agent is
switched off, and chooses, per prompt and per skill, which mode the agent runs in: tutor,
collaborator or delegate. It records its own evidence through the agent's hooks, as line-hash
snapshots of the working tree: hashes and counts, never text.

Status: Phase 1, the recorder. `praxis hook` attributes every line in the working tree to
the agent, the owner, the state before the session, or ambiguous, at every prompt boundary.
The standards are `AGENTS.md`, the plan `docs/kickoff.md`, the protocol
`docs/preregistration.md`, the recorder design `docs/design/01-recorder.md`, and the
measurement that chose this design over an external recorder `docs/experiments/E01.md`.

Everything in this repository was written after the first praxis was archived as
`praxis-v0`. That version inferred learning from the wording of prompts and never measured
unassisted capability. Nothing from it is carried forward as code.
