/**
 * code-index auto-reindex plugin for OpenCode.
 *
 * Keeps the code-index search index fresh by kicking off a BACKGROUND,
 * incremental reindex after git operations that bring in or switch code the
 * agent didn't write itself (pull / fetch / merge / rebase / checkout / switch /
 * commit / reset / stash pop|apply / clone). It deliberately does NOT reindex on
 * every file edit: while an agent is editing, it has already done its searching
 * and an immediate reindex would just churn. The point at which the index must
 * catch up is when a large chunk of code appears or the working tree is swapped
 * out from under the index — i.e. git, not individual edits.
 *
 * It calls the `code-index index --background` CLI, which returns at once and is
 * idempotent per service (it won't start a second run if one is already active),
 * so a burst of git commands can't pile up overlapping indexers.
 *
 * INSTALL (this file touches NOTHING inside your indexed repos):
 *   - Global (all projects):  ~/.config/opencode/plugin/code-index-reindex.js
 *   - Per project:            <your-project>/.opencode/plugin/code-index-reindex.js
 *   Then restart OpenCode. Requires `code-index` on PATH (`pip install -e .`
 *   from the code-index-mcp repo), or set CODE_INDEX_CLI below to the module
 *   form, e.g. "py -m code_index.cli" (Windows) / "python3 -m code_index.cli".
 *
 * CONFIG via env (all optional):
 *   CODE_INDEX_CLI            command to invoke (default "code-index")
 *   CODE_INDEX_REINDEX_DEBOUNCE_MS  coalesce a burst of git commands (default 4000)
 *   CODE_INDEX_REINDEX_PATH   project root to index (default: the worktree)
 *   CODE_INDEX_REINDEX_OFF    set to "1" to disable the plugin
 */

// git subcommands that change code the agent did not just write itself.
const TRIGGER_SUBCOMMANDS = new Set([
  "pull",
  "fetch",
  "merge",
  "rebase",
  "checkout",
  "switch",
  "commit",
  "reset",
  "clone",
  "stash", // only `stash pop` / `stash apply` (checked below)
  "cherry-pick",
  "am",
  "revert",
]);

function isGitTrigger(command) {
  if (typeof command !== "string") return false;
  // Cheap pre-filter; avoids tokenizing every shell command.
  if (!/\bgit\b/.test(command)) return false;

  // A bash line may chain several commands (&&, ;, |). Check each segment.
  const segments = command.split(/&&|\|\||;|\|/);
  for (const seg of segments) {
    const tokens = seg.trim().split(/\s+/).filter(Boolean);
    const gitIdx = tokens.indexOf("git");
    if (gitIdx === -1) continue;
    // Skip global flags like `git -C path` / `-c key=val` to find the subcommand.
    let i = gitIdx + 1;
    while (i < tokens.length) {
      const t = tokens[i];
      if (t === "-C" || t === "-c") {
        i += 2; // these take an argument
        continue;
      }
      if (t.startsWith("-")) {
        i += 1;
        continue;
      }
      break;
    }
    const sub = tokens[i];
    if (!sub || !TRIGGER_SUBCOMMANDS.has(sub)) continue;
    if (sub === "stash") {
      const next = tokens[i + 1];
      if (next !== "pop" && next !== "apply") continue; // `git stash` (push) changes nothing on disk
    }
    return true;
  }
  return false;
}

export const CodeIndexReindexPlugin = async ({ $, directory, worktree, client }) => {
  if (process.env.CODE_INDEX_REINDEX_OFF === "1") {
    return {};
  }

  const cli = (process.env.CODE_INDEX_CLI || "code-index").trim();
  const debounceMs = Number(process.env.CODE_INDEX_REINDEX_DEBOUNCE_MS || 4000);
  const root = process.env.CODE_INDEX_REINDEX_PATH || worktree || directory;

  let timer = null;

  async function log(level, message, extra) {
    try {
      await client?.app?.log?.({
        body: { service: "code-index-reindex", level, message, extra },
      });
    } catch {
      // best-effort logging only
    }
  }

  async function runReindex() {
    timer = null;
    try {
      // Split CODE_INDEX_CLI so "py -m code_index.cli" works as well as "code-index".
      const parts = cli.split(/\s+/).filter(Boolean);
      const bin = parts[0];
      const baseArgs = parts.slice(1);
      // `index --background` returns immediately and is idempotent per service.
      await $`${bin} ${baseArgs} index --background --path ${root}`.quiet().nothrow();
      await log("info", "background reindex requested", { root });
    } catch (err) {
      await log("warn", "reindex invocation failed", { error: String(err) });
    }
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(runReindex, debounceMs);
    // Don't keep the event loop alive just for this timer.
    if (typeof timer?.unref === "function") timer.unref();
  }

  return {
    "tool.execute.after": async (input, output) => {
      if (input.tool !== "bash") return;
      const command = output?.args?.command ?? input?.args?.command;
      if (isGitTrigger(command)) {
        schedule();
      }
    },
  };
};
