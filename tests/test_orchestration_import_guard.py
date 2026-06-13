"""RTM-7c.2 — orchestration package import boundary guards."""

from __future__ import annotations

import ast
from pathlib import Path

ORCHESTRATION_SRC = Path(__file__).resolve().parents[1] / "src" / "orchestration"

_FORBIDDEN_ROOTS = frozenset(
    {
        "socket",
        "websocket",
        "websockets",
        "http",
        "httpx",
        "urllib",
        "requests",
        "data",
        "llm",
        "broker",
    }
)

_ALLOWED_BY_FILE: dict[str, frozenset[str]] = {
    "fast_loop_execution.py": frozenset({"market_data", "execution", "allocator", "domain"}),
}


def test_orchestration_modules_respect_import_boundaries() -> None:
    offenders: list[str] = []
    for path in sorted(ORCHESTRATION_SRC.glob("*.py")):
        if path.name == "__init__.py":
            continue
        allowed_extra = _ALLOWED_BY_FILE.get(path.name, frozenset())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root in allowed_extra:
                    continue
                if root in _FORBIDDEN_ROOTS:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []
