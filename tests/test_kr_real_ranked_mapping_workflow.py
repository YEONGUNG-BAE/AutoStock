"""Real Intake 3G3-2 — operator-local ranked mapping workflow tests."""

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
SIGNALS_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_candidates"
    / "kr_ranking_signals.synthetic.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "build_kr_real_ranked_mapping.py"

_SYNTHETIC_CORP_CODES = {
    "900001": "90000010",
    "900002": "90000011",
    "900003": "90000012",
}
_SYNTHETIC_YFINANCE = {
    "900001": "900001.KS",
    "900002": "900002.KS",
    "900003": "900003.KS",
}

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from build_kr_real_ranked_mapping import (
    BuildKrRealRankedMappingError,
    run_build_kr_real_ranked_mapping,
)
from data.kr_provider_mapping_generator import parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _workflow_args(tmp_path: Path) -> dict[str, object]:
    return {
        "candidate_pool_path": POOL_FIXTURE,
        "ranking_signals_path": SIGNALS_FIXTURE,
        "corp_code_xml": SYNTHETIC_CORP_CODE_XML,
        "corp_code_zip": None,
        "ranked_out": tmp_path / "ranked.json",
        "selected_candidates_out": tmp_path / "selected.toml",
        "universe_out": tmp_path / "universe.generated.toml",
        "provider_mapping_out": tmp_path / "provider_mappings.generated.toml",
        "selection_name": "kr-ranked-selected-v1",
        "selection_description": "Operator-ranked KR candidates.",
        "universe_name": "kr-real-ranked-v1",
        "provider_mapping_name": "kr-real-ranked-provider-mappings-v1",
        "sectors": {"semiconductors", "internet"},
        "max_total": 5,
        "max_per_sector": 2,
        "top_n": 3,
        "force": True,
    }


def _run_workflow(tmp_path: Path) -> dict[str, object]:
    return run_build_kr_real_ranked_mapping(**_workflow_args(tmp_path))  # type: ignore[arg-type]


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
        "--ranking-signals",
        str(SIGNALS_FIXTURE),
        "--corp-code-xml",
        str(SYNTHETIC_CORP_CODE_XML),
        "--sector",
        "semiconductors",
        "--sector",
        "internet",
        "--max-total",
        "5",
        "--max-per-sector",
        "2",
        "--top-n",
        "3",
        "--ranked-out",
        str(tmp_path / "ranked.json"),
        "--selected-candidates-out",
        str(tmp_path / "selected.toml"),
        "--selection-name",
        "kr-ranked-selected-v1",
        "--selection-description",
        "Operator-ranked KR candidates.",
        "--universe-out",
        str(tmp_path / "universe.generated.toml"),
        "--provider-mapping-out",
        str(tmp_path / "provider_mappings.generated.toml"),
        "--universe-name",
        "kr-real-ranked-v1",
        "--provider-mapping-name",
        "kr-real-ranked-provider-mappings-v1",
    ]


