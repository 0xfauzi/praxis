# 02: the instrument, write-it items

## What it answers

Can the owner, with no agent present, produce the body of a function the agent wrote for
them, well enough that the project's own tests pass? One item is one function; one result
is pass or fail by execution, with the time it took. Ratings over items are a later layer
(docs/preregistration.md section 4); this document is the item.

## Where items come from

The recorder's agent deltas are hashes of lines. Hashing every line of the current file
the same way and looking each up recovers, without a byte of stored text, which lines of
the file as it stands today the agent wrote (proved in the span-recovery run described in
docs/experiments/E02.md). Python's `ast` then gives the functions, and a function whose
body is mostly agent lines is a candidate.

Candidates are tied to a repository by `repo_id = sha256(git toplevel path)[:16]`, which
the recorder stores per session in `session_repos`. The path itself is not stored.

The selection threshold, the fraction of a function's lines the agent wrote, is a
parameter with a default of 0.5. The default is a guess and is recorded on every item so
that the preregistration can set it from the baseline weeks rather than from here.

## Validation by execution, both directions

An item is valid only if the project's tests, run by the command the owner configured for
this repository, pass on the untouched file and fail with the function's body replaced by
`raise NotImplementedError`. The first failure means the item would be void whatever the
owner wrote; the second means the tests do not reach the function and a pass would prove
nothing. Both outcomes are recorded as void items with the reason, never silently skipped.

The docstring stays when the body is stripped: it is the specification the owner writes
to, and it is their own project's text on their own screen, not anything the store holds.

## The drill

`praxis drill`, run by the owner in a terminal inside the repository:

1. Refuses, and records the refusal as a result, if an agent process is attached. On
   Linux that means a process whose executable or first argument is named `claude` or
   `codex` and whose working directory is inside the repository (read from `/proc`).
   Where `/proc` is unavailable the rule is stricter: any such process anywhere refuses.
   What was found is written on the result's warnings.
2. Picks the valid candidate with the highest agent fraction, breaking ties by path.
3. Writes the original bytes of the one file to `$PRAXIS_HOME/drills/<item_id>/original`,
   then strips the body in place and prints the file, the function and the line to edit.
4. Waits for the owner. Enter runs the tests; `q` aborts.
5. Restores the original bytes exactly, deletes the backup, and records the result: pass,
   fail or aborted, with elapsed seconds.

In place rather than in a copy: the project's test command is configured against this
checkout, its virtual environment and its paths, and a copy would need all of them again.
The cost is the transient backup, which is why it is named, why `praxis drill --restore`
exists for a drill that died before step 5, and why the backup's existence refuses a new
drill until it is restored.

## What is recorded

- `items`: item id, repo id, file, qualified function name, kind, agent and total lines,
  the threshold used, the sessions whose deltas produced the span, and the validation
  outcome. Claim class derived.
- `results`: item id, outcome (pass, fail, aborted, refused), elapsed seconds, the hash of
  the test command, warnings. Claim class observed: the test runner decided, not a person
  and not a model.

Neither table holds a line of code. The integration suite reads the database bytes after
a drill and asserts the body the owner typed is not in them.

## What this version does not do

- Languages other than Python. The span recovery is language-free; the function finder is
  `ast`. Another language needs its own finder and is a measured addition.
- fix-it and explain-it items (docs/kickoff.md Phase 3).
- Scheduling, ratings, or any claim about the owner. One item, one result.
- A test command discovered on its own. The owner states it once per repository and it is
  remembered in `repo_config`.
