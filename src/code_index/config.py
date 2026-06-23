"""Configuration and small shared helpers.

Design goals for the microservices use case:
- ALL state lives OUTSIDE the indexed repositories (no files committed into
  service repos, no .gitignore edits, no git hooks).
- One index per service (separate SQLite DB + separate Qdrant collection),
  keyed by a stable id derived from the absolute path.
- An external project registry (a single global TOML) lists the services.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Languages (tree-sitter grammar names from tree-sitter-language-pack).
# ---------------------------------------------------------------------------
LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".lua": "lua",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
}

# ---------------------------------------------------------------------------
# Ignored directories. Tuned for JVM microservices (Gradle/Maven build output).
# ---------------------------------------------------------------------------
DEFAULT_IGNORE_DIRS: set[str] = {
    # VCS
    ".git", ".hg", ".svn",
    # JS / Python envs
    "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # Generic build output
    "dist", "out", "coverage",
    # Gradle / Maven (JVM microservices)
    "build", "target", ".gradle", "gradle",
    "bin", "obj", ".settings",
    # IDE / tooling
    ".idea", ".vscode", ".cache", ".turbo", ".next",
    "graphify-out",
}

# ---------------------------------------------------------------------------
# Files indexed for FTS even without a grammar. Added: JVM/Spring config types.
# ---------------------------------------------------------------------------
DEFAULT_TEXT_EXTS: set[str] = set(LANG_BY_EXT) | {
    ".txt", ".cfg", ".ini", ".env", ".dockerfile",
    ".xml", ".gitignore", ".jsonc", ".mdx",
    # JVM / microservices ecosystem
    ".gradle", ".properties", ".proto", ".avsc", ".graphql", ".graphqls",
    ".feature",          # Cucumber/Gherkin
    ".http",             # REST client request files
    ".conf", ".hocon",   # Lightbend/Typesafe config
    ".dockerignore",
}

# Files larger than this (bytes) are skipped (likely generated / binary).
MAX_FILE_BYTES = 1_500_000

# Prose/documentation extensions get a much smaller size cap: a 0.5 MB
# translated CHANGELOG adds enormous FTS bloat and slows indexing for almost
# no code-search value. Tunable via CODE_INDEX_MAX_DOC_BYTES.
DOC_EXTS: set[str] = {".md", ".mdx", ".txt", ".rst"}
MAX_DOC_BYTES = int(os.environ.get("CODE_INDEX_MAX_DOC_BYTES", "131072"))  # 128 KB

# Relative-path substrings (POSIX, lowercased) whose files are skipped entirely.
# Targets generated/translated docs that explode the index (e.g. i18n CHANGELOGs).
# Extendable via CODE_INDEX_IGNORE_PATHS (comma-separated).
DEFAULT_IGNORE_PATH_PARTS: tuple[str, ...] = (
    "docs/i18n/",
    "/i18n/",
    "/translated-changelogs/",
    "/locales/",
    "/translations/",
)


def ignore_path_parts() -> tuple[str, ...]:
    extra = os.environ.get("CODE_INDEX_IGNORE_PATHS", "")
    parts = [p.strip().lower() for p in extra.split(",") if p.strip()]
    return DEFAULT_IGNORE_PATH_PARTS + tuple(parts)

# Exact filenames to skip: lock files and common generated artifacts that are
# huge, single-line, or otherwise useless for code search.
DEFAULT_IGNORE_FILES: set[str] = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "uv.lock", "pdm.lock", "cargo.lock", "composer.lock",
    "gemfile.lock", "go.sum",
}

# Filename substrings that mark minified/generated bundles -> skip.
MINIFIED_MARKERS: tuple[str, ...] = (".min.js", ".min.css", ".bundle.js", ".map")

# A file whose longest line exceeds this is treated as minified/generated and
# skipped (protects FTS from blowing up on single-line megabyte files).
MAX_LINE_CHARS = 5_000

# Where the external configuration lives (NEVER inside service repos).
CONFIG_HOME = Path(os.environ.get("CODE_INDEX_CONFIG_HOME", str(Path.home() / ".config" / "code-index")))
CACHE_HOME = Path(os.environ.get("CODE_INDEX_CACHE_HOME", str(Path.home() / ".cache" / "code-index")))
REGISTRY_PATH = CONFIG_HOME / "projects.toml"

# Live indexing-status files (one JSON per service id) for the progress UI.
# Cross-process channel: whoever indexes (CLI, watcher, MCP) writes here; the
# `status`/web UIs read here. Kept OUTSIDE every service repo like all state.
STATUS_DIR = CACHE_HOME / "status"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def project_id(root: Path) -> str:
    """Stable id for a service: <dirname>_<hash-of-abs-path>.

    Used both for the SQLite filename and the Qdrant collection name, so two
    different services never collide even if they share a directory name.
    """
    root = root.resolve()
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in root.name) or "root"
    return f"{safe}_{digest}"


@dataclass
class Settings:
    """Resolved runtime settings for ONE service (per-service index)."""

    root: Path
    db_path: Path
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL", "http://localhost:6333"))
    qdrant_api_key: str | None = field(default_factory=lambda: os.environ.get("QDRANT_API_KEY"))
    semantic_enabled: bool = field(
        default_factory=lambda: _env("CODE_INDEX_SEMANTIC", "1") not in ("0", "false", "no")
    )

    # Embedding backend: "api" (OpenAI-compatible /embeddings) or "fastembed" (local).
    embed_backend: str = field(default_factory=lambda: _env("CODE_INDEX_EMBED_BACKEND", "api"))

    # Local fastembed model (used only when embed_backend == "fastembed").
    embed_model: str = field(default_factory=lambda: _env("CODE_INDEX_EMBED_MODEL", "BAAI/bge-small-en-v1.5"))

    # API embeddings (used when embed_backend == "api").
    embed_api_base: str = field(
        default_factory=lambda: _env("CODE_INDEX_EMBED_API_BASE", "https://routerai.ru/api/v1")
    )
    embed_api_model: str = field(
        default_factory=lambda: _env("CODE_INDEX_EMBED_API_MODEL", "qwen/qwen3-embedding-8b")
    )
    embed_api_key: str | None = field(default_factory=lambda: os.environ.get("CODE_INDEX_EMBED_API_KEY"))
    # Optional explicit vector size; if 0 we auto-detect from the first response.
    embed_dim: int = field(default_factory=lambda: int(_env("CODE_INDEX_EMBED_DIM", "0")))

    # How many chunks to embed per API request. The indexer fills a GLOBAL
    # buffer across files and flushes in batches of this size, so a repo with
    # thousands of tiny files makes ~N/batch requests instead of one per file.
    embed_batch: int = field(default_factory=lambda: max(1, int(_env("CODE_INDEX_EMBED_BATCH", "64"))))
    # How many embedding requests to run IN PARALLEL. The bottleneck is API
    # latency (~8s per batch on an 8B model), not local CPU, so concurrency is
    # nearly free on a weak laptop yet cuts wall-time several-fold.
    embed_concurrency: int = field(default_factory=lambda: max(1, int(_env("CODE_INDEX_EMBED_CONCURRENCY", "6"))))
    # How many points to send per Qdrant upsert request. High-dim vectors (e.g.
    # 4096) make a few hundred points a multi-MB body that Qdrant/HTTP can drop
    # (WinError 10053), so we upsert in conservative sub-batches. Independent of
    # the embedding batch/buffer size.
    upsert_batch: int = field(default_factory=lambda: max(1, int(_env("CODE_INDEX_UPSERT_BATCH", "64"))))
    # Retries (with exponential backoff) for a failed embeddings request.
    embed_max_retries: int = field(default_factory=lambda: max(0, int(_env("CODE_INDEX_EMBED_RETRIES", "3"))))
    # Optional minimum cosine score for semantic hits (0 = no threshold).
    search_score: float = field(default_factory=lambda: float(_env("CODE_INDEX_SEARCH_SCORE", "0")))

    # Per-project ignore configuration (from the external projects.toml).
    # Extra glob patterns to exclude, matched against POSIX-relative paths.
    ignore_globs: list[str] = field(default_factory=list)
    # If True, also honor the repo-root .gitignore (read-only; never written).
    use_gitignore: bool = False

    def collection_name(self) -> str:
        return f"code_{project_id(self.root)}"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def settings_for(
    root: Path | str,
    ignore_globs: list[str] | None = None,
    use_gitignore: bool = False,
) -> Settings:
    """Build per-service Settings with DB stored OUTSIDE the repo (cache dir)."""
    root = Path(root).resolve()
    pid = project_id(root)
    db_path = CACHE_HOME / f"{pid}.sqlite3"
    s = Settings(
        root=root,
        db_path=db_path,
        ignore_globs=list(ignore_globs or []),
        use_gitignore=use_gitignore,
    )
    s.ensure_dirs()
    return s


def load_settings() -> Settings:
    """Single-project entrypoint (CODE_INDEX_ROOT / CWD), DB outside repo.

    Honors CODE_INDEX_DB override for advanced/manual setups.
    """
    root = Path(_env("CODE_INDEX_ROOT", ".")).resolve()
    override_db = os.environ.get("CODE_INDEX_DB")
    if override_db:
        s = Settings(root=root, db_path=Path(override_db))
    else:
        s = Settings(root=root, db_path=CACHE_HOME / f"{project_id(root)}.sqlite3")
    s.ensure_dirs()
    return s
