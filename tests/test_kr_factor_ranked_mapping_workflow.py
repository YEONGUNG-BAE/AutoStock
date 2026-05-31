"""Real Intake 3G4-2 — factor signal → ranked mapping workflow tests."""

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
FACTOR_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_factors"
    / "kr_factor_inputs.synthetic.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
OPS_SCRIPT = REPO_ROOT / "ops" / "build_kr_factor_ranked_mapping.py"

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
_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "target_allocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
    }
)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from build_kr_factor_ranked_mapping import (
    BuildKrFactorRankedMappingError,
    run_build_kr_factor_ranked_mapping,
)
from data.kr_candidate_ranker import parse_ranking_signals_toml
from data.kr_provider_mapping_generator import parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _workflow_args(tmp_path: Path) -> dict[str, object]:
    return {
        "candidate_pool_path": POOL_FIXTURE,
        "factor_inputs_path": FACTOR_FIXTURE,
        "corp_code_xml": SYNTHETIC_CORP_CODE_XML,
        "corp_code_zip": None,
        "factor_signals_out": tmp_path / "factor_signals.generated.toml",
        "ranked_out": tmp_path / "ranked.json",
        "selected_candidates_out": tmp_path / "selected.toml",
        "universe_out": tmp_path / "universe.generated.toml",
        "provider_mapping_out": tmp_path / "provider_mappings.generated.toml",
        "factor_output_name": "kr-factor-signals-synthetic-v1",
        "factor_output_description": "Synthetic fixture-first KR factor signals.",
        "selection_name": "kr-factor-ranked-selected-v1",
        "selection_description": "Factor-ranked KR candidates.",
        "universe_name": "kr-factor-ranked-universe-v1",
        "provider_mapping_name": "kr-factor-ranked-provider-mappings-v1",
        "top_n": 3,
        "force": True,
    }


def _run_workflow(tmp_path: Path, **overrides: object) -> dict[str, object]:
    args = _workflow_args(tmp_path)
    args.update(overrides)
    return run_build_kr_factor_ranked_mapping(**args)  # type: ignore[arg-type]


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
        "--factor-inputs",
        str(FACTOR_FIXTURE),
        "--corp-code-xml",
        str(SYNTHETIC_CORP_CODE_XML),
        "--factor-signals-out",
        str(tmp_path / "factor_signals.generated.toml"),
        "--ranked-out",
        str(tmp_path / "ranked.json"),
        "--selected-candidates-out",
        str(tmp_path / "selected.toml"),
        "--universe-out",
        str(tmp_path / "universe.generated.toml"),
        "--provider-mapping-out",
        str(tmp_path / "provider_mappings.generated.toml"),
        "--factor-output-name",
        "kr-factor-signals-synthetic-v1",
        "--factor-output-description",
        "Synthetic fixture-first KR factor signals.",
        "--selection-name",
        "kr-factor-ranked-selected-v1",
        "--selection-description",
        "Factor-ranked KR candidates.",
        "--universe-name",
        "kr-factor-ranked-universe-v1",
        "--provider-mapping-name",
        "kr-factor-ranked-provider-mappings-v1",
        "--top-n",
        "3",
    ]


def _walk_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key not in _FORBIDDEN_OUTPUT_FIELDS
            _walk_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_forbidden_fields(nested)


