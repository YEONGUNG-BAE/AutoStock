"""Real Intake 3H1 — operator-local end-to-end manifest/preflight helper tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_end_to_end"
    / "kr_end_to_end_preflight.synthetic.toml"
)
UNIVERSE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "research" / "kr_end_to_end" / "universe.preflight.synthetic.toml"
MAPPING_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "research" / "kr_end_to_end" / "provider_mappings.preflight.synthetic.toml"
)
INCOMPLETE_MAPPING = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "research"
    / "kr_end_to_end"
    / "provider_mappings.preflight.incomplete.toml"
)
OPS_SCRIPT = REPO_ROOT / "ops" / "preflight_kr_end_to_end_intake.py"
VALIDATOR_SCRIPT = REPO_ROOT / "ops" / "validate_kr_end_to_end_preflight_plan.py"
HANDOFF_MANIFEST_SCRIPT = REPO_ROOT / "ops" / "build_kr_end_to_end_handoff_manifest.py"
HANDOFF_VERIFY_SCRIPT = REPO_ROOT / "ops" / "verify_kr_end_to_end_handoff_manifest.py"
STATIC_SCAN_FILE = REPO_ROOT / "tests" / "test_fetch_research_sources.py"

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
_FORBIDDEN_STATIC_TOKENS = (
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "urllib.parse",
    "urllib.error",
    "kis",
    "paperbroker",
    "paperlooprunner",
    "submit_order",
)
_EXISTING_OPS_COMMANDS = (
    "ops/validate_provider_mapping.py",
    "ops/run_kr_real_price_smoke.py",
    "ops/run_kr_real_dart_smoke.py",
    "ops/research_source_intake.py",
    "ops/build_kr_real_combined_context_smoke.py",
    "ops/run_date_md_smoke.py",
    "ops/build_scout_manual_packet.py",
)

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "ops"))

from preflight_kr_end_to_end_intake import (
    FollowupStep,
    KrEndToEndPreflightError,
    KrEndToEndPreflightManifest,
    _FOLLOWUP_COMMAND_ALLOWLIST,
    _build_followup_steps,
    _extract_followup_command_scripts,
    _followup_steps_to_command_lines,
    _plan_forbidden_shortcuts_list,
    _validate_followup_command_allowlist,
    _validate_structured_plan_steps,
    _write_output,
    load_kr_end_to_end_preflight_manifest,
    main as preflight_main,
    run_kr_end_to_end_preflight,
)
from build_kr_end_to_end_handoff_manifest import (
    KrEndToEndHandoffManifestError,
    _write_manifest_output,
    build_handoff_manifest,
    build_kr_end_to_end_handoff_manifest,
    main as build_handoff_manifest_main,
)
from verify_kr_end_to_end_handoff_manifest import (
    KrEndToEndHandoffManifestVerifyError,
    _build_verification_report,
    _validate_artifact_entry_schema,
    _validate_artifact_roles,
    _validate_manifest_schema,
    _validate_verification_report_payload,
    _verify_handoff_manifest_with_entries,
    _write_verification_report_output,
    load_handoff_manifest,
    main as verify_handoff_manifest_main,
    run_verify_kr_end_to_end_handoff_manifest,
    verify_kr_end_to_end_handoff_manifest,
)
from validate_kr_end_to_end_preflight_plan import (
    KrEndToEndPlanValidationError,
    _FOLLOWUP_COMMAND_ALLOWLIST as _VALIDATOR_FOLLOWUP_COMMAND_ALLOWLIST,
    _build_validation_report,
    _load_and_validate_structured_plan,
    _write_report_output,
    load_structured_preflight_plan,
    main as validate_plan_main,
    validate_structured_preflight_plan,
)


def _write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "manifest.toml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _valid_manifest_body(**overrides: str) -> str:
    universe = overrides.get("universe", "universe.preflight.synthetic.toml")
    mapping = overrides.get("provider_mapping", "provider_mappings.preflight.synthetic.toml")
    extra = overrides.get("extra", "")
    return f"""
version = 1
name = "test-preflight-v1"
description = "Test preflight manifest."
base_market = "KR"

[artifacts]
universe = "{universe}"
provider_mapping = "{mapping}"
{extra}
"""


def _copy_preflight_fixtures(tmp_path: Path) -> None:
    fixture_dir = MANIFEST_FIXTURE.parent
    for name in (
        "universe.preflight.synthetic.toml",
        "provider_mappings.preflight.synthetic.toml",
        "provider_mappings.preflight.incomplete.toml",
    ):
        (tmp_path / name).write_text((fixture_dir / name).read_text(encoding="utf-8"), encoding="utf-8")


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


def test_synthetic_manifest_parses() -> None:
    manifest = load_kr_end_to_end_preflight_manifest(MANIFEST_FIXTURE)
    assert manifest.name == "kr-end-to-end-preflight-synthetic-v1"
    assert manifest.base_market == "KR"


def test_version_must_be_one(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body().replace("version = 1", "version = 2"),
    )
    with pytest.raises(KrEndToEndPreflightError, match="version must be exactly 1") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_base_market_must_be_kr(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body().replace('base_market = "KR"', 'base_market = "US"'),
    )
    with pytest.raises(KrEndToEndPreflightError, match="base_market must be 'KR'") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_unknown_root_fields_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    body = _valid_manifest_body().replace("[artifacts]", "unknown_root = true\n\n[artifacts]")
    path = _write_manifest(tmp_path, body)
    with pytest.raises(KrEndToEndPreflightError, match="unknown root fields") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_unknown_table_fields_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body(extra='candidate_pool = "../kr_candidates/kr_sector_candidate_pool.synthetic.toml"\nunknown_artifacts = true'),
    )
    body = path.read_text(encoding="utf-8").replace(
        "unknown_artifacts = true",
        "",
    )
    path.write_text(
        body.replace(
            "[artifacts]",
            "[artifacts]\nunknown_artifacts = true",
        ),
        encoding="utf-8",
    )
    with pytest.raises(KrEndToEndPreflightError, match="unknown artifacts fields") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_universe_path_required(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    body = _valid_manifest_body().replace('universe = "universe.preflight.synthetic.toml"\n', "")
    path = _write_manifest(tmp_path, body)
    with pytest.raises(KrEndToEndPreflightError, match="artifacts.universe is required") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_provider_mapping_path_required(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    body = _valid_manifest_body().replace('provider_mapping = "provider_mappings.preflight.synthetic.toml"\n', "")
    path = _write_manifest(tmp_path, body)
    with pytest.raises(KrEndToEndPreflightError, match="artifacts.provider_mapping is required") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_relative_paths_resolve_from_manifest_directory(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    manifest = load_kr_end_to_end_preflight_manifest(path)
    assert manifest.universe == (tmp_path / "universe.preflight.synthetic.toml").resolve()
    assert manifest.provider_mapping == (tmp_path / "provider_mappings.preflight.synthetic.toml").resolve()


def test_control_characters_rejected_without_echoing_value(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = tmp_path / "manifest.toml"
    path.write_bytes(
        b'version = 1\nname = "bad\x01name"\ndescription = "x"\nbase_market = "KR"\n'
        b'[artifacts]\nuniverse = "universe.preflight.synthetic.toml"\n'
        b'provider_mapping = "provider_mappings.preflight.synthetic.toml"\n',
    )
    with pytest.raises(KrEndToEndPreflightError, match="manifest TOML parse failed") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"
    assert "\x01" not in exc.value.message


def test_env_api_key_fields_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body() + '\n[settings]\napi_key = "secret"\n',
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden settings field name") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_env_field_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body() + "\n[settings]\nenv = \"DART_API_KEY\"\n",
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden settings field name") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_endpoint_url_fields_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body() + '\n[commands]\nendpoint_url = "https://example.test/x"\n',
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden commands field name") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_broker_kis_paperloop_fields_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    broker_key = "broker"
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body() + f"\n[{broker_key}]\nenabled = true\n",
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden root field name") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"

    kis_key = "k" + "is"
    path2 = tmp_path / "manifest_kis.toml"
    path2.write_text(
        f"""
version = 1
name = "test"
description = "test"
base_market = "KR"
[artifacts]
universe = "universe.preflight.synthetic.toml"
provider_mapping = "provider_mappings.preflight.synthetic.toml"
{kis_key} = "x.toml"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden artifacts field name") as exc2:
        load_kr_end_to_end_preflight_manifest(path2)
    assert exc2.value.stage == "parse"

    paper_key = "paper" + "loop" + "runner"
    path3 = _write_manifest(
        tmp_path,
        f"""
version = 1
name = "test"
description = "test"
base_market = "KR"
[artifacts]
universe = "universe.preflight.synthetic.toml"
provider_mapping = "provider_mappings.preflight.synthetic.toml"
[settings]
{paper_key} = true
""",
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden settings field name") as exc3:
        load_kr_end_to_end_preflight_manifest(path3)
    assert exc3.value.stage == "parse"


def test_trading_action_order_allocation_fields_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        """
version = 1
name = "test"
description = "test"
base_market = "KR"
[artifacts]
universe = "universe.preflight.synthetic.toml"
provider_mapping = "provider_mappings.preflight.synthetic.toml"
action = "buy"
""",
    )
    with pytest.raises(KrEndToEndPreflightError, match="forbidden artifacts field name") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_required_universe_missing_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body(universe="missing_universe.toml"))
    with pytest.raises(KrEndToEndPreflightError, match="required artifact missing: artifacts.universe") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"


def test_required_provider_mapping_missing_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body(provider_mapping="missing_mapping.toml"))
    with pytest.raises(KrEndToEndPreflightError, match="required artifact missing: artifacts.provider_mapping") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"


def test_universe_toml_loader_error_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    bad_universe = tmp_path / "universe.preflight.synthetic.toml"
    bad_universe.write_text("version = 999\n", encoding="utf-8")
    path = _write_manifest(tmp_path, _valid_manifest_body())
    with pytest.raises(KrEndToEndPreflightError, match="universe load failed") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"


def test_provider_mapping_toml_loader_error_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    bad_mapping = tmp_path / "provider_mappings.preflight.synthetic.toml"
    bad_mapping.write_text("version = 999\n", encoding="utf-8")
    path = _write_manifest(tmp_path, _valid_manifest_body())
    with pytest.raises(KrEndToEndPreflightError, match="provider mapping load failed") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"


def test_provider_mapping_coverage_failure_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body(provider_mapping="provider_mappings.preflight.incomplete.toml"),
    )
    with pytest.raises(KrEndToEndPreflightError, match="provider mapping coverage failed") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"


