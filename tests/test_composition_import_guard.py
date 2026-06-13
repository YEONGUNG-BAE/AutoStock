"""RTM-7c.4a — composition-root import boundary guard.

``src/composition`` IS the only wiring root allowed to assemble broker + ledger +
execution + orchestration into a runnable stack, so it intentionally imports those
modules (which ``orchestration`` itself is forbidden from touching). What it must
NEVER do, even as the root, is reach a network/transport/credential surface: the
offline paper fast-loop opens no sockets, speaks no HTTP/WS, reads no credential,
and pulls in no live data/LLM client. This guard pins that boundary over every
``src/composition/*.py`` module and the operator CLI ``ops/run_paper_fast_loop.py``.

Documented deviation (Section-17): ``execution`` is on the ALLOWED list here, and the
keystone snapshot module imports ``execution`` for ``PaperPortfolioPolicy``. GPT's
original prompt wanted ``execution`` forbidden from the snapshot layer, but the repo
already allowlists ``execution`` for ``orchestration/fast_loop_execution.py`` and the
composition root must construct the real coordinator — so ``execution`` is allowed at
the composition boundary by design, not by oversight. The network/credential roots
below remain hard-forbidden regardless.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_SRC = _REPO_ROOT / "src" / "composition"
OPS_CLI = _REPO_ROOT / "ops" / "run_paper_fast_loop.py"

# Network / transport / credential / live-data / LLM surfaces. The offline
# fast-loop composition must not import any of these, even as the wiring root.
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
    }
)

# Roots the composition root is explicitly permitted to wire together. Anything
# outside this set that is also in _FORBIDDEN_ROOTS is an offence; stdlib and
# already-allowed domain packages are fine.
_ALLOWED_COMPOSITION_ROOTS = frozenset(
    {
        "broker",
        "ledger",
        "execution",
        "orchestration",
        "market_data",
        "risk",
        "paper_loop",
        "domain",
        "allocator",
        "decision",
        "analysis",
        "config",
        "composition",
    }
)


def _guarded_files() -> list[Path]:
    files = [p for p in sorted(COMPOSITION_SRC.glob("*.py")) if p.name != "__init__.py"]
    files.append(OPS_CLI)
    return files


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_composition_imports_no_network_or_credential_surface() -> None:
    offenders: list[str] = []
    for path in _guarded_files():
        for root in sorted(_imported_roots(path)):
            if root in _FORBIDDEN_ROOTS:
                offenders.append(f"{path.name}: {root}")
    assert offenders == [], f"composition reached a forbidden surface: {offenders}"


def test_composition_root_only_wires_allowed_first_party_packages() -> None:
    # First-party imports outside the documented allowlist would mean the
    # composition root grew an unexpected dependency; fail loudly so it is reviewed.
    first_party = {p.name for p in (_REPO_ROOT / "src").iterdir() if p.is_dir()}
    offenders: list[str] = []
    for path in _guarded_files():
        for root in sorted(_imported_roots(path)):
            if root in first_party and root not in _ALLOWED_COMPOSITION_ROOTS:
                offenders.append(f"{path.name}: {root}")
    assert offenders == [], f"unexpected first-party dependency: {offenders}"
