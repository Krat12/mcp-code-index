"""Shared fixtures. Keep CONFIG/CACHE homes pointed at a temp dir so tests
never touch the real registry or caches.
"""

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolated_homes(tmp_path, monkeypatch):
    """Redirect config/cache homes and disable semantic by default for tests."""
    cfg = tmp_path / "cfg"
    cache = tmp_path / "cache"
    monkeypatch.setenv("CODE_INDEX_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("CODE_INDEX_CACHE_HOME", str(cache))
    monkeypatch.setenv("CODE_INDEX_SEMANTIC", "0")
    # config module reads these at import-time into module-level constants;
    # reload it so the temp homes take effect for each test.
    import importlib

    import code_index.config as config

    importlib.reload(config)
    yield


@pytest.fixture
def sample_repo(tmp_path):
    """A tiny multi-language repo with a Gradle build dir that must be ignored."""
    root = tmp_path / "svc"
    (root / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
    (root / "build" / "classes").mkdir(parents=True)
    (root / ".git").mkdir()

    (root / "src" / "main" / "java" / "com" / "acme" / "BillingController.java").write_text(
        "@RestController\n"
        "public class BillingController {\n"
        "    public void chargeCustomer() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "src" / "main" / "java" / "com" / "acme" / "Money.java").write_text(
        "public record Money(long cents, String currency) {}\n", encoding="utf-8"
    )
    (root / "src" / "main" / "resources" / "application.properties").parent.mkdir(
        parents=True, exist_ok=True
    )
    (root / "src" / "main" / "resources" / "application.properties").write_text(
        "server.port=8080\n", encoding="utf-8"
    )
    (root / "helper.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n", encoding="utf-8"
    )
    # This must NOT be indexed (Gradle build output).
    (root / "build" / "classes" / "Generated.java").write_text(
        "class GENERATED_SHOULD_BE_IGNORED {}\n", encoding="utf-8"
    )
    return root
