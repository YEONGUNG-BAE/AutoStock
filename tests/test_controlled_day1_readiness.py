"""Controlled Day 1 Readiness 0A — runbook contract / no-write boundary smoke.

정적 docs-contract 테스트만 수행한다. Controlled Day 1 workflow, 8B–8I ops script,
subprocess, runtime artifact 생성은 하지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FOUNDATION_OPS_SCRIPTS = [
    "ops/research_source_intake.py",
    "ops/run_date_md_smoke.py",
    "ops/build_scout_manual_packet.py",
    "ops/validate_scout_raw_json.py",
    "ops/build_allocator_manual_packet.py",
    "ops/validate_allocator_raw_json.py",
    "ops/build_analysis_manual_packet.py",
    "ops/validate_analysis_raw_json.py",
    "ops/assemble_paper_loop_input.py",
    "ops/rehearse_paper_loop_no_write.py",
]

FOUNDATION_STEP_TOKENS = ["8B", "8C", "8D", "8E", "8F", "8G", "8H", "8I"]

# step-flow ordering 검사 전 range token(8B–8I 등) 제거용
_RANGE_TOKEN_RE = re.compile(r"8B[–\-~]8I")


def _read(path: str) -> str:
    """repo 상대 경로의 UTF-8 텍스트를 읽는다."""
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _controlled_day1_section(runbook: str) -> str:
    """RUNBOOK Controlled Day 1 섹션 본문을 추출한다."""
    start_marker = "## Controlled Day 1 paper walk-through"
    end_marker = "## 5. Scout → Allocator → Analysis → Risk → PaperLoop orchestration"
    start = runbook.index(start_marker)
    end = runbook.index(end_marker, start)
    return runbook[start:end]


def _controlled_day1_step_flow_section(section: str) -> str:
    """Controlled Day 1의 Step-by-step command flow 하위 본문만 추출한다."""
    start_marker = "### Step-by-step command flow"
    end_marker = "### Validation gates (summary)"
    start = section.index(start_marker)
    end = section.index(end_marker, start)
    return section[start:end]


def _paper_workflow_foundation_section(text: str) -> str:
    """PAPER_PILOT_WORKFLOW Foundation roadmap(§12) 본문을 추출한다."""
    start_marker = "## 12. Foundation roadmap (8B–8I) and evidence-based follow-ups"
    end_marker = "## 참고"
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _assert_in_order(section: str, tokens: list[str]) -> None:
    """scoped section에서 token first-occurrence 순서가 canonical order인지 검증한다."""
    positions: list[int] = []
    for token in tokens:
        pos = section.find(token)
        assert pos != -1, f"token {token!r} not found in scoped section"
        positions.append(pos)
    for i in range(len(positions) - 1):
        assert positions[i] < positions[i + 1], (
            f"token order violation: {tokens[i]!r} at {positions[i]} "
            f"not before {tokens[i + 1]!r} at {positions[i + 1]}"
        )


def _assert_any_present(section: str, phrases: list[str], label: str) -> None:
    """phrases 중 하나라도 section에 있어야 한다."""
    if not any(phrase in section for phrase in phrases):
        raise AssertionError(f"{label}: none of {phrases!r} found in section")


def test_controlled_day1_runbook_section_exists_and_is_no_write_bounded() -> None:
    """Controlled Day 1 runbook 섹션이 no-write bounded boundary를 명시하는지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    section = _controlled_day1_section(runbook)

    assert "Controlled Day 1" in section
    _assert_any_present(
        section,
        [
            "not the 30-trading-day paper pilot start",
            "30-trading-day paper pilot 시작이 아니다",
        ],
        "Controlled Day 1 is not 30-trading-day pilot start",
    )
    assert "8I" in section
    assert "no-write" in section
    assert "VALIDATION_ONLY" in section

    # 명시적 exclusion/forbidden boundary (positive assertions)
    _assert_any_present(
        section,
        [
            "real API fetchers",
            "real API fetcher",
        ],
        "real API fetchers excluded",
    )
    _assert_any_present(
        section,
        ["automatic LLM orchestration"],
        "automatic LLM orchestration excluded",
    )
    _assert_any_present(
        section,
        ["PaperLoopRunner.run()"],
        "PaperLoopRunner.run excluded",
    )
    _assert_any_present(
        section,
        [
            "write-mode `ops/run_paper_once.py`",
            "write-mode paper loop",
        ],
        "write-mode run_paper_once excluded",
    )
    _assert_any_present(
        section,
        [
            "broker/KIS",
            "broker, KIS",
            "broker order submission",
        ],
        "broker/KIS excluded",
    )
    _assert_any_present(
        section,
        [
            "ledger/decision DB writes",
            "ledger writes, decision snapshots",
            "ledger/decision DB **unchanged**",
        ],
        "ledger/decision DB writes excluded",
    )


