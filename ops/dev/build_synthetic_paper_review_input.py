#!/usr/bin/env python3
"""DEV-ONLY synthetic PaperReviewInput fixture builder.

Not a production collector.
Does not read ledger/decision/log/postmortem/emergency stores.
Does not call LLM/Ollama/KIS/PaperBroker/PaperLoopRunner.
Outputs only synthetic JSON under runtime/synthetic by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TextIO

from pydantic import ValidationError

from config.settings import ExecutionMode
from decision.canonical_json import canonicalize_payload
from domain.enums import AccountRole, AssetClass, Currency, Market, OrderSide, OrderType
from domain.money import Money
from domain.order import Fill, OrderIntent
from domain.portfolio import NavSnapshot
from paper_review.models import PaperReviewInput, ReviewPeriod, SampleSufficiency

ScenarioName = Literal["insufficient-minimal", "partial-with-trade", "sufficient-drawdown"]

GENERATED_BY = "ops/dev/build_synthetic_paper_review_input.py"
DEFAULT_OUTPUT_DIR = Path("runtime/synthetic")
KST = timezone(timedelta(hours=9))
FIXED_TS = datetime(2026, 1, 15, 10, 0, tzinfo=KST)
SYMBOL = "005930"

SCENARIO_REVIEW_ID: dict[str, str] = {
    "insufficient-minimal": "SYNTH-REVIEW-001",
    "partial-with-trade": "SYNTH-REVIEW-002",
    "sufficient-drawdown": "SYNTH-REVIEW-003",
}

EXPECTED_SAMPLE_SUFFICIENCY: dict[str, SampleSufficiency] = {
    "insufficient-minimal": SampleSufficiency.INSUFFICIENT,
    "partial-with-trade": SampleSufficiency.PARTIAL,
    "sufficient-drawdown": SampleSufficiency.SUFFICIENT,
}

EXPECTED_REPORT_PATH: dict[str, str] = {
    "insufficient-minimal": "insufficient sample — observe-only recommendations expected",
    "partial-with-trade": "partial sample — limited execution metrics path",
    "sufficient-drawdown": "sufficient sample — drawdown / MDD review path",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DEV-ONLY synthetic PaperReviewInput JSON builder (deterministic).",
    )
    parser.add_argument(
        "--scenario",
        choices=["insufficient-minimal", "partial-with-trade", "sufficient-drawdown"],
        default="insufficient-minimal",
        help="synthetic scenario (default: insufficient-minimal)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "output JSON path "
            "(default: runtime/synthetic/paper_review_input.<scenario>.SYNTH.json)"
        ),
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
    return DEFAULT_OUTPUT_DIR / f"paper_review_input.{scenario}.SYNTH.json"


def _as_of_kst(day: date) -> datetime:
    """review period 내 deterministic KST as_of timestamp."""
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=KST)


def _nav_snapshot(
    *,
    snapshot_id: str,
    day: date,
    total_nav_krw: Decimal,
    cash_ratio: Decimal = Decimal("0.20"),
    mdd_percent: Decimal | None = None,
    daily_return_percent: Decimal | None = None,
) -> NavSnapshot:
    """NavSnapshot cash/invested/total 일관성을 유지하며 생성한다."""
    cash_krw = (total_nav_krw * cash_ratio).quantize(Decimal("0.01"))
    invested_krw = total_nav_krw - cash_krw
    return NavSnapshot(
        snapshot_id=snapshot_id,
        as_of=_as_of_kst(day),
        total_nav_krw=total_nav_krw,
        cash_krw=cash_krw,
        invested_krw=invested_krw,
        daily_return_percent=daily_return_percent,
        mdd_percent=mdd_percent,
    )


def _build_period(scenario: ScenarioName) -> ReviewPeriod:
    """scenario별 ReviewPeriod.from_dates() 생성."""
    if scenario == "insufficient-minimal":
        return ReviewPeriod.from_dates(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 30),
            trading_days=20,
        )
    if scenario == "partial-with-trade":
        return ReviewPeriod.from_dates(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 4, 30),
            trading_days=78,
        )
    return ReviewPeriod.from_dates(
        start_date=date(2026, 1, 1),
        end_date=date(2026, 7, 15),
        trading_days=130,
    )


def _build_nav_snapshots(scenario: ScenarioName) -> tuple[NavSnapshot, ...]:
    """scenario별 deterministic NAV series."""
    if scenario == "insufficient-minimal":
        return (
            _nav_snapshot(
                snapshot_id="SYNTH-NAV-001",
                day=date(2026, 1, 5),
                total_nav_krw=Decimal("100000000"),
            ),
            _nav_snapshot(
                snapshot_id="SYNTH-NAV-002",
                day=date(2026, 1, 20),
                total_nav_krw=Decimal("101000000"),
                daily_return_percent=Decimal("1.0"),
            ),
        )

    if scenario == "partial-with-trade":
        return (
            _nav_snapshot(
                snapshot_id="SYNTH-NAV-001",
                day=date(2026, 1, 10),
                total_nav_krw=Decimal("100000000"),
            ),
            _nav_snapshot(
                snapshot_id="SYNTH-NAV-002",
                day=date(2026, 2, 10),
                total_nav_krw=Decimal("102000000"),
                daily_return_percent=Decimal("2.0"),
            ),
            _nav_snapshot(
                snapshot_id="SYNTH-NAV-003",
                day=date(2026, 3, 15),
                total_nav_krw=Decimal("103500000"),
                daily_return_percent=Decimal("1.47"),
            ),
        )

    # sufficient-drawdown: peak 이후 NavSnapshot.mdd_percent로 drawdown 표현
    return (
        _nav_snapshot(
            snapshot_id="SYNTH-NAV-001",
            day=date(2026, 1, 15),
            total_nav_krw=Decimal("100000000"),
            mdd_percent=Decimal("0"),
        ),
        _nav_snapshot(
            snapshot_id="SYNTH-NAV-002",
            day=date(2026, 2, 15),
            total_nav_krw=Decimal("105000000"),
            daily_return_percent=Decimal("5.0"),
            mdd_percent=Decimal("0"),
        ),
        _nav_snapshot(
            snapshot_id="SYNTH-NAV-003",
            day=date(2026, 4, 1),
            total_nav_krw=Decimal("99000000"),
            daily_return_percent=Decimal("-5.71"),
            mdd_percent=Decimal("-5.71"),
        ),
        _nav_snapshot(
            snapshot_id="SYNTH-NAV-004",
            day=date(2026, 5, 15),
            total_nav_krw=Decimal("92000000"),
            daily_return_percent=Decimal("-7.07"),
            mdd_percent=Decimal("-12.38"),
        ),
        _nav_snapshot(
            snapshot_id="SYNTH-NAV-005",
            day=date(2026, 7, 1),
            total_nav_krw=Decimal("95000000"),
            daily_return_percent=Decimal("3.26"),
            mdd_percent=Decimal("-9.52"),
        ),
    )


def _build_order_intent(scenario: ScenarioName) -> tuple[OrderIntent, ...]:
    """partial-with-trade만 BUY intent 1건."""
    if scenario != "partial-with-trade":
        return ()
    return (
        OrderIntent(
            order_id="SYNTH-ORDER-001",
            correlation_id="SYNTH-CORR-001",
            symbol=SYMBOL,
            market=Market.KR,
            asset_class=AssetClass.KR_EQUITY,
            account_role=AccountRole.PAPER,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            execution_mode=ExecutionMode.NORMAL,
            quantity=Decimal("10"),
            created_at=FIXED_TS,
        ),
    )


def _build_fills(scenario: ScenarioName) -> tuple[Fill, ...]:
    """partial-with-trade만 matching fill 1건."""
    if scenario != "partial-with-trade":
        return ()
    return (
        Fill(
            fill_id="SYNTH-FILL-001",
            order_id="SYNTH-ORDER-001",
            symbol=SYMBOL,
            market=Market.KR,
            side=OrderSide.BUY,
            quantity=Decimal("10"),
            fill_price=Decimal("70000"),
            commission=Money(amount=Decimal("100"), currency=Currency.KRW),
            tax=Money(amount=Decimal("0"), currency=Currency.KRW),
            filled_at=FIXED_TS,
        ),
    )


def _build_metadata(scenario: ScenarioName) -> dict[str, Any]:
    """canonical JSON-compatible synthetic marker."""
    return {
        "synthetic": True,
        "synthetic_scenario": scenario,
        "generated_by": GENERATED_BY,
        "production_use": False,
    }


def _build_review_input(scenario: ScenarioName) -> PaperReviewInput:
    """scenario별 deterministic PaperReviewInput 생성."""
    return PaperReviewInput(
        review_id=SCENARIO_REVIEW_ID[scenario],
        created_at=FIXED_TS,
        period=_build_period(scenario),
        nav_snapshots=_build_nav_snapshots(scenario),
        order_intents=_build_order_intent(scenario),
        fills=_build_fills(scenario),
        metadata=_build_metadata(scenario),
    )


def _serialize_review_input(review_input: PaperReviewInput) -> str:
    payload = canonicalize_payload(review_input.model_dump(mode="json"))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _validate_review_input(review_input: PaperReviewInput) -> None:
    """model object 생성 후 JSON semantic roundtrip 검증."""
    payload = canonicalize_payload(review_input.model_dump(mode="json"))
    PaperReviewInput.model_validate(payload)
    raw = json.loads(_serialize_review_input(review_input))
    PaperReviewInput.model_validate(raw)


def _payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _fail(stage: str, reason: str, *, as_json: bool = False, out: TextIO = sys.stderr) -> int:
    payload = {"outcome": "FAIL", "stage": stage, "reason": reason}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
    else:
        print("Synthetic review builder: FAIL", file=out)
        print(f"stage: {stage}", file=out)
        print(f"reason: {reason}", file=out)
    return 1


def _emit_summary(summary: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False), file=out)
        return
    print(f"Synthetic review builder: {summary['outcome']}", file=out)
    for key in (
        "scenario",
        "sample_sufficiency",
        "expected_report_path",
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
        review_input = _build_review_input(scenario)
        _validate_review_input(review_input)
    except ValidationError as exc:
        return _fail("validation", str(exc), as_json=as_json, out=summary_out)
    except ValueError as exc:
        return _fail("validation", str(exc), as_json=as_json, out=summary_out)

    payload_text = _serialize_review_input(review_input)
    payload_hash = _payload_hash(payload_text)

    if args.validate_only:
        summary = {
            "outcome": "PASS",
            "scenario": scenario,
            "sample_sufficiency": EXPECTED_SAMPLE_SUFFICIENCY[scenario].value,
            "expected_report_path": EXPECTED_REPORT_PATH[scenario],
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
        return _fail(
            "output",
            f"unable to write output file: {exc}",
            as_json=as_json,
            out=summary_out,
        )

    summary = {
        "outcome": "PASS",
        "scenario": scenario,
        "sample_sufficiency": EXPECTED_SAMPLE_SUFFICIENCY[scenario].value,
        "expected_report_path": EXPECTED_REPORT_PATH[scenario],
        "output": str(output_path),
        "payload_hash": payload_hash,
        "validated": "yes",
        "file_written": "yes",
    }
    _emit_summary(summary, as_json=as_json, out=summary_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
