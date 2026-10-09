---
name: claude-offload
description: Procedure for delegating work from Codex to Claude Code. claude-codex-router picks the delegation target (Claude Code via `claude -p`, or Codex itself) from the headroom of both usage windows (Codex itself if it is not installed). Hand off cross-file investigation, a review by another pair of eyes, running tests or E2E and triaging their logs, and edits that were already instructed, then verify the result before reporting. Use it for every task you would want to delegate, such as "investigate", "review", "with another agent" or "fix".
---

# claude-offload — claude-codex-router picks the delegation target (Codex itself if it is absent)

When you split off part of the work while handling a request, **call `pick.sh --main codex` first on every delegation, and follow the side it answers** ("Picking the delegation target" below).
The scope is **all of reading, running and editing**.

## What does not change
- **Delegating does not widen the scope of the instructions.** Hand off only the edits the requester instructed. Do not hand off edits in an investigation done to answer a question.
- **Verification is your responsibility.** Do not pass Claude Code's report to the requester as is. Finish "Verification" below before reporting.
- **Do not let Claude Code run git commit / push** (already forbidden in the preamble). You make commits yourself following the requester's policy; a human pushes.
- Places the requester or a skill has declared "read-only" (e.g. a worktree another session is working in) are handed off **only in read mode**. Do not make them the `cwd` of write mode.

## Running outside the Codex sandbox
`pick.sh` and `run.sh` need the network and files outside the workspace (`~/.claude`), so they must run outside the sandbox.
The claude-codex-router installer adds a Codex rule (`~/.codex/rules/claude-codex-router.rules`) that allows exactly these two files to run outside the sandbox without asking.
**Call them by their full path as the first word of the command, as written below** (the rule matches the command prefix; `bash pick.sh`, `cd … &&` or a relative path do not match).

## Picking the delegation target (on every delegation)
```bash
~/.agents/skills/claude-offload/pick.sh --main codex
```
- The output is one line, "`<codex|claude> <reason>`". Follow the first word.
  - If claude-codex-router (`~/.claude/codex-router/bin/router.py`, location `CLAUDE_CODEX_ROUTER_DIR`) is installed, follow its decision (for both usage windows, the leftover probability before the reset, and the guard).
  - When it is not installed, did not answer, or its answer is unreadable: `codex` (do it yourself).
- When delegating several tasks in parallel, call it once for each (every call reads the current usage windows; it takes a few seconds).
- **claude** → run.sh as in "How to call it" below.
- **codex** → do the work yourself.
- In the report to the requester, write one line with the delegation target and the reason from `pick.sh`.

## How to call it (handing off to Claude Code)
```bash
~/.agents/skills/claude-offload/run.sh <read|write> <cwd> <outdir> [--add-dir DIR]... [-m MODEL] <<'CLAUDE_EOF'
<request body>
CLAUDE_EOF
```
- `read` lets Claude Code read and search only; `write` lets it edit files and run any command (tests, builds). **If nothing needs to be written, use read.**
- `cwd` is an absolute path. Give each task its own `outdir` (e.g. `/tmp/claude-offload/<short name>`).
  run.sh leaves `prompt.md` (the actual instructions with the preamble), `result.json`, `stderr.txt`, `last.txt` (Claude Code's final report) and `summary.txt`.
- Stdout is only the summary: `rc`, seconds taken, whether `UNAVAILABLE:` / `NO-RESULT:` appeared, for a git working tree **the changed files seen from the status diff**, and a `WARNING` when the tree changed in read mode.
- **Do not run two writes on the same working tree at the same time** (the changes mix and you cannot tell which change came from which).
- The model is Claude Code's default. Pass `-m` only when the requester asked for a specific model.
- Each call is a new Claude Code session. To redo, write the key points of the previous `last.txt` in the body and start a new run.

## What to write in the request body (Claude Code receives neither AGENTS.md nor this conversation)
run.sh automatically prepends the preamble (`preamble.md`: no changes outside the instructions, no guesses, do not change the git state, `find -maxdepth`, do not loosen expected values, the report format). In the body, write:
1. The **goal** and the **done condition** (what must be known / what must pass for it to be complete).
2. **Targets**: absolute paths, and the files, symbols and lines to look at. Known facts (`path:line` you verified yourself).
3. **What not to do**: files and directories not to touch, read-only places, contracts not to change.
4. For edits, the **scope of the change** (what in which file) and **how to check it** (tests and commands to run).
5. If the project has a document of its conventions, give its path and write "read it before working".

## Verification (always, before reporting)
- Read `summary.txt` and `last.txt`. If `UNAVAILABLE` / `NO-RESULT` / `rc≠0`, do not treat it as a result.
- **read**: open at least the key `path:line` supporting the conclusion yourself and check them (whether Claude Code's citations match the real files). If `WARNING` (the tree changed) appears, report that change to the requester; do not revert it on your own.
- **write**: read the actual changes with `git -C <cwd> diff` and check for changes outside the scope of the instructions. If there are any, report them to the requester (do not silently revert them, and do not silently keep them).
  **Run yourself as well** the tests and gates Claude Code says passed.
- In the report to the requester, write the parts left to Claude Code separately from the parts you verified yourself.

## When it is unavailable
If handing off to Claude Code gives `UNAVAILABLE:` (usage limit, rate limit, authentication, connection), do the work yourself, and write one line in the report to the requester saying so.
`NO-RESULT:` (it ran but the report is empty) is not an availability problem. Look at the end of `stderr.txt` and `result.json` to find the cause.
