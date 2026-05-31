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
    KrEndToEndPreflightError,
    KrEndToEndPreflightManifest,
    load_kr_end_to_end_preflight_manifest,
    run_kr_end_to_end_preflight,
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
    with pytest.raises(KrEndToEndPreflightError, match="output already exists") as exc:
        run_kr_end_to_end_preflight(path, summary_out=summary_out, emit_followup_commands=False, force=False)
    assert exc.value.stage == "write"


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