def test_controlled_day1_runbook_references_foundation_8b_to_8i_in_step_flow_order() -> None:
    """Step-by-step command flow에서 8B–8I 참조 순서가 canonical order인지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    section = _controlled_day1_section(runbook)
    step_flow = _controlled_day1_step_flow_section(section)

    # intro/summary range token이 ordering을 깨지 않도록 제거
    scoped = _RANGE_TOKEN_RE.sub("", step_flow)

    for token in FOUNDATION_STEP_TOKENS:
        assert token in scoped, f"step flow missing Foundation token {token!r}"

    _assert_in_order(scoped, FOUNDATION_STEP_TOKENS)


def test_controlled_day1_runbook_references_existing_ops_entrypoints() -> None:
    """8B–8I ops script가 repo에 존재하고 runbook Controlled Day 1 섹션에 참조되는지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    section = _controlled_day1_section(runbook)

    for script_path in FOUNDATION_OPS_SCRIPTS:
        full_path = REPO_ROOT / script_path
        assert full_path.is_file(), f"expected ops script missing: {script_path}"
        assert script_path in section, f"runbook Controlled Day 1 missing reference: {script_path}"


def test_controlled_day1_runbook_requires_8i_no_write_rehearsal_and_validation_only() -> None:
    """8I no-write rehearsal command와 success criteria literal이 runbook에 있는지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    section = _controlled_day1_section(runbook)

    assert "ops/rehearse_paper_loop_no_write.py" in section
    assert "--no-write" in section
    assert "--json" in section

    for literal in (
        "run_paper_once_status",
        "VALIDATION_ONLY",
        "ledger_db unchanged",
        "decision_db unchanged",
        "git ls-files runtime",
    ):
        assert literal in section, f"8I success criteria missing literal: {literal!r}"

    _assert_any_present(
        section,
        [
            "external API / KIS / broker / write-mode paper loop **미사용**",
            "external API / KIS / broker / write-mode paper loop unused",
            "write-mode paper loop, broker, KIS, external API fetch **없이**",
        ],
        "8I external API/KIS/broker/write-mode exclusion",
    )


def test_controlled_day1_paper_workflow_declares_8b_to_8i_closed_and_next_step() -> None:
    """PAPER_PILOT_WORKFLOW가 8B–8I CLOSED와 Controlled Day 1 next step boundary를 선언하는지 검증한다."""
    workflow = _read("docs/PAPER_PILOT_WORKFLOW.md")
    foundation = _paper_workflow_foundation_section(workflow)

    assert "8B" in foundation
    assert "8I" in foundation
    assert "CLOSED" in foundation

    _assert_any_present(
        foundation,
        [
            "Controlled Day 1 paper walk-through",
            "Controlled Day 1 walk-through",
        ],
        "next operating step is Controlled Day 1",
    )
    _assert_any_present(
        foundation,
        [
            "8I no-write rehearsal에서 종료",
            "8I에서 종료",
            "8I no-write rehearsal",
        ],
        "chain stops at 8I no-write rehearsal",
    )
    _assert_any_present(
        foundation,
        [
            "30거래일 pilot **시작과 동일하지 않다**",
            "30-trading-day paper pilot start",
            "30-trading-day pilot start",
        ],
        "Controlled Day 1 is not automatic 30-trading-day pilot start",
    )
    _assert_any_present(
        foundation,
        [
            "real API fetchers **또는** repeatable manual intake discipline + explicit readiness decision 이후",
            "real API fetchers **또는**",
            "explicit readiness decision",
        ],
        "30-trading-day pilot start is deferred/non-automatic",
    )


def test_controlled_day1_runbook_runtime_artifacts_are_local_only() -> None:
    """runtime output이 runtime/ 하위이며 commit 금지·git safety check가 문서화되어 있는지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    section = _controlled_day1_section(runbook)

    assert "runtime/" in section
    _assert_any_present(
        section,
        [
            "runtime output **commit 금지**",
            "runtime/` commit 금지",
            "**commit 금지**",
        ],
        "runtime output must not be committed",
    )
    assert "git status -uall --short" in section
    assert "git ls-files runtime" in section
    _assert_any_present(
        section,
        [
            "# empty output",
            "empty",
        ],
        "git ls-files runtime expected empty",
    )


