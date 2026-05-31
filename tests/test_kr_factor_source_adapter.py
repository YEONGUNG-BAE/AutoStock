"""Real Intake 3G4-4 — fixture-first KR factor source adapter tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_factors"
    / "raw_kr_factor_source_synthetic_success.json"
)
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
BUNDLE_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_factors"
    / "kr_factor_bundle.synthetic.toml"
)
SYNTHETIC_CORP_CODE_XML = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "dart" / "corp_code_synthetic_multi.xml"
)
KR_REAL_UNIVERSE = REPO_ROOT / "config" / "universe.kr-real.sample.toml"
KR_REAL_MAPPING = REPO_ROOT / "config" / "provider_mappings.kr-real.sample.toml"
OPS_SCRIPT = REPO_ROOT / "ops" / "map_kr_factor_fixture.py"
ADAPTER_SOURCE = REPO_ROOT / "src" / "data" / "kr_factor_source_adapter.py"

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
        "targetAllocation",
        "quantity",
        "order",
        "order_type",
        "price_target",
        "stop_loss",
        "take_profit",
        "corp_code",
        "corpCode",
        "yfinance_provider_symbol",
        "provider_symbol",
        "providerSymbol",
        "stockProviderSymbol",
        "source_key",
        "source_format",
        "external_service",
        "universe_hint",
        "displayName",
        "sectorCode",
        "lastUpdated",
    }
)
_FORBIDDEN_ITEM_FIELDS = frozenset(
    {
        "corp_code",
        "corpCode",
        "yfinance_provider_symbol",
        "provider_symbol",
        "providerSymbol",
        "stockProviderSymbol",
        "action",
        "side",
        "buy",
        "sell",
        "hold",
        "target_weight",
        "targetAllocation",
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

from build_kr_factor_bundle_mapping import run_build_kr_factor_bundle_mapping
from build_kr_factor_ranked_mapping import run_build_kr_factor_ranked_mapping
from data.kr_factor_signal_generator import (
    KrFactorSignalGeneratorError,
    generate_ranking_signals_from_factors,
    load_kr_factor_inputs_toml,
)
from data.kr_factor_source_adapter import (
    KrFactorSourceAdapterError,
    load_kr_factor_source_payload,
    map_kr_factor_source_payload_to_factor_inputs,
    render_kr_factor_inputs_toml,
    replay_kr_factor_source_payload,
    write_kr_factor_inputs_toml,
)
from data.kr_provider_mapping_generator import parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_source(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "source.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _load_fixture_payload() -> dict[str, object]:
    return json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))


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
        "--source",
        str(SOURCE_FIXTURE),
        "--factor-inputs-out",
        str(tmp_path / "factor_inputs.generated.toml"),
        "--output-name",
        "kr-factor-inputs-from-source-v1",
        "--output-description",
        "Synthetic source-mapped KR factor inputs.",
        "--factor-score-version",
        "kr-factor-fixture-v1",
    ]


def _replay_to_factor_inputs(tmp_path: Path) -> Path:
    out_path = tmp_path / "factor_inputs.generated.toml"
    replay_kr_factor_source_payload(
        source_path=SOURCE_FIXTURE,
        factor_inputs_out=out_path,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
        force=True,
    )
    return out_path


def _walk_forbidden_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key not in _FORBIDDEN_OUTPUT_FIELDS
            _walk_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _walk_forbidden_fields(nested)


def test_synthetic_source_fixture_parses() -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    assert payload.source_key == "kr_factor_source"
    assert payload.source_format == "synthetic_factor_v1"
    assert payload.snapshot_version == 1
    assert payload.market == "KR"
    assert len(payload.items) == 5


def test_root_source_key_must_be_kr_factor_source(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["source_key"] = "other"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"


def test_root_source_format_must_be_synthetic_factor_v1(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["source_format"] = "other-v1"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"


def test_snapshot_version_must_be_one(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["snapshot_version"] = 2
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"


def test_root_market_must_be_kr(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["market"] = "US"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"


def test_root_as_of_must_be_timezone_aware(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["as_of"] = "2026-05-30T00:00:00"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "as_of" in exc_info.value.message


def test_unknown_root_fields_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["extra_root"] = True
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"


def test_unknown_item_fields_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    items[0]["extra_field"] = True
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"


def test_item_corp_code_rejected_as_forbidden_before_generic_unknown(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    items[0]["corp_code"] = "90000010"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "forbidden fields" in exc_info.value.message
    assert "corp_code" in exc_info.value.message
    assert "unknown fields" not in exc_info.value.message


@pytest.mark.parametrize(
    "field_name",
    ["provider_symbol", "providerSymbol", "yfinance_provider_symbol", "stockProviderSymbol"],
)
def test_provider_symbol_fields_rejected_as_forbidden_before_generic_unknown(
    tmp_path: Path,
    field_name: str,
) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    items[0][field_name] = "900001.KS"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "forbidden fields" in exc_info.value.message
    assert field_name in exc_info.value.message


@pytest.mark.parametrize("field_name", sorted(_FORBIDDEN_ITEM_FIELDS - {"corp_code", "corpCode"} - {
    "provider_symbol", "providerSymbol", "yfinance_provider_symbol", "stockProviderSymbol"
}))
def test_trading_action_order_allocation_fields_rejected_as_forbidden(
    tmp_path: Path,
    field_name: str,
) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    items[0][field_name] = "forbidden"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "forbidden fields" in exc_info.value.message
    assert field_name in exc_info.value.message


def test_numeric_fields_must_be_in_unit_interval(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    items[0]["liquidityPercentile"] = 1.5
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "between 0.0 and 1.0" in exc_info.value.message


def test_bool_numeric_rejected(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    items[0]["profitabilityScore"] = True
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "must be a number" in exc_info.value.message


def test_control_characters_rejected_without_echoing_offending_value(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    raw["universe_hint"] = "bad\x01hint"
    path = _write_source(tmp_path, raw)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        load_kr_factor_source_payload(path)
    assert exc_info.value.stage == "parse"
    assert "control character" in exc_info.value.message
    assert "\x01" not in exc_info.value.message
    assert "bad" not in exc_info.value.message


def test_ticker_normalizes_using_existing_normalize_stock_code() -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    symbols = {entry.symbol for entry in factor_inputs.factors}
    assert "900002" in symbols
    assert "KR:900002" not in symbols


def test_ticker_normalization_failure_maps_to_stage_map(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    raw["items"] = [items[0]]
    items[0]["ticker"] = "ABC"
    path = _write_source(tmp_path, raw)
    payload = load_kr_factor_source_payload(path)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        map_kr_factor_source_payload_to_factor_inputs(
            payload,
            output_name="kr-factor-inputs-from-source-v1",
            output_description="Synthetic source-mapped KR factor inputs.",
            factor_score_version="kr-factor-fixture-v1",
        )
    assert exc_info.value.stage == "map"
    assert "ticker" in exc_info.value.message


def test_duplicate_normalized_ticker_rejected_at_stage_map(tmp_path: Path) -> None:
    raw = _load_fixture_payload()
    items = raw["items"]
    assert isinstance(items, list)
    duplicate = dict(items[0])
    duplicate["ticker"] = "KR:900001"
    raw["items"] = [items[0], duplicate]
    path = _write_source(tmp_path, raw)
    payload = load_kr_factor_source_payload(path)
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        map_kr_factor_source_payload_to_factor_inputs(
            payload,
            output_name="kr-factor-inputs-from-source-v1",
            output_description="Synthetic source-mapped KR factor inputs.",
            factor_score_version="kr-factor-fixture-v1",
        )
    assert exc_info.value.stage == "map"
    assert "duplicate" in exc_info.value.message


def test_source_numeric_fields_map_to_canonical_snake_case_factor_fields() -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    entry = next(entry for entry in factor_inputs.factors if entry.symbol == "900001")
    assert entry.liquidity_percentile == 0.95
    assert entry.market_cap_percentile == 0.90
    assert entry.profitability_score == 0.85
    assert entry.balance_sheet_score == 0.75
    assert entry.momentum_percentile == 0.70
    assert entry.volatility_risk == 0.10
    assert entry.notes == "Synthetic fixture only."


def test_root_as_of_preserved_in_canonical_factor_input_toml(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    document = load_kr_factor_inputs_toml(out_path)
    assert document.as_of.isoformat() == "2026-05-30T00:00:00+09:00"


def test_factor_score_version_argument_written_to_canonical_toml(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    document = load_kr_factor_inputs_toml(out_path)
    assert document.factor_score_version == "kr-factor-fixture-v1"


def test_output_sorted_deterministically_by_market_symbol() -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    rendered = render_kr_factor_inputs_toml(factor_inputs)
    symbols = [
        line.split(" = ")[1].strip('"')
        for line in rendered.splitlines()
        if line.startswith("symbol = ")
    ]
    assert symbols == sorted(symbols)


def test_source_only_allowed_fields_accepted_in_source_items() -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    assert len(payload.items) == 5


def test_source_only_fields_absent_from_canonical_toml(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    text = out_path.read_text(encoding="utf-8")
    forbidden_text = {
        "displayName",
        "sectorCode",
        "lastUpdated",
        "source_key",
        "source_format",
        "external_service",
        "universe_hint",
        "liquidityPercentile",
        "marketCapPercentile",
    }
    for token in forbidden_text:
        assert token not in text


def test_output_toml_loads_through_existing_3g4_1_parser(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    document = load_kr_factor_inputs_toml(out_path)
    assert document.name == "kr-factor-inputs-from-source-v1"
    assert len(document.factors) == 5


def test_self_validation_failure_maps_to_adapter_validate_stage_not_lower_level_parse(
    tmp_path: Path,
) -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    out_path = tmp_path / "factor_inputs.generated.toml"
    with patch(
        "data.kr_factor_source_adapter.load_kr_factor_inputs_toml",
        side_effect=KrFactorSignalGeneratorError("parse", "forced self-validation failure"),
    ):
        with pytest.raises(KrFactorSourceAdapterError) as exc_info:
            write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert exc_info.value.stage == "validate"
    assert exc_info.value.message == "forced self-validation failure"
    assert not out_path.exists()


def test_self_validation_failure_does_not_create_final_output(tmp_path: Path) -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    out_path = tmp_path / "nested" / "factor_inputs.generated.toml"
    with patch(
        "data.kr_factor_source_adapter.load_kr_factor_inputs_toml",
        side_effect=KrFactorSignalGeneratorError("generate", "broken output"),
    ):
        with pytest.raises(KrFactorSourceAdapterError) as exc_info:
            write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert exc_info.value.stage == "validate"
    assert not out_path.exists()


def test_self_validation_failure_cleans_up_temp_file(tmp_path: Path) -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    out_path = tmp_path / "factor_inputs.generated.toml"
    with patch(
        "data.kr_factor_source_adapter.load_kr_factor_inputs_toml",
        side_effect=KrFactorSignalGeneratorError("parse", "broken output"),
    ):
        with pytest.raises(KrFactorSourceAdapterError):
            write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert list(tmp_path.glob(".tmp_factor_inputs_*.toml")) == []


def test_force_true_replaces_final_only_after_temp_validation_succeeds(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    original_text = out_path.read_text(encoding="utf-8")
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-replaced-v1",
        output_description="Replacement factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert out_path.read_text(encoding="utf-8") != original_text
    assert "kr-factor-inputs-replaced-v1" in out_path.read_text(encoding="utf-8")


def test_force_true_validation_failure_preserves_existing_final(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    original_text = out_path.read_text(encoding="utf-8")
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    with patch(
        "data.kr_factor_source_adapter.load_kr_factor_inputs_toml",
        side_effect=KrFactorSignalGeneratorError("validate", "replacement blocked"),
    ):
        with pytest.raises(KrFactorSourceAdapterError) as exc_info:
            write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert exc_info.value.stage == "validate"
    assert out_path.read_text(encoding="utf-8") == original_text


def test_temp_file_created_under_out_path_parent_not_global_tmp(tmp_path: Path) -> None:
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    out_path = tmp_path / "nested" / "factor_inputs.generated.toml"
    observed_temp_parent: list[Path] = []

    original_write_text = Path.write_text

    def _capture_temp_write(self: Path, *args: object, **kwargs: object) -> object:
        if self.name.startswith(".tmp_factor_inputs_"):
            observed_temp_parent.append(self.parent)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _capture_temp_write):
        write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert observed_temp_parent == [out_path.parent]
    assert out_path.is_file()


def test_write_kr_factor_inputs_toml_writes_valid_toml_normally(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    document = load_kr_factor_inputs_toml(out_path)
    assert document.name == "kr-factor-inputs-from-source-v1"
    assert len(document.factors) == 5


def test_generated_canonical_factor_input_feeds_3g4_1_signal_generator(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    factor_inputs = load_kr_factor_inputs_toml(out_path)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="kr-factor-signals-synthetic-v1",
        output_description="Synthetic fixture-first KR factor signals.",
    )
    assert len(signal_set.signals) == 5
    assert signal_set.score_version == "kr-factor-fixture-v1"


def test_generated_canonical_factor_input_feeds_3g4_2_factor_ranked_mapping_workflow(
    tmp_path: Path,
) -> None:
    factor_inputs_out = _replay_to_factor_inputs(tmp_path)
    payload = run_build_kr_factor_ranked_mapping(
        candidate_pool_path=POOL_FIXTURE,
        factor_inputs_path=factor_inputs_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        factor_signals_out=tmp_path / "factor_signals.generated.toml",
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=tmp_path / "selected.toml",
        universe_out=tmp_path / "universe.generated.toml",
        provider_mapping_out=tmp_path / "provider_mappings.generated.toml",
        factor_output_name="kr-factor-signals-synthetic-v1",
        factor_output_description="Synthetic fixture-first KR factor signals.",
        selection_name="kr-factor-ranked-selected-v1",
        selection_description="Factor-ranked KR candidates.",
        universe_name="kr-factor-ranked-universe-v1",
        provider_mapping_name="kr-factor-ranked-provider-mappings-v1",
        top_n=3,
        force=True,
    )
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["signals_count"] == 5
    assert payload["ranked_count"] == 3


def test_generated_canonical_factor_input_usable_in_3g4_3_bundle_workflow(tmp_path: Path) -> None:
    factor_inputs_out = _replay_to_factor_inputs(tmp_path)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    factor_inputs_copy = bundle_dir / "factor_inputs.generated.toml"
    factor_inputs_copy.write_text(factor_inputs_out.read_text(encoding="utf-8"), encoding="utf-8")
    bundle_path = bundle_dir / "bundle.toml"
    bundle_path.write_text(
        f"""
