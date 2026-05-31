"""Real Intake 3G4-3 — operator-local factor input bundle workflow tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_factors"
    / "kr_factor_bundle.synthetic.toml"
)
FACTOR_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_factors"
    / "kr_factor_inputs.synthetic.toml"
)
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
OPS_SCRIPT = REPO_ROOT / "ops" / "build_kr_factor_bundle_mapping.py"

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

from build_kr_factor_bundle_mapping import (
    BuildKrFactorBundleMappingError,
    KrFactorBundle,
    load_kr_factor_bundle_toml,
    run_build_kr_factor_bundle_mapping,
)
from data.kr_candidate_ranker import parse_ranking_signals_toml
from data.kr_provider_mapping_generator import parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_bundle(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bundle.toml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _valid_bundle_body(**overrides: str) -> str:
    base = """
version = 1
name = "kr-factor-bundle-test-v1"
description = "Test bundle."
base_market = "KR"

[inputs]
candidate_pool = "../kr_candidates/kr_sector_candidate_pool.synthetic.toml"
factor_inputs = "kr_factor_inputs.synthetic.toml"
corp_code_xml = "../dart/corp_code_synthetic_multi.xml"

[outputs]
factor_signals_out = "outputs/factor_signals.generated.toml"
ranked_out = "outputs/ranked.json"
selected_candidates_out = "outputs/selected.toml"
universe_out = "outputs/universe.generated.toml"
provider_mapping_out = "outputs/provider_mappings.generated.toml"

[names]
factor_output_name = "kr-factor-signals-synthetic-v1"
factor_output_description = "Synthetic fixture-first KR factor signals."
selection_name = "kr-factor-ranked-selected-v1"
selection_description = "Factor-ranked KR candidates."
universe_name = "kr-factor-ranked-universe-v1"
provider_mapping_name = "kr-factor-ranked-provider-mappings-v1"