def test_controlled_day1_runbook_forbids_auto_progression_after_pass() -> None:
    """Controlled Day 1 PASS 후 자동 진행 금지 항목과 next design step이 문서화되어 있는지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    section = _controlled_day1_section(runbook)

    _assert_any_present(
        section,
        [
            "아래는 자동으로 진행하지 않는다",
            "do not automatically proceed",
        ],
        "after Day 1 non-auto-progression header",
    )

    after_day1_items = [
        "real API fetcher",
        "30-trading-day paper pilot start",
        "KIS read-only `--run`",
        "write-mode `ops/run_paper_once.py`",
        "PaperLoopRunner.run()",
        "broker order submission",
        "ledger/decision DB writes",
        "fills",
        "NAV",
        "daily summary",
        "postmortem",
    ]
    for item in after_day1_items:
        assert item in section, f"after Day 1 boundary missing item: {item!r}"

    _assert_any_present(
        section,
        [
            "Real Research Source Intake v1",
            "REAL_RESEARCH_SOURCE_INTAKE.md",
        ],
        "next design step Real Research Source Intake v1",
    )


def test_controlled_day1_readiness_contract_uses_positive_forbidden_boundary_assertions() -> None:
    """Controlled Day 1 / workflow docs에 positive prohibition boundary 문구가 있는지 검증한다."""
    runbook = _read("docs/RUNBOOK.md")
    day1 = _controlled_day1_section(runbook)
    workflow = _read("docs/PAPER_PILOT_WORKFLOW.md")
    foundation = _paper_workflow_foundation_section(workflow)
    combined = day1 + "\n" + foundation

    _assert_any_present(
        combined,
        ["validator **우회 금지**", "validator bypass"],
        "validator bypass forbidden",
    )
    _assert_any_present(
        combined,
        ["validated JSON **수동 편집 금지**", "hand-edited validated JSON"],
        "validated JSON manual edit forbidden",
    )
    _assert_any_present(
        combined,
        [
            "required runtime artifact **synthesize 금지**",
            "synthesize missing runtime artifacts",
            "missing raw LLM output",
        ],
        "synthesize missing runtime artifacts forbidden",
    )
    _assert_any_present(
        combined,
        [
            "no-write invariant",
            "ledger/decision DB changed",
            "**즉시 중단**",
        ],
        "no-write invariant failure stops the process",
    )
    _assert_any_present(
        combined,
        [
            "write-mode `ops/run_paper_once.py`",
            "write-mode paper loop",
        ],
        "write-mode excluded",
    )
    _assert_any_present(
        combined,
        ["broker/KIS", "broker, KIS", "PaperBroker/KIS"],
        "broker/KIS excluded",
    )
    _assert_any_present(
        combined,
        ["PaperLoopRunner.run()", "PaperLoopRunner.run"],
        "PaperLoopRunner excluded",
    )