version = 1
name = "kr-factor-bundle-from-source-v1"
description = "Bundle using source-mapped factor inputs."
base_market = "KR"

[inputs]
candidate_pool = "{POOL_FIXTURE.as_posix()}"
factor_inputs = "{factor_inputs_copy.name}"
corp_code_xml = "{SYNTHETIC_CORP_CODE_XML.as_posix()}"

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
""".strip()
        + "\n",
        encoding="utf-8",
    )
    payload = run_build_kr_factor_bundle_mapping(
        bundle_path=bundle_path,
        out_dir=tmp_path / "bundle_outputs",
        force=True,
    )
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"


def test_provider_mapping_validation_succeeds_downstream(tmp_path: Path) -> None:
    factor_inputs_out = _replay_to_factor_inputs(tmp_path)
    payload = run_build_kr_factor_ranked_mapping(
        candidate_pool_path=POOL_FIXTURE,
        factor_inputs_path=factor_inputs_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        factor_signals_out=tmp_path / "factor_signals.generated.toml",
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=tmp_path / "selected.toml",
        universe_out=tmp_path / "universe.generated.toml",
        provider_mapping_out=tmp_path / "provider_mappings.generated.toml",
        factor_output_name="kr-factor-signals-synthetic-v1",
        factor_output_description="Synthetic fixture-first KR factor signals.",
        selection_name="kr-factor-ranked-selected-v1",
        selection_description="Factor-ranked KR candidates.",
        universe_name="kr-factor-ranked-universe-v1",
        provider_mapping_name="kr-factor-ranked-provider-mappings-v1",
        top_n=3,
        force=True,
    )
    universe = load_universe_toml(Path(str(payload["universe_out"])))
    registry = load_provider_mapping_toml(Path(str(payload["provider_mapping_out"])))
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )
    for symbol in ("900001", "900002", "900003"):
        mapping = registry.resolve(symbol=symbol, market="KR")
        assert mapping.yfinance is not None
        assert mapping.yfinance.provider_symbol == _SYNTHETIC_YFINANCE[symbol]
        assert mapping.dart is not None
        assert mapping.dart.corp_code == _SYNTHETIC_CORP_CODES[symbol]


def test_output_contains_no_trading_action_order_allocation_fields(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    raw = tomllib.loads(out_path.read_text(encoding="utf-8"))
    _walk_forbidden_fields(raw)


def test_output_contains_no_corp_code(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    text = out_path.read_text(encoding="utf-8").lower()
    assert "corp_code" not in text


def test_output_contains_no_provider_mapping_universe_fields(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    text = out_path.read_text(encoding="utf-8")
    forbidden = {
        "yfinance_provider_symbol",
        "provider_symbol",
        "providerSymbol",
        "universe_hint",
        "external_service",
    }
    for token in forbidden:
        assert token not in text


def test_write_refuses_overwrite_without_force(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    with pytest.raises(KrFactorSourceAdapterError) as exc_info:
        write_kr_factor_inputs_toml(factor_inputs, out_path, force=False)
    assert exc_info.value.stage == "write"


def test_force_allows_overwrite(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    payload = load_kr_factor_source_payload(SOURCE_FIXTURE)
    factor_inputs = map_kr_factor_source_payload_to_factor_inputs(
        payload,
        output_name="kr-factor-inputs-from-source-v1",
        output_description="Synthetic source-mapped KR factor inputs.",
        factor_score_version="kr-factor-fixture-v1",
    )
    write_kr_factor_inputs_toml(factor_inputs, out_path, force=True)
    assert out_path.is_file()


def test_cli_success_writes_toml_and_json_summary(tmp_path: Path) -> None:
    result = _run_cli(*(_base_cli_args(tmp_path) + ["--force", "--json"]))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "fixture-factor-source-adapter"
    assert payload["factors_count"] == 5
    assert Path(str(payload["factor_inputs_out"])).is_file()


def test_cli_invalid_args_fail_at_stage_args(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from map_kr_factor_fixture import main

    argv = _base_cli_args(tmp_path) + [
        "--output-name",
        "bad\nname",
        "--force",
        "--json",
    ]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_cli_bad_source_path_fails_at_stage_parse(tmp_path: Path) -> None:
    result = _run_cli(
        "--source",
        str(tmp_path / "missing.json"),
        "--factor-inputs-out",
        str(tmp_path / "out.toml"),
        "--output-name",
        "kr-factor-inputs-from-source-v1",
        "--factor-score-version",
        "kr-factor-fixture-v1",
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "parse"


def test_cli_output_exists_without_force_fails_at_stage_write(tmp_path: Path) -> None:
    result = _run_cli(*(_base_cli_args(tmp_path) + ["--force", "--json"]))
    assert result.returncode == 0, result.stderr
    retry = _run_cli(*(_base_cli_args(tmp_path) + ["--json"]))
    assert retry.returncode == 1
    payload = json.loads(retry.stdout)
    assert payload["stage"] == "write"


def test_no_env_or_api_key_read_in_new_files() -> None:
    forbidden_tokens = (
        "os.environ",
        "getenv",
        "FRED_API_KEY",
        "DART_API_KEY",
        "OPEN_DART",
    )
    for path in (ADAPTER_SOURCE, OPS_SCRIPT):
        source = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in source, f"{path.name} must not reference {token!r}"


def test_no_network_or_live_api_in_new_files() -> None:
    forbidden = (
        "urllib.request",
        "urllib.parse",
        "urllib.error",
        "requests",
        "httpx",
        "aiohttp",
        "import yfinance",
        "from yfinance",
        "paperbroker",
        "paperlooprunner",
        "submit_order",
    )
    for path in (ADAPTER_SOURCE, OPS_SCRIPT):
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


def test_static_scan_includes_new_files() -> None:
    paths_text = (REPO_ROOT / "tests" / "test_fetch_research_sources.py").read_text(encoding="utf-8")
    assert "kr_factor_source_adapter.py" in paths_text
    assert "map_kr_factor_fixture.py" in paths_text


def test_existing_3g4_1_tests_remain_importable() -> None:
    import test_kr_factor_signal_generator  # noqa: F401


def test_existing_3g4_2_tests_remain_importable() -> None:
    import test_kr_factor_ranked_mapping_workflow  # noqa: F401


def test_existing_3g4_3_tests_remain_importable() -> None:
    import test_kr_factor_bundle_workflow  # noqa: F401


def test_no_broker_paperloop_kis_path_in_new_files() -> None:
    forbidden = ("kis", "paperbroker", "paperlooprunner", "submit_order")
    for path in (ADAPTER_SOURCE, OPS_SCRIPT):
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


def test_fixture_default_selected_symbols_present_in_generated_factor_inputs(tmp_path: Path) -> None:
    out_path = _replay_to_factor_inputs(tmp_path)
    document = load_kr_factor_inputs_toml(out_path)
    symbols = {entry.symbol for entry in document.factors}
    assert {"900001", "900002", "900003"}.issubset(symbols)


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
