"""Real Intake 3G2 — operator-local sector pool → universe/mapping workflow tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POOL_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_candidates"
    / "kr_sector_candidate_pool.synthetic.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "build_kr_real_sector_pool_mapping.py"

_SYNTHETIC_CORP_CODES = {
    "900001": "90000010",
    "900002": "90000011",
    "900003": "90000012",
    "900004": "90000013",
    "900005": "90000014",
}
_SYNTHETIC_YFINANCE = {
    "900001": "900001.KS",
    "900002": "900002.KS",
    "900003": "900003.KS",
    "900004": "900004.KS",
    "900005": "900005.KS",
}

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from build_kr_real_sector_pool_mapping import (
    BuildKrRealSectorPoolMappingError,
    run_build_kr_real_sector_pool_mapping,
)
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _workflow_args(tmp_path: Path) -> dict[str, object]:
    return {
        "candidate_pool_path": POOL_FIXTURE,
        "corp_code_xml": SYNTHETIC_CORP_CODE_XML,
        "corp_code_zip": None,
        "selected_candidates_out": tmp_path / "selected.toml",
        "universe_out": tmp_path / "universe.generated.toml",
        "provider_mapping_out": tmp_path / "provider_mappings.generated.toml",
        "selection_name": "kr-real-selected-v1",
        "selection_description": "Operator-selected KR candidates.",
        "universe_name": "kr-real-generated-v1",
        "provider_mapping_name": "kr-real-provider-mappings-generated-v1",
        "sectors": {"semiconductors", "internet"},
        "max_total": 3,
        "max_per_sector": 2,
        "force": True,
    }


def _run_workflow(tmp_path: Path) -> dict[str, object]:
    return run_build_kr_real_sector_pool_mapping(**_workflow_args(tmp_path))  # type: ignore[arg-type]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(OPS_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _base_cli_args(tmp_path: Path) -> list[str]:
    return [
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--corp-code-xml",
        str(SYNTHETIC_CORP_CODE_XML),
        "--sector",
        "semiconductors",
        "--sector",
        "internet",
        "--max-total",
        "3",
        "--max-per-sector",
        "2",
        "--selected-candidates-out",
        str(tmp_path / "selected.toml"),
        "--universe-out",
        str(tmp_path / "universe.generated.toml"),
        "--provider-mapping-out",
        str(tmp_path / "provider_mappings.generated.toml"),
        "--selection-name",
        "kr-real-selected-v1",
        "--selection-description",
        "Operator-selected KR candidates.",
        "--universe-name",
        "kr-real-generated-v1",
        "--provider-mapping-name",
        "kr-real-provider-mappings-generated-v1",
    ]


def test_workflow_selects_and_writes_selected_candidate_toml(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    selected_out = Path(str(payload["selected_candidates_out"]))
    assert selected_out.is_file()
    assert payload["selected_candidates"] == 3
    assert payload["selected_symbols"] == ["900002", "900001", "900003"]


def test_selected_candidate_toml_has_no_root_base_market(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    raw = tomllib.loads(Path(str(payload["selected_candidates_out"])).read_text(encoding="utf-8"))
    assert "base_market" not in raw


def test_selected_candidate_toml_has_no_pool_only_fields(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    raw = tomllib.loads(Path(str(payload["selected_candidates_out"])).read_text(encoding="utf-8"))
    forbidden = {"sector", "industry", "eligible", "priority", "notes", "corp_code"}
    for entry in raw["candidates"]:
        assert forbidden.isdisjoint(entry.keys())


def test_workflow_generates_universe_and_provider_mapping(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert Path(str(payload["universe_out"])).is_file()
    assert Path(str(payload["provider_mapping_out"])).is_file()


def test_generated_universe_loads(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    assert universe.name == "kr-real-generated-v1"
    assert len(universe.enabled_symbols) == 3


def test_generated_provider_mapping_loads(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    assert registry.name == "kr-real-provider-mappings-generated-v1"
    assert len(registry.mappings) == 3


def test_validate_provider_mappings_cover_universe_passes(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )
    assert payload["validation"]["status"] == "ok"


def test_selected_yfinance_symbols_are_explicit_from_pool(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    for symbol in payload["selected_symbols"]:
        mapping = registry.resolve(symbol=str(symbol), market="KR")
        assert mapping.yfinance is not None
        assert mapping.yfinance.provider_symbol == _SYNTHETIC_YFINANCE[str(symbol)]


def test_dart_corp_codes_come_from_resolver_fixture(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    for symbol in payload["selected_symbols"]:
        mapping = registry.resolve(symbol=str(symbol), market="KR")
        assert mapping.dart is not None
        assert mapping.dart.corp_code == _SYNTHETIC_CORP_CODES[str(symbol)]


def test_empty_selection_fails_at_select_stage(tmp_path: Path) -> None:
    args = _workflow_args(tmp_path)
    args["sectors"] = {"nonexistent-sector"}
    with pytest.raises(BuildKrRealSectorPoolMappingError) as exc_info:
        run_build_kr_real_sector_pool_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "select"


def test_missing_corp_code_mode_fails_at_args(tmp_path: Path) -> None:
    from build_kr_real_sector_pool_mapping import main

    argv = _base_cli_args(tmp_path)
    argv = [arg for arg in argv if arg not in {"--corp-code-xml", str(SYNTHETIC_CORP_CODE_XML)}]
    assert main(argv + ["--force", "--json"]) == 1


def test_missing_corp_code_file_fails_at_resolve_stage(tmp_path: Path) -> None:
    args = _workflow_args(tmp_path)
    args["corp_code_xml"] = tmp_path / "missing_corp_code.xml"
    with pytest.raises(BuildKrRealSectorPoolMappingError) as exc_info:
        run_build_kr_real_sector_pool_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "resolve"


def test_both_corp_code_sources_rejected_at_args(tmp_path: Path) -> None:
    from build_kr_real_sector_pool_mapping import main

    argv = _base_cli_args(tmp_path) + [
        "--corp-code-zip",
        str(tmp_path / "corp_code.zip"),
        "--force",
        "--json",
    ]
    result_code = main(argv)
    assert result_code == 1


def test_cli_refuses_selected_candidates_overwrite_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_real_sector_pool_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_cli_refuses_generated_output_overwrite_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_real_sector_pool_mapping import main

    selected_out = tmp_path / "selected.toml"
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()

    selected_out.unlink()
    argv_retry = _base_cli_args(tmp_path) + ["--json"]
    assert main(argv_retry) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"
    assert universe_out.is_file()
    assert mapping_out.is_file()


def test_cli_json_stdout_is_pure_json(tmp_path: Path) -> None:
    result = _run_cli(*(_base_cli_args(tmp_path) + ["--force", "--json"]))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"


def test_ops_script_has_no_forbidden_tokens() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "dart_api_key",
        "fred_api_key",
        "import yfinance",
        "from yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
        "os.environ",
        "getenv",
    )
    for token in forbidden:
        assert token not in source, f"build_kr_real_sector_pool_mapping.py must not reference {token!r}"


def test_no_runtime_files_tracked_in_repo() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_static_kr_real_config_samples_unchanged() -> None:
    universe_before = KR_REAL_UNIVERSE.read_text(encoding="utf-8")
    mapping_before = KR_REAL_MAPPING.read_text(encoding="utf-8")
    assert "kr-real-sample-v0" in universe_before
    assert "kr-real-provider-mappings-v1" in mapping_before


def test_existing_3g1_tests_remain_importable() -> None:
    import test_kr_candidate_pool  # noqa: F401
