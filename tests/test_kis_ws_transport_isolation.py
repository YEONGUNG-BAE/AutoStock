"""RTM-6 — isolation / leakage guards for the KIS websocket transport modules.

The transport modules (src/data/kis_ws_auth.py, src/data/kis_ws_source.py) and the
operator smoke CLI must never import the broker/ledger/paper-execution path, and the
operator CLI must never invoke a real run at import time. These are AST-level guards so
a future edit that crosses the boundary fails loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DATA = _REPO / "src" / "data"
_OPS_CLI = _REPO / "ops" / "run_kis_ws_readonly_smoke.py"

# transport 모듈은 거래/원장/주문 경로를 절대 import하지 않는다. 네트워크 스택
# (asyncio/websockets/json)은 src/data에서 허용되지만 broker/ledger/paper는 금지.
_FORBIDDEN_ROOTS = {
    "broker",
    "ledger",
    "decision",
    "paper_loop",
    "paper_execution",
    "llm",
}

_TRANSPORT_MODULES = ("kis_ws_auth.py", "kis_ws_source.py")


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    return roots


def test_transport_modules_do_not_import_broker_or_ledger() -> None:
    offenders: list[str] = []
    for name in _TRANSPORT_MODULES:
        roots = _imported_roots(_DATA / name)
        for forbidden in _FORBIDDEN_ROOTS:
            if forbidden in roots:
                offenders.append(f"{name}: {forbidden}")
    assert offenders == []


def test_websockets_is_lazily_imported_in_source() -> None:
    # websockets는 open_kis_websocket 내부에서만 지연 import되어야 한다(모듈 최상위 import 금지).
    # validate-only 경로는 websockets 없이도 동작해야 하기 때문이다.
    tree = ast.parse((_DATA / "kis_ws_source.py").read_text(encoding="utf-8"))
    top_level_imports: set[str] = set()
    for node in tree.body:  # module top-level only
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level_imports.add((node.module or "").split(".")[0])
    assert "websockets" not in top_level_imports


def test_ops_cli_only_invokes_run_inside_main() -> None:
    # execute_run / open_kis_websocket 호출은 main() 함수 본문 안에서만 일어나야 한다.
    # 모듈 import만으로 실 네트워크 run이 자동 실행되어선 안 된다.
    tree = ast.parse(_OPS_CLI.read_text(encoding="utf-8"))
    main_func = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_func is not None

    def _call_names(scope: ast.AST) -> list[str]:
        names: list[str] = []
        for node in ast.walk(scope):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    names.append(func.id)
                elif isinstance(func, ast.Attribute):
                    names.append(func.attr)
        return names

    # outside main(): collect calls in every other top-level function and module body.
    forbidden_calls = {"execute_run", "open_kis_websocket"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            continue
        called = set(_call_names(node))
        assert not (called & forbidden_calls), f"{node}: real-run call outside main()"


def test_ops_cli_guards_module_entrypoint() -> None:
    # __main__ 가드가 있어야 import-time 자동 실행을 막는다.
    source = _OPS_CLI.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
