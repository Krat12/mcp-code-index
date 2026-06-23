"""External project registry for the microservices use case.

The registry is a single global TOML file living OUTSIDE every service repo
(default: ~/.config/code-index/projects.toml). It lets you index many services
without putting any file into the service repositories themselves.

Format::

    # Explicit services (each gets its own SQLite DB + Qdrant collection)
    [[service]]
    name = "billing"          # optional friendly name
    path = "C:/work/billing"
    ignore = ["**/generated/**", "*.pb.go"]  # optional extra ignore globs
    use_gitignore = true      # optional: also honor the repo-root .gitignore

    [[service]]
    path = "C:/work/payments"

    # Workspace roots: every git repo found one level deep is treated as a
    # service automatically (great for a monorepo-of-services parent folder).
    [[workspace]]
    path = "C:/work"
    depth = 1                 # how deep to scan for git repos (default 1)

Resolution rules:
- Explicit [[service]] entries are always included.
- [[workspace]] entries are scanned for child directories containing a `.git`
  folder; each such repo becomes a service.
- Results are de-duplicated by resolved absolute path.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import REGISTRY_PATH, Settings, project_id, settings_for

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - fallback for older interpreters
    import tomli as tomllib  # type: ignore


@dataclass
class Service:
    name: str
    path: Path
    # Per-project ignore configuration (read from projects.toml).
    ignore: list[str] = field(default_factory=list)
    use_gitignore: bool = False

    @property
    def id(self) -> str:
        return project_id(self.path)

    def settings(self) -> Settings:
        """Build per-service Settings carrying this service's ignore config."""
        return settings_for(
            self.path,
            ignore_globs=self.ignore,
            use_gitignore=self.use_gitignore,
        )


def _is_git_repo(p: Path) -> bool:
    return (p / ".git").exists()


def _scan_workspace(root: Path, depth: int) -> list[Path]:
    """Find git repos under root up to `depth` levels. depth=1 = direct children."""
    found: list[Path] = []
    root = root.resolve()
    if not root.exists():
        return found

    def walk(dir_: Path, level: int) -> None:
        if _is_git_repo(dir_):
            found.append(dir_)
            return  # don't descend into a repo's submodules by default
        if level >= depth:
            return
        try:
            children = [c for c in dir_.iterdir() if c.is_dir() and not c.name.startswith(".")]
        except OSError:
            return
        for child in children:
            walk(child, level + 1)

    # Start one level in: workspace itself is the container, its children are candidates.
    try:
        children = [c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".")]
    except OSError:
        return found
    for child in children:
        walk(child, 1)
    # Also allow the workspace root itself to be a repo.
    if _is_git_repo(root):
        found.append(root)
    return found


class RegistryError(Exception):
    """Raised when the registry file exists but cannot be parsed.

    Carries the offending path so callers can show an actionable message. Most
    callers should prefer `load_registry` (which never raises) and only use
    `load_registry_checked` when they want to surface the problem.
    """

    def __init__(self, path: Path, cause: Exception) -> None:
        self.path = path
        self.cause = cause
        super().__init__(f"Cannot read registry {path}: {cause}")


def load_registry_checked(path: Path | None = None) -> list[Service]:
    """Like `load_registry`, but raises RegistryError on a malformed TOML file.

    Use this where you want to report a broken config to the user (e.g. the CLI);
    `load_registry` swallows the error so a single typo can't take the whole MCP
    server / watcher down.
    """
    path = path or REGISTRY_PATH
    if not path.exists():
        return []

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(path, exc) from exc
    if not isinstance(data, dict):
        raise RegistryError(path, ValueError("top-level TOML is not a table"))

    return _resolve(data)


def load_registry(path: Path | None = None) -> list[Service]:
    """Read the registry TOML and resolve it into a de-duplicated service list.

    Tolerant by design: a missing file yields an empty list, and a malformed
    file is logged to stderr and also yields an empty list rather than crashing
    every frontend (server/watcher/CLI/web all call this). Use
    `load_registry_checked` if you need the error surfaced.
    """
    try:
        return load_registry_checked(path)
    except RegistryError as exc:
        print(f"[code-index] WARNING: {exc}; treating registry as empty", file=sys.stderr)
        return []


def _resolve(data: dict) -> list[Service]:
    services: dict[str, Service] = {}  # keyed by resolved abs path

    entries = data.get("service", [])
    if isinstance(entries, dict):  # tolerate a single [service] table
        entries = [entries]
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not raw or not isinstance(raw, str):
            continue
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            continue
        name = entry.get("name") or p.name
        services[str(p)] = Service(
            name=str(name),
            path=p,
            ignore=_as_str_list(entry.get("ignore")),
            use_gitignore=_as_bool(entry.get("use_gitignore")),
        )

    workspaces = data.get("workspace", [])
    if isinstance(workspaces, dict):
        workspaces = [workspaces]
    for ws in workspaces if isinstance(workspaces, list) else []:
        if not isinstance(ws, dict):
            continue
        raw = ws.get("path")
        if not raw or not isinstance(raw, str):
            continue
        try:
            depth = int(ws.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        # Auto-discovered repos inherit the workspace's ignore configuration.
        ws_ignore = _as_str_list(ws.get("ignore"))
        ws_gitignore = _as_bool(ws.get("use_gitignore"))
        for repo in _scan_workspace(Path(raw).expanduser(), depth):
            key = str(repo.resolve())
            services.setdefault(
                key,
                Service(
                    name=repo.name,
                    path=repo.resolve(),
                    ignore=list(ws_ignore),
                    use_gitignore=ws_gitignore,
                ),
            )

    return sorted(services.values(), key=lambda s: s.name.lower())


def _as_str_list(value) -> list[str]:
    """Coerce a TOML value into a clean list of pattern strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def settings_for_service(service: Service) -> Settings:
    """Convenience wrapper: per-service Settings with its ignore config."""
    return service.settings()


def add_service(path: Path | str, name: str | None = None, registry_path: Path | None = None) -> Service:
    """Append a [[service]] entry to the registry (creating the file if needed)."""
    registry_path = registry_path or REGISTRY_PATH
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    p = Path(path).expanduser().resolve()
    name = name or p.name

    # Append in a tolerant, human-editable way (no full TOML rewrite needed).
    block = f'\n[[service]]\nname = "{name}"\npath = {_toml_str(p)}\n'
    with registry_path.open("a", encoding="utf-8") as f:
        f.write(block)
    return Service(name=name, path=p)


def add_workspace(path: Path | str, depth: int = 1, registry_path: Path | None = None) -> Path:
    """Append a [[workspace]] entry that auto-discovers git repos under `path`."""
    registry_path = registry_path or REGISTRY_PATH
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    p = Path(path).expanduser().resolve()
    block = f"\n[[workspace]]\npath = {_toml_str(p)}\ndepth = {int(depth)}\n"
    with registry_path.open("a", encoding="utf-8") as f:
        f.write(block)
    return p


def _toml_str(p: Path) -> str:
    """Render a path as a TOML basic string with forward slashes (Windows-safe)."""
    return '"' + p.as_posix() + '"'