def test_workflow_success_with_synthetic_fixtures(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "factor-ranked-mapping-workflow"


def test_generated_factor_signals_toml_exists(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert Path(str(payload["factor_signals_out"])).is_file()


def test_generated_factor_signals_load_via_ranker_parser(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    document = parse_ranking_signals_toml(Path(str(payload["factor_signals_out"])))
    assert document.score_version == "kr-factor-fixture-v1"
    assert len(document.signals) == 5


def test_ranked_json_exists_and_has_ranked_count_three(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    assert ranked["ranked_count"] == 3


def test_selected_candidates_toml_exists_and_parses(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    document = parse_kr_candidates_toml(Path(str(payload["selected_candidates_out"])))
    assert len(document.candidates) == 3


def test_generated_universe_toml_exists_and_loads(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    assert universe.name == "kr-factor-ranked-universe-v1"
    assert len(universe.enabled_symbols) == 3


def test_generated_provider_mapping_toml_exists_and_loads(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    assert registry.name == "kr-factor-ranked-provider-mappings-v1"
    assert len(registry.mappings) == 3


def test_provider_mapping_validation_succeeds(tmp_path: Path) -> None:
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
    assert payload["validation"]["require_yfinance"] is True
    assert payload["validation"]["require_dart"] is True


def test_factor_signal_output_contains_five_signals(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert payload["signals_count"] == 5


def test_selected_candidates_use_default_pool_selection(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert payload["selected_count"] == 3
    selected = parse_kr_candidates_toml(Path(str(payload["selected_candidates_out"])))
    assert {entry.symbol for entry in selected.candidates} == {"900001", "900002", "900003"}


def test_happy_path_does_not_use_include_disabled_or_ineligible(tmp_path: Path) -> None:
    args = _workflow_args(tmp_path)
    assert "include_disabled" not in args
    assert "include_ineligible" not in args
    _run_workflow(tmp_path)


def test_workflow_outputs_contain_no_trading_fields(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    _walk_forbidden_fields(payload)
    _walk_forbidden_fields(json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["factor_signals_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["selected_candidates_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["universe_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["provider_mapping_out"])).read_text(encoding="utf-8")))


def test_factor_signal_output_contains_no_corp_code(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    raw = tomllib.loads(Path(str(payload["factor_signals_out"])).read_text(encoding="utf-8"))
    assert "corp_code" not in raw
    for entry in raw["signals"]:
        assert "corp_code" not in entry


def test_corp_code_appears_only_in_generated_provider_mapping(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    for entry in ranked["ranked"]:
        symbol = entry["symbol"]
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.dart is not None
        assert mapping.dart.corp_code == _SYNTHETIC_CORP_CODES[symbol]


def test_yfinance_provider_symbol_comes_from_candidate_pool_not_factor_generator(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    factor_raw = tomllib.loads(Path(str(payload["factor_signals_out"])).read_text(encoding="utf-8"))
    for entry in factor_raw["signals"]:
        assert "yfinance_provider_symbol" not in entry
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    for entry in ranked["ranked"]:
        symbol = entry["symbol"]
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.yfinance is not None
        assert mapping.yfinance.provider_symbol == _SYNTHETIC_YFINANCE[symbol]


def test_missing_corp_code_source_fails_at_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = [arg for arg in _base_cli_args(tmp_path) if arg not in {"--corp-code-xml", str(SYNTHETIC_CORP_CODE_XML)}]
    assert main(argv + ["--force", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_both_corp_code_xml_and_zip_fails_at_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + [
        "--corp-code-zip",
        str(tmp_path / "dummy.zip"),
        "--force",
        "--json",
    ]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_missing_corp_code_file_fails_at_resolve_stage(tmp_path: Path) -> None:
    args = _workflow_args(tmp_path)
    args["corp_code_xml"] = tmp_path / "missing_corp_code.xml"
    with pytest.raises(BuildKrFactorRankedMappingError) as exc_info:
        run_build_kr_factor_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "resolve"


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--top-n", "0"],
        ["--max-total", "0"],
        ["--max-per-sector", "-1"],
    ],
)
def test_invalid_limit_args_fail_at_args_stage(
    tmp_path: Path,
    extra_args: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + extra_args + ["--force", "--json"]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_cli_text_control_characters_fail_at_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + [
        "--factor-output-name",
        "bad\x01name",
        "--force",
        "--json",
    ]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"
    assert "control character" in payload["error"]
    assert "bad" not in payload["error"]


def test_bad_factor_input_fails_at_parse_stage(tmp_path: Path) -> None:
    bad_factor = tmp_path / "bad_factor.toml"
    bad_factor.write_text(
        """
version = 2
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    args = _workflow_args(tmp_path)
    args["factor_inputs_path"] = bad_factor
    with pytest.raises(BuildKrFactorRankedMappingError) as exc_info:
        run_build_kr_factor_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "parse"


def test_duplicate_factor_symbol_fails_at_generate_stage(tmp_path: Path) -> None:
    dup_factor = tmp_path / "dup_factor.toml"
    dup_factor.write_text(
        """
version = 1
name = "dup"
description = "dup"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1

[[factors]]
symbol = "KR:900001"
market = "KR"
liquidity_percentile = 0.6
market_cap_percentile = 0.6
profitability_score = 0.6
balance_sheet_score = 0.6
momentum_percentile = 0.6
volatility_risk = 0.2
""".strip()
        + "\n",
        encoding="utf-8",
    )
    args = _workflow_args(tmp_path)
    args["factor_inputs_path"] = dup_factor
    with pytest.raises(BuildKrFactorRankedMappingError) as exc_info:
        run_build_kr_factor_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "generate"


def test_candidate_pool_parse_failure_remains_parse_stage(tmp_path: Path) -> None:
    bad_pool = tmp_path / "bad_pool.toml"
    bad_pool.write_text("version = 2\n", encoding="utf-8")
    args = _workflow_args(tmp_path)
    args["candidate_pool_path"] = bad_pool
    with pytest.raises(BuildKrFactorRankedMappingError) as exc_info:
        run_build_kr_factor_ranked_mapping(**args)  # type: ignore[arg-type]
    assert exc_info.value.stage == "parse"


def test_factor_signals_out_exists_without_force_fails_at_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_ranked_out_exists_without_force_fails_at_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    Path(str(tmp_path / "factor_signals.generated.toml")).unlink()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_selected_candidates_out_exists_without_force_fails_at_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    Path(str(tmp_path / "factor_signals.generated.toml")).unlink()
    Path(str(tmp_path / "ranked.json")).unlink()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"
    assert Path(str(tmp_path / "selected.toml")).is_file()


def test_universe_out_exists_without_force_fails_at_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    for name in (
        "factor_signals.generated.toml",
        "ranked.json",
        "selected.toml",
    ):
        Path(str(tmp_path / name)).unlink()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"
    assert Path(str(tmp_path / "universe.generated.toml")).is_file()


def test_provider_mapping_out_exists_without_force_fails_at_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_ranked_mapping import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    for name in (
        "factor_signals.generated.toml",
        "ranked.json",
        "selected.toml",
        "universe.generated.toml",
    ):
        Path(str(tmp_path / name)).unlink()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"
    assert Path(str(tmp_path / "provider_mappings.generated.toml")).is_file()


def test_force_allows_all_outputs_to_be_overwritten(tmp_path: Path) -> None:
    _run_workflow(tmp_path)
    payload = _run_workflow(tmp_path)
    assert payload["stage"] == "complete"


def test_sector_filter_passes_through(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path, sectors={"semiconductors"}, top_n=2)
    assert payload["ranked_count"] == 2
    assert payload["selected_count"] == 2


def test_top_n_passes_through(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path, top_n=2)
    assert payload["selected_count"] == 2
    selected = parse_kr_candidates_toml(Path(str(payload["selected_candidates_out"])))
    assert len(selected.candidates) == 2


def test_max_total_and_max_per_sector_pass_through(tmp_path: Path) -> None:
    payload = _run_workflow(
        tmp_path,
        sectors={"semiconductors", "internet"},
        max_total=5,
        max_per_sector=2,
        top_n=3,
    )
    assert payload["ranked_count"] == 3
    assert payload["selected_count"] == 3


def test_success_json_contains_expected_paths_counts_and_validation(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    assert payload["factor_signals_out"] == str(tmp_path / "factor_signals.generated.toml")
    assert payload["ranked_out"] == str(tmp_path / "ranked.json")
    assert payload["selected_candidates_out"] == str(tmp_path / "selected.toml")
    assert payload["universe_out"] == str(tmp_path / "universe.generated.toml")
    assert payload["provider_mapping_out"] == str(tmp_path / "provider_mappings.generated.toml")
    assert payload["signals_count"] == 5
    assert payload["ranked_count"] == 3
    assert payload["selected_count"] == 3
    assert payload["validation"]["status"] == "ok"


def test_success_json_contains_no_trading_fields(tmp_path: Path) -> None:
    payload = _run_workflow(tmp_path)
    _walk_forbidden_fields(payload)


def test_no_env_or_api_key_read_in_ops_file() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8")
    for token in ("os.environ", "getenv", "FRED_API_KEY", "DART_API_KEY"):
        assert token not in source, f"ops script must not reference {token!r}"


def test_no_network_or_live_api_in_ops_file() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "import yfinance",
    )
    for token in forbidden:
        assert token not in source, f"ops script must not reference {token!r}"


def test_static_scan_includes_new_ops_file() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "build_kr_factor_ranked_mapping.py" in paths_text


def test_no_broker_paperloop_kis_path_in_ops_file() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    for token in ("paperlooprunner", "paperbroker", "submit_order", "kis"):
        assert token not in source, f"ops script must not reference {token!r}"


def test_existing_3g4_1_tests_remain_importable() -> None:
    import test_kr_factor_signal_generator  # noqa: F401

    assert test_kr_factor_signal_generator.FACTOR_FIXTURE.is_file()


def test_existing_3g3_2_tests_remain_importable() -> None:
    import test_kr_real_ranked_mapping_workflow  # noqa: F401

    assert test_kr_real_ranked_mapping_workflow.OPS_SCRIPT.is_file()