[selection]
top_n = 3
"""
    text = base.strip()
    for key, value in overrides.items():
        text = text.replace(key, value)
    return text


def _copy_bundle_fixture_tree(tmp_path: Path) -> Path:
    """fixture bundle과 참조 입력을 tmp tree로 복사한다(relative path 해석 보존)."""
    research_root = tmp_path / "research"
    factors_dir = research_root / "kr_factors"
    candidates_dir = research_root / "kr_candidates"
    dart_dir = research_root / "dart"
    factors_dir.mkdir(parents=True)
    candidates_dir.mkdir(parents=True)
    dart_dir.mkdir(parents=True)
    (candidates_dir / "kr_sector_candidate_pool.synthetic.toml").write_text(
        POOL_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (dart_dir / "corp_code_synthetic_multi.xml").write_text(
        SYNTHETIC_CORP_CODE_XML.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (factors_dir / "kr_factor_inputs.synthetic.toml").write_text(
        FACTOR_FIXTURE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    bundle_path = factors_dir / "kr_factor_bundle.synthetic.toml"
    bundle_path.write_text(BUNDLE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return bundle_path


def _run_bundle(tmp_path: Path, **overrides: object) -> dict[str, object]:
    bundle_path = overrides.pop("bundle_path", BUNDLE_FIXTURE)
    out_dir = overrides.pop("out_dir", tmp_path / "outputs")
    force = overrides.pop("force", True)
    return run_build_kr_factor_bundle_mapping(
        bundle_path=Path(str(bundle_path)),
        out_dir=Path(str(out_dir)) if out_dir is not None else None,
        force=bool(force),
    )


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


def _walk_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key not in _FORBIDDEN_OUTPUT_FIELDS
            _walk_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_forbidden_fields(nested)


def test_synthetic_bundle_fixture_parses() -> None:
    bundle = load_kr_factor_bundle_toml(BUNDLE_FIXTURE)
    assert bundle.name == "kr-factor-bundle-synthetic-v1"
    assert bundle.base_market == "KR"


def test_bundle_version_must_be_one(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, _valid_bundle_body().replace("version = 1", "version = 2"))
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_base_market_must_be_kr(tmp_path: Path) -> None:
    path = _write_bundle(tmp_path, _valid_bundle_body().replace('base_market = "KR"', 'base_market = "US"'))
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_unknown_root_fields_rejected(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace("base_market = \"KR\"", "base_market = \"KR\"\nextra = true")
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"
    assert "unknown root fields" in exc_info.value.message


def test_unknown_table_fields_rejected(tmp_path: Path) -> None:
    path = _write_bundle(
        tmp_path,
        _valid_bundle_body().replace("[inputs]", "[inputs]\nextra = true"),
    )
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"
    assert "unknown inputs fields" in exc_info.value.message


def test_candidate_pool_required(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace(
        'candidate_pool = "../kr_candidates/kr_sector_candidate_pool.synthetic.toml"\n',
        "",
    )
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_factor_inputs_required(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace('factor_inputs = "kr_factor_inputs.synthetic.toml"\n', "")
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_exactly_one_corp_code_source_required(tmp_path: Path) -> None:
    bundle = load_kr_factor_bundle_toml(BUNDLE_FIXTURE)
    assert bundle.corp_code_xml is not None
    assert bundle.corp_code_zip is None


def test_both_corp_code_xml_and_zip_in_bundle_fail_at_parse(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace(
        'corp_code_xml = "../dart/corp_code_synthetic_multi.xml"',
        'corp_code_xml = "../dart/corp_code_synthetic_multi.xml"\ncorp_code_zip = "../dart/dummy.zip"',
    )
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_no_corp_code_source_in_bundle_fails_at_parse(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace('corp_code_xml = "../dart/corp_code_synthetic_multi.xml"\n', "")
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_selection_max_total_must_be_positive(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace("[selection]\ntop_n = 3", "[selection]\nmax_total = 0")
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_selection_max_per_sector_must_be_positive(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace("[selection]\ntop_n = 3", "[selection]\nmax_per_sector = -1")
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_selection_top_n_must_be_positive(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace("top_n = 3", "top_n = 0")
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"


def test_control_characters_rejected_without_echoing_offending_value(tmp_path: Path) -> None:
    body = _valid_bundle_body().replace(
        'factor_output_name = "kr-factor-signals-synthetic-v1"',
        'factor_output_name = "bad\\u0001name"',
    )
    path = _write_bundle(tmp_path, body)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        load_kr_factor_bundle_toml(path)
    assert exc_info.value.stage == "parse"
    assert "control character" in exc_info.value.message
    assert "bad" not in exc_info.value.message


def test_relative_input_paths_resolve_from_bundle_file_directory(tmp_path: Path) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    bundle = load_kr_factor_bundle_toml(bundle_path)
    bundle_dir = bundle_path.parent.resolve()
    payload = run_build_kr_factor_bundle_mapping(
        bundle_path=bundle_path,
        out_dir=tmp_path / "outputs",
        force=True,
    )
    assert payload["status"] == "ok"
    assert bundle.candidate_pool == "../kr_candidates/kr_sector_candidate_pool.synthetic.toml"
    expected_pool = (bundle_dir / bundle.candidate_pool).resolve()
    assert expected_pool.is_file()
    assert expected_pool.parent.name == "kr_candidates"


def test_relative_output_paths_resolve_from_bundle_file_directory_when_no_out_dir(
    tmp_path: Path,
) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    bundle_dir = bundle_path.parent.resolve()
    payload = run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=None, force=True)
    assert payload["factor_signals_out"] == str((bundle_dir / "outputs" / "factor_signals.generated.toml").resolve())


def test_out_dir_overrides_manifest_output_paths(tmp_path: Path) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    out_dir = tmp_path / "override_outputs"
    payload = run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=out_dir, force=True)
    assert payload["factor_signals_out"] == str((out_dir / "factor_signals.generated.toml").resolve())
    assert payload["ranked_out"] == str((out_dir / "ranked.json").resolve())
    assert payload["selected_candidates_out"] == str((out_dir / "selected_candidates.toml").resolve())
    assert payload["universe_out"] == str((out_dir / "universe.generated.toml").resolve())
    assert payload["provider_mapping_out"] == str((out_dir / "provider_mappings.generated.toml").resolve())


def test_out_dir_uses_deterministic_output_filenames(tmp_path: Path) -> None:
    out_dir = tmp_path / "deterministic_outputs"
    payload = _run_bundle(tmp_path, out_dir=out_dir)
    assert Path(str(payload["factor_signals_out"])).name == "factor_signals.generated.toml"
    assert Path(str(payload["ranked_out"])).name == "ranked.json"
    assert Path(str(payload["selected_candidates_out"])).name == "selected_candidates.toml"
    assert Path(str(payload["universe_out"])).name == "universe.generated.toml"
    assert Path(str(payload["provider_mapping_out"])).name == "provider_mappings.generated.toml"


def test_workflow_success_with_synthetic_bundle_and_out_dir(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "factor-bundle-ranked-mapping-workflow"


def test_generated_factor_signals_toml_exists(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    assert Path(str(payload["factor_signals_out"])).is_file()


def test_generated_factor_signals_toml_loads_through_parse_ranking_signals_toml(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    document = parse_ranking_signals_toml(Path(str(payload["factor_signals_out"])))
    assert document.score_version == "kr-factor-fixture-v1"
    assert len(document.signals) == 5


def test_ranked_json_exists_and_ranked_count_is_three(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    assert ranked["ranked_count"] == 3


def test_selected_candidates_toml_exists_and_parses(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    document = parse_kr_candidates_toml(Path(str(payload["selected_candidates_out"])))
    assert len(document.candidates) == 3


def test_universe_toml_exists_and_loads(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    assert universe.name == "kr-factor-ranked-universe-v1"
    assert len(universe.enabled_symbols) == 3


def test_provider_mapping_toml_exists_and_loads(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    assert registry.name == "kr-factor-ranked-provider-mappings-v1"
    assert len(registry.mappings) == 3


def test_provider_mapping_validation_succeeds_with_require_yfinance_and_dart(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
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


def test_success_json_includes_expected_paths_counts_and_validation_block(tmp_path: Path) -> None:
    out_dir = tmp_path / "json_outputs"
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    payload = run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=out_dir, force=True)
    assert payload["bundle"] == str(bundle_path.resolve())
    assert payload["out_dir"] == str(out_dir.resolve())
    assert payload["signals_count"] == 5
    assert payload["ranked_count"] == 3
    assert payload["selected_count"] == 3
    assert payload["validation"]["status"] == "ok"


def test_success_json_contains_no_trading_action_allocation_order_fields(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    _walk_forbidden_fields(payload)


def test_output_files_contain_no_trading_action_allocation_order_fields(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    _walk_forbidden_fields(json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["factor_signals_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["selected_candidates_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["universe_out"])).read_text(encoding="utf-8")))
    _walk_forbidden_fields(tomllib.loads(Path(str(payload["provider_mapping_out"])).read_text(encoding="utf-8")))


def test_factor_signal_output_contains_no_corp_code(tmp_path: Path) -> None:
    payload = _run_bundle(tmp_path)
    raw = tomllib.loads(Path(str(payload["factor_signals_out"])).read_text(encoding="utf-8"))
    assert "corp_code" not in raw
    for entry in raw["signals"]:
        assert "corp_code" not in entry


def test_corp_code_appears_only_in_generated_provider_mapping_from_3f1_resolver_path(
    tmp_path: Path,
) -> None:
    payload = _run_bundle(tmp_path)
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    ranked = json.loads(Path(str(payload["ranked_out"])).read_text(encoding="utf-8"))
    for entry in ranked["ranked"]:
        symbol = entry["symbol"]
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.dart is not None
        assert mapping.dart.corp_code == _SYNTHETIC_CORP_CODES[symbol]


def test_yfinance_provider_symbol_comes_only_from_candidate_pool_fixture_and_3f1_path(
    tmp_path: Path,
) -> None:
    payload = _run_bundle(tmp_path)
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


def test_missing_corp_code_file_referenced_by_bundle_fails_at_resolve_stage(tmp_path: Path) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    body = bundle_path.read_text(encoding="utf-8").replace(
        'corp_code_xml = "../dart/corp_code_synthetic_multi.xml"',
        'corp_code_xml = "../dart/missing_corp_code.xml"',
    )
    bundle_path.write_text(body, encoding="utf-8")
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=tmp_path / "outputs", force=True)
    assert exc_info.value.stage == "resolve"


def test_bad_factor_input_referenced_by_bundle_fails_at_parse_stage(tmp_path: Path) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    bad_factor = bundle_path.parent / "bad_factor.toml"
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
    body = bundle_path.read_text(encoding="utf-8").replace(
        'factor_inputs = "kr_factor_inputs.synthetic.toml"',
        'factor_inputs = "bad_factor.toml"',
    )
    bundle_path.write_text(body, encoding="utf-8")
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=tmp_path / "outputs", force=True)
    assert exc_info.value.stage == "parse"


def test_duplicate_factor_symbol_referenced_by_bundle_fails_at_generate_stage(tmp_path: Path) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    dup_factor = bundle_path.parent / "dup_factor.toml"
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
    body = bundle_path.read_text(encoding="utf-8").replace(
        'factor_inputs = "kr_factor_inputs.synthetic.toml"',
        'factor_inputs = "dup_factor.toml"',
    )
    bundle_path.write_text(body, encoding="utf-8")
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=tmp_path / "outputs", force=True)
    assert exc_info.value.stage == "generate"


def test_bad_candidate_pool_referenced_by_bundle_preserves_parse_stage(tmp_path: Path) -> None:
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    bad_pool = bundle_path.parent / "bad_pool.toml"
    bad_pool.write_text("version = 2\n", encoding="utf-8")
    body = bundle_path.read_text(encoding="utf-8").replace(
        'candidate_pool = "../kr_candidates/kr_sector_candidate_pool.synthetic.toml"',
        'candidate_pool = "bad_pool.toml"',
    )
    bundle_path.write_text(body, encoding="utf-8")
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=tmp_path / "outputs", force=True)
    assert exc_info.value.stage == "parse"


def test_overwrite_without_force_fails_at_write_stage(tmp_path: Path) -> None:
    out_dir = tmp_path / "write_outputs"
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=out_dir, force=True)
    with pytest.raises(BuildKrFactorBundleMappingError) as exc_info:
        run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=out_dir, force=False)
    assert exc_info.value.stage == "write"


def test_force_allows_outputs_to_be_overwritten(tmp_path: Path) -> None:
    out_dir = tmp_path / "force_outputs"
    bundle_path = _copy_bundle_fixture_tree(tmp_path)
    run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=out_dir, force=True)
    payload = run_build_kr_factor_bundle_mapping(bundle_path=bundle_path, out_dir=out_dir, force=True)
    assert payload["stage"] == "complete"


def test_cli_success_writes_all_expected_outputs(tmp_path: Path) -> None:
    out_dir = tmp_path / "cli_outputs"
    result = _run_cli(
        "--bundle",
        str(BUNDLE_FIXTURE),
        "--out-dir",
        str(out_dir),
        "--force",
        "--json",
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout.strip())
    assert payload["stage"] == "complete"
    for key in (
        "factor_signals_out",
        "ranked_out",
        "selected_candidates_out",
        "universe_out",
        "provider_mapping_out",
    ):
        assert Path(str(payload[key])).is_file()


def test_cli_invalid_bundle_path_fails_at_parse_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_bundle_mapping import main

    missing = tmp_path / "missing_bundle.toml"
    assert main(["--bundle", str(missing), "--force", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "parse"


def test_cli_invalid_out_dir_argument_fails_at_args_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from build_kr_factor_bundle_mapping import main

    assert main(["--bundle", str(BUNDLE_FIXTURE), "--out-dir", "bad\x01dir", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"
    assert "control character" in payload["error"]
    assert "bad" not in payload["error"]


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
    assert "build_kr_factor_bundle_mapping.py" in paths_text


def test_existing_3g4_2_tests_remain_green() -> None:
    import test_kr_factor_ranked_mapping_workflow  # noqa: F401

    assert test_kr_factor_ranked_mapping_workflow.OPS_SCRIPT.is_file()


def test_no_broker_paperloop_kis_path_in_ops_file() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    for token in ("paperlooprunner", "paperbroker", "submit_order", "kis"):
        assert token not in source, f"ops script must not reference {token!r}"
