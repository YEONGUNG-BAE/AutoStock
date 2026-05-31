"""Real Intake 3H1 — operator-local end-to-end manifest/preflight helper tests."""

from __future__ import annotations

import json
import os
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
    run_kr_end_to_end_preflight,
)
from build_kr_end_to_end_handoff_manifest import (
    KrEndToEndHandoffManifestError,
    _write_manifest_output,
    build_handoff_manifest,
    build_kr_end_to_end_handoff_manifest,
)
from verify_kr_end_to_end_handoff_manifest import (
    KrEndToEndHandoffManifestVerifyError,
    _validate_artifact_entry_schema,
    _validate_manifest_schema,
    load_handoff_manifest,
    verify_kr_end_to_end_handoff_manifest,
)
from validate_kr_end_to_end_preflight_plan import (
    KrEndToEndPlanValidationError,
    _FOLLOWUP_COMMAND_ALLOWLIST as _VALIDATOR_FOLLOWUP_COMMAND_ALLOWLIST,
    _build_validation_report,
    _load_and_validate_structured_plan,
    _write_report_output,
    load_structured_preflight_plan,
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
