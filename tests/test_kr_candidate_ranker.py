"""Real Intake 3G3-1 — fixture-first KR candidate ranking tests."""

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
OPS_SCRIPT = REPO_ROOT / "ops" / "rank_kr_candidates.py"

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

from data.kr_candidate_ranker import (
    KrCandidateRankerError,
    compute_ranking_score,
    parse_ranking_signals_toml,
    rank_kr_candidates,
    rank_selected_candidates,
)
from data.kr_candidate_pool import parse_kr_candidate_pool_toml, select_candidates
from data.kr_provider_mapping_generator import generate_kr_provider_mapping_files, parse_kr_candidates_toml
from data.provider_mapping_registry import (
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml


def _write_signals(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "signals.toml"
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


def _base_cli_args(tmp_path: Path) -> list[str]:
    return [
        "--candidate-pool",
        str(POOL_FIXTURE),
        "--ranking-signals",
        str(SIGNALS_FIXTURE),
        "--ranked-out",
        str(tmp_path / "ranked.json"),
    ]


def test_ranking_signal_fixture_parses() -> None:
    document = parse_ranking_signals_toml(SIGNALS_FIXTURE)
    assert document.name == "kr-ranking-signals-synthetic-v1"
    assert document.score_version == "kr-ranking-fixture-v1"
    assert document.as_of.isoformat() == "2026-05-30T00:00:00+09:00"
    assert len(document.signals) == 3


def test_fixture_covers_default_selected_candidates() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    signals = parse_ranking_signals_toml(SIGNALS_FIXTURE)
    selected = select_candidates(pool)
    signal_keys = {(entry.market, entry.symbol) for entry in signals.signals}
    for candidate in selected:
        assert (candidate.market, candidate.symbol) in signal_keys


def test_unknown_root_field_rejected(tmp_path: Path) -> None:
    path = _write_signals(
        tmp_path,
        """
version = 1
name = "bad-root"
description = "bad"
as_of = "2026-05-30T00:00:00+09:00"
score_version = "kr-ranking-fixture-v1"
extra = true

[[signals]]
symbol = "900001"
market = "KR"
liquidity_score = 0.5
market_cap_score = 0.5
quality_score = 0.5
momentum_score = 0.5
risk_penalty = 0.1
""",
    )
    with pytest.raises(KrCandidateRankerError) as exc_info:
        parse_ranking_signals_toml(path)
    assert exc_info.value.stage == "parse"


def test_duplicate_signal_rejected(tmp_path: Path) -> None:
    path = _write_signals(
        tmp_path,
        """
version = 1
name = "dup"
description = "dup"
as_of = "2026-05-30T00:00:00+09:00"
score_version = "kr-ranking-fixture-v1"

[[signals]]
symbol = "900001"
market = "KR"
liquidity_score = 0.5
market_cap_score = 0.5
quality_score = 0.5
momentum_score = 0.5
risk_penalty = 0.1

[[signals]]
symbol = "900001"
market = "KR"
liquidity_score = 0.6
market_cap_score = 0.6
quality_score = 0.6
momentum_score = 0.6
risk_penalty = 0.2
""",
    )
    with pytest.raises(KrCandidateRankerError) as exc_info:
        parse_ranking_signals_toml(path)
    assert exc_info.value.stage == "parse"
    assert "duplicate" in exc_info.value.message


def test_control_characters_rejected_in_signal_description(tmp_path: Path) -> None:
    path = _write_signals(
        tmp_path,
        """
version = 1
name = "control"
description = "bad\\nline"
as_of = "2026-05-30T00:00:00+09:00"
score_version = "kr-ranking-fixture-v1"

[[signals]]
symbol = "900001"
market = "KR"
liquidity_score = 0.5
market_cap_score = 0.5
quality_score = 0.5
momentum_score = 0.5
risk_penalty = 0.1
""",
    )
    with pytest.raises(KrCandidateRankerError) as exc_info:
        parse_ranking_signals_toml(path)
    assert exc_info.value.stage == "parse"
    assert "control character" in exc_info.value.message


def test_score_components_must_be_in_unit_interval(tmp_path: Path) -> None:
    path = _write_signals(
        tmp_path,
        """
version = 1
name = "bad-score"
description = "bad score"
as_of = "2026-05-30T00:00:00+09:00"
score_version = "kr-ranking-fixture-v1"

[[signals]]
symbol = "900001"
market = "KR"
liquidity_score = 1.5
market_cap_score = 0.5
quality_score = 0.5
momentum_score = 0.5
risk_penalty = 0.1
""",
    )
    with pytest.raises(KrCandidateRankerError) as exc_info:
        parse_ranking_signals_toml(path)
    assert exc_info.value.stage == "parse"


def test_missing_signal_fails_at_rank_stage(tmp_path: Path) -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    selected = select_candidates(pool)
    signals_path = _write_signals(
        tmp_path,
        """
version = 1
name = "partial-signals"
description = "only one signal"
as_of = "2026-05-30T00:00:00+09:00"
score_version = "kr-ranking-fixture-v1"

[[signals]]
symbol = "900001"
market = "KR"
liquidity_score = 0.5
market_cap_score = 0.5
quality_score = 0.5
momentum_score = 0.5
risk_penalty = 0.1
""",
    )
    signals = parse_ranking_signals_toml(signals_path)
    with pytest.raises(KrCandidateRankerError) as exc_info:
        rank_selected_candidates(selected, signals)
    assert exc_info.value.stage == "rank"
    assert "900002" in exc_info.value.message or "900003" in exc_info.value.message


def test_empty_candidate_selection_fails_at_rank_stage(tmp_path: Path) -> None:
    with pytest.raises(KrCandidateRankerError) as exc_info:
        rank_kr_candidates(
            candidate_pool_path=POOL_FIXTURE,
            ranking_signals_path=SIGNALS_FIXTURE,
            ranked_out=tmp_path / "ranked.json",
            sectors={"nonexistent-sector"},
            force=True,
        )
    assert exc_info.value.stage == "rank"


def test_cli_invalid_max_total_fails_at_args_stage(tmp_path: Path) -> None:
    from rank_kr_candidates import main

    argv = _base_cli_args(tmp_path) + ["--max-total", "0", "--force", "--json"]
    assert main(argv) == 1


def test_deterministic_score_formula_for_900001() -> None:
    signals = parse_ranking_signals_toml(SIGNALS_FIXTURE)
    signal = next(entry for entry in signals.signals if entry.symbol == "900001")
    score, _, contributions, _ = compute_ranking_score(signal)
    assert score == 0.8375
    assert contributions["liquidity_score"] == 0.3325
    assert contributions["market_cap_score"] == 0.2250
    assert contributions["quality_score"] == 0.16
    assert contributions["momentum_score"] == 0.14
    assert contributions["risk_penalty"] == -0.02


def test_component_contributions_use_four_decimal_precision() -> None:
    signals = parse_ranking_signals_toml(SIGNALS_FIXTURE)
    signal = next(entry for entry in signals.signals if entry.symbol == "900003")
    _, _, contributions, _ = compute_ranking_score(signal)
    for value in contributions.values():
        assert value == round(value, 4)


def test_score_is_clamped_to_unit_interval() -> None:
    signals = parse_ranking_signals_toml(SIGNALS_FIXTURE)
    high_signal = signals.signals[0]
    high_signal = type(high_signal)(
        symbol=high_signal.symbol,
        market=high_signal.market,
        liquidity_score=1.0,
        market_cap_score=1.0,
        quality_score=1.0,
        momentum_score=1.0,
        risk_penalty=0.0,
        notes=None,
    )
    score, _, _, _ = compute_ranking_score(high_signal)
    assert score == 1.0

    low_signal = type(high_signal)(
        symbol=high_signal.symbol,
        market=high_signal.market,
        liquidity_score=0.0,
        market_cap_score=0.0,
        quality_score=0.0,
        momentum_score=0.0,
        risk_penalty=1.0,
        notes=None,
    )
    score, _, _, _ = compute_ranking_score(low_signal)
    assert score == 0.0


def test_ranking_order_is_deterministic() -> None:
    pool = parse_kr_candidate_pool_toml(POOL_FIXTURE)
    signals = parse_ranking_signals_toml(SIGNALS_FIXTURE)
    ranked = rank_selected_candidates(select_candidates(pool), signals)
    assert [row.candidate.symbol for row in ranked] == ["900001", "900002", "900003"]
    assert [row.rank for row in ranked] == [1, 2, 3]


def test_ranked_output_contains_required_metadata_fields(tmp_path: Path) -> None:
    payload = rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        force=True,
    )
    assert payload["score_version"] == "kr-ranking-fixture-v1"
    assert payload["score_precision"] == 4
    assert payload["as_of"] == "2026-05-30T00:00:00+09:00"
    first = payload["ranked"][0]
    assert "score_components" in first
    assert "score_contributions" in first
    assert "explanation" in first


def test_ranked_output_contains_no_trading_fields(tmp_path: Path) -> None:
    payload = rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        force=True,
    )

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in _FORBIDDEN_OUTPUT_FIELDS
                _walk(nested)
        elif isinstance(value, list):
            for nested in value:
                _walk(nested)

    _walk(payload)