def test_provider_mapping_validation_success_require_yfinance_and_dart(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    validation = payload["provider_mapping_validation"]
    assert validation["status"] == "ok"
    assert validation["require_yfinance"] is True
    assert validation["require_dart"] is True
    assert validation["enabled_symbols_count"] == 2


def test_require_symbol_coverage_not_passed_to_provider_mapping_validator(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    with patch(
        "data.provider_mapping_registry.validate_provider_mappings_cover_universe",
        wraps=__import__("data.provider_mapping_registry", fromlist=["validate_provider_mappings_cover_universe"]).validate_provider_mappings_cover_universe,
    ) as mocked:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
        _, kwargs = mocked.call_args
        assert "require_symbol_coverage" not in kwargs
        assert kwargs["require_yfinance"] is True
        assert kwargs["require_dart"] is True


def test_optional_artifact_present_but_missing_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body(extra='candidate_pool = "missing_pool.toml"'),
    )
    with pytest.raises(KrEndToEndPreflightError, match="optional artifact missing: artifacts.candidate_pool") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"


def test_optional_artifact_absent_produces_warning_not_failure(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert payload["status"] == "ok"
    assert any("optional artifact not listed" in w for w in payload["warnings"])


def test_summary_includes_resolved_paths_not_raw_contents(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert "artifacts" in payload
    assert str(tmp_path / "universe.preflight.synthetic.toml") in payload["artifacts"]["universe"]
    dumped = json.dumps(payload)
    assert "Samsung Electronics" not in dumped
    assert "00126380" not in dumped


def test_summary_includes_provider_validation_block(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert "provider_mapping_validation" in payload
    assert payload["provider_mapping_validation"]["enabled_symbols_count"] == 2


def test_summary_json_no_trading_action_order_allocation_fields(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    _walk_forbidden_fields(payload)


def test_summary_json_no_env_api_key_names(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    dumped = json.dumps(payload).lower()
    assert "fred_api_key" not in dumped
    assert "dart_api_key" not in dumped
    assert "api_key" not in dumped


def test_summary_json_no_endpoint_urls(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    dumped = json.dumps(payload)
    assert "https://" not in dumped
    assert "http://" not in dumped


def test_followup_command_plan_contains_only_existing_ops_commands(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    joined = "\n".join(payload["followup_commands"])
    for cmd in _EXISTING_OPS_COMMANDS:
        assert cmd in joined
    assert "ops/run_3h0" not in joined


def test_followup_plan_no_invented_3h0_command(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    joined = "\n".join(payload["followup_commands"])
    assert "ops/run_3h0" not in joined
    assert "preflight_kr_end_to_end_intake.py" not in joined


def test_followup_plan_no_broker_kis_paperloop_commands(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    joined = "\n".join(payload["followup_commands"]).lower()
    broker_token = "run_" + "kis"
    assert broker_token not in joined
    assert "paperbroker" not in joined
    assert "paperlooprunner" not in joined
    assert "run_paper_once" not in joined


def test_followup_plan_no_config_promotion_command(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    joined = "\n".join(payload["followup_commands"]).lower()
    assert "cp " not in joined
    assert "config/universe" not in joined


def test_plan_out_writes_markdown(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    plan_out = tmp_path / "plan.md"
    run_kr_end_to_end_preflight(path, plan_out=plan_out, emit_followup_commands=True, force=True)
    text = plan_out.read_text(encoding="utf-8")
    assert "# KR End-to-End Intake Preflight Plan" in text
    assert "Follow-up commands to run manually" in text


def test_summary_out_writes_json(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "summary.json"
    run_kr_end_to_end_preflight(path, summary_out=summary_out, emit_followup_commands=False, force=True)
    payload = json.loads(summary_out.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"


def test_output_exists_without_force_write_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "summary.json"
    summary_out.write_text("{}", encoding="utf-8")
    with pytest.raises(KrEndToEndPreflightError, match="output already exists: summary_out") as exc:
        run_kr_end_to_end_preflight(path, summary_out=summary_out, emit_followup_commands=False, force=False)
    assert exc.value.stage == "write"
    assert str(summary_out) not in exc.value.message


def test_output_exists_without_force_reports_field_name_not_path(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    plan_out = tmp_path / "plan.md"
    plan_out.write_text("old", encoding="utf-8")
    with pytest.raises(KrEndToEndPreflightError, match="output already exists: plan_out") as exc:
        run_kr_end_to_end_preflight(path, plan_out=plan_out, emit_followup_commands=False, force=False)
    assert exc.value.stage == "write"
    assert str(plan_out) not in exc.value.message


def test_force_allows_summary_plan_overwrite(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "summary.json"
    plan_out = tmp_path / "plan.md"
    summary_out.write_text("{}", encoding="utf-8")
    plan_out.write_text("old", encoding="utf-8")
    payload = run_kr_end_to_end_preflight(
        path,
        summary_out=summary_out,
        plan_out=plan_out,
        emit_followup_commands=True,
        force=True,
    )
    assert payload["status"] == "ok"
    assert json.loads(summary_out.read_text(encoding="utf-8"))["status"] == "ok"
    assert "Preflight Plan" in plan_out.read_text(encoding="utf-8")


def test_force_never_modifies_input_artifacts(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    universe = tmp_path / "universe.preflight.synthetic.toml"
    mapping = tmp_path / "provider_mappings.preflight.synthetic.toml"
    universe_before = universe.read_bytes()
    mapping_before = mapping.read_bytes()
    run_kr_end_to_end_preflight(
        path,
        summary_out=tmp_path / "summary.json",
        emit_followup_commands=False,
        force=True,
    )
    assert universe.read_bytes() == universe_before
    assert mapping.read_bytes() == mapping_before


def test_cli_success_with_json(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    result = _run_cli("--manifest", str(path), "--no-emit-followup-commands", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"


def test_cli_bad_manifest_path_parse_stage() -> None:
    result = _run_cli("--manifest", "/nonexistent/manifest.toml", "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "parse"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_output_exists_without_force_write_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "summary.json"
    summary_out.write_text("{}", encoding="utf-8")
    result = _run_cli(
        "--manifest",
        str(path),
        "--summary-out",
        str(summary_out),
        "--no-emit-followup-commands",
        "--json",
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["stage"] == "write"


def test_cli_emits_no_traceback_for_known_errors(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body(universe="missing.toml"))
    result = _run_cli("--manifest", str(path), "--no-emit-followup-commands", "--json")
    assert result.returncode == 1
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_no_env_api_key_read_in_ops_source() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    assert "os.environ" not in source
    assert "getenv" not in source
    assert "fred_api_key" not in source
    assert "dart_api_key" not in source


def test_no_network_live_api_in_ops_source() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_STATIC_TOKENS:
        assert token not in source, f"ops source must not contain {token!r}"


def test_static_scan_includes_new_ops_file() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert "preflight_kr_end_to_end_intake.py" in text


def test_new_ops_file_passes_shared_static_scan() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_STATIC_TOKENS:
        assert token not in source, f"preflight ops must not reference {token!r}"


def test_generated_plan_commands_not_executed_in_tests(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    assert "followup_commands" in payload
    # tests never invoke subprocess on generated commands — only string inspection above


def test_git_ls_files_runtime_empty() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runtime"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert tracked.returncode == 0
    assert tracked.stdout.strip() == ""


def test_manifest_fixture_with_repo_relative_optional_artifacts() -> None:
    payload = run_kr_end_to_end_preflight(MANIFEST_FIXTURE, emit_followup_commands=False)
    assert payload["status"] == "ok"
    checks = {item["artifact"]: item for item in payload["optional_artifact_checks"]}
    assert checks["candidate_pool"]["status"] == "ok"
    assert checks["factor_inputs"]["status"] == "ok"


def test_synthetic_manifest_with_optional_paths_from_fixture_dir() -> None:
    manifest = load_kr_end_to_end_preflight_manifest(MANIFEST_FIXTURE)
    assert isinstance(manifest, KrEndToEndPreflightManifest)
    assert manifest.candidate_pool is not None
    assert manifest.factor_inputs is not None


# --- 3H2 hardening: atomic write ---


def test_write_output_summary_succeeds_normally(tmp_path: Path) -> None:
    out_path = tmp_path / "summary.json"
    _write_output(out_path, '{"status":"ok"}\n', force=True, field_name="summary_out")
    assert out_path.read_text(encoding="utf-8") == '{"status":"ok"}\n'


def test_write_output_plan_succeeds_normally(tmp_path: Path) -> None:
    out_path = tmp_path / "plan.md"
    _write_output(out_path, "# plan\n", force=True, field_name="plan_out")
    assert out_path.read_text(encoding="utf-8") == "# plan\n"


def test_force_overwrites_summary_only_after_temp_write_succeeds(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "summary.json"
    summary_out.write_text('{"old": true}', encoding="utf-8")
    payload = run_kr_end_to_end_preflight(path, summary_out=summary_out, emit_followup_commands=False, force=True)
    written = json.loads(summary_out.read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert payload["status"] == "ok"


def test_force_overwrites_plan_only_after_temp_write_succeeds(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    plan_out = tmp_path / "plan.md"
    plan_out.write_text("old plan", encoding="utf-8")
    run_kr_end_to_end_preflight(path, plan_out=plan_out, emit_followup_commands=True, force=True)
    assert "Preflight Plan" in plan_out.read_text(encoding="utf-8")
    assert "old plan" not in plan_out.read_text(encoding="utf-8")


def test_summary_temp_write_failure_preserves_existing_summary(tmp_path: Path) -> None:
    out_path = tmp_path / "summary.json"
    original = '{"status":"ok","preserved":true}\n'
    out_path.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPreflightError, match="output write failed: PermissionError") as exc:
            _write_output(out_path, '{"broken":true}\n', force=True, field_name="summary_out")
    assert exc.value.stage == "write"
    assert out_path.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_plan_temp_write_failure_preserves_existing_plan(tmp_path: Path) -> None:
    out_path = tmp_path / "plan.md"
    original = "# preserved plan\n"
    out_path.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPreflightError, match="output write failed: PermissionError") as exc:
            _write_output(out_path, "# broken\n", force=True, field_name="plan_out")
    assert exc.value.stage == "write"
    assert out_path.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_summary_replace_failure_preserves_existing_summary(tmp_path: Path) -> None:
    out_path = tmp_path / "summary.json"
    original = '{"status":"ok","preserved":true}\n'
    out_path.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"
    original_replace = Path.replace

    def _raise_os_error(self: Path, target: Path) -> None:
        if self.name.startswith(".tmp_preflight_summary_out_"):
            raise OSError(f"replace blocked for {secret}")
        original_replace(self, target)

    with patch.object(Path, "replace", _raise_os_error):
        with pytest.raises(KrEndToEndPreflightError, match="output write failed: OSError") as exc:
            _write_output(out_path, '{"broken":true}\n', force=True, field_name="summary_out")
    assert exc.value.stage == "write"
    assert out_path.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_plan_replace_failure_preserves_existing_plan(tmp_path: Path) -> None:
    out_path = tmp_path / "plan.md"
    original = "# preserved plan\n"
    out_path.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"
    original_replace = Path.replace

    def _raise_os_error(self: Path, target: Path) -> None:
        if self.name.startswith(".tmp_preflight_plan_out_"):
            raise OSError(f"replace blocked for {secret}")
        original_replace(self, target)

    with patch.object(Path, "replace", _raise_os_error):
        with pytest.raises(KrEndToEndPreflightError, match="output write failed: OSError") as exc:
            _write_output(out_path, "# broken\n", force=True, field_name="plan_out")
    assert exc.value.stage == "write"
    assert out_path.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_temp_files_cleaned_up_after_write_failure(tmp_path: Path) -> None:
    out_path = tmp_path / "nested" / "summary.json"
    out_path.parent.mkdir(parents=True)

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        if _self.name.startswith(".tmp_preflight_summary_out_"):
            raise PermissionError("blocked")
        Path.write_text(_self, *_args, **_kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPreflightError):
            _write_output(out_path, '{"broken":true}\n', force=True, field_name="summary_out")
    leftovers = list(out_path.parent.glob(".tmp_preflight_*"))
    assert leftovers == []


def test_temp_files_created_under_output_parent_not_global_tmp(tmp_path: Path) -> None:
    out_path = tmp_path / "nested" / "summary.json"
    observed_temp_parent: list[Path] = []
    original_write_text = Path.write_text

    def _capture_temp_write(self: Path, *args: object, **kwargs: object) -> object:
        if self.name.startswith(".tmp_preflight_summary_out_"):
            observed_temp_parent.append(self.parent)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _capture_temp_write):
        _write_output(out_path, '{"status":"ok"}\n', force=True, field_name="summary_out")
    assert observed_temp_parent == [out_path.parent]
    assert out_path.is_file()


# --- 3H2 hardening: error sanitization ---


def test_universe_loader_failure_no_traceback_in_message(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    bad_universe = tmp_path / "universe.preflight.synthetic.toml"
    bad_universe.write_text("version = 999\n", encoding="utf-8")
    path = _write_manifest(tmp_path, _valid_manifest_body())
    with pytest.raises(KrEndToEndPreflightError, match="universe load failed") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"
    assert "Traceback" not in exc.value.message


def test_provider_mapping_loader_failure_no_traceback_in_message(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    bad_mapping = tmp_path / "provider_mappings.preflight.synthetic.toml"
    bad_mapping.write_text("version = 999\n", encoding="utf-8")
    path = _write_manifest(tmp_path, _valid_manifest_body())
    with pytest.raises(KrEndToEndPreflightError, match="provider mapping load failed") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"
    assert "Traceback" not in exc.value.message


def test_provider_mapping_coverage_failure_may_include_symbol_context(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body(provider_mapping="provider_mappings.preflight.incomplete.toml"),
    )
    with pytest.raises(KrEndToEndPreflightError, match="provider mapping coverage failed") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"
    assert "Traceback" not in exc.value.message


def test_optional_artifact_parser_failure_no_raw_traceback(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    pool_path = tmp_path / "bad_pool.toml"
    pool_path.write_text("version = 999\n", encoding="utf-8")
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body(extra=f'candidate_pool = "{pool_path.name}"'),
    )
    with pytest.raises(KrEndToEndPreflightError, match="optional artifact parse failed: candidate_pool") as exc:
        run_kr_end_to_end_preflight(path, emit_followup_commands=False)
    assert exc.value.stage == "validate"
    assert "Traceback" not in exc.value.message


def test_output_write_failure_sanitizes_secret_from_exception(tmp_path: Path) -> None:
    out_path = tmp_path / "summary.json"
    secret = "/raw/path/with/SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPreflightError) as exc:
            _write_output(out_path, '{"status":"ok"}\n', force=True, field_name="summary_out")
    assert exc.value.stage == "write"
    assert exc.value.message == "output write failed: PermissionError"
    assert secret not in exc.value.message
    assert exc.value.__cause__ is None


def test_cli_known_errors_json_status_error_no_traceback(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body(universe="missing.toml"))
    result = _run_cli("--manifest", str(path), "--no-emit-followup-commands", "--json")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_cli_known_errors_no_endpoint_urls(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body(universe="missing.toml"))
    result = _run_cli("--manifest", str(path), "--no-emit-followup-commands", "--json")
    combined = result.stdout + result.stderr
    assert "https://" not in combined
    assert "http://" not in combined


def test_cli_known_errors_no_env_api_key_strings(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body(universe="missing.toml"))
    result = _run_cli("--manifest", str(path), "--no-emit-followup-commands", "--json")
    combined = (result.stdout + result.stderr).lower()
    assert "fred_api_key" not in combined
    assert "dart_api_key" not in combined
    assert "api_key" not in combined


# --- 3H2 hardening: command plan allowlist ---


def test_generated_command_plan_validates_against_positive_allowlist(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    scripts = _extract_followup_command_scripts(payload["followup_commands"])
    assert scripts
    for script in scripts:
        assert script in _FOLLOWUP_COMMAND_ALLOWLIST


def test_allowlist_contains_only_existing_repo_files() -> None:
    for script in _FOLLOWUP_COMMAND_ALLOWLIST:
        assert (REPO_ROOT / script).is_file(), f"allowlisted script must exist: {script}"


def test_generated_command_plan_no_invented_3h1_command(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    joined = "\n".join(payload["followup_commands"])
    assert "preflight_kr_end_to_end_intake.py" not in joined


def test_disallowed_followup_command_fails_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    bad_steps = [
        FollowupStep(
            id="bad-step",
            label="Bad step",
            command_lines=(
                "# Review-only",
                "PYTHONPATH=src uv run python ops/not_allowlisted_script.py --json",
            ),
            script="ops/not_allowlisted_script.py",
        ),
    ]
    with patch("preflight_kr_end_to_end_intake._build_followup_steps", return_value=bad_steps):
        with pytest.raises(KrEndToEndPreflightError, match="follow-up command not allowlisted") as exc:
            run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    assert exc.value.stage == "validate"


def test_validate_followup_command_allowlist_rejects_disallowed_script() -> None:
    with pytest.raises(KrEndToEndPreflightError, match="follow-up command not allowlisted") as exc:
        _validate_followup_command_allowlist(
            ["PYTHONPATH=src uv run python ops/not_allowlisted_script.py --json"],
        )
    assert exc.value.stage == "validate"


def test_comment_lines_in_plan_not_subject_to_allowlist() -> None:
    commands = [
        "# cat /tmp/autostock_fred_YYYY-MM-DD.jsonl /tmp/price.jsonl > /tmp/combined.jsonl",
        "PYTHONPATH=src uv run python ops/validate_provider_mapping.py --json",
    ]
    _validate_followup_command_allowlist(commands)
    scripts = _extract_followup_command_scripts(commands)
    assert scripts == ["ops/validate_provider_mapping.py"]


def test_followup_plan_no_submit_order_token(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    joined = "\n".join(payload["followup_commands"]).lower()
    sensitive = "submit_" + "order"
    assert sensitive not in joined


def test_no_subprocess_os_system_exec_eval_in_ops_source() -> None:
    source = OPS_SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "subprocess" not in lowered
    assert "os.system" not in lowered
    assert " exec(" not in lowered
    assert " eval(" not in lowered


def test_summary_json_schema_compatible_with_3h1(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    expected_keys = {
        "status",
        "stage",
        "mode",
        "manifest",
        "name",
        "artifacts",
        "provider_mapping_validation",
        "optional_artifact_checks",
        "settings",
        "warnings",
        "followup_commands",
    }
    assert expected_keys <= set(payload.keys())


def test_plan_markdown_contains_manual_followup_commands(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    plan_out = tmp_path / "plan.md"
    run_kr_end_to_end_preflight(path, plan_out=plan_out, emit_followup_commands=True, force=True)
    text = plan_out.read_text(encoding="utf-8")
    assert "Follow-up commands to run manually" in text
    assert "ops/validate_provider_mapping.py" in text


def test_static_scan_shared_forbidden_tuple_unchanged() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert '"requests"' in text
    assert '"httpx"' in text
    assert '"aiohttp"' in text
    assert '"urllib.request"' in text
    assert '"urllib.parse"' in text
    assert '"urllib.error"' in text
    assert '"kis"' in text
    assert '"paperbroker"' in text
    assert '"paperlooprunner"' in text
    assert '"submit_order"' in text


# --- 3H3: structured follow-up plan JSON ---


def test_manifest_accepts_structured_plan_out(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body(extra="")
        + '\n[outputs]\nstructured_plan_out = "structured_plan.json"\n',
    )
    manifest = load_kr_end_to_end_preflight_manifest(path)
    assert manifest.structured_plan_out == (tmp_path / "structured_plan.json").resolve()


def test_unknown_outputs_field_still_rejected(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(
        tmp_path,
        _valid_manifest_body()
        + '\n[outputs]\nsummary_out = "summary.json"\nunknown_outputs = true\n',
    )
    with pytest.raises(KrEndToEndPreflightError, match="unknown outputs fields") as exc:
        load_kr_end_to_end_preflight_manifest(path)
    assert exc.value.stage == "parse"


def test_cli_structured_plan_out_overrides_manifest(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    manifest_path = _write_manifest(
        tmp_path,
        _valid_manifest_body()
        + '\n[outputs]\nstructured_plan_out = "manifest_structured.json"\n',
    )
    cli_out = tmp_path / "cli_structured.json"
    payload = run_kr_end_to_end_preflight(
        manifest_path,
        structured_plan_out=cli_out,
        emit_followup_commands=True,
        force=True,
    )
    assert payload["structured_plan_out"] == str(cli_out)
    assert cli_out.is_file()
    assert not (tmp_path / "manifest_structured.json").exists()


def test_structured_plan_writes_json_normally(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    structured_out = tmp_path / "structured_plan.json"
    run_kr_end_to_end_preflight(
        path,
        structured_plan_out=structured_out,
        emit_followup_commands=True,
        force=True,
    )
    payload = json.loads(structured_out.read_text(encoding="utf-8"))
    assert payload["review_only"] is True
    assert payload["steps"]


def test_structured_plan_exists_without_force_write_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    structured_out = tmp_path / "structured_plan.json"
    structured_out.write_text("{}", encoding="utf-8")
    with pytest.raises(KrEndToEndPreflightError, match="output already exists: structured_plan_out") as exc:
        run_kr_end_to_end_preflight(
            path,
            structured_plan_out=structured_out,
            emit_followup_commands=True,
            force=False,
        )
    assert exc.value.stage == "write"
    assert str(structured_out) not in exc.value.message


def test_force_allows_structured_plan_overwrite(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    structured_out = tmp_path / "structured_plan.json"
    structured_out.write_text('{"old": true}', encoding="utf-8")
    payload = run_kr_end_to_end_preflight(
        path,
        structured_plan_out=structured_out,
        emit_followup_commands=True,
        force=True,
    )
    written = json.loads(structured_out.read_text(encoding="utf-8"))
    assert written["review_only"] is True
    assert payload["structured_plan_generated"] is True


def test_structured_plan_write_uses_atomic_helper(tmp_path: Path) -> None:
    out_path = tmp_path / "structured_plan.json"
    _write_output(out_path, '{"version":1}\n', force=True, field_name="structured_plan_out")
    assert out_path.read_text(encoding="utf-8") == '{"version":1}\n'


def test_structured_plan_write_failure_preserves_existing_file(tmp_path: Path) -> None:
    out_path = tmp_path / "structured_plan.json"
    original = '{"version":1,"preserved":true}\n'
    out_path.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPreflightError, match="output write failed: PermissionError") as exc:
            _write_output(out_path, '{"broken":true}\n', force=True, field_name="structured_plan_out")
    assert exc.value.stage == "write"
    assert out_path.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_structured_plan_write_failure_sanitizes_exception_detail(tmp_path: Path) -> None:
    out_path = tmp_path / "structured_plan.json"
    secret = "/raw/path/with/SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPreflightError) as exc:
            _write_output(out_path, '{"status":"ok"}\n', force=True, field_name="structured_plan_out")
    assert exc.value.message == "output write failed: PermissionError"
    assert secret not in exc.value.message


def _structured_plan_payload(tmp_path: Path) -> dict[str, object]:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    structured_out = tmp_path / "structured_plan.json"
    run_kr_end_to_end_preflight(
        path,
        structured_plan_out=structured_out,
        emit_followup_commands=True,
        force=True,
    )
    return json.loads(structured_out.read_text(encoding="utf-8"))


def test_structured_plan_top_level_shape(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    assert set(payload.keys()) >= {
        "version",
        "mode",
        "manifest",
        "name",
        "generated_by",
        "review_only",
        "steps",
        "forbidden_shortcuts",
        "warnings",
    }


def test_structured_plan_version_is_one(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    assert payload["version"] == 1


def test_structured_plan_mode_value(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    assert payload["mode"] == "kr-end-to-end-intake-followup-plan"


def test_structured_plan_executable_steps_in_allowlist(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    for step in payload["steps"]:
        script = step.get("script")
        if script is not None:
            assert script in _FOLLOWUP_COMMAND_ALLOWLIST


def test_structured_plan_comment_step_has_null_script(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    concat = next(step for step in payload["steps"] if step["id"] == "concatenate-jsonl")
    assert concat["script"] is None
    assert concat["executes_in_preflight"] is False


def test_structured_plan_every_step_requires_operator_review(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    for step in payload["steps"]:
        assert step["requires_operator_review"] is True


def test_structured_plan_every_step_not_executed_in_preflight(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    for step in payload["steps"]:
        assert step["executes_in_preflight"] is False


def test_structured_plan_step_ids_deterministic(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    first = _build_followup_steps(load_kr_end_to_end_preflight_manifest(path))
    second = _build_followup_steps(load_kr_end_to_end_preflight_manifest(path))
    assert [step.id for step in first] == [step.id for step in second]
    assert [step.id for step in first] == [
        "validate-provider-mapping",
        "price-smoke",
        "dart-smoke",
        "concatenate-jsonl",
        "research-source-intake-validate-only",
        "combined-context-smoke",
        "date-md-smoke",
        "scout-manual-packet",
    ]


def test_markdown_and_structured_plan_share_internal_steps(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    manifest = load_kr_end_to_end_preflight_manifest(path)
    steps = _build_followup_steps(manifest)
    payload = run_kr_end_to_end_preflight(
        path,
        structured_plan_out=tmp_path / "structured_plan.json",
        plan_out=tmp_path / "plan.md",
        emit_followup_commands=True,
        force=True,
    )
    structured = json.loads((tmp_path / "structured_plan.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "plan.md").read_text(encoding="utf-8")
    for step in structured["steps"]:
        for line in step["command"]:
            assert line in markdown
    assert payload["followup_commands"] == _followup_steps_to_command_lines(steps)


def test_markdown_plan_semantically_compatible_with_3h2(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    plan_out = tmp_path / "plan.md"
    run_kr_end_to_end_preflight(path, plan_out=plan_out, emit_followup_commands=True, force=True)
    text = plan_out.read_text(encoding="utf-8")
    joined = "\n".join(payload["followup_commands"])
    assert joined in text
    assert "Follow-up commands to run manually" in text
    assert "Forbidden shortcuts reminder" in text


def test_structured_plan_no_invented_3h0_command(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    dumped = json.dumps(payload)
    assert "ops/run_3h0" not in dumped


def test_structured_plan_no_invented_3h1_command(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    for step in payload["steps"]:
        joined = "\n".join(step["command"])
        assert "preflight_kr_end_to_end_intake.py" not in joined


def test_structured_plan_no_config_promotion_command(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    dumped = json.dumps(payload).lower()
    assert "cp " not in dumped
    assert "config/universe" not in dumped


def test_structured_plan_no_broker_kis_paperloop_commands(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    dumped = json.dumps(payload).lower()
    broker_token = "run_" + "kis"
    assert broker_token not in dumped
    assert "paperbroker" not in dumped
    assert "paperlooprunner" not in dumped


def test_structured_plan_no_submit_order_token(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    dumped = json.dumps(payload).lower()
    sensitive = "submit_" + "order"
    assert sensitive not in dumped


def test_structured_plan_no_endpoint_urls(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    dumped = json.dumps(payload)
    assert "https://" not in dumped
    assert "http://" not in dumped


def test_structured_plan_no_env_api_key_names(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    dumped = json.dumps(payload).lower()
    assert "fred_api_key" not in dumped
    assert "dart_api_key" not in dumped
    assert "api_key" not in dumped


def test_structured_plan_no_trading_action_order_allocation_fields(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    _walk_forbidden_fields(payload)


def test_structured_forbidden_shortcuts_use_runtime_fragment_source(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    assert payload["forbidden_shortcuts"] == _plan_forbidden_shortcuts_list()
    exec_path = "".join(("broker", "/", "PaperLoop", "/", "K", "IS"))
    assert any(exec_path in item for item in payload["forbidden_shortcuts"])


def test_disallowed_structured_step_script_fails_validate_stage(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    bad_steps = [
        FollowupStep(
            id="bad-step",
            label="Bad step",
            command_lines=("PYTHONPATH=src uv run python ops/not_allowlisted_script.py --json",),
            script="ops/not_allowlisted_script.py",
        ),
    ]
    with patch("preflight_kr_end_to_end_intake._build_followup_steps", return_value=bad_steps):
        with pytest.raises(KrEndToEndPreflightError, match="(structured plan step not allowlisted|follow-up command not allowlisted)") as exc:
            run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    assert exc.value.stage == "validate"


def test_validate_structured_plan_steps_rejects_disallowed_script() -> None:
    with pytest.raises(KrEndToEndPreflightError, match="structured plan step not allowlisted") as exc:
        _validate_structured_plan_steps(
            [
                FollowupStep(
                    id="bad-step",
                    label="Bad step",
                    command_lines=("PYTHONPATH=src uv run python ops/not_allowlisted_script.py --json",),
                    script="ops/not_allowlisted_script.py",
                ),
            ],
        )
    assert exc.value.stage == "validate"


def test_markdown_plan_still_validates_against_allowlist(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    scripts = _extract_followup_command_scripts(payload["followup_commands"])
    for script in scripts:
        assert script in _FOLLOWUP_COMMAND_ALLOWLIST


def test_summary_json_still_contains_existing_3h2_keys(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    expected_keys = {
        "status",
        "stage",
        "mode",
        "manifest",
        "name",
        "artifacts",
        "provider_mapping_validation",
        "optional_artifact_checks",
        "settings",
        "warnings",
        "followup_commands",
    }
    assert expected_keys <= set(payload.keys())


def test_summary_json_reports_structured_plan_metadata_when_written(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    structured_out = tmp_path / "structured_plan.json"
    summary_out = tmp_path / "summary.json"
    payload = run_kr_end_to_end_preflight(
        path,
        structured_plan_out=structured_out,
        summary_out=summary_out,
        emit_followup_commands=True,
        force=True,
    )
    assert payload["structured_plan_generated"] is True
    assert payload["structured_plan_steps_count"] == 8
    assert payload["structured_plan_out"] == str(structured_out)
    written = json.loads(summary_out.read_text(encoding="utf-8"))
    assert written["structured_plan_generated"] is True
    assert written["structured_plan_steps_count"] == 8
    assert written["structured_plan_out"] == str(structured_out)


def test_summary_json_does_not_inline_structured_steps(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "summary.json"
    run_kr_end_to_end_preflight(
        path,
        structured_plan_out=tmp_path / "structured_plan.json",
        summary_out=summary_out,
        emit_followup_commands=True,
        force=True,
    )
    written = json.loads(summary_out.read_text(encoding="utf-8"))
    assert "steps" not in written
    assert "structured_plan_steps_count" in written


def test_summary_without_structured_plan_omits_structured_metadata(tmp_path: Path) -> None:
    _copy_preflight_fixtures(tmp_path)
    path = _write_manifest(tmp_path, _valid_manifest_body())
    payload = run_kr_end_to_end_preflight(path, emit_followup_commands=True)
    assert "structured_plan_generated" not in payload
    assert "structured_plan_steps_count" not in payload
    assert "structured_plan_out" not in payload


# --- 3H4: structured follow-up plan validator ---


def _run_validator_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(VALIDATOR_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _write_structured_plan(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "structured_plan.json"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _valid_structured_plan_path(tmp_path: Path) -> Path:
    payload = _structured_plan_payload(tmp_path)
    return _write_structured_plan(tmp_path, payload)


def test_validator_accepts_plan_generated_by_3h3_helper(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    result = validate_structured_preflight_plan(plan_path)
    assert result["status"] == "ok"
    assert result["stage"] == "complete"


def test_validator_cli_success_with_json(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    proc = _run_validator_cli("--structured-plan", str(plan_path), "--json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["stage"] == "complete"
    assert payload["mode"] == "kr-end-to-end-preflight-plan-validation"


def test_validator_missing_plan_file_parse_stage() -> None:
    with pytest.raises(KrEndToEndPlanValidationError, match="structured plan file not found") as exc:
        validate_structured_preflight_plan(Path("/nonexistent/structured_plan.json"))
    assert exc.value.stage == "parse"


def test_validator_invalid_json_parse_stage(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KrEndToEndPlanValidationError, match="JSON parse failed") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "parse"


def test_validator_json_root_list_parse_stage(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(KrEndToEndPlanValidationError, match="root must be an object") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "parse"


def test_validator_version_must_be_one(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["version"] = 2
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="version must be exactly 1") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_mode_must_match(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["mode"] = "wrong-mode"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="mode mismatch") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_generated_by_must_match(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["generated_by"] = "ops/other.py"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="generated_by mismatch") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_review_only_must_be_true(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["review_only"] = False
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="review_only must be true") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_steps_must_be_non_empty(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"] = []
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="steps must be a non-empty list") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_step_requires_core_fields(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    del payload["steps"][0]["id"]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="id is required") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_step_command_must_be_non_empty_string_list(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = []
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="command must be a non-empty list") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_executable_step_script_must_be_allowlisted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["script"] = "ops/not_allowlisted_script.py"
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/not_allowlisted_script.py --json",
    ]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="script not allowlisted") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_executable_step_command_ops_script_allowlisted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/not_allowlisted_script.py --json",
    ]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="(script not allowlisted|command script not allowlisted)") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_executable_step_script_mismatch_fails(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["script"] = "ops/validate_provider_mapping.py"
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/run_kr_real_price_smoke.py --json",
    ]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="script mismatch") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_comment_manual_step_with_null_script_accepted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    concat = next(step for step in payload["steps"] if step["id"] == "concatenate-jsonl")
    assert concat["script"] is None
    path = _write_structured_plan(tmp_path, payload)
    validate_structured_preflight_plan(path)


def test_validator_comment_manual_step_with_ops_script_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    concat = next(step for step in payload["steps"] if step["id"] == "concatenate-jsonl")
    concat["script"] = None
    concat["command"] = ["PYTHONPATH=src uv run python ops/validate_provider_mapping.py --json"]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="manual step must not contain ops script") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_executes_in_preflight_true_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["executes_in_preflight"] = True
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="executes_in_preflight must be false") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_requires_operator_review_false_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["requires_operator_review"] = False
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="requires_operator_review must be true") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_allowed_false_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["allowed"] = False
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="allowed must be true") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_default_synthetic_plan_has_eight_step_ids_in_order(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    assert [step["id"] for step in payload["steps"]] == [
        "validate-provider-mapping",
        "price-smoke",
        "dart-smoke",
        "concatenate-jsonl",
        "research-source-intake-validate-only",
        "combined-context-smoke",
        "date-md-smoke",
        "scout-manual-packet",
    ]
    result = validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))
    assert result["steps_count"] == 8
    assert result["scripts_count"] == 7


def test_validator_accepts_conditional_step_id_subset(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"] = [
        step
        for step in payload["steps"]
        if step["id"] in {"validate-provider-mapping", "research-source-intake-validate-only"}
    ]
    path = _write_structured_plan(tmp_path, payload)
    result = validate_structured_preflight_plan(path)
    assert result["steps_count"] == 2


def test_validator_unknown_step_id_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["id"] = "unknown-step"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="step id not recognized") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_duplicate_step_id_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    duplicate = dict(payload["steps"][0])
    payload["steps"] = [payload["steps"][0], duplicate]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="step id duplicated") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_step_ids_out_of_canonical_order_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    steps = payload["steps"]
    payload["steps"] = [steps[1], steps[0], *steps[2:]]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="out of canonical order") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_invented_3h0_command_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = ["PYTHONPATH=src uv run python ops/run_3h0_smoke.py --json"]
    payload["steps"][0]["script"] = None
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="invented workflow command") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_invented_3h1_command_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/preflight_kr_end_to_end_intake.py --json",
    ]
    payload["steps"][0]["script"] = None
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="invented preflight command") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_config_promotion_command_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = ["cp generated.toml config/universe.toml"]
    payload["steps"][0]["script"] = None
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="config promotion command") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_endpoint_url_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["manifest"] = "https://example.test/manifest.toml"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="endpoint URL") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_env_api_key_name_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = ["# DART_API_KEY=secret"]
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="env or API key reference") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_trading_order_field_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["order"] = "buy"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="unknown top-level fields") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_trading_action_field_in_step_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["action"] = "buy"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="unknown fields") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_disallowed_script_validate_stage(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["script"] = "ops/evil_script.py"
    path = _write_structured_plan(tmp_path, payload)
    with pytest.raises(KrEndToEndPlanValidationError, match="script not allowlisted") as exc:
        validate_structured_preflight_plan(path)
    assert exc.value.stage == "validate"


def test_validator_cli_known_errors_no_traceback(tmp_path: Path) -> None:
    proc = _run_validator_cli("--structured-plan", str(tmp_path / "missing.json"), "--json")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr


def test_validator_cli_known_errors_do_not_echo_raw_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    secret = '{"version":1,"secret_marker":"RAW_JSON_SECRET"}'
    path.write_text(secret, encoding="utf-8")
    proc = _run_validator_cli("--structured-plan", str(path), "--json")
    assert proc.returncode == 1
    assert "RAW_JSON_SECRET" not in proc.stdout
    assert secret not in proc.stdout


def test_validator_does_not_execute_plan_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)

    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("validator must not execute plan commands")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    validate_structured_preflight_plan(plan_path)


def test_validator_ops_source_passes_shared_static_scan() -> None:
    source = VALIDATOR_SCRIPT.read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_STATIC_TOKENS:
        assert token not in source, f"validator ops must not reference {token!r}"


def test_static_scan_includes_validator_ops_file() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert "validate_kr_end_to_end_preflight_plan.py" in text


def test_validator_allowlist_matches_preflight_allowlist() -> None:
    assert _VALIDATOR_FOLLOWUP_COMMAND_ALLOWLIST == _FOLLOWUP_COMMAND_ALLOWLIST


def test_validator_no_env_api_key_read_in_ops_source() -> None:
    source = VALIDATOR_SCRIPT.read_text(encoding="utf-8").lower()
    assert "os.environ" not in source
    assert "getenv" not in source


def test_validator_no_subprocess_os_system_exec_eval_in_ops_source() -> None:
    source = VALIDATOR_SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "subprocess" not in lowered
    assert "os.system" not in lowered
    assert " exec(" not in lowered
    assert " eval(" not in lowered


def test_load_structured_preflight_plan_returns_object(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    loaded = load_structured_preflight_plan(plan_path)
    assert loaded["mode"] == "kr-end-to-end-intake-followup-plan"


# --- 3H5: structured plan validator command-line safety hardening ---


def test_validator_allowlisted_command_harmless_order_argument_accepted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/validate_provider_mapping.py "
        "--manifest /tmp/manifest.toml --notes reorder-check-order",
    ]
    validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))


def test_validator_allowlisted_command_harmless_action_argument_accepted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/validate_provider_mapping.py "
        "--json --label post-action-review",
    ]
    validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))


def test_validator_allowlisted_command_harmless_hold_prose_accepted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    concat = next(step for step in payload["steps"] if step["id"] == "concatenate-jsonl")
    concat["command"] = [
        "# Hold combined JSONL locally until operator review completes",
        "# threshold check optional; no execution",
    ]
    validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))


def test_validator_harmless_embedded_words_in_command_accepted(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = [
        "PYTHONPATH=src uv run python ops/validate_provider_mapping.py "
        "--json --tag reorder-transaction-threshold",
    ]
    validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))


def test_validator_endpoint_url_in_command_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = ["curl https://example.test/smoke"]
    payload["steps"][0]["script"] = None
    with pytest.raises(KrEndToEndPlanValidationError, match="endpoint URL") as exc:
        validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))
    assert exc.value.stage == "validate"


def test_validator_env_api_key_name_in_command_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["command"] = ["export DART_API_KEY=secret"]
    payload["steps"][0]["script"] = None
    with pytest.raises(KrEndToEndPlanValidationError, match="env or API key reference") as exc:
        validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))
    assert exc.value.stage == "validate"


def test_validator_exact_unsafe_execution_token_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    unsafe = "".join(("submit", "_", "order"))
    payload["steps"][0]["command"] = [f"PYTHONPATH=src uv run python -m ops.{unsafe}"]
    payload["steps"][0]["script"] = None
    with pytest.raises(KrEndToEndPlanValidationError, match="unsafe execution token") as exc:
        validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))
    assert exc.value.stage == "validate"


def test_validator_substring_words_with_generic_terms_accepted_when_safe(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    concat = next(step for step in payload["steps"] if step["id"] == "concatenate-jsonl")
    concat["command"] = [
        "# reorder files after transaction log threshold review",
        "# placeholder only — not an executable instruction",
    ]
    validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))


def test_validator_unknown_structured_field_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["unexpected_field"] = "value"
    with pytest.raises(KrEndToEndPlanValidationError, match="unknown fields") as exc:
        validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))
    assert exc.value.stage == "validate"


def test_validator_forbidden_structured_allocation_field_rejected(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["steps"][0]["allocation"] = "50%"
    with pytest.raises(KrEndToEndPlanValidationError, match="unknown fields") as exc:
        validate_structured_preflight_plan(_write_structured_plan(tmp_path, payload))
    assert exc.value.stage == "validate"


# --- 3H6: structured plan validator optional validation report ---


_REPORT_EXPECTED_KEYS = frozenset(
    {
        "version",
        "mode",
        "status",
        "stage",
        "structured_plan",
        "plan_mode",
        "plan_name",
        "generated_by",
        "review_only",
        "commands_execute_in_validator",
        "steps_count",
        "scripts_count",
        "manual_steps_count",
        "step_ids",
        "scripts",
        "warnings_count",
        "forbidden_shortcuts_count",
        "allowlist_status",
        "schema_status",
    }
)


def test_validator_success_without_report_out_unchanged(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    result = validate_structured_preflight_plan(plan_path)
    assert result["status"] == "ok"
    assert result["stage"] == "complete"
    assert result["mode"] == "kr-end-to-end-preflight-plan-validation"
    assert "report_out" not in result
    assert "report_written" not in result


def test_validator_force_without_report_out_is_harmless_noop(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    proc = _run_validator_cli("--structured-plan", str(plan_path), "--force", "--json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["report_written"] is False


def test_validator_cli_accepts_report_out(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    assert report_out.is_file()


def test_validator_cli_accepts_force_flag(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    written = json.loads(report_out.read_text(encoding="utf-8"))
    assert written["status"] == "ok"


def test_validator_report_out_writes_json_normally(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["stage"] == "complete"


def test_validator_report_write_only_after_validation_success(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["report_written"] is True
    assert payload["report_out"] == str(report_out.resolve())
    assert report_out.is_file()


def test_validator_failure_with_existing_report_out_fails_at_validate_not_write(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["version"] = 2
    plan_path = _write_structured_plan(tmp_path, payload)
    report_out = tmp_path / "validation_report.json"
    report_out.write_text('{"preexisting": true}\n', encoding="utf-8")
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "validate"
    assert json.loads(report_out.read_text(encoding="utf-8"))["preexisting"] is True


def test_validator_failure_does_not_create_report(tmp_path: Path) -> None:
    payload = _structured_plan_payload(tmp_path)
    payload["mode"] = "wrong-mode"
    plan_path = _write_structured_plan(tmp_path, payload)
    report_out = tmp_path / "validation_report.json"
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    assert not report_out.exists()


def test_validator_invalid_plan_with_report_out_fails_at_validate_not_write(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    report_out = tmp_path / "validation_report.json"
    proc = _run_validator_cli(
        "--structured-plan",
        str(path),
        "--report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "parse"
    assert not report_out.exists()


def test_validator_report_top_level_schema(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert set(report.keys()) == _REPORT_EXPECTED_KEYS


def test_validator_report_mode_is_validation_report_mode(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["mode"] == "kr-end-to-end-preflight-plan-validation-report"


def test_validator_report_plan_mode_matches_structured_plan(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["plan_mode"] == "kr-end-to-end-intake-followup-plan"


def test_validator_success_json_mode_unchanged_with_report_out(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "kr-end-to-end-preflight-plan-validation"


def test_validator_report_contains_no_validated_at(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert "validated_at" not in report


def test_validator_report_contains_no_full_steps_array(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert "steps" not in report


def test_validator_report_contains_no_command_lines(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert "command" not in report
    dumped = json.dumps(report)
    assert "PYTHONPATH=src" not in dumped


def test_validator_report_forbidden_shortcuts_count_only_not_contents(tmp_path: Path) -> None:
    plan_payload = _structured_plan_payload(tmp_path)
    plan_path = _write_structured_plan(tmp_path, plan_payload)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["forbidden_shortcuts_count"] == len(plan_payload["forbidden_shortcuts"])
    assert "forbidden_shortcuts" not in report
    for shortcut in plan_payload["forbidden_shortcuts"]:
        assert shortcut not in json.dumps(report)


def test_validator_report_step_ids_in_validated_order(tmp_path: Path) -> None:
    plan_payload = _structured_plan_payload(tmp_path)
    plan_path = _write_structured_plan(tmp_path, plan_payload)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["step_ids"] == [step["id"] for step in plan_payload["steps"]]


def test_validator_report_scripts_list_and_count(tmp_path: Path) -> None:
    plan_payload = _structured_plan_payload(tmp_path)
    plan_path = _write_structured_plan(tmp_path, plan_payload)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    expected_scripts = [
        step["script"]
        for step in plan_payload["steps"]
        if step.get("script") is not None
    ]
    assert report["scripts"] == expected_scripts
    assert report["scripts_count"] == len(expected_scripts)


def test_validator_report_manual_steps_count_correct(tmp_path: Path) -> None:
    plan_payload = _structured_plan_payload(tmp_path)
    plan_path = _write_structured_plan(tmp_path, plan_payload)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    expected_manual = sum(1 for step in plan_payload["steps"] if step.get("script") is None)
    assert report["manual_steps_count"] == expected_manual


def test_validator_report_steps_count_invariant(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["steps_count"] == report["scripts_count"] + report["manual_steps_count"]


def test_validator_report_review_only_true(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["review_only"] is True


def test_validator_report_commands_execute_in_validator_false(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["commands_execute_in_validator"] is False


def test_validator_report_allowlist_and_schema_status_ok(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["allowlist_status"] == "ok"
    assert report["schema_status"] == "ok"


def test_validator_report_exists_without_force_write_stage(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "write"


def test_validator_report_exists_without_force_reports_field_name_not_path(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--json",
    )
    error = json.loads(proc.stdout)
    assert error["message"] == "output already exists: report_out"
    assert str(report_out) not in error["message"]


def test_validator_force_overwrites_existing_report(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    written = json.loads(report_out.read_text(encoding="utf-8"))
    assert written["mode"] == "kr-end-to-end-preflight-plan-validation-report"
    assert "old" not in written


def test_validator_report_write_failure_preserves_existing_report(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    original = '{"status":"ok","preserved":true}\n'
    report_out.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPlanValidationError, match="output write failed: PermissionError") as exc:
            _write_report_output(
                report_out,
                {"version": 1, "mode": "kr-end-to-end-preflight-plan-validation-report"},
                force=True,
            )
    assert exc.value.stage == "write"
    assert report_out.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_validator_report_write_failure_sanitizes_exception_detail(tmp_path: Path) -> None:
    report_out = tmp_path / "validation_report.json"
    secret = "/raw/path/with/SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPlanValidationError) as exc:
            _write_report_output(
                report_out,
                {"version": 1, "mode": "kr-end-to-end-preflight-plan-validation-report"},
                force=True,
            )
    assert exc.value.message == "output write failed: PermissionError"
    assert secret not in exc.value.message
    assert exc.value.__cause__ is None


def test_validator_report_temp_file_cleaned_after_write_failure(tmp_path: Path) -> None:
    report_out = tmp_path / "nested" / "validation_report.json"
    report_out.parent.mkdir(parents=True)

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        if _self.name.startswith(".tmp_validation_report_"):
            raise PermissionError("blocked")
        Path.write_text(_self, *_args, **_kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndPlanValidationError):
            _write_report_output(
                report_out,
                {"version": 1, "mode": "kr-end-to-end-preflight-plan-validation-report"},
                force=True,
            )
    leftovers = list(report_out.parent.glob(".tmp_validation_report_*"))
    assert leftovers == []


def test_validator_report_temp_file_created_under_report_parent(tmp_path: Path) -> None:
    report_out = tmp_path / "nested" / "validation_report.json"
    observed_temp_parent: list[Path] = []
    original_write_text = Path.write_text

    def _capture_temp_write(self: Path, *args: object, **kwargs: object) -> object:
        if self.name.startswith(".tmp_validation_report_"):
            observed_temp_parent.append(self.parent)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _capture_temp_write):
        _write_report_output(
            report_out,
            {"version": 1, "mode": "kr-end-to-end-preflight-plan-validation-report", "status": "ok"},
            force=True,
        )
    assert observed_temp_parent == [report_out.parent]
    assert report_out.is_file()


def test_validator_cli_report_write_errors_no_traceback(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr


def test_validator_report_json_no_endpoint_urls(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8")
    assert "https://" not in dumped
    assert "http://" not in dumped


def test_validator_report_json_no_env_api_key_names(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8").lower()
    assert "fred_api_key" not in dumped
    assert "dart_api_key" not in dumped
    assert "api_key" not in dumped


def test_validator_report_json_no_trading_action_order_allocation_fields(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    _run_validator_cli(
        "--structured-plan",
        str(plan_path),
        "--report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    _walk_forbidden_fields(report)


def test_validator_build_validation_report_derived_from_validated_payload(tmp_path: Path) -> None:
    plan_payload = _structured_plan_payload(tmp_path)
    plan_path = _write_structured_plan(tmp_path, plan_payload)
    loaded = load_structured_preflight_plan(plan_path)
    plan, summary = _load_and_validate_structured_plan(plan_path)
    report = _build_validation_report(plan, plan_path, summary)
    assert report["plan_mode"] == loaded["mode"]
    assert report["plan_name"] == loaded["name"]
    assert report["generated_by"] == loaded["generated_by"]
    assert report["warnings_count"] == len(loaded["warnings"])
    assert report["forbidden_shortcuts_count"] == len(loaded["forbidden_shortcuts"])
    assert report["step_ids"] == summary["step_ids"]
    assert report["scripts"] == summary["scripts"]


# --- 3H7: operator handoff manifest / artifact integrity index ---


_MANIFEST_EXPECTED_TOP_KEYS = frozenset(
    {
        "version",
        "mode",
        "status",
        "stage",
        "generated_by",
        "artifacts",
        "artifacts_count",
        "all_artifacts_present",
        "commands_execute_in_builder",
        "review_only",
    }
)
_ARTIFACT_ENTRY_KEYS = frozenset(
    {
        "role",
        "path",
        "exists",
        "kind",
        "size_bytes",
        "sha256",
        "json_mode",
        "json_status",
        "json_stage",
    }
)
_ROLE_ORDER = (
    "preflight_summary",
    "plan_md",
    "structured_plan",
    "validation_report",
)


def _run_handoff_manifest_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(HANDOFF_MANIFEST_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _valid_preflight_summary_path(tmp_path: Path) -> Path:
    _copy_preflight_fixtures(tmp_path)
    manifest_path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "preflight_summary.json"
    run_kr_end_to_end_preflight(
        manifest_path,
        summary_out=summary_out,
        emit_followup_commands=True,
        force=True,
    )
    return summary_out


def _valid_plan_md_path(tmp_path: Path) -> Path:
    _copy_preflight_fixtures(tmp_path)
    manifest_path = _write_manifest(tmp_path, _valid_manifest_body())
    plan_out = tmp_path / "plan.md"
    run_kr_end_to_end_preflight(
        manifest_path,
        plan_out=plan_out,
        emit_followup_commands=True,
        force=True,
    )
    return plan_out


def _valid_validation_report_path(tmp_path: Path) -> Path:
    plan_path = _valid_structured_plan_path(tmp_path)
    report_out = tmp_path / "validation_report.json"
    plan, summary = _load_and_validate_structured_plan(plan_path)
    report = _build_validation_report(plan, plan_path, summary)
    _write_report_output(report_out, report, force=True)
    return report_out


def _all_four_artifact_paths(tmp_path: Path) -> dict[str, Path]:
    _copy_preflight_fixtures(tmp_path)
    manifest_path = _write_manifest(tmp_path, _valid_manifest_body())
    summary_out = tmp_path / "preflight_summary.json"
    plan_out = tmp_path / "plan.md"
    structured_out = tmp_path / "structured_plan.json"
    run_kr_end_to_end_preflight(
        manifest_path,
        summary_out=summary_out,
        plan_out=plan_out,
        structured_plan_out=structured_out,
        emit_followup_commands=True,
        force=True,
    )
    report_out = tmp_path / "validation_report.json"
    plan, summary = _load_and_validate_structured_plan(structured_out)
    report = _build_validation_report(plan, structured_out, summary)
    _write_report_output(report_out, report, force=True)
    return {
        "preflight_summary": summary_out,
        "plan_md": plan_out,
        "structured_plan": structured_out,
        "validation_report": report_out,
    }


def test_handoff_manifest_cli_requires_at_least_one_artifact_input(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli("--manifest-out", str(manifest_out), "--json")
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "args"
    assert "at least one artifact input" in error["message"]


@pytest.mark.parametrize(
    ("flag", "path_factory"),
    [
        ("--preflight-summary", _valid_preflight_summary_path),
        ("--plan-md", _valid_plan_md_path),
        ("--structured-plan", _valid_structured_plan_path),
        ("--validation-report", _valid_validation_report_path),
    ],
)
def test_handoff_manifest_cli_accepts_each_artifact_independently(
    tmp_path: Path,
    flag: str,
    path_factory: object,
) -> None:
    artifact_path = path_factory(tmp_path)  # type: ignore[operator]
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        flag,
        str(artifact_path),
        "--manifest-out",
        str(manifest_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["artifacts_count"] == 1
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert manifest["artifacts_count"] == 1


def test_handoff_manifest_cli_accepts_all_four_artifact_inputs(tmp_path: Path) -> None:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(artifacts["preflight_summary"]),
        "--plan-md",
        str(artifacts["plan_md"]),
        "--structured-plan",
        str(artifacts["structured_plan"]),
        "--validation-report",
        str(artifacts["validation_report"]),
        "--manifest-out",
        str(manifest_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["artifacts_count"] == 4
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert set(manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS


def test_handoff_manifest_file_mode_is_handoff_manifest_mode(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert manifest["mode"] == "kr-end-to-end-handoff-manifest"


def test_handoff_manifest_cli_success_mode_is_build_mode(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "kr-end-to-end-handoff-manifest-build"


def test_handoff_manifest_cli_error_mode_is_build_mode(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli("--manifest-out", str(manifest_out), "--json")
    error = json.loads(proc.stdout)
    assert error["mode"] == "kr-end-to-end-handoff-manifest-build"


def test_handoff_manifest_artifact_entries_in_role_order(tmp_path: Path) -> None:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert [entry["role"] for entry in manifest["artifacts"]] == list(_ROLE_ORDER)


def test_handoff_manifest_artifact_entry_shape(tmp_path: Path) -> None:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    for entry in manifest["artifacts"]:
        assert set(entry.keys()) == _ARTIFACT_ENTRY_KEYS


def test_handoff_manifest_sha256_matches_file_bytes(tmp_path: Path) -> None:
    import hashlib

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    expected = hashlib.sha256(summary.read_bytes()).hexdigest()
    assert manifest["artifacts"][0]["sha256"] == expected


def test_handoff_manifest_size_bytes_matches_file_size(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["size_bytes"] == summary.stat().st_size


def test_handoff_manifest_plan_md_not_content_parsed(tmp_path: Path) -> None:
    plan = _valid_plan_md_path(tmp_path)
    secret_marker = "SECRET_PLAN_MARKER_NOT_IN_MANIFEST"
    plan.write_text(plan.read_text(encoding="utf-8") + f"\n{secret_marker}\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(manifest_out=manifest_out, plan_md=plan, force=True)
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    dumped = json.dumps(manifest)
    assert secret_marker not in dumped
    assert manifest["artifacts"][0]["kind"] == "markdown"
    assert manifest["artifacts"][0]["json_mode"] is None


def test_handoff_manifest_preflight_summary_status_checked_only_if_present(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload.pop("status", None)
    summary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["json_status"] is None


def test_handoff_manifest_preflight_summary_mode_extracted_not_strict(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["mode"] = "custom-preflight-mode-for-test"
    summary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["json_mode"] == "custom-preflight-mode-for-test"


def test_handoff_manifest_preflight_summary_uses_nullable_getters(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["stage"] = "complete"
    summary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    entry = json.loads(manifest_out.read_text(encoding="utf-8"))["artifacts"][0]
    assert entry["json_mode"] == payload["mode"]
    assert entry["json_status"] == payload["status"]
    assert entry["json_stage"] == "complete"


def test_handoff_manifest_preflight_summary_non_ok_status_fails_validate(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["status"] = "error"
    summary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="status must be ok") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_structured_plan_mode_must_match(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["mode"] = "wrong-mode"
    plan_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="structured plan mode mismatch") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            structured_plan=plan_path,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_structured_plan_review_only_must_be_true(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["review_only"] = False
    plan_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="review_only must be true") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            structured_plan=plan_path,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_structured_plan_status_stage_absence_recorded_as_null(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        structured_plan=plan_path,
        force=True,
    )
    entry = json.loads(manifest_out.read_text(encoding="utf-8"))["artifacts"][0]
    assert entry["json_mode"] == "kr-end-to-end-intake-followup-plan"
    assert entry["json_status"] is None
    assert entry["json_stage"] is None


def test_handoff_manifest_validation_report_mode_status_stage(tmp_path: Path) -> None:
    report = _valid_validation_report_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        validation_report=report,
        force=True,
    )
    entry = json.loads(manifest_out.read_text(encoding="utf-8"))["artifacts"][0]
    assert entry["json_mode"] == "kr-end-to-end-preflight-plan-validation-report"
    assert entry["json_status"] == "ok"
    assert entry["json_stage"] == "complete"


def test_handoff_manifest_invalid_json_artifact_fails_parse(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="JSON parse failed") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            structured_plan=bad,
            force=True,
        )
    assert exc.value.stage == "parse"


def test_handoff_manifest_missing_artifact_path_fails_validate(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="artifact not found") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            structured_plan=tmp_path / "missing.json",
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_directory_path_fails_validate(tmp_path: Path) -> None:
    directory = tmp_path / "not_a_file"
    directory.mkdir()
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="artifact is not a file") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            plan_md=directory,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_validation_report_wrong_mode_fails_validate(tmp_path: Path) -> None:
    report = _valid_validation_report_path(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["mode"] = "wrong-mode"
    report.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="validation report mode mismatch") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            validation_report=report,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_validation_report_non_ok_status_fails_validate(tmp_path: Path) -> None:
    report = _valid_validation_report_path(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["status"] = "error"
    report.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="validation report status must be ok") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            validation_report=report,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_validation_report_non_complete_stage_fails_validate(tmp_path: Path) -> None:
    report = _valid_validation_report_path(tmp_path)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload["stage"] = "validate"
    report.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="validation report stage must be complete") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            validation_report=report,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_invalid_artifact_with_existing_manifest_out_fails_before_write(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest_out.write_text('{"preserved": true}\n', encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestError, match="JSON parse failed") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            structured_plan=bad,
            force=False,
        )
    assert exc.value.stage == "parse"


def test_handoff_manifest_invalid_artifact_leaves_existing_manifest_untouched(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    original = '{"preserved": true}\n'
    manifest_out.write_text(original, encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestError):
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            structured_plan=bad,
            force=False,
        )
    assert manifest_out.read_text(encoding="utf-8") == original


def test_handoff_manifest_out_exists_without_force_write_stage(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "write"


def test_handoff_manifest_out_exists_without_force_reports_field_name_not_path(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--json",
    )
    error = json.loads(proc.stdout)
    assert error["message"] == "output already exists: manifest_out"
    assert str(manifest_out) not in error["message"]


def test_handoff_manifest_force_overwrites_manifest_out(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    written = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert written["mode"] == "kr-end-to-end-handoff-manifest"
    assert "old" not in written


def test_handoff_manifest_write_failure_preserves_existing_manifest_out(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    original = '{"status":"ok","preserved":true}\n'
    manifest_out.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndHandoffManifestError, match="output write failed: PermissionError") as exc:
            _write_manifest_output(
                manifest_out,
                build_handoff_manifest({"preflight_summary": summary}),
                force=True,
            )
    assert exc.value.stage == "write"
    assert manifest_out.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_handoff_manifest_write_failure_sanitizes_exception_detail(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    secret = "/raw/path/with/SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndHandoffManifestError) as exc:
            _write_manifest_output(
                manifest_out,
                {"version": 1, "mode": "kr-end-to-end-handoff-manifest"},
                force=True,
            )
    assert exc.value.message == "output write failed: PermissionError"
    assert secret not in exc.value.message
    assert exc.value.__cause__ is None


def test_handoff_manifest_temp_file_cleaned_after_write_failure(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "nested" / "handoff_manifest.json"
    manifest_out.parent.mkdir(parents=True)

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        if _self.name.startswith(".tmp_handoff_manifest_"):
            raise PermissionError("blocked")
        Path.write_text(_self, *_args, **_kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndHandoffManifestError):
            _write_manifest_output(
                manifest_out,
                build_handoff_manifest({"preflight_summary": summary}),
                force=True,
            )
    leftovers = list(manifest_out.parent.glob(".tmp_handoff_manifest_*"))
    assert leftovers == []


def test_handoff_manifest_temp_file_created_under_manifest_parent(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "nested" / "handoff_manifest.json"
    observed_temp_parent: list[Path] = []
    original_write_text = Path.write_text

    def _capture_temp_write(self: Path, *args: object, **kwargs: object) -> object:
        if self.name.startswith(".tmp_handoff_manifest_"):
            observed_temp_parent.append(self.parent)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _capture_temp_write):
        _write_manifest_output(
            manifest_out,
            build_handoff_manifest({"preflight_summary": summary}),
            force=True,
        )
    assert observed_temp_parent == [manifest_out.parent]
    assert manifest_out.is_file()


def test_handoff_manifest_success_json_compact_keys(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == {
        "status",
        "stage",
        "mode",
        "manifest_out",
        "artifacts_count",
        "all_artifacts_present",
        "commands_execute_in_builder",
        "review_only",
    }


def test_handoff_manifest_cli_known_errors_no_traceback(tmp_path: Path) -> None:
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--structured-plan",
        str(tmp_path / "missing.json"),
        "--manifest-out",
        str(manifest_out),
        "--json",
    )
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr


def test_handoff_manifest_cli_known_errors_do_not_echo_raw_artifact_bodies(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    secret = '{"version":1,"secret_marker":"RAW_JSON_SECRET"}'
    bad.write_text(secret, encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--structured-plan",
        str(bad),
        "--manifest-out",
        str(manifest_out),
        "--json",
    )
    assert proc.returncode == 1
    assert "RAW_JSON_SECRET" not in proc.stdout
    assert secret not in proc.stdout


def test_handoff_manifest_does_not_include_structured_steps(tmp_path: Path) -> None:
    plan_path = _valid_structured_plan_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        structured_plan=plan_path,
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert "steps" not in manifest
    dumped = json.dumps(manifest)
    assert "validate-provider-mapping" not in dumped


def test_handoff_manifest_does_not_include_command_lines(tmp_path: Path) -> None:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert "command" not in manifest
    assert "followup_commands" not in manifest
    for entry in manifest["artifacts"]:
        assert "command" not in entry
    dumped = json.dumps(manifest)
    assert "PYTHONPATH=src" not in dumped


def test_handoff_manifest_does_not_include_endpoint_urls(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    dumped = manifest_out.read_text(encoding="utf-8")
    assert "https://" not in dumped
    assert "http://" not in dumped


def test_handoff_manifest_does_not_include_env_api_key_names(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    dumped = manifest_out.read_text(encoding="utf-8").lower()
    assert "fred_api_key" not in dumped
    assert "dart_api_key" not in dumped
    assert "api_key" not in dumped


def test_handoff_manifest_does_not_include_trading_action_order_allocation_fields(tmp_path: Path) -> None:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    _walk_forbidden_fields(manifest)


def test_handoff_manifest_builder_does_not_execute_generated_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"

    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("handoff manifest builder must not execute generated commands")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )


def test_handoff_manifest_ops_source_passes_shared_static_scan() -> None:
    source = HANDOFF_MANIFEST_SCRIPT.read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_STATIC_TOKENS:
        assert token not in source, f"handoff manifest ops must not reference {token!r}"


def test_static_scan_includes_handoff_manifest_ops_file() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert "build_kr_end_to_end_handoff_manifest.py" in text


def test_handoff_manifest_shared_forbidden_tuple_unchanged() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert '"submit_order"' in text
    assert '"paperbroker"' in text


def test_handoff_manifest_no_env_api_key_read_in_ops_source() -> None:
    source = HANDOFF_MANIFEST_SCRIPT.read_text(encoding="utf-8").lower()
    assert "os.environ" not in source
    assert "getenv" not in source


def test_handoff_manifest_no_subprocess_os_system_exec_eval_in_ops_source() -> None:
    source = HANDOFF_MANIFEST_SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "subprocess" not in lowered
    assert "os.system" not in lowered
    assert " exec(" not in lowered
    assert " eval(" not in lowered


# --- 3H14: handoff manifest builder validate-before-commit ---


def test_handoff_manifest_builder_generated_manifest_passes_verifier(tmp_path: Path) -> None:
    manifest_out = _valid_handoff_manifest_path(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_out)
    assert result["status"] == "ok"
    assert result["verified_artifacts_count"] == 4


def test_handoff_manifest_builder_calls_verifier_before_final_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    order: list[str] = []
    original_verify = build_module.verify_kr_end_to_end_handoff_manifest
    original_replace = Path.replace

    def _tracking_verify(path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        order.append("verify")
        return original_verify(path, base_dir=base_dir)

    def _tracking_replace(self: Path, target: Path) -> Path:
        order.append("replace")
        return original_replace(self, target)

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _tracking_verify)
    monkeypatch.setattr(Path, "replace", _tracking_replace)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    assert order == ["verify", "replace"]


def test_handoff_manifest_builder_validation_failure_maps_to_validate_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert exc.value.stage == "validate"
    assert exc.value.message == "stub verification failed"
    assert exc.value.__cause__ is None


def test_handoff_manifest_builder_validation_failure_does_not_create_final_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "nested" / "handoff_manifest.json"

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError):
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert not manifest_out.exists()


def test_handoff_manifest_builder_validation_failure_does_not_replace_existing_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    original = '{"status":"ok","preserved":true}\n'
    manifest_out.write_text(original, encoding="utf-8")

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert exc.value.stage == "validate"
    assert manifest_out.read_text(encoding="utf-8") == original


def test_handoff_manifest_builder_validation_failure_cleans_up_temp_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError):
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    leftovers = list(tmp_path.glob(".tmp_handoff_manifest_*"))
    assert leftovers == []


def test_handoff_manifest_builder_validation_failure_with_force_is_validate_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest_out.write_text('{"old": true}\n', encoding="utf-8")

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert exc.value.stage == "validate"
    assert exc.value.stage != "write"


def test_handoff_manifest_builder_temp_manifest_verified_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    verified_paths: list[Path] = []
    replace_called = {"value": False}
    original_verify = build_module.verify_kr_end_to_end_handoff_manifest
    original_replace = Path.replace

    def _capture_verify(path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        assert not replace_called["value"]
        verified_paths.append(path)
        assert path.name.startswith(".tmp_handoff_manifest_")
        assert path.parent == manifest_out.parent.resolve()
        return original_verify(path, base_dir=base_dir)

    def _mark_replace(self: Path, target: Path) -> Path:
        replace_called["value"] = True
        assert verified_paths
        return original_replace(self, target)

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _capture_verify)
    monkeypatch.setattr(Path, "replace", _mark_replace)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    assert replace_called["value"] is True


def test_handoff_manifest_builder_replace_failure_maps_to_write_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"

    def _raise_on_replace(_self: Path, _target: Path) -> Path:
        raise PermissionError("blocked replace")

    monkeypatch.setattr(Path, "replace", _raise_on_replace)
    with pytest.raises(KrEndToEndHandoffManifestError, match="output write failed: PermissionError") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert exc.value.stage == "write"
    assert not manifest_out.exists()
    leftovers = list(tmp_path.glob(".tmp_handoff_manifest_*"))
    assert leftovers == []


def test_handoff_manifest_builder_replace_failure_preserves_existing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    original = '{"status":"ok","preserved":true}\n'
    manifest_out.write_text(original, encoding="utf-8")

    def _raise_on_replace(_self: Path, _target: Path) -> Path:
        raise PermissionError("blocked replace")

    monkeypatch.setattr(Path, "replace", _raise_on_replace)
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
        )
    assert exc.value.stage == "write"
    assert manifest_out.read_text(encoding="utf-8") == original


# --- 3H15: handoff manifest builder optional path containment ---


def test_handoff_manifest_builder_without_base_dir_preserves_happy_path(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest = build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    assert set(manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS
    assert manifest_out.is_file()


def test_handoff_manifest_builder_without_base_dir_manifest_key_set_unchanged(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    manifest = build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    assert set(manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS


def test_handoff_manifest_builder_with_base_dir_manifest_key_set_unchanged(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    manifest = build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
        base_dir=bundle_dir,
    )
    assert set(manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS


def test_handoff_manifest_builder_cli_accepts_base_dir(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--base-dir",
        str(bundle_dir),
        "--force",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr


def test_handoff_manifest_builder_api_accepts_base_dir(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    manifest = build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
        base_dir=bundle_dir,
    )
    assert manifest["status"] == "ok"


def test_handoff_manifest_builder_base_dir_omitted_preserves_3h14_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    captured: dict[str, Path | None] = {"base_dir": "unset"}  # type: ignore[assignment]
    original_verify = build_module.verify_kr_end_to_end_handoff_manifest

    def _capture_verify(path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        captured["base_dir"] = base_dir
        return original_verify(path, base_dir=base_dir)

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _capture_verify)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
    )
    assert captured["base_dir"] is None


def test_handoff_manifest_builder_with_base_dir_inside_bundle_passes(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    artifacts = _all_four_artifact_paths(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    manifest = build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
        base_dir=bundle_dir,
    )
    assert manifest["artifacts_count"] == 4
    result = verify_kr_end_to_end_handoff_manifest(manifest_out, base_dir=bundle_dir)
    assert result["status"] == "ok"


def test_handoff_manifest_builder_with_base_dir_calls_verifier_with_resolved_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    captured: dict[str, Path | None] = {"base_dir": "unset"}  # type: ignore[assignment]
    original_verify = build_module.verify_kr_end_to_end_handoff_manifest

    def _capture_verify(path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        captured["base_dir"] = base_dir
        return original_verify(path, base_dir=base_dir)

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _capture_verify)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
        base_dir=bundle_dir,
    )
    assert captured["base_dir"] == bundle_dir.resolve()


def test_handoff_manifest_builder_cli_enforces_base_dir_for_manifest_out_outside_base(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = outside_dir / "handoff_manifest.json"
    exit_code = build_handoff_manifest_main(
        [
            "--preflight-summary",
            str(summary),
            "--manifest-out",
            str(manifest_out),
            "--base-dir",
            str(bundle_dir),
            "--force",
            "--json",
        ]
    )
    assert exit_code == 1


def test_handoff_manifest_builder_cli_enforces_base_dir_for_artifact_outside_base(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    exit_code = build_handoff_manifest_main(
        [
            "--preflight-summary",
            str(outside_summary),
            "--manifest-out",
            str(manifest_out),
            "--base-dir",
            str(bundle_dir),
            "--force",
            "--json",
        ]
    )
    assert exit_code == 1


def test_handoff_manifest_builder_blank_base_dir_fails_args_stage_cli() -> None:
    exit_code = build_handoff_manifest_main(
        [
            "--preflight-summary",
            "summary.json",
            "--manifest-out",
            "manifest.json",
            "--base-dir",
            "   ",
            "--json",
        ]
    )
    assert exit_code == 1


def test_handoff_manifest_builder_blank_base_dir_fails_args_stage_api() -> None:
    with pytest.raises(KrEndToEndHandoffManifestError, match="base directory path is required") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=Path("manifest.json"),
            preflight_summary=Path("summary.json"),
            base_dir=Path("   "),
        )
    assert exc.value.stage == "args"


def test_handoff_manifest_builder_missing_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="base directory not found") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_base_dir_is_file_fails_validate(tmp_path: Path) -> None:
    file_base = tmp_path / "file_base"
    file_base.write_text("not a dir\n", encoding="utf-8")
    summary = _valid_preflight_summary_path(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError, match="base directory is not a directory") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=file_base,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_manifest_outside_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = outside_dir / "handoff_manifest.json"
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="manifest_out path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_manifest_outside_base_dir_does_not_create_parent(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = outside_dir / "nested" / "handoff_manifest.json"
    with pytest.raises(KrEndToEndHandoffManifestError):
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert not manifest_out.parent.exists()


def test_handoff_manifest_builder_manifest_outside_base_dir_skips_exists_force_check(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = outside_dir / "handoff_manifest.json"
    manifest_out.write_text('{"existing": true}\n', encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=False,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"
    assert exc.value.message == "manifest_out path escapes base directory"


def test_handoff_manifest_builder_manifest_outside_base_with_existing_output_fails_validate_not_write(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = outside_dir / "handoff_manifest.json"
    original = '{"preserved": true}\n'
    manifest_out.write_text(original, encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"
    assert manifest_out.read_text(encoding="utf-8") == original


def test_handoff_manifest_builder_artifact_outside_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="preflight_summary artifact path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=outside_summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_artifact_outside_base_rejected_before_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    original_read_bytes = Path.read_bytes

    def _guard_read_bytes(self: Path) -> bytes:
        if self.resolve() == outside_summary.resolve():
            raise AssertionError("outside artifact bytes must not be read before containment check")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _guard_read_bytes)
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="preflight_summary artifact path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=outside_summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_missing_artifact_outside_base_fails_containment_not_missing(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    missing_outside = outside_dir / "missing_summary.json"
    manifest_out = bundle_dir / "handoff_manifest.json"
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="preflight_summary artifact path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=missing_outside,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"
    assert "not found" not in exc.value.message


def test_handoff_manifest_builder_sibling_prefix_outside_base_rejected(tmp_path: Path) -> None:
    base_dir = tmp_path / "h"
    base_dir.mkdir()
    sibling_dir = tmp_path / "handoff_evil"
    sibling_dir.mkdir()
    sibling_summary = _valid_preflight_summary_path(sibling_dir)
    manifest_out = base_dir / "handoff_manifest.json"
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="preflight_summary artifact path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=sibling_summary,
            force=True,
            base_dir=base_dir,
        )
    assert exc.value.stage == "validate"


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform lacks Path.symlink_to")
def test_handoff_manifest_builder_symlink_resolved_artifact_outside_base_rejected(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    link_dir = bundle_dir / "linked"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation not permitted: {exc}")
    manifest_out = bundle_dir / "handoff_manifest.json"
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="preflight_summary artifact path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=link_dir / outside_summary.name,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform lacks Path.symlink_to")
def test_handoff_manifest_builder_symlink_resolved_manifest_out_outside_base_rejected(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    outside_manifest = outside_dir / "handoff_manifest.json"
    link_manifest = bundle_dir / "linked_manifest.json"
    try:
        link_manifest.symlink_to(outside_manifest)
    except OSError as exc:
        pytest.skip(f"symlink creation not permitted: {exc}")
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="manifest_out path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=link_manifest,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_containment_failure_messages_do_not_echo_resolved_paths(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(outside_summary),
        "--manifest-out",
        str(manifest_out),
        "--base-dir",
        str(bundle_dir),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "validate"
    assert str(outside_summary.resolve()) not in proc.stdout
    assert str(bundle_dir.resolve()) not in proc.stdout


def test_handoff_manifest_builder_output_exists_inside_base_without_force_fails_write(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    manifest_out.write_text('{"existing": true}\n', encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestError, match="output already exists: manifest_out") as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=False,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "write"


def test_handoff_manifest_builder_valid_output_inside_base_with_force_overwrites(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    manifest_out.write_text('{"old": true}\n', encoding="utf-8")
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
        base_dir=bundle_dir,
    )
    manifest = json.loads(manifest_out.read_text(encoding="utf-8"))
    assert manifest["mode"] == "kr-end-to-end-handoff-manifest"
    assert manifest["generated_by"] == "ops/build_kr_end_to_end_handoff_manifest.py"


def test_handoff_manifest_builder_with_base_dir_generated_manifest_passes_verifier(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest_out = _valid_handoff_manifest_path(bundle_dir)
    result = verify_kr_end_to_end_handoff_manifest(manifest_out, base_dir=bundle_dir)
    assert result["status"] == "ok"


def test_handoff_manifest_builder_with_base_dir_validate_before_commit_still_happens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    order: list[str] = []
    original_verify = build_module.verify_kr_end_to_end_handoff_manifest
    original_replace = Path.replace

    def _tracking_verify(path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        order.append("verify")
        return original_verify(path, base_dir=base_dir)

    def _tracking_replace(self: Path, target: Path) -> Path:
        order.append("replace")
        return original_replace(self, target)

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _tracking_verify)
    monkeypatch.setattr(Path, "replace", _tracking_replace)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
        base_dir=bundle_dir,
    )
    assert order == ["verify", "replace"]


def test_handoff_manifest_builder_with_base_dir_verifier_failure_maps_to_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"


def test_handoff_manifest_builder_with_base_dir_verifier_failure_preserves_existing_with_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    original = '{"status":"ok","preserved":true}\n'
    manifest_out.write_text(original, encoding="utf-8")

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    assert exc.value.stage == "validate"
    assert manifest_out.read_text(encoding="utf-8") == original


def test_handoff_manifest_builder_with_base_dir_temp_manifest_under_manifest_out_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "nested" / "handoff_manifest.json"
    verified_paths: list[Path] = []
    original_verify = build_module.verify_kr_end_to_end_handoff_manifest

    def _capture_verify(path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        verified_paths.append(path)
        assert path.name.startswith(".tmp_handoff_manifest_")
        assert path.parent == manifest_out.parent.resolve()
        return original_verify(path, base_dir=base_dir)

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _capture_verify)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=summary,
        force=True,
        base_dir=bundle_dir,
    )
    assert verified_paths


def test_handoff_manifest_builder_with_base_dir_temp_cleanup_on_verifier_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import build_kr_end_to_end_handoff_manifest as build_module

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"

    def _failing_verify(_path: Path, *, base_dir: Path | None = None) -> dict[str, object]:
        raise KrEndToEndHandoffManifestVerifyError("validate", "stub verification failed")

    monkeypatch.setattr(build_module, "verify_kr_end_to_end_handoff_manifest", _failing_verify)
    with pytest.raises(KrEndToEndHandoffManifestError):
        build_kr_end_to_end_handoff_manifest(
            manifest_out=manifest_out,
            preflight_summary=summary,
            force=True,
            base_dir=bundle_dir,
        )
    leftovers = list(bundle_dir.glob(".tmp_handoff_manifest_*"))
    assert leftovers == []


def test_handoff_manifest_builder_with_base_dir_success_payload_keys_unchanged(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    summary = _valid_preflight_summary_path(bundle_dir)
    manifest_out = bundle_dir / "handoff_manifest.json"
    proc = _run_handoff_manifest_cli(
        "--preflight-summary",
        str(summary),
        "--manifest-out",
        str(manifest_out),
        "--base-dir",
        str(bundle_dir),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == {
        "status",
        "stage",
        "mode",
        "manifest_out",
        "artifacts_count",
        "all_artifacts_present",
        "commands_execute_in_builder",
        "review_only",
    }


# --- 3H8: operator handoff manifest integrity verifier ---


_VERIFY_SUCCESS_KEYS = frozenset(
    {
        "status",
        "stage",
        "mode",
        "manifest",
        "artifacts_count",
        "verified_artifacts_count",
        "hashes_verified",
        "metadata_verified",
        "commands_execute_in_verifier",
        "review_only",
    }
)


def _run_verify_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(HANDOFF_VERIFY_SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def _valid_handoff_manifest_path(tmp_path: Path) -> Path:
    artifacts = _all_four_artifact_paths(tmp_path)
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_out,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    return manifest_out


def _write_handoff_manifest_dict(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "handoff_manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _loaded_handoff_manifest_dict(tmp_path: Path) -> dict[str, object]:
    return json.loads(_valid_handoff_manifest_path(tmp_path).read_text(encoding="utf-8"))


def _refresh_manifest_integrity_for_role(payload: dict[str, object], role: str) -> None:
    """artifact 파일 변경 후 manifest entry size/sha256만 현재 바이트에 맞춘다."""
    import hashlib

    for entry in payload["artifacts"]:  # type: ignore[union-attr]
        if entry["role"] != role:
            continue
        data = Path(entry["path"]).read_bytes()
        entry["size_bytes"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        return
    raise AssertionError(f"artifact role not found in manifest: {role}")


def test_handoff_verifier_accepts_manifest_from_3h7_builder(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert result["status"] == "ok"
    assert result["artifacts_count"] == 4
    assert result["verified_artifacts_count"] == 4


def test_handoff_verifier_cli_validates_manifest_with_json(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli("--manifest", str(manifest_path), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["mode"] == "kr-end-to-end-handoff-manifest-verification"


def test_handoff_verifier_blank_manifest_path_args_stage() -> None:
    proc = _run_verify_cli("--manifest", "   ", "--json")
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "args"


def test_handoff_verifier_missing_manifest_file_parse_stage(tmp_path: Path) -> None:
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="file not found") as exc:
        load_handoff_manifest(tmp_path / "missing.json")
    assert exc.value.stage == "parse"


def test_handoff_verifier_invalid_manifest_json_parse_stage(tmp_path: Path) -> None:
    path = tmp_path / "handoff_manifest.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="JSON parse failed") as exc:
        load_handoff_manifest(path)
    assert exc.value.stage == "parse"


def test_handoff_verifier_manifest_root_list_parse_stage(tmp_path: Path) -> None:
    path = tmp_path / "handoff_manifest.json"
    path.write_text("[1,2,3]\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="root must be an object") as exc:
        load_handoff_manifest(path)
    assert exc.value.stage == "parse"


def test_handoff_verifier_manifest_top_level_mode_mismatch(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["mode"] = "wrong-mode"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="mode mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_manifest_status_must_be_ok(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["status"] = "error"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="status must be ok") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_manifest_stage_must_be_complete(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["stage"] = "validate"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="stage must be complete") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifacts_must_be_non_empty_list(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["artifacts"] = []
    payload["artifacts_count"] = 0
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="non-empty list") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifacts_count_must_equal_len(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["artifacts_count"] = 99
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="artifacts_count mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_entries_contain_required_keys(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    entry = dict(payload["artifacts"][0])  # type: ignore[index]
    entry.pop("sha256")
    payload["artifacts"] = [entry]
    payload["artifacts_count"] = 1
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="missing required fields") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_manifest_top_level_unknown_key_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["unexpected_top_level_field"] = "not-allowed"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="unknown top-level fields",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_manifest_top_level_missing_key_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload.pop("review_only")
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="missing required fields",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_manifest_top_level_exact_key_set_from_builder(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == _MANIFEST_EXPECTED_TOP_KEYS
    verify_kr_end_to_end_handoff_manifest(manifest_path)


def test_handoff_verifier_artifact_entry_unknown_key_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    entry = dict(payload["artifacts"][0])  # type: ignore[index]
    entry["unexpected_entry_field"] = "not-allowed"
    payload["artifacts"] = [entry]
    payload["artifacts_count"] = 1
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="unknown fields",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_entry_exact_key_set_from_builder(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in payload["artifacts"]:
        assert set(entry.keys()) == _ARTIFACT_ENTRY_KEYS
    verify_kr_end_to_end_handoff_manifest(manifest_path)


def test_handoff_verifier_artifact_entry_unknown_key_checked_on_raw_input(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    raw_entry = dict(payload["artifacts"][0])  # type: ignore[index]
    raw_entry["unexpected_entry_field"] = "not-allowed"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="unknown fields",
    ) as exc:
        _validate_artifact_entry_schema(raw_entry, index=0)
    assert exc.value.stage == "validate"


def test_handoff_verifier_manifest_unknown_key_checked_on_raw_payload(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["unexpected_top_level_field"] = "not-allowed"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="unknown top-level fields",
    ) as exc:
        _validate_manifest_schema(payload)  # type: ignore[arg-type]
    assert exc.value.stage == "validate"


def test_handoff_verifier_unknown_artifact_role_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    entry = dict(payload["artifacts"][0])  # type: ignore[index]
    entry["role"] = "unknown_role"
    payload["artifacts"] = [entry]
    payload["artifacts_count"] = 1
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="role not recognized") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_duplicate_artifact_role_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    duplicate = dict(payload["artifacts"][0])  # type: ignore[index]
    payload["artifacts"] = [duplicate, duplicate]
    payload["artifacts_count"] = 2
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="role duplicated") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_roles_out_of_order_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    artifacts = list(payload["artifacts"])  # type: ignore[arg-type]
    payload["artifacts"] = [artifacts[1], artifacts[0], *artifacts[2:]]
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="out of canonical order") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_kind_mismatch_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    entry = dict(payload["artifacts"][0])  # type: ignore[index]
    entry["kind"] = "markdown"
    payload["artifacts"] = [entry]
    payload["artifacts_count"] = 1
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="kind mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_exists_false_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    entry = dict(payload["artifacts"][0])  # type: ignore[index]
    entry["exists"] = False
    payload["artifacts"] = [entry]
    payload["artifacts_count"] = 1
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="exists must be true") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_path_missing_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["path"] = str(tmp_path / "missing_summary.json")
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="artifact not found") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_path_directory_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    directory = tmp_path / "not_a_file"
    directory.mkdir()
    payload["artifacts"][0]["path"] = str(directory)
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="artifact is not a file") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_size_mismatch_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["size_bytes"] = 1
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="size mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_sha256_mismatch_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["sha256"] = "a" * 64
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="sha256 mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_sha256_malformed_fails_validate(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["artifacts"][0]["sha256"] = "NOT_VALID_HEX"  # type: ignore[index]
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="lowercase hex") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_referenced_json_invalid_json_parse_stage(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary_path = Path(payload["artifacts"][0]["path"])
    summary_path.write_text("{bad-json", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "preflight_summary")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="JSON parse failed") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "parse"


def test_handoff_verifier_referenced_json_root_non_object_parse_stage(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary_path = Path(payload["artifacts"][0]["path"])
    summary_path.write_text("[1,2,3]\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "preflight_summary")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="root must be an object") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "parse"


def test_handoff_verifier_preflight_summary_non_ok_status_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary_path = Path(payload["artifacts"][0]["path"])
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_payload["status"] = "error"
    summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "preflight_summary")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="status must be ok") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_preflight_summary_custom_mode_accepted_when_recorded_matches(tmp_path: Path) -> None:
    summary = _valid_preflight_summary_path(tmp_path)
    payload = json.loads(summary.read_text(encoding="utf-8"))
    payload["mode"] = "custom-preflight-mode-for-test"
    summary.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(manifest_out=manifest_out, preflight_summary=summary, force=True)
    verify_kr_end_to_end_handoff_manifest(manifest_out)


def test_handoff_verifier_preflight_summary_recorded_metadata_mismatch_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["json_mode"] = "wrong-recorded-mode"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="json_mode mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_structured_plan_wrong_actual_mode_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan_entry = next(entry for entry in payload["artifacts"] if entry["role"] == "structured_plan")
    plan_path = Path(plan_entry["path"])
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["mode"] = "wrong-mode"
    plan_path.write_text(json.dumps(plan_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "structured_plan")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="structured plan mode mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_structured_plan_review_only_false_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan_entry = next(entry for entry in payload["artifacts"] if entry["role"] == "structured_plan")
    plan_path = Path(plan_entry["path"])
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_payload["review_only"] = False
    plan_path.write_text(json.dumps(plan_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "structured_plan")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="review_only must be true") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_structured_plan_recorded_json_status_non_null_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in payload["artifacts"]:
        if entry["role"] == "structured_plan":
            entry["json_status"] = "ok"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="json_status must be null") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_structured_plan_recorded_json_mode_mismatch_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in payload["artifacts"]:
        if entry["role"] == "structured_plan":
            entry["json_mode"] = "wrong-recorded-mode"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="json_mode mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_validation_report_wrong_actual_mode_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_entry = next(entry for entry in payload["artifacts"] if entry["role"] == "validation_report")
    report_path = Path(report_entry["path"])
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload["mode"] = "wrong-mode"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "validation_report")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="validation report mode mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_validation_report_non_ok_status_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_entry = next(entry for entry in payload["artifacts"] if entry["role"] == "validation_report")
    report_path = Path(report_entry["path"])
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload["status"] = "error"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "validation_report")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="validation report status must be ok") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_validation_report_non_complete_stage_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_entry = next(entry for entry in payload["artifacts"] if entry["role"] == "validation_report")
    report_path = Path(report_entry["path"])
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload["stage"] = "validate"
    report_path.write_text(json.dumps(report_payload, ensure_ascii=False) + "\n", encoding="utf-8")
    _refresh_manifest_integrity_for_role(payload, "validation_report")
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="validation report stage must be complete") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_validation_report_recorded_metadata_mismatch_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in payload["artifacts"]:
        if entry["role"] == "validation_report":
            entry["json_stage"] = "validate"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="json_stage mismatch") as exc:
        verify_kr_end_to_end_handoff_manifest(path)
    assert exc.value.stage == "validate"


def test_handoff_verifier_plan_md_content_not_parsed_only_size_hash_checked(tmp_path: Path) -> None:
    plan = _valid_plan_md_path(tmp_path)
    secret = "SECRET_PLAN_BODY_NOT_PARSED"
    plan.write_text(plan.read_text(encoding="utf-8") + f"\n{secret}\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(manifest_out=manifest_out, plan_md=plan, force=True)
    result = verify_kr_end_to_end_handoff_manifest(manifest_out)
    assert result["hashes_verified"] is True
    proc = _run_verify_cli("--manifest", str(manifest_out), "--json")
    assert secret not in proc.stdout


def test_handoff_verifier_success_json_compact_keys(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli("--manifest", str(manifest_path), "--json")
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS


def test_handoff_verifier_cli_known_errors_no_traceback(tmp_path: Path) -> None:
    proc = _run_verify_cli("--manifest", str(tmp_path / "missing.json"), "--json")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr


def test_handoff_verifier_cli_known_errors_do_not_echo_raw_manifest_body(tmp_path: Path) -> None:
    path = tmp_path / "handoff_manifest.json"
    secret = '{"version":1,"secret_marker":"RAW_MANIFEST_SECRET"}'
    path.write_text(secret, encoding="utf-8")
    proc = _run_verify_cli("--manifest", str(path), "--json")
    assert proc.returncode == 1
    assert "RAW_MANIFEST_SECRET" not in proc.stdout
    assert secret not in proc.stdout


def test_handoff_verifier_does_not_write_any_files(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    verify_kr_end_to_end_handoff_manifest(manifest_path)
    after = {p.name for p in tmp_path.iterdir()}
    assert before == after


def test_handoff_verifier_does_not_create_temp_files(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    verify_kr_end_to_end_handoff_manifest(manifest_path)
    leftovers = list(tmp_path.glob(".tmp_*"))
    assert leftovers == []


def test_handoff_verifier_does_not_execute_generated_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)

    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("handoff manifest verifier must not execute generated commands")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    verify_kr_end_to_end_handoff_manifest(manifest_path)


def test_handoff_verifier_ops_source_passes_shared_static_scan() -> None:
    source = HANDOFF_VERIFY_SCRIPT.read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_STATIC_TOKENS:
        assert token not in source, f"handoff verifier ops must not reference {token!r}"


def test_static_scan_includes_handoff_verifier_ops_file() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert "verify_kr_end_to_end_handoff_manifest.py" in text


def test_handoff_verifier_shared_forbidden_tuple_unchanged() -> None:
    text = STATIC_SCAN_FILE.read_text(encoding="utf-8")
    assert '"submit_order"' in text
    assert '"paperbroker"' in text


def test_handoff_verifier_no_env_api_key_read_in_ops_source() -> None:
    source = HANDOFF_VERIFY_SCRIPT.read_text(encoding="utf-8").lower()
    assert "os.environ" not in source
    assert "getenv" not in source


def test_handoff_verifier_no_subprocess_os_system_exec_eval_in_ops_source() -> None:
    source = HANDOFF_VERIFY_SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "subprocess" not in lowered
    assert "os.system" not in lowered
    assert " exec(" not in lowered
    assert " eval(" not in lowered


# --- 3H10: handoff manifest verifier optional path containment ---


_VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT = _VERIFY_SUCCESS_KEYS | {
    "base_dir",
    "path_containment_verified",
}


def test_handoff_verifier_without_base_dir_preserves_success_json_keys(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert set(result.keys()) == _VERIFY_SUCCESS_KEYS
    assert "base_dir" not in result
    assert "path_containment_verified" not in result


def test_handoff_verifier_cli_accepts_base_dir(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli("--manifest", str(manifest_path), "--base-dir", str(tmp_path), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["path_containment_verified"] is True


def test_handoff_verifier_api_accepts_base_dir(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=tmp_path)
    assert result["status"] == "ok"
    assert result["path_containment_verified"] is True


def test_handoff_verifier_with_base_dir_adds_containment_success_keys(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli("--manifest", str(manifest_path), "--base-dir", str(tmp_path), "--json")
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT
    assert payload["base_dir"] == str(tmp_path.resolve())
    assert payload["path_containment_verified"] is True


def test_handoff_verifier_base_dir_missing_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    missing_base = tmp_path / "missing_base_dir"
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="base directory not found") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=missing_base)
    assert exc.value.stage == "validate"


def test_handoff_verifier_base_dir_is_file_fails_validate(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    file_base = tmp_path / "not_a_directory.txt"
    file_base.write_text("x\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="not a directory") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=file_base)
    assert exc.value.stage == "validate"


def test_handoff_verifier_blank_base_dir_args_stage_cli() -> None:
    proc = _run_verify_cli("--manifest", "manifest.json", "--base-dir", "   ", "--json")
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "args"
    assert error["message"] == "base directory path is required"


def test_handoff_verifier_blank_base_dir_args_stage_api() -> None:
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="base directory path is required") as exc:
        verify_kr_end_to_end_handoff_manifest(Path("manifest.json"), base_dir=Path("   "))
    assert exc.value.stage == "args"


def test_handoff_verifier_manifest_outside_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_outside = outside_dir / "handoff_manifest.json"
    artifacts = _all_four_artifact_paths(bundle_dir)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_outside,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    with pytest.raises(KrEndToEndHandoffManifestVerifyError, match="manifest path escapes base directory") as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_outside, base_dir=bundle_dir)
    assert exc.value.stage == "validate"
    assert str(manifest_outside.resolve()) not in exc.value.message


def test_handoff_verifier_artifact_outside_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    manifest_path = bundle_dir / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_path,
        preflight_summary=outside_summary,
        force=True,
    )
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="preflight_summary path escapes base directory",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc.value.stage == "validate"
    assert str(outside_summary.resolve()) not in exc.value.message


def test_handoff_verifier_manifest_and_artifacts_inside_base_dir_pass(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True)
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert result["status"] == "ok"
    assert result["path_containment_verified"] is True


def test_handoff_verifier_containment_uses_resolved_paths_not_string_prefix(tmp_path: Path) -> None:
    base_dir = tmp_path / "h"
    base_dir.mkdir()
    sibling_dir = tmp_path / "handoff_evil"
    sibling_dir.mkdir()
    sibling_summary = _valid_preflight_summary_path(sibling_dir)
    manifest_path = base_dir / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_path,
        preflight_summary=sibling_summary,
        force=True,
    )
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="preflight_summary path escapes base directory",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=base_dir)
    assert exc.value.stage == "validate"


def test_handoff_verifier_relative_base_dir_works_after_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True)
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    relative_base = Path(".") / "bundle"
    monkeypatch.chdir(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=relative_base)
    assert result["path_containment_verified"] is True
    assert result["base_dir"] == str(bundle_dir.resolve())


def test_handoff_verifier_relative_manifest_path_works_with_base_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir(parents=True)
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    relative_manifest = Path(".") / "bundle" / manifest_path.name
    monkeypatch.chdir(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(relative_manifest, base_dir=bundle_dir)
    assert result["status"] == "ok"


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform lacks Path.symlink_to")
def test_handoff_verifier_symlink_resolved_outside_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    link_dir = bundle_dir / "linked"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation not permitted: {exc}")
    manifest_path = bundle_dir / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_path,
        preflight_summary=link_dir / outside_summary.name,
        force=True,
    )
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="preflight_summary path escapes base directory",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc.value.stage == "validate"


def test_handoff_verifier_artifact_containment_checked_before_reading_outside_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    manifest_path = bundle_dir / "handoff_manifest.json"
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_path,
        preflight_summary=outside_summary,
        force=True,
    )
    original_read_bytes = Path.read_bytes

    def _guard_read_bytes(self: Path) -> bytes:
        if self.resolve() == outside_summary.resolve():
            raise AssertionError("outside artifact bytes must not be read before containment check")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _guard_read_bytes)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="preflight_summary path escapes base directory",
    ) as exc:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc.value.stage == "validate"


def test_handoff_verifier_containment_failure_messages_do_not_echo_resolved_paths(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_outside = outside_dir / "handoff_manifest.json"
    artifacts = _all_four_artifact_paths(bundle_dir)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_outside,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_outside),
        "--base-dir",
        str(bundle_dir),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "validate"
    assert error["message"] == "manifest path escapes base directory"
    assert str(manifest_outside.resolve()) not in proc.stdout
    assert str(bundle_dir.resolve()) not in proc.stdout


# --- 3H11: handoff manifest verifier optional verification report ---


_HANDOFF_VERIFY_REPORT_EXPECTED_KEYS = frozenset(
    {
        "version",
        "mode",
        "status",
        "stage",
        "generated_by",
        "manifest",
        "base_dir",
        "path_containment_verified",
        "artifacts_count",
        "verified_artifacts_count",
        "hashes_verified",
        "metadata_verified",
        "schema_verified",
        "commands_execute_in_verifier",
        "review_only",
        "artifact_roles",
    }
)

_VERIFY_SUCCESS_KEYS_WITH_REPORT = _VERIFY_SUCCESS_KEYS | {
    "verification_report_out",
    "verification_report_written",
}

_VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT_AND_REPORT = _VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT | {
    "verification_report_out",
    "verification_report_written",
}

_EXPECTED_ARTIFACT_ROLES = (
    "preflight_summary",
    "plan_md",
    "structured_plan",
    "validation_report",
)


def test_handoff_verifier_happy_path_without_report_out_unchanged(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert result["status"] == "ok"
    assert "verification_report_out" not in result
    assert "verification_report_written" not in result


def test_handoff_verifier_base_dir_without_report_out_unchanged(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    result = verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=tmp_path)
    assert set(result.keys()) == _VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT
    assert "verification_report_out" not in result
    assert "verification_report_written" not in result


def test_handoff_verifier_cli_accepts_verification_report_out(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    assert report_out.is_file()


def test_handoff_verifier_cli_accepts_force_flag(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    written = json.loads(report_out.read_text(encoding="utf-8"))
    assert written["status"] == "ok"


def test_handoff_verifier_wrapper_writes_report_after_success(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    result = run_verify_kr_end_to_end_handoff_manifest(
        manifest_path,
        verification_report_out=report_out,
        force=True,
    )
    assert result["verification_report_written"] is True
    assert result["verification_report_out"] == str(report_out.resolve())
    assert report_out.is_file()


def test_handoff_verifier_force_without_report_out_is_harmless_noop(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli("--manifest", str(manifest_path), "--force", "--json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS
    assert "verification_report_written" not in payload


def test_handoff_verifier_report_out_omitted_preserves_success_keys_without_base_dir(
    tmp_path: Path,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli("--manifest", str(manifest_path), "--json")
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS


def test_handoff_verifier_report_out_omitted_preserves_success_keys_with_base_dir(
    tmp_path: Path,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(tmp_path),
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT


def test_handoff_verifier_report_out_adds_success_keys(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS_WITH_REPORT
    assert payload["verification_report_written"] is True
    assert payload["verification_report_out"] == str(report_out.resolve())


def test_handoff_verifier_report_out_omitted_does_not_add_written_false(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    result = run_verify_kr_end_to_end_handoff_manifest(manifest_path)
    assert "verification_report_written" not in result


def test_handoff_verifier_report_top_level_schema(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert set(report.keys()) == _HANDOFF_VERIFY_REPORT_EXPECTED_KEYS


def test_handoff_verifier_report_mode_is_verification_report_mode(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["mode"] == "kr-end-to-end-handoff-manifest-verification-report"


def test_handoff_verifier_cli_success_mode_unchanged_with_report_out(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert payload["mode"] == "kr-end-to-end-handoff-manifest-verification"


def test_handoff_verifier_report_manifest_path_and_base_dir_null(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["manifest"] == str(manifest_path.resolve())
    assert report["base_dir"] is None
    assert report["path_containment_verified"] is False


def test_handoff_verifier_report_base_dir_and_containment_when_supplied(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(tmp_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["base_dir"] == str(tmp_path.resolve())
    assert report["path_containment_verified"] is True


def test_handoff_verifier_report_artifacts_counts(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["artifacts_count"] == 4
    assert report["verified_artifacts_count"] == 4


def test_handoff_verifier_report_verification_flags_true(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["hashes_verified"] is True
    assert report["metadata_verified"] is True
    assert report["schema_verified"] is True


def test_handoff_verifier_report_review_only_and_no_command_execution(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["commands_execute_in_verifier"] is False
    assert report["review_only"] is True


def test_handoff_verifier_report_artifact_roles_only_not_full_entries(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    assert report["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)
    assert "artifacts" not in report
    for role in _EXPECTED_ARTIFACT_ROLES:
        assert role in report["artifact_roles"]


def test_handoff_verifier_report_contains_no_manifest_body(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_sha = manifest_payload["artifacts"][0]["sha256"]
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8")
    assert artifact_sha not in dumped
    assert "all_artifacts_present" not in dumped
    assert "artifacts" not in json.loads(dumped)


def test_handoff_verifier_report_contains_no_artifact_body(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    secret = "SECRET_ARTIFACT_BODY_NOT_IN_REPORT"
    plan = _valid_plan_md_path(tmp_path)
    plan.write_text(plan.read_text(encoding="utf-8") + f"\n{secret}\n", encoding="utf-8")
    manifest_out = tmp_path / "handoff_manifest_with_secret.json"
    build_kr_end_to_end_handoff_manifest(manifest_out=manifest_out, plan_md=plan, force=True)
    _run_verify_cli(
        "--manifest",
        str(manifest_out),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8")
    assert secret not in dumped


def test_handoff_verifier_report_contains_no_command_lines(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8")
    assert "PYTHONPATH=src" not in dumped
    assert "command" not in json.loads(dumped)


def test_handoff_verifier_failure_does_not_create_report(tmp_path: Path) -> None:
    payload = _loaded_handoff_manifest_dict(tmp_path)
    payload["mode"] = "wrong-mode"
    path = _write_handoff_manifest_dict(tmp_path, payload)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    assert not report_out.exists()


def test_handoff_verifier_invalid_manifest_with_report_out_fails_at_parse_not_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "handoff_manifest.json"
    path.write_text("{not-json", encoding="utf-8")
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"preexisting": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(path),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "parse"
    assert json.loads(report_out.read_text(encoding="utf-8"))["preexisting"] is True


def test_handoff_verifier_containment_failure_does_not_create_report(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_outside = outside_dir / "handoff_manifest.json"
    artifacts = _all_four_artifact_paths(bundle_dir)
    build_kr_end_to_end_handoff_manifest(
        manifest_out=manifest_outside,
        preflight_summary=artifacts["preflight_summary"],
        plan_md=artifacts["plan_md"],
        structured_plan=artifacts["structured_plan"],
        validation_report=artifacts["validation_report"],
        force=True,
    )
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_outside),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    assert not report_out.exists()


def test_handoff_verifier_report_exists_without_force_write_stage(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "write"


def test_handoff_verifier_report_exists_error_uses_field_name_not_path(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    error = json.loads(proc.stdout)
    assert error["message"] == "output already exists: verification_report_out"
    assert str(report_out) not in error["message"]


def test_handoff_verifier_force_overwrites_existing_report(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    written = json.loads(report_out.read_text(encoding="utf-8"))
    assert written["mode"] == "kr-end-to-end-handoff-manifest-verification-report"
    assert "old" not in written


def test_handoff_verifier_report_write_failure_preserves_existing_report(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    original = '{"status":"ok","preserved":true}\n'
    report_out.write_text(original, encoding="utf-8")
    secret = "SECRET_VALUE_TEST"
    summary, roles, resolved_base = _verify_handoff_manifest_with_entries(manifest_path)
    report = _build_verification_report(summary, roles, resolved_base=resolved_base)

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(
            KrEndToEndHandoffManifestVerifyError,
            match="output write failed: PermissionError",
        ) as exc:
            _write_verification_report_output(report_out, report, force=True)
    assert exc.value.stage == "write"
    assert report_out.read_text(encoding="utf-8") == original
    assert secret not in exc.value.message


def test_handoff_verifier_report_write_failure_sanitizes_exception_detail(tmp_path: Path) -> None:
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    secret = "/raw/path/with/SECRET_VALUE_TEST"

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        raise PermissionError(secret)

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc:
            _write_verification_report_output(
                report_out,
                {"version": 1, "mode": "kr-end-to-end-handoff-manifest-verification-report"},
                force=True,
            )
    assert exc.value.message == "output write failed: PermissionError"
    assert secret not in exc.value.message
    assert exc.value.__cause__ is None


def test_handoff_verifier_report_temp_file_cleaned_after_write_failure(tmp_path: Path) -> None:
    report_out = tmp_path / "nested" / "handoff_manifest_verification_report.json"
    report_out.parent.mkdir(parents=True)

    def _raise_permission_error(_self: Path, *_args: object, **_kwargs: object) -> None:
        if _self.name.startswith(".tmp_handoff_verification_report_"):
            raise PermissionError("blocked")
        Path.write_text(_self, *_args, **_kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _raise_permission_error):
        with pytest.raises(KrEndToEndHandoffManifestVerifyError):
            _write_verification_report_output(
                report_out,
                {"version": 1, "mode": "kr-end-to-end-handoff-manifest-verification-report"},
                force=True,
            )
    leftovers = list(report_out.parent.glob(".tmp_handoff_verification_report_*"))
    assert leftovers == []


def test_handoff_verifier_report_temp_file_created_under_report_parent(tmp_path: Path) -> None:
    report_out = tmp_path / "nested" / "handoff_manifest_verification_report.json"
    observed_temp_parent: list[Path] = []
    original_write_text = Path.write_text

    def _capture_temp_write(self: Path, *args: object, **kwargs: object) -> object:
        if self.name.startswith(".tmp_handoff_verification_report_"):
            observed_temp_parent.append(self.parent)
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "write_text", _capture_temp_write):
        _write_verification_report_output(
            report_out,
            {"version": 1, "mode": "kr-end-to-end-handoff-manifest-verification-report", "status": "ok"},
            force=True,
        )
    assert observed_temp_parent == [report_out.parent]
    assert report_out.is_file()


def test_handoff_verifier_cli_report_write_errors_no_traceback(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr


def test_handoff_verifier_report_json_no_endpoint_urls(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8").lower()
    assert "http://" not in dumped
    assert "https://" not in dumped


def test_handoff_verifier_report_json_no_env_api_key_names(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    dumped = report_out.read_text(encoding="utf-8").lower()
    assert "api_key" not in dumped
    assert "fred_api_key" not in dumped
    assert "dart_api_key" not in dumped


def test_handoff_verifier_report_json_no_trading_action_order_allocation_fields(
    tmp_path: Path,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    report = json.loads(report_out.read_text(encoding="utf-8"))
    forbidden = {"action", "order", "allocation", "buy", "sell", "hold", "position", "broker"}
    assert forbidden.isdisjoint(set(report.keys()))


def test_handoff_verifier_blank_verification_report_out_args_stage_cli() -> None:
    proc = _run_verify_cli(
        "--manifest",
        "manifest.json",
        "--verification-report-out",
        "   ",
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "args"
    assert error["message"] == "verification report output path is required"


def test_handoff_verifier_with_base_dir_and_report_adds_all_success_keys(tmp_path: Path) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(tmp_path),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    payload = json.loads(proc.stdout)
    assert set(payload.keys()) == _VERIFY_SUCCESS_KEYS_WITH_CONTAINMENT_AND_REPORT


# --- 3H12: verification report output path containment ---


def test_handoff_verifier_report_out_without_base_dir_writes_outside_base(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    result = run_verify_kr_end_to_end_handoff_manifest(
        manifest_path,
        verification_report_out=report_out,
        force=True,
    )
    assert result["verification_report_written"] is True
    assert report_out.is_file()


def test_handoff_verifier_report_out_inside_base_dir_with_base_dir_passes(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = bundle_dir / "nested" / "handoff_manifest_verification_report.json"
    result = run_verify_kr_end_to_end_handoff_manifest(
        manifest_path,
        base_dir=bundle_dir,
        verification_report_out=report_out,
        force=True,
    )
    assert result["verification_report_written"] is True
    assert report_out.is_file()


def test_handoff_verifier_report_out_outside_base_dir_fails_validate(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification_report_out path escapes base directory",
    ) as exc:
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=bundle_dir,
            verification_report_out=report_out,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_out_outside_base_dir_does_not_create_report(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    assert not report_out.exists()


def test_handoff_verifier_report_out_outside_base_dir_does_not_create_parent_dirs(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    report_out = outside_dir / "nested" / "handoff_manifest_verification_report.json"
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    assert not outside_dir.exists()
    assert not report_out.parent.exists()


def test_handoff_verifier_report_out_containment_checked_after_manifest_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    build_called: list[bool] = []

    original_build = _build_verification_report

    def _track_build(*args: object, **kwargs: object) -> dict[str, object]:
        build_called.append(True)
        return original_build(*args, **kwargs)  # type: ignore[arg-type]

    import verify_kr_end_to_end_handoff_manifest as verify_module

    monkeypatch.setattr(verify_module, "_build_verification_report", _track_build)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification_report_out path escapes base directory",
    ):
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=bundle_dir,
            verification_report_out=report_out,
            force=True,
        )
    assert build_called == []


def test_handoff_verifier_invalid_manifest_with_report_outside_base_fails_parse_not_validate(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    path = bundle_dir / "handoff_manifest.json"
    path.write_text("{not-json", encoding="utf-8")
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "parse"
    assert not report_out.exists()


def test_handoff_verifier_report_out_containment_failure_message(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "validate"
    assert error["message"] == "verification_report_out path escapes base directory"


def test_handoff_verifier_report_out_containment_failure_does_not_echo_report_path(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert str(report_out.resolve()) not in proc.stdout
    assert str(bundle_dir.resolve()) not in proc.stdout
    assert str(report_out.resolve()) not in error["message"]


def test_handoff_verifier_existing_report_out_outside_base_not_write_stage(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = outside_dir / "handoff_manifest_verification_report.json"
    report_out.write_text('{"preexisting": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "validate"
    assert error["message"] == "verification_report_out path escapes base directory"
    assert json.loads(report_out.read_text(encoding="utf-8"))["preexisting"] is True


def test_handoff_verifier_existing_report_out_inside_base_without_force_write_stage(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = bundle_dir / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "write"
    assert error["message"] == "output already exists: verification_report_out"


def test_handoff_verifier_report_out_inside_base_with_force_overwrites(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = bundle_dir / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        "--verification-report-out",
        str(report_out),
        "--force",
        "--json",
    )
    assert proc.returncode == 0
    written = json.loads(report_out.read_text(encoding="utf-8"))
    assert written["mode"] == "kr-end-to-end-handoff-manifest-verification-report"
    assert "old" not in written


def test_handoff_verifier_report_out_sibling_prefix_outside_base_rejected(tmp_path: Path) -> None:
    base_dir = tmp_path / "h"
    base_dir.mkdir()
    sibling_dir = tmp_path / "handoff_evil"
    sibling_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(base_dir)
    report_out = sibling_dir / "handoff_manifest_verification_report.json"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification_report_out path escapes base directory",
    ) as exc:
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=base_dir,
            verification_report_out=report_out,
            force=True,
        )
    assert exc.value.stage == "validate"


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform lacks Path.symlink_to")
def test_handoff_verifier_symlink_report_out_resolved_outside_base_rejected(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_report = outside_dir / "handoff_manifest_verification_report.json"
    link_path = bundle_dir / "report_link.json"
    try:
        link_path.symlink_to(outside_report)
    except OSError as exc:
        pytest.skip(f"symlink creation not permitted: {exc}")
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification_report_out path escapes base directory",
    ) as exc:
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=bundle_dir,
            verification_report_out=link_path,
            force=True,
        )
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_out_containment_uses_resolved_paths_not_string_prefix(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "h"
    base_dir.mkdir()
    sibling_dir = tmp_path / "handoff_evil"
    sibling_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(base_dir)
    report_out = sibling_dir / "handoff_manifest_verification_report.json"
    assert str(report_out).startswith(str(tmp_path / "handoff"))
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification_report_out path escapes base directory",
    ):
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=base_dir,
            verification_report_out=report_out,
            force=True,
        )


def test_handoff_verifier_report_out_parent_not_created_before_containment_succeeds(
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    outside_dir = tmp_path / "outside"
    nested_report = outside_dir / "nested" / "handoff_manifest_verification_report.json"
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    mkdir_calls: list[Path] = []
    original_mkdir = Path.mkdir

    def _track_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        mkdir_calls.append(self)
        return original_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "mkdir", _track_mkdir):
        with pytest.raises(
            KrEndToEndHandoffManifestVerifyError,
            match="verification_report_out path escapes base directory",
        ):
            run_verify_kr_end_to_end_handoff_manifest(
                manifest_path,
                base_dir=bundle_dir,
                verification_report_out=nested_report,
                force=True,
            )
    assert mkdir_calls == []


def test_handoff_verifier_report_out_parent_created_only_for_contained_write(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest_path = _valid_handoff_manifest_path(bundle_dir)
    report_out = bundle_dir / "nested" / "handoff_manifest_verification_report.json"
    assert not report_out.parent.exists()
    run_verify_kr_end_to_end_handoff_manifest(
        manifest_path,
        base_dir=bundle_dir,
        verification_report_out=report_out,
        force=True,
    )
    assert report_out.parent.is_dir()
    assert report_out.is_file()


# --- 3H13: verification report schema self-validation ---


def _built_valid_verification_report(tmp_path: Path) -> dict[str, object]:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    summary, roles, resolved_base = _verify_handoff_manifest_with_entries(manifest_path)
    report = _build_verification_report(summary, roles, resolved_base=resolved_base)
    _validate_verification_report_payload(report)
    return report


def test_handoff_verifier_report_self_validator_accepts_generated_report(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    assert report["mode"] == "kr-end-to-end-handoff-manifest-verification-report"
    assert report["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)


def test_handoff_verifier_report_self_validator_rejects_unknown_top_level_key(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["unexpected"] = True
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report contains unknown fields",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_missing_required_key(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    del report["schema_verified"]
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report missing required fields",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_wrong_mode(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["mode"] = "wrong-mode"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report has invalid mode",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_wrong_status(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["status"] = "error"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="invalid status",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_wrong_stage(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["stage"] = "validate"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="invalid stage",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_wrong_generated_by(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["generated_by"] = "ops/other.py"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="invalid generated_by",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_blank_manifest(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["manifest"] = "   "
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="manifest is required",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_invalid_base_dir_type(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["base_dir"] = 123
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="base_dir must be string or null",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_containment_false_when_base_dir_null(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["path_containment_verified"] = True
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="path_containment_verified must be false when base_dir is null",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_containment_false_when_base_dir_set(
    tmp_path: Path,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    summary, roles, resolved_base = _verify_handoff_manifest_with_entries(manifest_path, base_dir=tmp_path)
    report = _build_verification_report(summary, roles, resolved_base=resolved_base)
    report["path_containment_verified"] = False
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="path_containment_verified must be true when base_dir is set",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_artifacts_count_zero(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["artifacts_count"] = 0
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="artifacts_count must be a positive integer",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_verified_artifacts_count_mismatch(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["verified_artifacts_count"] = 3
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report counts are inconsistent",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_hashes_verified_false(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["hashes_verified"] = False
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="hashes_verified must be true",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_metadata_verified_false(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["metadata_verified"] = False
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="metadata_verified must be true",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_schema_verified_false(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["schema_verified"] = False
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="schema_verified must be true",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_commands_execute_true(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["commands_execute_in_verifier"] = True
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="commands_execute_in_verifier must be false",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_review_only_false(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["review_only"] = False
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="review_only must be true",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_artifact_roles_empty(tmp_path: Path) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["artifact_roles"] = []
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report has invalid artifact roles",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_artifact_roles_unknown_role(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["artifact_roles"] = ["preflight_summary", "unknown_role"]
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="artifact role not recognized",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_artifact_roles_duplicate_role(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["artifact_roles"] = ["preflight_summary", "preflight_summary"]
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="artifact role duplicated",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_self_validator_rejects_artifact_roles_out_of_canonical_order(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["artifact_roles"] = ["plan_md", "preflight_summary"]
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="artifact roles out of canonical order",
    ) as exc:
        _validate_verification_report_payload(report)
    assert exc.value.stage == "validate"


def test_handoff_verifier_report_artifact_roles_validation_reuses_validate_artifact_roles(
    tmp_path: Path,
) -> None:
    report = _built_valid_verification_report(tmp_path)
    report["artifact_roles"] = ["validation_report", "structured_plan"]
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as direct_exc:
        _validate_artifact_roles(report["artifact_roles"])  # type: ignore[arg-type]
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as report_exc:
        _validate_verification_report_payload(report)
    assert direct_exc.value.message == report_exc.value.message
    assert direct_exc.value.stage == report_exc.value.stage == "validate"


def test_handoff_verifier_invalid_generated_report_fails_validate_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    original_build = _build_verification_report

    def _bad_build(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_build(*args, **kwargs)  # type: ignore[arg-type]
        report["unexpected_field"] = True
        return report

    import verify_kr_end_to_end_handoff_manifest as verify_module

    monkeypatch.setattr(verify_module, "_build_verification_report", _bad_build)
    write_called: list[bool] = []
    original_write = _write_verification_report_output

    def _track_write(*args: object, **kwargs: object) -> None:
        write_called.append(True)
        return original_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(verify_module, "_write_verification_report_output", _track_write)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report contains unknown fields",
    ) as exc:
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            verification_report_out=report_out,
            force=True,
        )
    assert exc.value.stage == "validate"
    assert write_called == []
    assert not report_out.exists()


def test_handoff_verifier_invalid_generated_report_with_existing_report_out_fails_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"preexisting": true}\n', encoding="utf-8")
    original_build = _build_verification_report

    def _bad_build(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_build(*args, **kwargs)  # type: ignore[arg-type]
        report["mode"] = "wrong-mode"
        return report

    import verify_kr_end_to_end_handoff_manifest as verify_module

    monkeypatch.setattr(verify_module, "_build_verification_report", _bad_build)
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification report has invalid mode",
    ) as exc:
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            verification_report_out=report_out,
        )
    assert exc.value.stage == "validate"
    assert json.loads(report_out.read_text(encoding="utf-8"))["preexisting"] is True


def test_handoff_verifier_invalid_generated_report_does_not_create_report_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "nested" / "handoff_manifest_verification_report.json"
    original_build = _build_verification_report

    def _bad_build(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_build(*args, **kwargs)  # type: ignore[arg-type]
        report["status"] = "error"
        return report

    import verify_kr_end_to_end_handoff_manifest as verify_module

    monkeypatch.setattr(verify_module, "_build_verification_report", _bad_build)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError):
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            verification_report_out=report_out,
            force=True,
        )
    assert not report_out.exists()


def test_handoff_verifier_invalid_generated_report_does_not_create_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "nested" / "handoff_manifest_verification_report.json"
    original_build = _build_verification_report

    def _bad_build(*args: object, **kwargs: object) -> dict[str, object]:
        report = original_build(*args, **kwargs)  # type: ignore[arg-type]
        report["review_only"] = False
        return report

    import verify_kr_end_to_end_handoff_manifest as verify_module

    monkeypatch.setattr(verify_module, "_build_verification_report", _bad_build)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError):
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            verification_report_out=report_out,
            force=True,
        )
    assert not report_out.parent.exists()


def test_handoff_verifier_valid_report_out_existing_without_force_still_write_stage(
    tmp_path: Path,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"old": true}\n', encoding="utf-8")
    proc = _run_verify_cli(
        "--manifest",
        str(manifest_path),
        "--verification-report-out",
        str(report_out),
        "--json",
    )
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "write"


def test_handoff_verifier_report_self_validation_runs_before_exists_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _valid_handoff_manifest_path(tmp_path)
    report_out = tmp_path / "handoff_manifest_verification_report.json"
    report_out.write_text('{"preexisting": true}\n', encoding="utf-8")
    call_order: list[str] = []
    original_validate = _validate_verification_report_payload
    original_write = _write_verification_report_output

    def _track_validate(report: dict[str, object]) -> None:
        call_order.append("validate")
        return original_validate(report)

    def _track_write(*args: object, **kwargs: object) -> None:
        call_order.append("write")
        return original_write(*args, **kwargs)  # type: ignore[arg-type]

    import verify_kr_end_to_end_handoff_manifest as verify_module

    monkeypatch.setattr(verify_module, "_validate_verification_report_payload", _track_validate)
    monkeypatch.setattr(verify_module, "_write_verification_report_output", _track_write)
    run_verify_kr_end_to_end_handoff_manifest(
        manifest_path,
        verification_report_out=report_out,
        force=True,
    )
    assert call_order == ["validate", "write"]


# --- 3H16: end-to-end handoff bundle round-trip smoke (fixture-only, no-exec) ---


def _path_resolves_inside_base(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _run_handoff_bundle_round_trip(bundle_dir: Path) -> dict[str, object]:
    """checked-in synthetic manifest → bundle_dir 내 handoff 산출물 round-trip(API only, no subprocess)."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    summary_out = bundle_dir / "preflight_summary.json"
    plan_out = bundle_dir / "plan.md"
    structured_plan_out = bundle_dir / "structured_plan.json"
    validation_report_out = bundle_dir / "validation_report.json"
    handoff_manifest_out = bundle_dir / "handoff_manifest.json"
    verification_report_out = bundle_dir / "handoff_manifest_verification_report.json"

    preflight_result = run_kr_end_to_end_preflight(
        MANIFEST_FIXTURE,
        summary_out=summary_out,
        plan_out=plan_out,
        structured_plan_out=structured_plan_out,
        emit_followup_commands=True,
        force=True,
    )
    validator_exit = validate_plan_main(
        [
            "--structured-plan",
            str(structured_plan_out),
            "--report-out",
            str(validation_report_out),
            "--force",
            "--json",
        ]
    )
    build_result = build_kr_end_to_end_handoff_manifest(
        manifest_out=handoff_manifest_out,
        preflight_summary=summary_out,
        plan_md=plan_out,
        structured_plan=structured_plan_out,
        validation_report=validation_report_out,
        base_dir=bundle_dir,
        force=True,
    )
    verify_result = run_verify_kr_end_to_end_handoff_manifest(
        handoff_manifest_out,
        base_dir=bundle_dir,
        verification_report_out=verification_report_out,
        force=True,
    )
    return {
        "bundle_dir": bundle_dir,
        "paths": {
            "preflight_summary": summary_out,
            "plan_md": plan_out,
            "structured_plan": structured_plan_out,
            "validation_report": validation_report_out,
            "handoff_manifest": handoff_manifest_out,
            "verification_report": verification_report_out,
        },
        "preflight_result": preflight_result,
        "validator_exit": validator_exit,
        "build_result": build_result,
        "verify_result": verify_result,
    }


def test_end_to_end_handoff_bundle_round_trip_api_no_exec(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    preflight_result = round_trip["preflight_result"]
    assert isinstance(preflight_result, dict)
    assert preflight_result["status"] == "ok"
    assert preflight_result.get("stage") == "complete"
    assert round_trip["validator_exit"] == 0

    for name, path in paths.items():
        assert isinstance(path, Path)
        assert path.is_file(), f"missing round-trip output: {name}"
        assert _path_resolves_inside_base(path, bundle_dir)

    structured_plan = json.loads(paths["structured_plan"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert structured_plan["mode"] == "kr-end-to-end-intake-followup-plan"
    assert structured_plan["review_only"] is True
    assert structured_plan["steps"]
    for step in structured_plan["steps"]:
        assert step["executes_in_preflight"] is False

    validation_report = json.loads(paths["validation_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert validation_report["mode"] == "kr-end-to-end-preflight-plan-validation-report"
    assert validation_report["status"] == "ok"
    assert validation_report["stage"] == "complete"

    handoff_manifest = json.loads(paths["handoff_manifest"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert set(handoff_manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS
    assert handoff_manifest["mode"] == "kr-end-to-end-handoff-manifest"
    assert handoff_manifest["status"] == "ok"
    assert handoff_manifest["stage"] == "complete"
    assert handoff_manifest["commands_execute_in_builder"] is False
    assert handoff_manifest["review_only"] is True
    assert handoff_manifest["artifacts_count"] == 4
    assert [entry["role"] for entry in handoff_manifest["artifacts"]] == list(_ROLE_ORDER)

    build_result = round_trip["build_result"]
    assert isinstance(build_result, dict)
    assert build_result["status"] == "ok"
    assert build_result["stage"] == "complete"

    verify_result = round_trip["verify_result"]
    assert isinstance(verify_result, dict)
    assert verify_result["status"] == "ok"
    assert verify_result["stage"] == "complete"
    assert verify_result["commands_execute_in_verifier"] is False
    assert verify_result["review_only"] is True
    assert verify_result["path_containment_verified"] is True
    assert verify_result["verification_report_written"] is True

    verification_report = json.loads(paths["verification_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert verification_report["mode"] == "kr-end-to-end-handoff-manifest-verification-report"
    assert verification_report["status"] == "ok"
    assert verification_report["stage"] == "complete"
    assert verification_report["schema_verified"] is True
    assert verification_report["hashes_verified"] is True
    assert verification_report["metadata_verified"] is True
    assert verification_report["commands_execute_in_verifier"] is False
    assert verification_report["review_only"] is True
    assert verification_report["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)


def test_end_to_end_handoff_bundle_round_trip_all_outputs_inside_base_dir(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    handoff_manifest = json.loads(paths["handoff_manifest"].read_text(encoding="utf-8"))  # type: ignore[index]
    for entry in handoff_manifest["artifacts"]:
        assert _path_resolves_inside_base(Path(entry["path"]), bundle_dir)
        assert set(entry.keys()) == _ARTIFACT_ENTRY_KEYS


def test_end_to_end_handoff_bundle_round_trip_outputs_are_body_free(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    handoff_manifest = json.loads(paths["handoff_manifest"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert "steps" not in handoff_manifest
    assert "command" not in handoff_manifest
    assert "followup_commands" not in handoff_manifest
    for entry in handoff_manifest["artifacts"]:
        assert "content" not in entry
        assert "body" not in entry
        assert "command" not in entry
    manifest_dumped = json.dumps(handoff_manifest)
    assert "PYTHONPATH=src" not in manifest_dumped

    validation_report = json.loads(paths["validation_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert "steps" not in validation_report
    assert "commands" not in validation_report
    assert "command" not in validation_report
    assert "artifacts" not in validation_report
    _walk_forbidden_fields(validation_report)

    verification_report = json.loads(paths["verification_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert set(verification_report.keys()) == _HANDOFF_VERIFY_REPORT_EXPECTED_KEYS
    assert "artifacts" not in verification_report
    assert "steps" not in verification_report
    assert "commands" not in verification_report
    assert "command" not in verification_report
    verification_dumped = paths["verification_report"].read_text(encoding="utf-8")  # type: ignore[index]
    assert "https://" not in verification_dumped
    assert "http://" not in verification_dumped
    assert "fred_api_key" not in verification_dumped.lower()
    assert "dart_api_key" not in verification_dumped.lower()
    assert "api_key" not in verification_dumped.lower()
    _walk_forbidden_fields(verification_report)


def test_end_to_end_handoff_bundle_round_trip_rejects_outside_handoff_report(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)
    outside_dir = tmp_path / "outside"
    outside_report = outside_dir / "nested" / "handoff_manifest_verification_report.json"
    with pytest.raises(
        KrEndToEndHandoffManifestVerifyError,
        match="verification_report_out path escapes base directory",
    ) as exc:
        run_verify_kr_end_to_end_handoff_manifest(
            paths["handoff_manifest"],  # type: ignore[arg-type]
            base_dir=bundle_dir,
            verification_report_out=outside_report,
            force=True,
        )
    assert exc.value.stage == "validate"
    assert not outside_dir.exists()
    assert not outside_report.parent.exists()


def test_end_to_end_handoff_bundle_round_trip_rejects_outside_builder_artifact(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    outside_manifest = outside_dir / "nested" / "handoff_manifest.json"
    with pytest.raises(
        KrEndToEndHandoffManifestError,
        match="preflight_summary artifact path escapes base directory",
    ) as exc:
        build_kr_end_to_end_handoff_manifest(
            manifest_out=paths["handoff_manifest"],  # type: ignore[arg-type]
            preflight_summary=outside_summary,
            plan_md=paths["plan_md"],  # type: ignore[arg-type]
            structured_plan=paths["structured_plan"],  # type: ignore[arg-type]
            validation_report=paths["validation_report"],  # type: ignore[arg-type]
            base_dir=bundle_dir,
            force=True,
        )
    assert exc.value.stage == "validate"
    assert not outside_manifest.parent.exists()


def test_end_to_end_handoff_bundle_round_trip_no_generated_commands_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("handoff bundle round-trip must not execute generated commands")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    preflight_result = round_trip["preflight_result"]
    assert isinstance(preflight_result, dict)
    assert preflight_result["status"] == "ok"
    verify_result = round_trip["verify_result"]
    assert isinstance(verify_result, dict)
    assert verify_result["status"] == "ok"


# --- 3H17: in-process CLI handoff bundle round-trip smoke ---


def _run_handoff_bundle_cli_round_trip(
    bundle_dir: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    """3H17 — checked-in 합성 manifest를 네 CLI main([...]) in-process 호출만으로 한 바퀴 돌린다.

    subprocess/os.system/exec/eval을 일절 쓰지 않고, operator가 실제로 입력하는 CLI 인자
    와이어링(플래그 파싱 → 산출물 경로 → base-dir containment)을 끝에서 끝까지 검증한다.
    생성 산출물 6종은 모두 bundle_dir 내부에 머문다.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    summary_out = bundle_dir / "preflight_summary.json"
    plan_out = bundle_dir / "plan.md"
    structured_plan_out = bundle_dir / "structured_plan.json"
    validation_report_out = bundle_dir / "validation_report.json"
    handoff_manifest_out = bundle_dir / "handoff_manifest.json"
    verification_report_out = bundle_dir / "handoff_manifest_verification_report.json"

    # 1) preflight CLI: [outputs] 섹션 없는 합성 fixture이므로 세 출력 경로를 모두 명시한다.
    preflight_rc = preflight_main(
        [
            "--manifest",
            str(MANIFEST_FIXTURE),
            "--summary-out",
            str(summary_out),
            "--plan-out",
            str(plan_out),
            "--structured-plan-out",
            str(structured_plan_out),
            "--emit-followup-commands",
            "--force",
            "--json",
        ]
    )
    preflight_stdout = capsys.readouterr().out

    # 2) validator CLI: 구조화 plan을 검증하고 validation_report를 in-process로 기록한다.
    validator_rc = validate_plan_main(
        [
            "--structured-plan",
            str(structured_plan_out),
            "--report-out",
            str(validation_report_out),
            "--force",
            "--json",
        ]
    )
    validator_stdout = capsys.readouterr().out

    # 3) builder CLI: 네 산출물을 base-dir containment 하에 handoff manifest로 묶는다.
    build_rc = build_handoff_manifest_main(
        [
            "--preflight-summary",
            str(summary_out),
            "--plan-md",
            str(plan_out),
            "--structured-plan",
            str(structured_plan_out),
            "--validation-report",
            str(validation_report_out),
            "--manifest-out",
            str(handoff_manifest_out),
            "--base-dir",
            str(bundle_dir),
            "--force",
            "--json",
        ]
    )
    build_stdout = capsys.readouterr().out

    # 4) verifier CLI: manifest를 검증하고 검증 리포트를 base-dir 내부에 기록한다.
    verify_rc = verify_handoff_manifest_main(
        [
            "--manifest",
            str(handoff_manifest_out),
            "--base-dir",
            str(bundle_dir),
            "--verification-report-out",
            str(verification_report_out),
            "--force",
            "--json",
        ]
    )
    verify_stdout = capsys.readouterr().out

    return {
        "bundle_dir": bundle_dir,
        "paths": {
            "preflight_summary": summary_out,
            "plan_md": plan_out,
            "structured_plan": structured_plan_out,
            "validation_report": validation_report_out,
            "handoff_manifest": handoff_manifest_out,
            "verification_report": verification_report_out,
        },
        "returncodes": {
            "preflight": preflight_rc,
            "validator": validator_rc,
            "build": build_rc,
            "verify": verify_rc,
        },
        "stdout": {
            "preflight": preflight_stdout,
            "validator": validator_stdout,
            "build": build_stdout,
            "verify": verify_stdout,
        },
    }


def test_end_to_end_handoff_bundle_cli_round_trip_in_process_no_exec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "cli_bundle"
    round_trip = _run_handoff_bundle_cli_round_trip(bundle_dir, capsys)

    returncodes = round_trip["returncodes"]
    assert isinstance(returncodes, dict)
    assert returncodes == {"preflight": 0, "validator": 0, "build": 0, "verify": 0}

    paths = round_trip["paths"]
    assert isinstance(paths, dict)
    for name, path in paths.items():
        assert isinstance(path, Path)
        assert path.is_file(), f"missing CLI round-trip output: {name}"
        assert _path_resolves_inside_base(path, bundle_dir)

    preflight_summary = json.loads(paths["preflight_summary"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert preflight_summary["status"] == "ok"
    if "stage" in preflight_summary:
        assert preflight_summary["stage"] == "complete"

    structured_plan = json.loads(paths["structured_plan"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert structured_plan["mode"] == "kr-end-to-end-intake-followup-plan"
    assert structured_plan["review_only"] is True
    assert structured_plan["steps"]
    for step in structured_plan["steps"]:
        assert step["executes_in_preflight"] is False

    validation_report = json.loads(paths["validation_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert validation_report["mode"] == "kr-end-to-end-preflight-plan-validation-report"
    assert validation_report["status"] == "ok"
    assert validation_report["stage"] == "complete"

    handoff_manifest = json.loads(paths["handoff_manifest"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert set(handoff_manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS
    assert handoff_manifest["mode"] == "kr-end-to-end-handoff-manifest"
    assert handoff_manifest["status"] == "ok"
    assert handoff_manifest["stage"] == "complete"
    assert handoff_manifest["commands_execute_in_builder"] is False
    assert handoff_manifest["review_only"] is True
    assert handoff_manifest["artifacts_count"] == 4
    assert [entry["role"] for entry in handoff_manifest["artifacts"]] == list(_ROLE_ORDER)

    verification_report = json.loads(paths["verification_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert verification_report["mode"] == "kr-end-to-end-handoff-manifest-verification-report"
    assert verification_report["status"] == "ok"
    assert verification_report["stage"] == "complete"
    assert verification_report["schema_verified"] is True
    assert verification_report["hashes_verified"] is True
    assert verification_report["metadata_verified"] is True
    assert verification_report["commands_execute_in_verifier"] is False
    assert verification_report["review_only"] is True
    assert verification_report["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)

    # --json 성공 stdout은 파일 본문이 아니라 상태/리턴 페이로드 확인용으로만 사용한다.
    verify_payload = json.loads(round_trip["stdout"]["verify"])  # type: ignore[index]
    assert verify_payload["status"] == "ok"


def test_end_to_end_handoff_bundle_cli_round_trip_all_outputs_inside_base_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "cli_bundle"
    round_trip = _run_handoff_bundle_cli_round_trip(bundle_dir, capsys)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    handoff_manifest = json.loads(paths["handoff_manifest"].read_text(encoding="utf-8"))  # type: ignore[index]
    for entry in handoff_manifest["artifacts"]:
        assert _path_resolves_inside_base(Path(entry["path"]), bundle_dir)
        assert set(entry.keys()) == _ARTIFACT_ENTRY_KEYS


def test_end_to_end_handoff_bundle_cli_round_trip_outputs_are_body_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "cli_bundle"
    round_trip = _run_handoff_bundle_cli_round_trip(bundle_dir, capsys)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    handoff_manifest = json.loads(paths["handoff_manifest"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert "steps" not in handoff_manifest
    assert "command" not in handoff_manifest
    assert "followup_commands" not in handoff_manifest
    for entry in handoff_manifest["artifacts"]:
        assert "content" not in entry
        assert "body" not in entry
        assert "command" not in entry
    manifest_dumped = json.dumps(handoff_manifest)
    assert "PYTHONPATH=src" not in manifest_dumped

    validation_report = json.loads(paths["validation_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert "steps" not in validation_report
    assert "commands" not in validation_report
    assert "command" not in validation_report
    assert "artifacts" not in validation_report
    _walk_forbidden_fields(validation_report)

    verification_report = json.loads(paths["verification_report"].read_text(encoding="utf-8"))  # type: ignore[index]
    assert set(verification_report.keys()) == _HANDOFF_VERIFY_REPORT_EXPECTED_KEYS
    assert "artifacts" not in verification_report
    assert "steps" not in verification_report
    assert "commands" not in verification_report
    assert "command" not in verification_report
    verification_dumped = paths["verification_report"].read_text(encoding="utf-8")  # type: ignore[index]
    assert "https://" not in verification_dumped
    assert "http://" not in verification_dumped
    assert "fred_api_key" not in verification_dumped.lower()
    assert "dart_api_key" not in verification_dumped.lower()
    assert "api_key" not in verification_dumped.lower()
    _walk_forbidden_fields(verification_report)


def test_end_to_end_handoff_bundle_cli_round_trip_rejects_outside_handoff_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "cli_bundle"
    round_trip = _run_handoff_bundle_cli_round_trip(bundle_dir, capsys)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    outside_dir = tmp_path / "outside"
    outside_report = outside_dir / "nested" / "handoff_manifest_verification_report.json"
    # known-error CLI 규약: 도메인 예외를 재전파하지 않고 rc==1을 반환한다(pytest.raises 금지).
    rc = verify_handoff_manifest_main(
        [
            "--manifest",
            str(paths["handoff_manifest"]),
            "--base-dir",
            str(bundle_dir),
            "--verification-report-out",
            str(outside_report),
            "--force",
            "--json",
        ]
    )
    error_payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert error_payload["status"] == "error"
    assert error_payload["stage"] == "validate"
    assert not outside_dir.exists()
    assert not outside_report.parent.exists()


def test_end_to_end_handoff_bundle_cli_round_trip_rejects_outside_builder_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "cli_bundle"
    round_trip = _run_handoff_bundle_cli_round_trip(bundle_dir, capsys)
    paths = round_trip["paths"]
    assert isinstance(paths, dict)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = _valid_preflight_summary_path(outside_dir)
    outside_manifest = outside_dir / "nested" / "handoff_manifest.json"
    # base-dir 밖 입력 산출물은 containment 단계에서 거부되며 rc==1이다.
    rc = build_handoff_manifest_main(
        [
            "--preflight-summary",
            str(outside_summary),
            "--plan-md",
            str(paths["plan_md"]),
            "--structured-plan",
            str(paths["structured_plan"]),
            "--validation-report",
            str(paths["validation_report"]),
            "--manifest-out",
            str(outside_manifest),
            "--base-dir",
            str(bundle_dir),
            "--force",
            "--json",
        ]
    )
    error_payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert error_payload["status"] == "error"
    assert error_payload["stage"] == "validate"
    assert not outside_manifest.parent.exists()


def test_end_to_end_handoff_bundle_cli_round_trip_no_generated_commands_executed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("CLI handoff bundle round-trip must not execute generated commands")

    # subprocess.run을 폭파시켜 in-process CLI 경로가 어떤 명령도 실행하지 않음을 증명한다.
    monkeypatch.setattr(subprocess, "run", _fail_run)
    bundle_dir = tmp_path / "cli_bundle"
    round_trip = _run_handoff_bundle_cli_round_trip(bundle_dir, capsys)
    returncodes = round_trip["returncodes"]
    assert isinstance(returncodes, dict)
    assert returncodes == {"preflight": 0, "validator": 0, "build": 0, "verify": 0}


# --- 3H18: API/CLI handoff bundle round-trip parity ---------------------------
#
# 3H16은 API-only, 3H17은 CLI-main wiring을 각각 검증한다. 두 경로가 같은 artifact
# 역할/스키마/메타데이터/containment 의미를 내는지는 비교하지 않았으므로, 한쪽만 drift해도
# 둘 다 "통과"할 수 있다. 3H18은 두 round-trip 산출물을 경로/해시 비의존 의미 요약으로
# 정규화해 동등성을 단언한다. 절대경로·sha256 값·base_dir 문자열은 bundle마다 다를 수
# 있으므로 교차 비교하지 않는다(값이 아니라 shape/존재/부호만 비교).

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _normalize_handoff_manifest_for_parity(payload: dict[str, object]) -> dict[str, object]:
    """handoff manifest를 경로/해시 비의존 의미 요약으로 정규화한다."""
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    return {
        "top_keys": sorted(payload.keys()),
        "version": payload["version"],
        "mode": payload["mode"],
        "status": payload["status"],
        "stage": payload["stage"],
        "generated_by": payload["generated_by"],
        "artifacts_count": payload["artifacts_count"],
        "all_artifacts_present": payload["all_artifacts_present"],
        "commands_execute_in_builder": payload["commands_execute_in_builder"],
        "review_only": payload["review_only"],
        "roles": [entry["role"] for entry in artifacts],
        "kinds": [entry["kind"] for entry in artifacts],
        "exists_flags": [entry["exists"] for entry in artifacts],
        "entry_keys": [sorted(entry.keys()) for entry in artifacts],
        "json_modes": [entry["json_mode"] for entry in artifacts],
        "json_statuses": [entry["json_status"] for entry in artifacts],
        "json_stages": [entry["json_stage"] for entry in artifacts],
        # 해시는 값이 아니라 64자 소문자 hex shape만, 크기는 양수 여부만 비교한다.
        "sha256_shape": [bool(_SHA256_RE.match(str(entry["sha256"]))) for entry in artifacts],
        "size_positive": [int(entry["size_bytes"]) > 0 for entry in artifacts],
    }


def _normalize_verification_report_for_parity(payload: dict[str, object]) -> dict[str, object]:
    """verification report를 경로/해시 비의존 의미 요약으로 정규화한다."""
    return {
        "keys": sorted(payload.keys()),
        "version": payload["version"],
        "mode": payload["mode"],
        "status": payload["status"],
        "stage": payload["stage"],
        "generated_by": payload["generated_by"],
        "path_containment_verified": payload["path_containment_verified"],
        "artifacts_count": payload["artifacts_count"],
        "verified_artifacts_count": payload["verified_artifacts_count"],
        "hashes_verified": payload["hashes_verified"],
        "metadata_verified": payload["metadata_verified"],
        "schema_verified": payload["schema_verified"],
        "commands_execute_in_verifier": payload["commands_execute_in_verifier"],
        "review_only": payload["review_only"],
        "artifact_roles": payload["artifact_roles"],
        # base_dir 문자열은 bundle마다 다르므로 존재 여부만 비교한다.
        "base_dir_present": payload["base_dir"] is not None,
    }


def _load_api_and_cli_handoff_bundles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    """API round-trip과 CLI round-trip을 별도 bundle에 돌리고 산출물을 로드한다."""
    api_bundle = tmp_path / "api_bundle"
    cli_bundle = tmp_path / "cli_bundle"
    api_round_trip = _run_handoff_bundle_round_trip(api_bundle)
    cli_round_trip = _run_handoff_bundle_cli_round_trip(cli_bundle, capsys)

    api_paths = api_round_trip["paths"]
    cli_paths = cli_round_trip["paths"]
    assert isinstance(api_paths, dict)
    assert isinstance(cli_paths, dict)

    return {
        "api_bundle": api_bundle,
        "cli_bundle": cli_bundle,
        "api_manifest": json.loads(api_paths["handoff_manifest"].read_text(encoding="utf-8")),
        "cli_manifest": json.loads(cli_paths["handoff_manifest"].read_text(encoding="utf-8")),
        "api_report": json.loads(api_paths["verification_report"].read_text(encoding="utf-8")),
        "cli_report": json.loads(cli_paths["verification_report"].read_text(encoding="utf-8")),
    }


def test_end_to_end_handoff_bundle_api_cli_round_trip_manifest_parity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundles = _load_api_and_cli_handoff_bundles(tmp_path, capsys)
    api_norm = _normalize_handoff_manifest_for_parity(bundles["api_manifest"])  # type: ignore[arg-type]
    cli_norm = _normalize_handoff_manifest_for_parity(bundles["cli_manifest"])  # type: ignore[arg-type]

    # 두 경로의 의미 요약이 완전히 동일해야 한다(역할/종류/모드/플래그/스키마/해시 shape).
    assert api_norm == cli_norm
    assert api_norm["top_keys"] == sorted(_MANIFEST_EXPECTED_TOP_KEYS)
    assert api_norm["mode"] == "kr-end-to-end-handoff-manifest"
    assert api_norm["roles"] == list(_ROLE_ORDER)
    assert api_norm["artifacts_count"] == 4
    assert api_norm["commands_execute_in_builder"] is False
    assert api_norm["review_only"] is True
    assert all(api_norm["sha256_shape"])  # type: ignore[arg-type]
    assert all(api_norm["size_positive"])  # type: ignore[arg-type]
    assert all(keys == sorted(_ARTIFACT_ENTRY_KEYS) for keys in api_norm["entry_keys"])  # type: ignore[union-attr]


def test_end_to_end_handoff_bundle_api_cli_round_trip_verification_report_parity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundles = _load_api_and_cli_handoff_bundles(tmp_path, capsys)
    api_norm = _normalize_verification_report_for_parity(bundles["api_report"])  # type: ignore[arg-type]
    cli_norm = _normalize_verification_report_for_parity(bundles["cli_report"])  # type: ignore[arg-type]

    assert api_norm == cli_norm
    assert api_norm["keys"] == sorted(_HANDOFF_VERIFY_REPORT_EXPECTED_KEYS)
    assert api_norm["mode"] == "kr-end-to-end-handoff-manifest-verification-report"
    assert api_norm["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)
    assert api_norm["path_containment_verified"] is True
    assert api_norm["hashes_verified"] is True
    assert api_norm["metadata_verified"] is True
    assert api_norm["schema_verified"] is True
    assert api_norm["commands_execute_in_verifier"] is False
    assert api_norm["review_only"] is True
    assert api_norm["base_dir_present"] is True


def test_end_to_end_handoff_bundle_api_cli_round_trip_path_containment_parity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundles = _load_api_and_cli_handoff_bundles(tmp_path, capsys)
    # containment은 정규화 dict가 아니라 각 bundle별로 따로 단언한다(절대경로 교차 비교 금지).
    for manifest_key, bundle_key in (
        ("api_manifest", "api_bundle"),
        ("cli_manifest", "cli_bundle"),
    ):
        manifest = bundles[manifest_key]
        bundle = bundles[bundle_key]
        assert isinstance(manifest, dict)
        assert isinstance(bundle, Path)
        for entry in manifest["artifacts"]:
            assert _path_resolves_inside_base(Path(entry["path"]), bundle)

    for report_key, bundle_key in (
        ("api_report", "api_bundle"),
        ("cli_report", "cli_bundle"),
    ):
        report = bundles[report_key]
        bundle = bundles[bundle_key]
        assert isinstance(report, dict)
        assert isinstance(bundle, Path)
        assert report["base_dir"] is not None
        assert _path_resolves_inside_base(Path(report["base_dir"]), bundle)


def test_end_to_end_handoff_bundle_api_cli_round_trip_outputs_body_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundles = _load_api_and_cli_handoff_bundles(tmp_path, capsys)
    for manifest_key in ("api_manifest", "cli_manifest"):
        manifest = bundles[manifest_key]
        assert isinstance(manifest, dict)
        assert "steps" not in manifest
        assert "command" not in manifest
        assert "commands" not in manifest
        assert "followup_commands" not in manifest
        for entry in manifest["artifacts"]:
            assert "content" not in entry
            assert "body" not in entry
            assert "command" not in entry
            assert "commands" not in entry
        _walk_forbidden_fields(manifest)

    for report_key in ("api_report", "cli_report"):
        report = bundles[report_key]
        assert isinstance(report, dict)
        assert "artifacts" not in report
        assert "steps" not in report
        assert "command" not in report
        assert "commands" not in report
        _walk_forbidden_fields(report)


def test_end_to_end_handoff_bundle_api_cli_round_trip_no_generated_commands_executed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("API/CLI parity round-trip must not execute generated commands")

    # subprocess.run을 폭파시켜 두 in-process round-trip(신규 헬퍼)만으로 parity가 성립함을
    # 증명한다. 구형 subprocess 기반 헬퍼(_run_handoff_manifest_cli)는 호출하지 않는다.
    monkeypatch.setattr(subprocess, "run", _fail_run)
    bundles = _load_api_and_cli_handoff_bundles(tmp_path, capsys)
    api_norm = _normalize_handoff_manifest_for_parity(bundles["api_manifest"])  # type: ignore[arg-type]
    cli_norm = _normalize_handoff_manifest_for_parity(bundles["cli_manifest"])  # type: ignore[arg-type]
    assert api_norm == cli_norm


# --- 3H19: generated handoff bundle tamper-detection smoke -------------------


def _handoff_manifest_path(round_trip: dict[str, object]) -> Path:
    paths = round_trip["paths"]
    assert isinstance(paths, dict)
    manifest_path = paths["handoff_manifest"]
    assert isinstance(manifest_path, Path)
    return manifest_path


def _verification_report_path(round_trip: dict[str, object]) -> Path:
    paths = round_trip["paths"]
    assert isinstance(paths, dict)
    report_path = paths["verification_report"]
    assert isinstance(report_path, Path)
    return report_path


def _artifact_path_by_role(manifest: dict[str, object], role: str) -> Path:
    artifacts = manifest.get("artifacts")
    assert isinstance(artifacts, list)
    for entry in artifacts:
        if isinstance(entry, dict) and entry.get("role") == role:
            path_text = entry.get("path")
            assert isinstance(path_text, str)
            return Path(path_text)
    raise AssertionError(f"artifact role not found in manifest: {role}")


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: dict[str, object], *, indent: int | None = None) -> None:
    if indent is None:
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")


def _refresh_manifest_entry_integrity(manifest_path: Path, role: str) -> None:
    """artifact 파일 변경 후 manifest entry size/sha256만 현재 바이트에 맞춘다."""
    payload = _load_json(manifest_path)
    _refresh_manifest_integrity_for_role(payload, role)
    _write_json(manifest_path, payload, indent=2)


def _generated_handoff_bundle(
    tmp_path: Path,
    *,
    capsys: pytest.CaptureFixture[str] | None = None,
) -> tuple[Path, Path]:
    """3H16 API round-trip으로 유효 handoff bundle을 생성한다(이미 검증 완료).

    capsys가 주어지면 round-trip 중 validate_plan_main --json stdout을 비워
    이후 verifier CLI --json 파싱과 섞이지 않게 한다.
    """
    bundle_dir = tmp_path / "bundle"
    round_trip = _run_handoff_bundle_round_trip(bundle_dir)
    manifest_path = _handoff_manifest_path(round_trip)
    if capsys is not None:
        capsys.readouterr()
    return bundle_dir, manifest_path


def test_handoff_bundle_tamper_modified_artifact_integrity_fails_validate(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n# tampered-appendix\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"


def test_handoff_bundle_tamper_deleted_artifact_fails_validate(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    summary_path = _artifact_path_by_role(manifest, "preflight_summary")
    summary_path.unlink()
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"


def test_handoff_bundle_tamper_malformed_json_artifact_fails_parse_after_integrity_refresh(
    tmp_path: Path,
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    structured_path = _artifact_path_by_role(manifest, "structured_plan")
    structured_path.write_text("{not-valid-json", encoding="utf-8")
    _refresh_manifest_entry_integrity(manifest_path, "structured_plan")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "parse"


def test_handoff_bundle_tamper_structured_plan_mode_drift_fails_validate_after_integrity_refresh(
    tmp_path: Path,
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    structured_path = _artifact_path_by_role(manifest, "structured_plan")
    structured_payload = _load_json(structured_path)
    structured_payload["mode"] = "tampered-mode-label"
    _write_json(structured_path, structured_payload)
    _refresh_manifest_entry_integrity(manifest_path, "structured_plan")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"


def test_handoff_bundle_tamper_validation_report_status_drift_fails_validate_after_integrity_refresh(
    tmp_path: Path,
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    report_path = _artifact_path_by_role(manifest, "validation_report")
    report_payload = _load_json(report_path)
    report_payload["status"] = "tampered"
    _write_json(report_path, report_payload)
    _refresh_manifest_entry_integrity(manifest_path, "validation_report")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"


def test_handoff_bundle_tamper_manifest_recorded_sha_mismatch_fails_validate(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    payload = _load_json(manifest_path)
    payload["artifacts"][0]["sha256"] = "b" * 64  # type: ignore[index]
    _write_json(manifest_path, payload, indent=2)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"


def test_handoff_bundle_tamper_manifest_recorded_size_mismatch_fails_validate(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    payload = _load_json(manifest_path)
    payload["artifacts"][0]["size_bytes"] = 1  # type: ignore[index]
    _write_json(manifest_path, payload, indent=2)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"


def test_handoff_bundle_tamper_artifact_path_outside_base_fails_validate(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = outside_dir / "outside_summary.json"
    outside_summary.write_text('{"status": "tampered"}\n', encoding="utf-8")
    payload = _load_json(manifest_path)
    payload["artifacts"][0]["path"] = str(outside_summary)  # type: ignore[index]
    _write_json(manifest_path, payload, indent=2)
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"
    assert "path escapes base directory" in exc_info.value.message


def test_handoff_bundle_tamper_verification_report_not_written_on_failure(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text("# tampered plan body\n", encoding="utf-8")
    report_out = bundle_dir / "tampered_verification_report.json"
    with pytest.raises(KrEndToEndHandoffManifestVerifyError):
        run_verify_kr_end_to_end_handoff_manifest(
            manifest_path,
            base_dir=bundle_dir,
            verification_report_out=report_out,
            force=True,
        )
    assert not report_out.exists()


def test_handoff_bundle_tamper_failure_does_not_echo_artifact_body(tmp_path: Path) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path)
    manifest = _load_json(manifest_path)
    marker = "UNIQUE_TAMPER_MARKER_3H19"
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text(f"# plan\n{marker}\n", encoding="utf-8")
    with pytest.raises(KrEndToEndHandoffManifestVerifyError) as exc_info:
        verify_kr_end_to_end_handoff_manifest(manifest_path, base_dir=bundle_dir)
    assert exc_info.value.stage == "validate"
    assert marker not in exc_info.value.message


# --- 3H20: CLI verifier tamper-rejection smoke --------------------------------
#
# 3H19는 API 경로(verify_kr_end_to_end_handoff_manifest / pytest.raises)만 검증한다.
# operator-facing verifier CLI main([...])는 rc==1 + --json stdout 페이로드로 동일 거부를
# 내야 하며, pytest.raises 없이 in-process만 호출한다(subprocess 금지).


def _verify_cli_args(manifest_path: Path, bundle_dir: Path, *extra: str) -> list[str]:
    return [
        "--manifest",
        str(manifest_path),
        "--base-dir",
        str(bundle_dir),
        *extra,
        "--json",
    ]


def _run_cli_main_json(
    main_func,
    args: list[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, dict[str, object], str, str]:
    rc = main_func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    return rc, payload, captured.out, captured.err


def _assert_cli_error_payload(
    payload: dict[str, object],
    *,
    mode: str,
    stage: str,
) -> None:
    assert payload["status"] == "error"
    assert payload["stage"] == stage
    assert payload["mode"] == mode
    assert isinstance(payload["message"], str)
    assert payload["message"]


def _run_verify_cli_main_json(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object], str, str]:
    return _run_cli_main_json(verify_handoff_manifest_main, args, capsys)


def _assert_verify_cli_error_payload(payload: dict[str, object], *, stage: str) -> None:
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-handoff-manifest-verification",
        stage=stage,
    )


def _handoff_bundle_through_validator_cli(
    bundle_dir: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, Path]:
    """preflight + validator CLI만 실행해 builder/verifier 이전 산출물을 만든다."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    summary_out = bundle_dir / "preflight_summary.json"
    plan_out = bundle_dir / "plan.md"
    structured_plan_out = bundle_dir / "structured_plan.json"
    validation_report_out = bundle_dir / "validation_report.json"

    preflight_rc = preflight_main(
        [
            "--manifest",
            str(MANIFEST_FIXTURE),
            "--summary-out",
            str(summary_out),
            "--plan-out",
            str(plan_out),
            "--structured-plan-out",
            str(structured_plan_out),
            "--emit-followup-commands",
            "--force",
            "--json",
        ]
    )
    assert preflight_rc == 0
    capsys.readouterr()

    validator_rc = validate_plan_main(
        [
            "--structured-plan",
            str(structured_plan_out),
            "--report-out",
            str(validation_report_out),
            "--force",
            "--json",
        ]
    )
    assert validator_rc == 0
    capsys.readouterr()

    return {
        "preflight_summary": summary_out,
        "plan_md": plan_out,
        "structured_plan": structured_plan_out,
        "validation_report": validation_report_out,
    }


def test_handoff_bundle_tamper_cli_modified_artifact_integrity_returns_validate_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "\n# tampered-appendix\n", encoding="utf-8")
    rc, payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="validate")


def test_handoff_bundle_tamper_cli_deleted_artifact_returns_validate_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    summary_path = _artifact_path_by_role(manifest, "preflight_summary")
    summary_path.unlink()
    rc, payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="validate")


def test_handoff_bundle_tamper_cli_malformed_json_returns_parse_error_after_integrity_refresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    structured_path = _artifact_path_by_role(manifest, "structured_plan")
    structured_path.write_text("{not-valid-json", encoding="utf-8")
    _refresh_manifest_entry_integrity(manifest_path, "structured_plan")
    rc, payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="parse")


def test_handoff_bundle_tamper_cli_structured_plan_mode_drift_returns_validate_error_after_integrity_refresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    structured_path = _artifact_path_by_role(manifest, "structured_plan")
    structured_payload = _load_json(structured_path)
    structured_payload["mode"] = "tampered-mode-label"
    _write_json(structured_path, structured_payload)
    _refresh_manifest_entry_integrity(manifest_path, "structured_plan")
    rc, payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="validate")


def test_handoff_bundle_tamper_cli_validation_report_status_drift_returns_validate_error_after_integrity_refresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    report_path = _artifact_path_by_role(manifest, "validation_report")
    report_payload = _load_json(report_path)
    report_payload["status"] = "tampered"
    _write_json(report_path, report_payload)
    _refresh_manifest_entry_integrity(manifest_path, "validation_report")
    rc, payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="validate")


def test_handoff_bundle_tamper_cli_manifest_recorded_sha_mismatch_returns_validate_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    payload = _load_json(manifest_path)
    payload["artifacts"][0]["sha256"] = "b" * 64  # type: ignore[index]
    _write_json(manifest_path, payload, indent=2)
    rc, cli_payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(cli_payload, stage="validate")


def test_handoff_bundle_tamper_cli_manifest_recorded_size_mismatch_returns_validate_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    payload = _load_json(manifest_path)
    payload["artifacts"][0]["size_bytes"] = 1  # type: ignore[index]
    _write_json(manifest_path, payload, indent=2)
    rc, cli_payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(cli_payload, stage="validate")


def test_handoff_bundle_tamper_cli_artifact_path_outside_base_returns_validate_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_summary = outside_dir / "outside_summary.json"
    outside_summary.write_text('{"status": "tampered"}\n', encoding="utf-8")
    payload = _load_json(manifest_path)
    payload["artifacts"][0]["path"] = str(outside_summary)  # type: ignore[index]
    _write_json(manifest_path, payload, indent=2)
    rc, cli_payload, _, _ = _run_verify_cli_main_json(_verify_cli_args(manifest_path, bundle_dir), capsys)
    assert rc == 1
    _assert_verify_cli_error_payload(cli_payload, stage="validate")


def test_handoff_bundle_tamper_cli_verification_report_not_written_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text("# tampered plan body\n", encoding="utf-8")
    report_out = bundle_dir / "tampered_cli_verification_report.json"
    rc, payload, _, _ = _run_verify_cli_main_json(
        _verify_cli_args(
            manifest_path,
            bundle_dir,
            "--verification-report-out",
            str(report_out),
            "--force",
        ),
        capsys,
    )
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="validate")
    assert not report_out.exists()


def test_handoff_bundle_tamper_cli_error_payload_does_not_echo_artifact_body_or_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    marker = "UNIQUE_TAMPER_MARKER_3H20"
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text(f"# plan\n{marker}\n", encoding="utf-8")
    rc, payload, stdout, stderr = _run_verify_cli_main_json(
        _verify_cli_args(manifest_path, bundle_dir),
        capsys,
    )
    assert rc == 1
    _assert_verify_cli_error_payload(payload, stage="validate")
    message = payload["message"]
    assert isinstance(message, str)
    assert marker not in message
    assert marker not in stdout
    assert marker not in stderr


# --- 3H21: handoff pipeline failure no-partial-output smoke (in-process CLI) ---
#
# happy path(3H16–3H18)와 tamper 거부(3H19–3H20)는 검증했지만, upstream 단계 실패 시
# downstream 산출물·부모 디렉터리가 남지 않는 fail-closed 계약을 pipeline 관점에서 묶는다.


def test_handoff_pipeline_validator_failure_does_not_write_validation_report_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    structured_plan_out = bundle_dir / "structured_plan.json"
    plan_out = bundle_dir / "plan.md"
    summary_out = bundle_dir / "preflight_summary.json"
    preflight_rc = preflight_main(
        [
            "--manifest",
            str(MANIFEST_FIXTURE),
            "--summary-out",
            str(summary_out),
            "--plan-out",
            str(plan_out),
            "--structured-plan-out",
            str(structured_plan_out),
            "--emit-followup-commands",
            "--force",
            "--json",
        ]
    )
    assert preflight_rc == 0
    capsys.readouterr()

    structured_plan_out.write_text("{not-valid-json", encoding="utf-8")
    report_out = bundle_dir / "validation_report.json"
    assert not report_out.exists()

    rc, payload, _, _ = _run_cli_main_json(
        validate_plan_main,
        [
            "--structured-plan",
            str(structured_plan_out),
            "--report-out",
            str(report_out),
            "--force",
            "--json",
        ],
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-preflight-plan-validation",
        stage="parse",
    )
    assert not report_out.exists()


def test_handoff_pipeline_builder_failure_does_not_write_handoff_manifest_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "bundle"
    paths = _handoff_bundle_through_validator_cli(bundle_dir, capsys)
    report_payload = _load_json(paths["validation_report"])
    report_payload["status"] = "tampered"
    _write_json(paths["validation_report"], report_payload)

    manifest_out = bundle_dir / "handoff_manifest.json"
    assert not manifest_out.exists()

    rc, payload, _, _ = _run_cli_main_json(
        build_handoff_manifest_main,
        [
            "--preflight-summary",
            str(paths["preflight_summary"]),
            "--plan-md",
            str(paths["plan_md"]),
            "--structured-plan",
            str(paths["structured_plan"]),
            "--validation-report",
            str(paths["validation_report"]),
            "--manifest-out",
            str(manifest_out),
            "--base-dir",
            str(bundle_dir),
            "--force",
            "--json",
        ],
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-handoff-manifest-build",
        stage="validate",
    )
    assert not manifest_out.exists()


def test_handoff_pipeline_verifier_failure_does_not_write_verification_report_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    manifest = _load_json(manifest_path)
    plan_path = _artifact_path_by_role(manifest, "plan_md")
    plan_path.write_text("# tampered plan body\n", encoding="utf-8")
    report_out = bundle_dir / "pipeline_failed_verification_report.json"
    assert not report_out.exists()

    rc, payload, _, _ = _run_cli_main_json(
        verify_handoff_manifest_main,
        _verify_cli_args(
            manifest_path,
            bundle_dir,
            "--verification-report-out",
            str(report_out),
            "--force",
        ),
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-handoff-manifest-verification",
        stage="validate",
    )
    assert not report_out.exists()


def test_handoff_pipeline_builder_base_dir_failure_does_not_create_manifest_parent_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    paths = _handoff_bundle_through_validator_cli(bundle_dir, capsys)
    outside_parent = tmp_path / "outside_parent"
    manifest_out = outside_parent / "handoff_manifest.json"
    assert not outside_parent.exists()

    rc, payload, _, _ = _run_cli_main_json(
        build_handoff_manifest_main,
        [
            "--preflight-summary",
            str(paths["preflight_summary"]),
            "--plan-md",
            str(paths["plan_md"]),
            "--structured-plan",
            str(paths["structured_plan"]),
            "--validation-report",
            str(paths["validation_report"]),
            "--manifest-out",
            str(manifest_out),
            "--base-dir",
            str(bundle_dir),
            "--force",
            "--json",
        ],
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-handoff-manifest-build",
        stage="validate",
    )
    assert not outside_parent.exists()


def test_handoff_pipeline_verifier_base_dir_failure_does_not_create_report_parent_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir, manifest_path = _generated_handoff_bundle(tmp_path, capsys=capsys)
    outside_parent = tmp_path / "outside_parent"
    report_out = outside_parent / "handoff_manifest_verification_report.json"
    assert not outside_parent.exists()

    rc, payload, _, _ = _run_cli_main_json(
        verify_handoff_manifest_main,
        _verify_cli_args(
            manifest_path,
            bundle_dir,
            "--verification-report-out",
            str(report_out),
            "--force",
        ),
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-handoff-manifest-verification",
        stage="validate",
    )
    assert not outside_parent.exists()


def test_handoff_pipeline_failed_overwrite_preserves_existing_output_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "bundle"
    paths = _handoff_bundle_through_validator_cli(bundle_dir, capsys)
    report_out = paths["validation_report"]
    original_bytes = report_out.read_bytes()

    structured_plan_out = paths["structured_plan"]
    structured_plan_out.write_text("{not-valid-json", encoding="utf-8")

    rc, payload, _, _ = _run_cli_main_json(
        validate_plan_main,
        [
            "--structured-plan",
            str(structured_plan_out),
            "--report-out",
            str(report_out),
            "--force",
            "--json",
        ],
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-preflight-plan-validation",
        stage="parse",
    )
    assert report_out.read_bytes() == original_bytes


def test_handoff_pipeline_failure_payload_does_not_echo_artifact_body_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    structured_plan_out = bundle_dir / "structured_plan.json"
    plan_out = bundle_dir / "plan.md"
    summary_out = bundle_dir / "preflight_summary.json"
    preflight_rc = preflight_main(
        [
            "--manifest",
            str(MANIFEST_FIXTURE),
            "--summary-out",
            str(summary_out),
            "--plan-out",
            str(plan_out),
            "--structured-plan-out",
            str(structured_plan_out),
            "--emit-followup-commands",
            "--force",
            "--json",
        ]
    )
    assert preflight_rc == 0
    capsys.readouterr()

    marker = "UNIQUE_NO_PARTIAL_MARKER_3H21"
    structured_plan_out.write_text(f"{{not-valid-json {marker}", encoding="utf-8")
    report_out = bundle_dir / "validation_report.json"
    assert not report_out.exists()

    rc, payload, stdout, stderr = _run_cli_main_json(
        validate_plan_main,
        [
            "--structured-plan",
            str(structured_plan_out),
            "--report-out",
            str(report_out),
            "--force",
            "--json",
        ],
        capsys,
    )
    assert rc == 1
    _assert_cli_error_payload(
        payload,
        mode="kr-end-to-end-preflight-plan-validation",
        stage="parse",
    )
    message = payload["message"]
    assert isinstance(message, str)
    assert marker not in message
    assert marker not in stdout
    assert marker not in stderr
    assert not report_out.exists()


# --- 3H22: handoff bundle reproducibility/determinism smoke (in-process, no-exec) ---
#
# 3H16–3H21은 happy path·parity·tamper·fail-closed를 검증했지만, 동일 fixture로 반복 실행 시
# 의미 계약(역할/종류/모드/플래그/스키마 키 집합)이 안정적인지는 단언하지 않았다. 3H22는 API/CLI
# round-trip을 각각 별도 bundle에 두 번 돌리고 3H18 정규화 요약으로 A vs B 동등성을 증명한다.
# 절대경로·sha256 값·size_bytes 값·base_dir 문자열·wall-clock 타임스탬프는 bundle마다 달라질 수
# 있으므로 교차 비교하지 않는다.


def _load_round_trip_contracts(round_trip: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """round-trip 헬퍼 산출물에서 handoff manifest와 verification report를 로드한다."""
    paths = round_trip["paths"]
    assert isinstance(paths, dict)
    manifest = _load_json(paths["handoff_manifest"])
    report = _load_json(paths["verification_report"])
    return manifest, report


def _assert_handoff_contract_body_free(
    manifest: dict[str, object],
    report: dict[str, object],
) -> None:
    """manifest/report가 exact-key·body-free 계약을 만족하는지 단언한다(_walk_forbidden_fields 단독 금지)."""
    assert set(manifest.keys()) == _MANIFEST_EXPECTED_TOP_KEYS
    assert "steps" not in manifest
    assert "command" not in manifest
    assert "commands" not in manifest
    assert "followup_commands" not in manifest
    for entry in manifest["artifacts"]:
        assert isinstance(entry, dict)
        assert set(entry.keys()) == _ARTIFACT_ENTRY_KEYS
        assert "content" not in entry
        assert "body" not in entry
        assert "command" not in entry
        assert "commands" not in entry

    assert set(report.keys()) == _HANDOFF_VERIFY_REPORT_EXPECTED_KEYS
    assert "artifacts" not in report
    assert "steps" not in report
    assert "command" not in report
    assert "commands" not in report


def _assert_handoff_bundle_containment(
    bundle_dir: Path,
    manifest: dict[str, object],
    report: dict[str, object],
) -> None:
    """각 run의 bundle_dir 기준으로 artifact path와 report base_dir containment를 단언한다."""
    for entry in manifest["artifacts"]:
        assert isinstance(entry, dict)
        assert _path_resolves_inside_base(Path(entry["path"]), bundle_dir)
    base_dir = report.get("base_dir")
    assert base_dir is not None
    assert _path_resolves_inside_base(Path(str(base_dir)), bundle_dir)


def test_handoff_bundle_api_round_trip_reproducible_manifest_contract(tmp_path: Path) -> None:
    api_bundle_a = tmp_path / "api_a"
    api_bundle_b = tmp_path / "api_b"
    manifest_a, _report_a = _load_round_trip_contracts(_run_handoff_bundle_round_trip(api_bundle_a))
    manifest_b, _report_b = _load_round_trip_contracts(_run_handoff_bundle_round_trip(api_bundle_b))

    norm_a = _normalize_handoff_manifest_for_parity(manifest_a)
    norm_b = _normalize_handoff_manifest_for_parity(manifest_b)
    assert norm_a == norm_b
    assert norm_a["top_keys"] == sorted(_MANIFEST_EXPECTED_TOP_KEYS)
    assert norm_a["roles"] == list(_ROLE_ORDER)
    assert norm_a["commands_execute_in_builder"] is False
    assert norm_a["review_only"] is True
    assert all(norm_a["sha256_shape"])  # type: ignore[arg-type]
    assert all(norm_a["size_positive"])  # type: ignore[arg-type]
    assert all(keys == sorted(_ARTIFACT_ENTRY_KEYS) for keys in norm_a["entry_keys"])  # type: ignore[union-attr]


def test_handoff_bundle_api_round_trip_reproducible_verification_report_contract(tmp_path: Path) -> None:
    api_bundle_a = tmp_path / "api_a"
    api_bundle_b = tmp_path / "api_b"
    _manifest_a, report_a = _load_round_trip_contracts(_run_handoff_bundle_round_trip(api_bundle_a))
    _manifest_b, report_b = _load_round_trip_contracts(_run_handoff_bundle_round_trip(api_bundle_b))

    norm_a = _normalize_verification_report_for_parity(report_a)
    norm_b = _normalize_verification_report_for_parity(report_b)
    assert norm_a == norm_b
    assert norm_a["keys"] == sorted(_HANDOFF_VERIFY_REPORT_EXPECTED_KEYS)
    assert norm_a["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)
    assert norm_a["path_containment_verified"] is True
    assert norm_a["hashes_verified"] is True
    assert norm_a["metadata_verified"] is True
    assert norm_a["schema_verified"] is True
    assert norm_a["commands_execute_in_verifier"] is False
    assert norm_a["review_only"] is True
    assert norm_a["base_dir_present"] is True


def test_handoff_bundle_cli_round_trip_reproducible_manifest_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_bundle_a = tmp_path / "cli_a"
    cli_bundle_b = tmp_path / "cli_b"
    manifest_a, _report_a = _load_round_trip_contracts(
        _run_handoff_bundle_cli_round_trip(cli_bundle_a, capsys)
    )
    manifest_b, _report_b = _load_round_trip_contracts(
        _run_handoff_bundle_cli_round_trip(cli_bundle_b, capsys)
    )

    norm_a = _normalize_handoff_manifest_for_parity(manifest_a)
    norm_b = _normalize_handoff_manifest_for_parity(manifest_b)
    assert norm_a == norm_b
    assert norm_a["top_keys"] == sorted(_MANIFEST_EXPECTED_TOP_KEYS)
    assert norm_a["roles"] == list(_ROLE_ORDER)
    assert norm_a["commands_execute_in_builder"] is False
    assert norm_a["review_only"] is True


def test_handoff_bundle_cli_round_trip_reproducible_verification_report_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_bundle_a = tmp_path / "cli_a"
    cli_bundle_b = tmp_path / "cli_b"
    _manifest_a, report_a = _load_round_trip_contracts(
        _run_handoff_bundle_cli_round_trip(cli_bundle_a, capsys)
    )
    _manifest_b, report_b = _load_round_trip_contracts(
        _run_handoff_bundle_cli_round_trip(cli_bundle_b, capsys)
    )

    norm_a = _normalize_verification_report_for_parity(report_a)
    norm_b = _normalize_verification_report_for_parity(report_b)
    assert norm_a == norm_b
    assert norm_a["keys"] == sorted(_HANDOFF_VERIFY_REPORT_EXPECTED_KEYS)
    assert norm_a["artifact_roles"] == list(_EXPECTED_ARTIFACT_ROLES)
    assert norm_a["path_containment_verified"] is True
    assert norm_a["commands_execute_in_verifier"] is False
    assert norm_a["review_only"] is True
    assert norm_a["base_dir_present"] is True


def test_handoff_bundle_reproducible_runs_remain_contained_and_body_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """API/CLI 각 두 run의 containment·exact-key·body-free 계약을 per-bundle로 재단언한다."""
    api_bundle_a = tmp_path / "api_a"
    api_bundle_b = tmp_path / "api_b"
    api_round_a = _run_handoff_bundle_round_trip(api_bundle_a)
    api_round_b = _run_handoff_bundle_round_trip(api_bundle_b)
    cli_bundle_a = tmp_path / "cli_a"
    cli_bundle_b = tmp_path / "cli_b"
    cli_round_a = _run_handoff_bundle_cli_round_trip(cli_bundle_a, capsys)
    cli_round_b = _run_handoff_bundle_cli_round_trip(cli_bundle_b, capsys)

    for bundle_dir, round_trip in (
        (api_bundle_a, api_round_a),
        (api_bundle_b, api_round_b),
        (cli_bundle_a, cli_round_a),
        (cli_bundle_b, cli_round_b),
    ):
        manifest, report = _load_round_trip_contracts(round_trip)
        _assert_handoff_bundle_containment(bundle_dir, manifest, report)
        _assert_handoff_contract_body_free(manifest, report)
        _walk_forbidden_fields(manifest)
        _walk_forbidden_fields(report)


def test_handoff_bundle_reproducible_round_trips_no_generated_commands_executed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("reproducibility round-trip must not execute generated commands")

    monkeypatch.setattr(subprocess, "run", _fail_run)
    api_bundle_a = tmp_path / "api_a"
    api_bundle_b = tmp_path / "api_b"
    manifest_a, report_a = _load_round_trip_contracts(_run_handoff_bundle_round_trip(api_bundle_a))
    manifest_b, report_b = _load_round_trip_contracts(_run_handoff_bundle_round_trip(api_bundle_b))
    assert _normalize_handoff_manifest_for_parity(manifest_a) == _normalize_handoff_manifest_for_parity(
        manifest_b
    )
    assert _normalize_verification_report_for_parity(report_a) == _normalize_verification_report_for_parity(
        report_b
    )

    cli_bundle_a = tmp_path / "cli_a"
    cli_bundle_b = tmp_path / "cli_b"
    cli_manifest_a, cli_report_a = _load_round_trip_contracts(
        _run_handoff_bundle_cli_round_trip(cli_bundle_a, capsys)
    )
    cli_manifest_b, cli_report_b = _load_round_trip_contracts(
        _run_handoff_bundle_cli_round_trip(cli_bundle_b, capsys)
    )
    assert _normalize_handoff_manifest_for_parity(cli_manifest_a) == _normalize_handoff_manifest_for_parity(
        cli_manifest_b
    )
    assert _normalize_verification_report_for_parity(cli_report_a) == _normalize_verification_report_for_parity(
        cli_report_b
    )