def test_workflow_ranks_synthetic_candidate_pool(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert payload["ranked_count"] == 3
    assert payload["selected_count"] == 3


def test_workflow_writes_ranked_json(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    ranked_out = Path(str(payload["ranked_out"]))
    assert ranked_out.is_file()
    ranked = json.loads(ranked_out.read_text(encoding="utf-8"))
    assert ranked["ranked_count"] == 3
    assert [entry["symbol"] for entry in ranked["ranked"]] == ["900001", "900002", "900003"]


def test_workflow_writes_selected_candidate_toml(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    selected_out = Path(str(payload["selected_candidates_out"]))
    assert selected_out.is_file()


def test_selected_candidate_toml_has_no_ranking_metadata(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    raw = tomllib.loads(Path(str(payload["selected_candidates_out"])).read_text(encoding="utf-8"))
    forbidden = {
        "score",
        "rank",
        "score_components",
        "score_contributions",
        "explanation",
        "sector",
        "industry",
        "priority",
        "notes",
        "eligible",
        "base_market",
        "corp_code",
    }
    assert "base_market" not in raw
    for entry in raw["candidates"]:
        assert forbidden.isdisjoint(entry.keys())


def test_selected_candidate_toml_loads_via_3f1_parser(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    document = parse_kr_candidates_toml(Path(str(payload["selected_candidates_out"])))
    assert len(document.candidates) == 3


def test_workflow_generates_universe_and_provider_mapping(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert Path(str(payload["universe_out"])).is_file()
    assert Path(str(payload["provider_mapping_out"])).is_file()


def test_generated_universe_loads(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    assert universe.name == "kr-real-ranked-v1"
    assert len(universe.enabled_symbols) == 3


def test_generated_provider_mapping_loads(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    assert registry.name == "kr-real-ranked-provider-mappings-v1"
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


def test_yfinance_symbols_are_explicit_from_pool(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    for entry in ranked["ranked"]:
        symbol = entry["symbol"]
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.yfinance is not None
        assert mapping.yfinance.provider_symbol == _SYNTHETIC_YFINANCE[symbol]


def test_dart_corp_codes_come_from_resolver_fixture(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    for entry in ranked["ranked"]:
        symbol = entry["symbol"]
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.dart is not None
        assert mapping.dart.corp_code == _SYNTHETIC_CORP_CODES[symbol]


def test_missing_corp_code_mode_fails_at_args(tmp_path: Path) -> None:
    from build_kr_real_ranked_mapping import main

    argv = _base_cli_args(tmp_path)
    argv = [arg for arg in argv if arg not in {"--corp-code-xml", str(SYNTHETIC_CORP_CODE_XML)}]
    assert main(argv + ["--force", "--json"]) == 1


def test_missing_corp_code_file_fails_at_resolve_stage(tmp_path: Path) -> None:
    args = _workflow_args(tmp_path)
    args["corp_code_xml"] = tmp_path / "missing_corp_code.xml"
    with pytest.raises(BuildKrRealRankedMappingError) as exc_info:
        run_build_kr_real_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "resolve"


def test_empty_ranking_selection_fails_at_rank_stage(tmp_path: Path) -> None:
    args = _workflow_args(tmp_path)
    args["sectors"] = {"nonexistent-sector"}
    with pytest.raises(BuildKrRealRankedMappingError) as exc_info:
        run_build_kr_real_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "rank"


def test_missing_ranking_signal_fails_at_rank_stage(tmp_path: Path) -> None:
    partial_signals = tmp_path / "partial_signals.toml"
    partial_signals.write_text(
        SIGNALS_FIXTURE.read_text(encoding="utf-8").replace("900003", "999999"),
        encoding="utf-8",
    )
    args = _workflow_args(tmp_path)
    args["ranking_signals_path"] = partial_signals
    with pytest.raises(BuildKrRealRankedMappingError) as exc_info:
        run_build_kr_real_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "rank"
    assert "missing ranking signal" in exc_info.value.message


@pytest.mark.parametrize(
    ("extra_args",),
    [
        (["--max-total", "0"],),
        (["--max-per-sector", "-1"],),
        (["--top-n", "0"],),
        (["--top-n", "-1"],),
    ],
)
def test_invalid_limit_args_fail_at_args_stage(
    tmp_path: Path,
    extra_args: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_real_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + extra_args + ["--force", "--json"]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_missing_required_cli_args_caught_by_argparse(tmp_path: Path) -> None:
    result = _run_cli(
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--ranking-signals",
        str(SIGNALS_FIXTURE),
    )
    assert result.returncode == 2
    assert "required" in result.stderr.lower()


def test_cli_text_control_characters_fail_at_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from build_kr_real_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + [
        "--selection-name",
        "bad\x01name",
        "--force",
        "--json",
    ]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"
    assert "control character" in payload["error"]


def test_cli_refuses_ranked_json_overwrite_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_real_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_cli_refuses_selected_candidate_overwrite_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_real_ranked_mapping import main

    ranked_out = tmp_path / "ranked.json"
    selected_out = tmp_path / "selected.toml"

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()

    ranked_out.unlink()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"
    assert selected_out.is_file()


def test_cli_refuses_generated_output_overwrite_without_force(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_real_ranked_mapping import main

    ranked_out = tmp_path / "ranked.json"
    selected_out = tmp_path / "selected.toml"
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()

    ranked_out.unlink()
    selected_out.unlink()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
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
        assert token not in source, f"build_kr_real_ranked_mapping.py must not reference {token!r}"


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


def test_static_scan_includes_new_ops_file() -> None:
    import test_fetch_research_sources  # noqa: F401

    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "build_kr_real_ranked_mapping.py" in paths_text


def test_existing_3g3_1_tests_remain_importable() -> None:
    import test_kr_candidate_ranker  # noqa: F401
