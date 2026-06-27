# OpenCode auto-reindex plugin

`code-index-reindex.js` is an optional [OpenCode plugin](https://opencode.ai/docs/plugins)
that keeps the code-index search index fresh automatically: after a git
operation that brings in or switches code (`pull`, `fetch`, `merge`, `rebase`,
`checkout`/`switch`, `commit`, `reset`, `cherry-pick`, `revert`, `stash pop`,
`clone`), it fires a **background, incremental** reindex of the worktree.

## Why git, not file edits

The plugin intentionally does **not** reindex on every file edit. While an agent
is editing, it has already done its searching for that change, so an immediate
reindex would just churn. The index needs to catch up when a large chunk of code
appears or the working tree is swapped out from under it — i.e. git operations.
`git checkout`/`switch` in particular changes half the repo at once; without a
reindex the agent would search stale code from the other branch.

## How it stays safe

It calls `code-index index --background`, which **returns at once** and is
**idempotent per service** (it won't start a second run while one is active), so
a burst of git commands can't pile up overlapping indexers. A short debounce
(default 4s) coalesces command bursts.

This plugin lives in **your OpenCode config**, not inside any indexed repo, so it
honors the code-index invariant: nothing is ever written into a target/service
repository.

## Install

Copy the file (no edits needed) to one of:

- **Global (all projects):** `~/.config/opencode/plugin/code-index-reindex.js`
- **Per project:** `<project>/.opencode/plugin/code-index-reindex.js`

Then restart OpenCode. Requires `code-index` on `PATH` (`pip install -e .` from
the code-index-mcp repo).

## Config (env, all optional)

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODE_INDEX_CLI` | `code-index` | Command to invoke. Use the module form if the script isn't on PATH, e.g. `py -m code_index.cli` (Windows) or `python3 -m code_index.cli`. |
| `CODE_INDEX_REINDEX_DEBOUNCE_MS` | `4000` | Coalesce a burst of git commands into one reindex. |
| `CODE_INDEX_REINDEX_PATH` | the worktree | Project root to index. |
| `CODE_INDEX_REINDEX_OFF` | unset | Set to `1` to disable the plugin. |

## Relationship to `code-index-watch`

This plugin and the `code-index-watch` daemon both keep the index fresh and are
complementary:

- **`code-index-watch`** is a standalone filesystem-watcher daemon that reindexes
  on any change, independent of which editor/agent you use. Good as an always-on
  background service.
- **This plugin** ties refreshes to the OpenCode agent's own git activity, with
  no separate daemon to run. Good when you live inside OpenCode and don't want a
  long-running watcher.

Use either or both — the CLI's per-service idempotency means they won't trip over
each other.