def test_cli_writes_ranked_json_to_tmp_path(tmp_path: Path) -> None:
    ranked_out = tmp_path / "ranked.json"
    result = _run_cli(*(_base_cli_args(tmp_path) + ["--force", "--json"]))
    assert result.returncode == 0, result.stderr
    assert ranked_out.is_file()
    payload = json.loads(ranked_out.read_text(encoding="utf-8"))
    assert payload["ranked_count"] == 3


def test_cli_refuses_overwrite_without_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rank_kr_candidates import main

    argv = _base_cli_args(tmp_path) + ["--force", "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(_base_cli_args(tmp_path) + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["stage"] == "write"


def test_cli_json_stdout_is_pure_json(tmp_path: Path) -> None:
    result = _run_cli(*(_base_cli_args(tmp_path) + ["--force", "--json"]))
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"


def test_selected_candidate_export_has_no_ranking_metadata(tmp_path: Path) -> None:
    selected_out = tmp_path / "selected.toml"
    rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked synthetic KR candidates.",
        top_n=2,
        force=True,
    )
    raw = tomllib.loads(selected_out.read_text(encoding="utf-8"))
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
    selected_out = tmp_path / "selected.toml"
    rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked synthetic KR candidates.",
        top_n=3,
        force=True,
    )
    document = parse_kr_candidates_toml(selected_out)
    assert len(document.candidates) == 3


