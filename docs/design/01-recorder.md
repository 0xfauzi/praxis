# 01: the recorder

## What it answers

For every file in the working tree, at every prompt boundary of a session: how many
changed lines (relative to HEAD) did the agent produce, how many did the owner produce,
how many were already there before the session started, how many agent lines are no longer
present, and how many pre-session lines are no longer present. Counts, per file, per
boundary, as Evidence rows with claim class `derived`.

## Why this mechanism

The obvious design, reading the agent's own patch out of the Edit tool's `structuredPatch`,
covers one tool. Write gives no patch, MultiEdit gives a list, NotebookEdit gives cells, and
a Bash command that runs `sed -i` or `tee` gives nothing at all. Every tool-specific parser
is a place where a new tool version silently stops being counted.

Instead the recorder never looks at tool inputs. It snapshots the working tree before and
after every mutating tool call; the difference is what that call did, whatever the tool
was. The same snapshot at a prompt boundary, matched against everything the agent did so
far and against the state before the session, yields the attribution. One mechanism, every
tool, and a tool the recorder has never heard of is still counted if its hook fires.

## The snapshot

`git diff --no-color --no-ext-diff --no-renames --no-prefix --unified=0 --diff-algorithm=myers
HEAD`, plus every untracked, non-ignored file read as added lines. The result is, per
repo-relative path, a multiset of line hashes, where a hash covers the diff kind (`+` or
`-`) and the exact bytes of the line: `sha256(kind || 0x00 || line)[:32]`.

Lines, not hunks. Hunk boundaries move when a nearby edit lands and merge when two edits
touch adjacent lines; a line's identity does not. Multisets, not sets: two identical added
lines are two lines (AGENTS.md invariant 13).

Measured on this machine: `git diff -U0 HEAD` takes 4 to 6 ms warm and 16 ms cold on
repositories of 361 and 652 tracked files (docs/experiments/E01.md). Larger repositories
need measuring; the git call has a 2 s timeout and a timeout is recorded as unobservable.

Skipped, and recorded as skipped on the row's `warnings`: binary files (git prints no
lines for them; untracked ones are detected by a NUL byte), untracked files over 1,000,000
bytes, unreadable files. A file with a skip has coverage `partial`.

## The events

| Event | What the recorder does |
|---|---|
| SessionStart | Baseline snapshot B. A second SessionStart for the same session (resume) keeps the first baseline. |
| PreToolUse, tool in {Edit, Write, MultiEdit, NotebookEdit, Bash} | Snapshot, stored as pending under the tool_use_id. |
| PostToolUse, PostToolUseFailure, same tools | Snapshot; delta against the pending snapshot; each changed (file, hash) count is appended as an agent delta. The pending row is consumed. A failed tool still gets its delta: a Bash command that wrote before it failed still wrote. |
| UserPromptSubmit, Stop | Boundary snapshot D; attribution against A (the agent multiset, the sum of this session's deltas) and B. |
| anything else | Recorded as a hook event, nothing else. |

## Attribution, per file

For each line hash h with count n in D:

1. agent takes `min(n, A[h])`
2. pre-existing takes `min(remaining, B[h])`
3. human takes what remains

Then: `ambiguous` is the agent count not found in D (an agent line the owner changed or
removed; it is never folded into human), and `reverted` is the baseline count not found in
D (a pre-session change that is gone; attributed to nobody).

Without a baseline (the session started before the hook was installed, or SessionStart was
unobservable) `human` and `pre-existing` cannot be separated: both are stored NULL and the
row's coverage is `no_baseline`. Unknown stays unknown.

## What it cannot tell, stated on every row

The `assumptions` field carries these verbatim:

- Content-identical lines from two authors are indistinguishable; multiset counts split
  them by count, not by author.
- A change the owner makes between a PreToolUse and its PostToolUse is attributed to the
  agent. The window is the tool's own duration.
- Lines are compared relative to HEAD. A commit during the session moves HEAD, and lines
  committed stop being changed lines; attribution is of the uncommitted tree only.

## What is stored, and what is not

Stored under `$PRAXIS_HOME/ledger.db`: hook events with timing and outcome, baseline
hashes with counts, pending pre-snapshots (consumed on PostToolUse; the one table that is
deleted from, because it is scratch and not an observation), agent deltas, and attribution
rows. Never stored: any line of any file, any prompt, any tool input or response. The
integration suite reads the database file's bytes after a session and asserts that none of
the distinctive strings written to the repository appear in it.

## Fail open

`praxis hook` exits 0 on every input: valid, garbage, empty, oversize, a cwd that is not a
repository, a repository with no commits, git missing, git hanging (2 s per call, 20 s
watchdog on the whole hook). Each of those is a hook_events row with outcome
`unobservable` or `error` and a note. It never prints to stdout, so it never adds context
to the agent's conversation, and it never changes the tool's output or exit code.

## Cost

Measured on this container with `experiments/E01/hook_cost.py`: 20 sessions of the
five-event sequence (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop)
against a real repository, 100 invocations of the installed binary. Wall clock around the
process, which is what the agent waits for: p50 71.5 ms, p95 92.3 ms, max 100.5 ms, with
no event type outside 69 to 74 ms at p50. Inside the process, the hook's own work (git
calls plus SQLite) was p50 8 to 12 ms per event. The remaining 60 ms is interpreter and
package start-up, which is where any future reduction would come from; it needs measuring
on the owner's machine before it is worth chasing. The hook_events table records
`duration_ms` for every live invocation so the distribution is read from the ledger, not
estimated.
