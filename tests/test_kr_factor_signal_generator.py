"""Real Intake 3G4-1 — fixture-first KR factor signal generator tests."""

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
OPS_SCRIPT = REPO_ROOT / "ops" / "generate_kr_factor_signals.py"
GENERATOR_SOURCE = REPO_ROOT / "src" / "data" / "kr_factor_signal_generator.py"
CLI_SOURCE = REPO_ROOT / "ops" / "generate_kr_factor_signals.py"

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

from data.kr_candidate_ranker import parse_ranking_signals_toml, rank_kr_candidates
from data.kr_candidate_pool import parse_kr_candidate_pool_toml, select_candidates
from data.kr_factor_signal_generator import (
    KrFactorSignalGeneratorError,
    generate_kr_factor_signals_file,
    generate_ranking_signals_from_factors,
    load_kr_factor_inputs_toml,
    render_kr_ranking_signals_toml,
    write_kr_ranking_signals_toml,
)
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files, parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_factor_inputs(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "factors.toml"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


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


def _generate_signals(tmp_path: Path) -> Path:
    out_path = tmp_path / "signals.generated.toml"
    generate_kr_factor_signals_file(
        factor_inputs_path=FACTOR_FIXTURE,
        out_signals=out_path,
        output_name="kr-factor-signals-synthetic-v1",
        output_description="Synthetic fixture-first KR factor signals.",
        force=True,
    )
    return out_path


def test_synthetic_factor_input_fixture_parses() -> None:
    document = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    assert document.name == "kr-factor-inputs-synthetic-v1"
    assert document.factor_score_version == "kr-factor-fixture-v1"
    assert document.as_of.isoformat() == "2026-05-30T00:00:00+09:00"
    assert len(document.factors) == 5


def test_root_version_must_be_one(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
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
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"


def test_root_as_of_must_be_timezone_aware(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00"
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
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "timezone-aware" in exc_info.value.message


def test_root_factor_score_version_required(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"

[[factors]]
symbol = "900001"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "factor_score_version" in exc_info.value.message


def test_unknown_root_fields_rejected(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"
extra = true

[[factors]]
symbol = "900001"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "unknown" in exc_info.value.message


def test_unknown_factor_fields_rejected(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
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
extra = true
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "unknown fields" in exc_info.value.message


def test_factor_corp_code_rejected(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "KR"
corp_code = "00123456"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "corp_code" in exc_info.value.message


@pytest.mark.parametrize("field_name", sorted(_FORBIDDEN_OUTPUT_FIELDS))
def test_trading_action_allocation_fields_rejected(tmp_path: Path, field_name: str) -> None:
    path = _write_factor_inputs(
        tmp_path,
        f"""
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "KR"
{field_name} = "forbidden"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"


def test_non_kr_market_rejected(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "US"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "KR" in exc_info.value.message


def test_invalid_stock_code_rejected(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bad"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "NOT-A-CODE"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"


def test_symbol_normalizes_to_six_digit_stock_code(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "pad"
description = "pad"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "5930"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    document = load_kr_factor_inputs_toml(path)
    assert document.factors[0].symbol == "005930"


def test_market_prefixed_symbol_normalizes_to_six_digit(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "prefixed"
description = "prefixed"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "KR:900001"
market = "KR"
liquidity_percentile = 0.5
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    document = load_kr_factor_inputs_toml(path)
    assert document.factors[0].symbol == "900001"


def test_duplicate_normalized_market_symbol_rejected_at_generate(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
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
""",
    )
    factor_inputs = load_kr_factor_inputs_toml(path)
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        generate_ranking_signals_from_factors(
            factor_inputs,
            output_name="dup",
            output_description="dup",
        )
    assert exc_info.value.stage == "generate"
    assert "duplicate" in exc_info.value.message


@pytest.mark.parametrize("field_name,value", [("liquidity_percentile", 1.5)])
def test_numeric_factor_fields_must_be_in_unit_interval(
    tmp_path: Path,
    field_name: str,
    value: float,
) -> None:
    path = _write_factor_inputs(
        tmp_path,
        f"""
version = 1
name = "range"
description = "range"
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
""".replace(f"{field_name} = 0.5", f"{field_name} = {value}"),
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "between 0.0 and 1.0" in exc_info.value.message


def test_negative_volatility_risk_rejected(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "range"
description = "range"
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
volatility_risk = -0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "between 0.0 and 1.0" in exc_info.value.message


def test_bool_not_accepted_as_numeric_factor_value(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "bool"
description = "bool"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "KR"
liquidity_percentile = true
market_cap_percentile = 0.5
profitability_score = 0.5
balance_sheet_score = 0.5
momentum_percentile = 0.5
volatility_risk = 0.1
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "must be a number" in exc_info.value.message


def test_control_characters_rejected_without_echoing_offending_value(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "control"
description = "bad\\nline"
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
""",
    )
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        load_kr_factor_inputs_toml(path)
    assert exc_info.value.stage == "parse"
    assert "control character" in exc_info.value.message
    assert "bad" not in exc_info.value.message


def test_generated_liquidity_score_equals_rounded_percentile() -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="test",
        output_description="test",
    )
    factor = next(entry for entry in factor_inputs.factors if entry.symbol == "900001")
    signal = next(entry for entry in signal_set.signals if entry.symbol == "900001")
    assert signal.liquidity_score == round(factor.liquidity_percentile, 4)


def test_generated_market_cap_score_equals_rounded_percentile() -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="test",
        output_description="test",
    )
    factor = next(entry for entry in factor_inputs.factors if entry.symbol == "900001")
    signal = next(entry for entry in signal_set.signals if entry.symbol == "900001")
    assert signal.market_cap_score == round(factor.market_cap_percentile, 4)


def test_generated_quality_score_equals_rounded_average() -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="test",
        output_description="test",
    )
    factor = next(entry for entry in factor_inputs.factors if entry.symbol == "900001")
    signal = next(entry for entry in signal_set.signals if entry.symbol == "900001")
    expected = round((factor.profitability_score + factor.balance_sheet_score) / 2, 4)
    assert signal.quality_score == expected


def test_generated_momentum_score_equals_rounded_percentile() -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="test",
        output_description="test",
    )
    factor = next(entry for entry in factor_inputs.factors if entry.symbol == "900003")
    signal = next(entry for entry in signal_set.signals if entry.symbol == "900003")
    assert signal.momentum_score == round(factor.momentum_percentile, 4)


def test_generated_risk_penalty_equals_rounded_volatility_risk() -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="test",
        output_description="test",
    )
    factor = next(entry for entry in factor_inputs.factors if entry.symbol == "900003")
    signal = next(entry for entry in signal_set.signals if entry.symbol == "900003")
    assert signal.risk_penalty == round(factor.volatility_risk, 4)


def test_output_precision_fixed_to_four_decimals(tmp_path: Path) -> None:
    path = _write_factor_inputs(
        tmp_path,
        """
version = 1
name = "precision"
description = "precision"
as_of = "2026-05-30T00:00:00+09:00"
factor_score_version = "kr-factor-fixture-v1"

[[factors]]
symbol = "900001"
market = "KR"
liquidity_percentile = 0.123456
market_cap_percentile = 0.234567
profitability_score = 0.345678
balance_sheet_score = 0.456789
momentum_percentile = 0.567891
volatility_risk = 0.678912
""",
    )
    factor_inputs = load_kr_factor_inputs_toml(path)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="precision",
        output_description="precision",
    )
    rendered = render_kr_ranking_signals_toml(signal_set)
    assert "liquidity_score = 0.1235" in rendered
    assert "quality_score = 0.4012" in rendered


def test_output_ordering_deterministic_by_market_symbol() -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="order",
        output_description="order",
    )
    keys = [(entry.market, entry.symbol) for entry in signal_set.signals]
    assert keys == sorted(keys)
    assert [entry.symbol for entry in signal_set.signals] == [
        "900001",
        "900002",
        "900003",
        "900004",
        "900005",
    ]


def test_generated_ranking_signal_toml_loads_via_ranker_parser(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    document = parse_ranking_signals_toml(signals_path)
    assert document.name == "kr-factor-signals-synthetic-v1"
    assert document.score_version == "kr-factor-fixture-v1"
    assert len(document.signals) == 5


def test_generated_ranking_signals_integrate_with_rank_kr_candidates(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    payload = rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=signals_path,
        ranked_out=tmp_path / "ranked.json",
        force=True,
    )
    assert payload["ranked_count"] == 3
    assert payload["score_version"] == "kr-factor-fixture-v1"


def test_integration_with_candidate_pool_default_selection(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    signals = parse_ranking_signals_toml(signals_path)
    selected = select_candidates(pool)
    signal_keys = {(entry.market, entry.symbol) for entry in signals.signals}
    assert {candidate.symbol for candidate in selected} == {"900001", "900002", "900003"}
    for candidate in selected:
        assert (candidate.market, candidate.symbol) in signal_keys


def test_generated_selected_candidates_flow_through_3f1_generator(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    selected_out = tmp_path / "selected.toml"
    rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=signals_path,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-factor-ranked-selected-v1",
        selection_description="Factor-generated ranked KR candidates.",
        top_n=3,
        force=True,
    )
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    payload = generate_kr_provider_mapping_files(
        candidates_path=selected_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-factor-generated-v1",
        provider_mapping_name="kr-factor-provider-mappings-v1",
        force=True,
    )
    assert payload["candidates_read"] == 3


def test_provider_mapping_validation_succeeds_with_required_providers(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    selected_out = tmp_path / "selected.toml"
    rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=signals_path,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-factor-ranked-selected-v1",
        selection_description="Factor-generated ranked KR candidates.",
        top_n=3,
        force=True,
    )
    universe_out = tmp_path / "universe.generated.toml"
    mapping_out = tmp_path / "provider_mappings.generated.toml"
    generate_kr_provider_mapping_files(
        candidates_path=selected_out,
        corp_code_xml=SYNTHETIC_CORP_CODE_XML,
        corp_code_zip=None,
        universe_out=universe_out,
        provider_mapping_out=mapping_out,
        universe_name="kr-factor-generated-v1",
        provider_mapping_name="kr-factor-provider-mappings-v1",
        force=True,
    )
    universe = load_universe_toml(universe_out)
    registry = load_provider_mapping_toml(mapping_out)
    validate_provider_mappings_cover_universe(
        registry,
        universe,
        require_yfinance=True,
        require_dart=True,
    )


def test_output_contains_no_trading_action_order_allocation_fields(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    raw = tomllib.loads(signals_path.read_text(encoding="utf-8"))

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in _FORBIDDEN_OUTPUT_FIELDS
                _walk(nested)
        elif isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(raw)


def test_output_contains_no_corp_code(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    raw = tomllib.loads(signals_path.read_text(encoding="utf-8"))
    assert "corp_code" not in raw
    for entry in raw["signals"]:
        assert "corp_code" not in entry


def test_output_contains_no_provider_mapping_or_universe_fields(tmp_path: Path) -> None:
    signals_path = _generate_signals(tmp_path)
    raw = tomllib.loads(signals_path.read_text(encoding="utf-8"))
    forbidden = {
        "yfinance_provider_symbol",
        "provider_mapping",
        "universe",
        "mappings",
        "candidates",
    }
    assert forbidden.isdisjoint(raw.keys())
    for entry in raw["signals"]:
        assert "yfinance_provider_symbol" not in entry


def test_write_refuses_overwrite_without_force(tmp_path: Path) -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="kr-factor-signals-synthetic-v1",
        output_description="Synthetic fixture-first KR factor signals.",
    )
    out_path = tmp_path / "signals.toml"
    write_kr_ranking_signals_toml(signal_set, out_path, force=True)
    with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
        write_kr_ranking_signals_toml(signal_set, out_path, force=False)
    assert exc_info.value.stage == "write"


def test_cli_success_writes_toml_and_json_summary(tmp_path: Path) -> None:
    out_path = tmp_path / "signals.generated.toml"
    result = _run_cli(
        "--factor-inputs",
        str(FACTOR_FIXTURE),
        "--out-signals",
        str(out_path),
        "--output-name",
        "kr-factor-signals-synthetic-v1",
        "--output-description",
        "Synthetic fixture-first KR factor signals.",
        "--force",
        "--json",
    )
    assert result.returncode == 0, result.stderr
    assert out_path.is_file()
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "fixture-factor-signal-generator"
    assert payload["signals_count"] == 5


def test_cli_invalid_args_fail_at_args_stage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from generate_kr_factor_signals import main

    argv = [
        "--factor-inputs",
        str(FACTOR_FIXTURE),
        "--out-signals",
        str(tmp_path / "signals.toml"),
        "--output-name",
        "bad\nname",
        "--force",
        "--json",
    ]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "args"


def test_cli_invalid_input_fails_at_parse_stage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from generate_kr_factor_signals import main

    bad_path = _write_factor_inputs(
        tmp_path,
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
""",
    )
    argv = [
        "--factor-inputs",
        str(bad_path),
        "--out-signals",
        str(tmp_path / "signals.toml"),
        "--output-name",
        "bad",
        "--force",
        "--json",
    ]
    assert main(argv) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "parse"


def test_cli_output_exists_without_force_fails_at_write_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from generate_kr_factor_signals import main

    out_path = tmp_path / "signals.toml"
    argv = [
        "--factor-inputs",
        str(FACTOR_FIXTURE),
        "--out-signals",
        str(out_path),
        "--output-name",
        "kr-factor-signals-synthetic-v1",
        "--force",
        "--json",
    ]
    assert main(argv) == 0
    capsys.readouterr()
    argv_no_force = [arg for arg in argv if arg != "--force"]
    assert main(argv_no_force) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_self_validation_failure_maps_to_validate_stage(tmp_path: Path) -> None:
    factor_inputs = load_kr_factor_inputs_toml(FACTOR_FIXTURE)
    signal_set = generate_ranking_signals_from_factors(
        factor_inputs,
        output_name="kr-factor-signals-synthetic-v1",
        output_description="Synthetic fixture-first KR factor signals.",
    )
    out_path = tmp_path / "signals.toml"
    with patch(
        "data.kr_factor_signal_generator.parse_ranking_signals_toml",
        side_effect=__import__(
            "data.kr_candidate_ranker",
            fromlist=["KrCandidateRankerError"],
        ).KrCandidateRankerError("validate", "forced validation failure"),
    ):
        with pytest.raises(KrFactorSignalGeneratorError) as exc_info:
            write_kr_ranking_signals_toml(signal_set, out_path, force=True)
    assert exc_info.value.stage == "validate"


def test_no_env_or_api_key_read_in_new_files() -> None:
    forbidden_tokens = (
        "os.environ",
        "getenv",
        "FRED_API_KEY",
        "DART_API_KEY",
        "OPEN_DART",
    )
    for path in (GENERATOR_SOURCE, CLI_SOURCE):
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
    )
    for path in (GENERATOR_SOURCE, CLI_SOURCE):
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


def test_static_scan_includes_new_files() -> None:
    import test_fetch_research_sources  # noqa: F401

    paths_text = test_fetch_research_sources.__file__
    assert paths_text
    source = Path(paths_text).read_text(encoding="utf-8")
    assert "kr_factor_signal_generator.py" in source
    assert "generate_kr_factor_signals.py" in source


def test_no_broker_paperloop_kis_path_in_new_files() -> None:
    forbidden = (
        "paperlooprunner",
        "paperbroker",
        "submit_order",
        "kis",
    )
    for path in (GENERATOR_SOURCE, CLI_SOURCE):
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


def test_existing_3g3_1_ranker_tests_remain_importable() -> None:
    import test_kr_candidate_ranker  # noqa: F401

    assert test_kr_candidate_ranker.SIGNALS_FIXTURE.is_file()