def test_selected_candidates_feed_3f1_generator(tmp_path: Path) -> None:
    selected_out = tmp_path / "selected.toml"
    rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked synthetic KR candidates.",
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
        universe_name="kr-ranked-generated-v1",
        provider_mapping_name="kr-ranked-provider-mappings-v1",
        force=True,
    )
    assert payload["candidates_read"] == 3


def test_generated_universe_and_mapping_validate(tmp_path: Path) -> None:
    selected_out = tmp_path / "selected.toml"
    rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked synthetic KR candidates.",
        top_n=2,
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
        universe_name="kr-ranked-generated-v1",
        provider_mapping_name="kr-ranked-provider-mappings-v1",
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


def test_top_n_export_limits_selected_candidates(tmp_path: Path) -> None:
    selected_out = tmp_path / "selected.toml"
    payload = rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        selected_candidates_out=selected_out,
        selection_name="kr-ranked-selected-v1",
        selection_description="Ranked synthetic KR candidates.",
        top_n=2,
        force=True,
    )
    assert payload["selected_count"] == 2
    document = parse_kr_candidates_toml(selected_out)
    assert [entry.symbol for entry in document.candidates] == ["900001", "900002"]


def test_max_per_sector_pre_filter_works(tmp_path: Path) -> None:
    payload = rank_kr_candidates(
        candidate_pool_path=POOL_FIXTURE,
        ranking_signals_path=SIGNALS_FIXTURE,
        ranked_out=tmp_path / "ranked.json",
        max_per_sector=1,
        force=True,
    )
    assert payload["ranked_count"] == 2
    symbols = [entry["symbol"] for entry in payload["ranked"]]
    assert symbols == ["900001", "900002"]


def test_ops_script_has_no_forbidden_tokens() -> None:
    paths = [
        REPO_ROOT / "src" / "data" / "kr_candidate_ranker.py",
        REPO_ROOT / "ops" / "rank_kr_candidates.py",
    ]
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
    for path in paths:
        source = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in source, f"{path.name} must not reference {token!r}"


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


def test_existing_3g1_and_3g2_tests_remain_importable() -> None:
    import test_kr_candidate_pool  # noqa: F401
    import test_kr_real_sector_pool_workflow  # noqa: F401
