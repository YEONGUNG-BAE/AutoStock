#!/usr/bin/env python3
"""DEV-ONLY synthetic PaperLoopInput fixture builder.

Not production Layer A.
Does not call LLM/Ollama/Scout/Allocator/Analysis/PaperLoopRunner/PaperBroker/KIS.
Outputs only synthetic JSON under runtime/synthetic by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import ValidationError

from analysis import (
    AnalysisAction,
    AnalysisDecision,
    AnalysisReason,
    BearPerspective,
    BullPerspective,
    FundManagerDecision,
    RiskManagerEvaluation,
)
from allocator import (
    AllocatorDecision,
    AllocatorReason,
    AssetAllocatorView,
    CashManagerView,
    CashPolicy,
    ConsistencyCheckerView,
    GoldPolicyMode,
    SignalSummary,
    TargetWeights,
)
from decision.canonical_json import canonicalize_payload
from domain import Currency, DateId, DecisionId, Market, MarketPrice, Money, Percent
from domain.enums import AccountRole
from domain._datetime import require_timezone_aware_datetime
from paper_loop import PaperLoopInput
from risk import RiskFilterContext, RiskMode

ScenarioName = Literal["normal-buy", "noop", "risk-blocked"]

GENERATED_BY = "ops/dev/build_synthetic_paper_loop_input.py"
DEFAULT_OUTPUT_DIR = Path("runtime/synthetic")
KST = timezone(timedelta(hours=9))
FIXED_TS = datetime(2026, 1, 15, 10, 0, tzinfo=KST)
SYMBOL = "005930"
PRICE = Decimal("70000")

EXPECTED_STATUS: dict[str, str] = {
    "normal-buy": "FILLED",
    "noop": "NOOP",
    "risk-blocked": "RISK_BLOCKED",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DEV-ONLY synthetic PaperLoopInput JSON builder (deterministic).",
    )
    parser.add_argument(
        "--scenario",
        choices=["normal-buy", "noop", "risk-blocked"],
        default="normal-buy",
        help="synthetic scenario (default: normal-buy)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output JSON path (default: runtime/synthetic/paper_loop_input.<scenario>.SYNTH.json)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="write JSON payload to stdout (no file write)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing output file",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="build and validate only; no file write or stdout JSON",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable summary",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print non-sensitive metadata",
    )
    return parser


def _default_output_path(scenario: str) -> Path:
    return DEFAULT_OUTPUT_DIR / f"paper_loop_input.{scenario}.SYNTH.json"


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return require_timezone_aware_datetime(parsed, field_name="datetime")


def _coerce_iso_datetimes(value: Any) -> Any:
    """JSON load 후 PaperLoopInput validation용 ISO datetime string을 변환한다."""
    if isinstance(value, dict):
        coerced: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"created_at", "as_of"} and isinstance(item, str):
                coerced[key] = _parse_iso_datetime(item)
            else:
                coerced[key] = _coerce_iso_datetimes(item)
        return coerced
    if isinstance(value, list):
        return [_coerce_iso_datetimes(item) for item in value]
    return value


def _analysis_reason(date_suffix: str = "1") -> AnalysisReason:
    return AnalysisReason(
        reason="SYNTHETIC DEV FIXTURE analysis reason",
        date_id=DateId(f"260115-{date_suffix}"),
    )


def _allocator_reason(date_suffix: str = "1") -> AllocatorReason:
    return AllocatorReason(
        reason="SYNTHETIC DEV FIXTURE allocator reason",
        date_id=DateId(f"260115-{date_suffix}"),
    )


def _build_allocator_decision(*, scenario: str) -> AllocatorDecision:
    alloc_reasons = (_allocator_reason(),)
    weights = TargetWeights(kr=Percent("50"), us=Percent("30"), gold=Percent("20"))
    cash = Percent("20")
    return AllocatorDecision(
        decision_id=DecisionId(f"SYNTH-ALLOC-{scenario.upper()}"),
        created_at=FIXED_TS,
        universe="ALL",
        summary_one_liner=f"SYNTHETIC DEV FIXTURE allocator ({scenario})",
        gold_policy_mode=GoldPolicyMode.NORMAL,
        signal_summary=SignalSummary(
            summary=f"SYNTHETIC DEV FIXTURE signal ({scenario})",
            reasons=alloc_reasons,
        ),
        cash_manager=CashManagerView(
            summary="SYNTHETIC DEV FIXTURE cash manager",
            recommended_cash_percent=cash,
            reasons=alloc_reasons,
        ),
        asset_allocator=AssetAllocatorView(
            summary="SYNTHETIC DEV FIXTURE asset allocator",
            target_weights=weights,
            reasons=alloc_reasons,
        ),
        consistency_checker=ConsistencyCheckerView(
            passed=True,
            summary="SYNTHETIC DEV FIXTURE consistency ok",
            reasons=alloc_reasons,
        ),
        cash_policy=CashPolicy(
            cash_target_percent=cash,
            rationale="SYNTHETIC DEV FIXTURE cash policy",
            reasons=alloc_reasons,
        ),
        target_weights=weights,
        reasons=alloc_reasons,
    )


def _build_analysis_decision(
    *,
    scenario: str,
    action: AnalysisAction,
    target_weight: str,
) -> AnalysisDecision:
    reasons = (_analysis_reason("1"),)
    return AnalysisDecision(
        decision_id=DecisionId(f"SYNTH-ANALYSIS-{scenario.upper()}"),
        created_at=FIXED_TS,
        universe="KR_LARGE",
        symbol=SYMBOL,
        market="KR",
        summary_one_liner=f"SYNTHETIC DEV FIXTURE analysis ({scenario})",
        bear=BearPerspective(
            summary="SYNTHETIC DEV FIXTURE bear",
            risks=("SYNTHETIC DEV FIXTURE risk",),
            reasons=reasons,
        ),
        bull=BullPerspective(
            summary="SYNTHETIC DEV FIXTURE bull",
            catalysts=("SYNTHETIC DEV FIXTURE catalyst",),
            reasons=(_analysis_reason("2"),),
        ),
        risk_manager=RiskManagerEvaluation(
            summary="SYNTHETIC DEV FIXTURE risk manager",
            reasons=(_analysis_reason("3"),),
        ),
        fund_manager=FundManagerDecision(
            action=action,
            target_weight_percent=Percent(target_weight),
            rationale=f"SYNTHETIC DEV FIXTURE fund manager ({scenario})",
            reasons=(_analysis_reason("4"),),
        ),
        reasons=(_analysis_reason("5"),),
    )


def _build_risk_context(*, scenario: str) -> RiskFilterContext:
    base: dict[str, Any] = {
        "created_at": FIXED_TS,
        "mode": RiskMode.NORMAL,
        "total_nav": Money.from_str("100000000", Currency.KRW),
        "cash": Money.from_str("20000000", Currency.KRW),
        "invested_amount": Money.from_str("80000000", Currency.KRW),
    }
    if scenario == "normal-buy":
        base.update(
            {
                "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
                "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
            }
        )
    elif scenario == "noop":
        base.update(
            {
                "current_symbol_market_value": Money.from_str("2000000", Currency.KRW),
                "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
            }
        )
    elif scenario == "risk-blocked":
        base.update(
            {
                "allocator_symbol_target_weight": Percent("5"),
                "current_symbol_market_value": Money.from_str("4000000", Currency.KRW),
                "current_symbol_cumulative_buy_cost": Money.from_str("1000000", Currency.KRW),
            }
        )
    return RiskFilterContext(**base)


def _build_market_price() -> MarketPrice:
    return MarketPrice(
        symbol=SYMBOL,
        market=Market.KR,
        currency=Currency.KRW,
        price=PRICE,
        as_of=FIXED_TS,
    )


def _build_loop_input(scenario: ScenarioName) -> PaperLoopInput:
    if scenario == "normal-buy":
        action = AnalysisAction.BUY
        target_weight = "5"
    elif scenario == "noop":
        action = AnalysisAction.HOLD
        target_weight = "5"
    else:
        action = AnalysisAction.BUY
        target_weight = "12"

    return PaperLoopInput(
        run_id=DecisionId(f"SYNTH-RUN-{scenario.upper()}"),
        created_at=FIXED_TS,
        allocator_decision=_build_allocator_decision(scenario=scenario),
        analysis_decision=_build_analysis_decision(
            scenario=scenario,
            action=action,
            target_weight=target_weight,
        ),
        risk_context=_build_risk_context(scenario=scenario),
        market_price=_build_market_price(),
        broker_account_role=AccountRole.PAPER,
        correlation_id=f"SYNTH-CORR-{scenario.upper()}",
        metadata={
            "synthetic": True,
            "synthetic_scenario": scenario,
            "generated_by": GENERATED_BY,
            "production_use": False,
        },
    )


def _serialize_loop_input(loop_input: PaperLoopInput) -> str:
    payload = canonicalize_payload(loop_input.model_dump(mode="json"))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _validate_loop_input(loop_input: PaperLoopInput) -> None:
    """객체 및 JSON semantic roundtrip 검증."""
    PaperLoopInput.model_validate(loop_input.model_dump())
    raw = json.loads(_serialize_loop_input(loop_input))
    PaperLoopInput.model_validate(_coerce_iso_datetimes(raw))


def _payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _fail(stage: str, reason: str, *, as_json: bool = False, out: TextIO = sys.stderr) -> int:
    payload = {"outcome": "FAIL", "stage": stage, "reason": reason}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
    else:
        print("Synthetic builder: FAIL", file=out)
        print(f"stage: {stage}", file=out)
        print(f"reason: {reason}", file=out)
    return 1


def _emit_summary(summary: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), file=out)
        return
    print(f"Synthetic builder: {summary['outcome']}", file=out)
    for key in (
        "scenario",
        "expected_status",
        "output",
        "payload_hash",
        "validated",
        "file_written",
    ):
        if key in summary:
            print(f"{key}: {summary[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    scenario: ScenarioName = args.scenario
    as_json = args.json
    summary_out: TextIO = sys.stderr if args.stdout else sys.stdout

    if as_json and args.stdout:
        return _fail(
            "input",
            "--json and --stdout cannot be used together",
            as_json=as_json,
            out=summary_out,
        )

    output_path = Path(args.out) if args.out else _default_output_path(scenario)

    if args.verbose:
        print(f"verbose: scenario={scenario}", file=summary_out)
        print(f"verbose: output={output_path}", file=summary_out)
        print(f"verbose: validate_only={'yes' if args.validate_only else 'no'}", file=summary_out)

    try:
        loop_input = _build_loop_input(scenario)
        _validate_loop_input(loop_input)
    except ValidationError as exc:
        return _fail("validation", str(exc), as_json=as_json, out=summary_out)
    except ValueError as exc:
        return _fail("validation", str(exc), as_json=as_json, out=summary_out)

    payload_text = _serialize_loop_input(loop_input)
    payload_hash = _payload_hash(payload_text)

    if args.validate_only:
        summary = {
            "outcome": "PASS",
            "scenario": scenario,
            "expected_status": EXPECTED_STATUS[scenario],
            "validated": "yes",
            "file_written": "no",
            "payload_hash": payload_hash,
        }
        _emit_summary(summary, as_json=as_json, out=summary_out)
        return 0

    if args.stdout:
        print(payload_text, file=sys.stdout, end="")
        return 0

    if output_path.exists() and not args.force:
        return _fail(
            "output",
            f"output file already exists: {output_path} (use --force to overwrite)",
            as_json=as_json,
            out=summary_out,
        )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload_text, encoding="utf-8")
    except OSError as exc:
        return _fail("output", f"unable to write output file: {exc}", as_json=as_json, out=summary_out)

    summary = {
        "outcome": "PASS",
        "scenario": scenario,
        "expected_status": EXPECTED_STATUS[scenario],
        "output": str(output_path),
        "payload_hash": payload_hash,
        "validated": "yes",
        "file_written": "yes",
    }
    _emit_summary(summary, as_json=as_json, out=summary_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
