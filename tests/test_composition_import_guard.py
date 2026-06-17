"""RTM-7c.4a — composition-root import boundary guard.

``src/composition`` IS the only wiring root allowed to assemble broker + ledger +
execution + orchestration into a runnable stack, so it intentionally imports those
modules (which ``orchestration`` itself is forbidden from touching). What it must
NEVER do, even as the root, is reach a network/transport/credential surface: the
offline paper fast-loop opens no sockets, speaks no HTTP/WS, reads no credential,
and pulls in no live data/LLM client. This guard pins that boundary over every
``src/composition/*.py`` module and operator CLIs.

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
ATTENDED_PAPER_DAY_CLI = _REPO_ROOT / "ops" / "run_attended_paper_day.py"

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
    files.append(ATTENDED_PAPER_DAY_CLI)
    return files


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module)
    return modules


def _imported_roots(path: Path) -> set[str]:
    return {module.split(".")[0] for module in _imports(path)}


def _allowed_forbidden_imports(path: Path) -> set[str]:
    if path == ATTENDED_PAPER_DAY_CLI:
        return {
            "data.kis_ws_auth",
            "data.kis_ws_source",
        }
    return set()


def _allowed_first_party_roots(path: Path) -> set[str]:
    if path == ATTENDED_PAPER_DAY_CLI:
        return _ALLOWED_COMPOSITION_ROOTS | {"data"}
    return _ALLOWED_COMPOSITION_ROOTS


def test_composition_imports_no_network_or_credential_surface() -> None:
    offenders: list[str] = []
    for path in _guarded_files():
        allowed_modules = _allowed_forbidden_imports(path)
        for module in sorted(_imports(path)):
            root = module.split(".")[0]
            if root in _FORBIDDEN_ROOTS:
                if module not in allowed_modules:
                    offenders.append(f"{path.name}: {module}")
    assert offenders == [], f"composition reached a forbidden surface: {offenders}"


def test_attended_cli_broker_imports_limited_to_exact_allowlist() -> None:
    # The attended paper-day CLI may reach the live KIS read-only surface, but only
    # through an exact module allowlist. No other broker submodule (e.g. a live order
    # adapter) and no extra vendor data client may be imported, even lazily.
    allowed = {"broker.kis_transport", "data.kis_ws_auth", "data.kis_ws_source"}
    offenders: list[str] = []
    for module in sorted(_imports(ATTENDED_PAPER_DAY_CLI)):
        root = module.split(".")[0]
        if root in {"broker", "data"} and module not in allowed:
            offenders.append(module)
    assert offenders == [], f"attended CLI reached a disallowed broker/data module: {offenders}"


def test_composition_root_only_wires_allowed_first_party_packages() -> None:
    # First-party imports outside the documented allowlist would mean the
    # composition root grew an unexpected dependency; fail loudly so it is reviewed.
    first_party = {p.name for p in (_REPO_ROOT / "src").iterdir() if p.is_dir()}
    offenders: list[str] = []
    for path in _guarded_files():
        allowed_roots = _allowed_first_party_roots(path)
        for root in sorted(_imported_roots(path)):
            if root in first_party and root not in allowed_roots:
                offenders.append(f"{path.name}: {root}")
    assert offenders == [], f"unexpected first-party dependency: {offenders}"
