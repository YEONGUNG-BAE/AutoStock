#!/usr/bin/env python3
"""Foundation 8H Production PaperLoopInput assembler.

validated Layer A artifacts + local paper-only context로 PaperLoopInput을 조립·검증한다.
실행 러너·브로커 API·KIS·주문 생성·실행 경로를 호출하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from allocator.models import AllocatorDecision
from allocator.validator import extract_date_ids_from_allocator_decision
from analysis.models import AnalysisDecision
from analysis.validator import extract_date_ids_from_analysis_decision
from build_allocator_manual_packet import PacketError as PortfolioPacketError
from build_allocator_manual_packet import load_portfolio_state
from data.date_id_store import SQLiteDateIdSourceStore
from decision.canonical_json import canonical_json_dumps, canonicalize_payload
from domain._datetime import parse_timezone_aware_datetime, require_timezone_aware_datetime
from domain._strings import normalize_required_string
from domain.enums import AccountRole, Currency, Market
from domain.market import MarketPrice
from paper_loop.models import PaperLoopInput
from risk.models import RiskFilterContext
from run_date_md_smoke import SmokeError, parse_date_md_sections
from scout.models import ScoutSummary
from scout.validator import extract_date_ids_from_scout_summary

StageName = Literal[
    "args",
    "context",
    "scout_summary",
    "allocator_decision",
    "analysis_decision",
    "date_md",
    "store",
    "membership",
    "paper_loop_input",
    "write",
    "complete",
]


class AssemblyError(Exception):
    """PaperLoopInput assembler 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class PaperLoopContextBundle:
    """Foundation 8H local paper loop context bundle."""

    version: int
    run_id: str
    created_at: datetime
    correlation_id: str | None
    market_price: MarketPrice
    risk_context: RiskFilterContext
    metadata: dict[str, Any]


def _output_stem(market: str, symbol: str) -> str:
    return f"{market.lower()}.{symbol}"


def output_filenames(market: str, symbol: str) -> tuple[str, str, str]:
    """8H assembler 출력 파일명 3종."""
    stem = _output_stem(market, symbol)
    return (
        f"paper_loop_input.{stem}.json",
        f"paper_loop_input_assembly.{stem}.txt",
        f"paper_loop_input_summary.{stem}.json",
    )


def load_paper_loop_context(path: Path) -> PaperLoopContextBundle:
    """Paper loop context JSON을 로드하고 Foundation 8H convention으로 검증한다."""
    if not path.is_file():
        raise AssemblyError("context", f"paper loop context not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssemblyError("context", f"invalid paper loop context JSON: {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise AssemblyError("context", "paper loop context root must be a JSON object")

    version = raw.get("version")
    if version != 1:
        raise AssemblyError("context", "version must be exactly 1")

    try:
        run_id = normalize_required_string(raw.get("run_id"), field_name="run_id")
        created_at = parse_timezone_aware_datetime(raw.get("created_at"), field_name="created_at")
    except ValueError as exc:
        raise AssemblyError("context", str(exc)) from exc

    correlation_id_raw = raw.get("correlation_id")
    correlation_id: str | None
    if correlation_id_raw is None:
        correlation_id = None
    else:
        try:
            correlation_id = normalize_required_string(correlation_id_raw, field_name="correlation_id")
        except ValueError as exc:
            raise AssemblyError("context", str(exc)) from exc

    market_price_raw = raw.get("market_price")
    if not isinstance(market_price_raw, dict):
        raise AssemblyError("context", "market_price must be a JSON object")
    try:
        market_price = MarketPrice.model_validate(market_price_raw)
    except ValueError as exc:
        raise AssemblyError("context", str(exc)) from exc

    risk_context_raw = raw.get("risk_context")
    if not isinstance(risk_context_raw, dict):
        raise AssemblyError("context", "risk_context must be a JSON object")
    try:
        risk_context = RiskFilterContext.model_validate(risk_context_raw)
    except ValueError as exc:
        raise AssemblyError("context", str(exc)) from exc

    metadata_raw = raw.get("metadata")
    if not isinstance(metadata_raw, dict):
        raise AssemblyError("context", "metadata must be a JSON object")
    if metadata_raw.get("paper_only") is not True:
        raise AssemblyError("context", "metadata.paper_only must be true")

    if risk_context.currency is not None and risk_context.currency != market_price.currency:
        raise AssemblyError(
            "context",
            "risk_context.currency must match market_price.currency when provided",
        )

    try:
        metadata = {
            "source": normalize_required_string(metadata_raw.get("source"), field_name="metadata.source"),
            "created_by": normalize_required_string(
                metadata_raw.get("created_by"),
                field_name="metadata.created_by",
            ),
            "paper_only": True,
        }
    except ValueError as exc:
        raise AssemblyError("context", str(exc)) from exc

    notes = metadata_raw.get("notes")
    if notes is not None:
        metadata["notes"] = normalize_required_string(notes, field_name="metadata.notes")

    return PaperLoopContextBundle(
        version=1,
        run_id=run_id,
        created_at=created_at,
        correlation_id=correlation_id,
        market_price=market_price,
        risk_context=risk_context,
        metadata=metadata,
    )


def _load_scout_summary(path: Path) -> ScoutSummary:
    if not path.is_file():
        raise AssemblyError("scout_summary", f"validated scout not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssemblyError("scout_summary", f"invalid validated scout JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise AssemblyError("scout_summary", "validated scout must be a JSON object")
    try:
        return ScoutSummary.model_validate(payload)
    except ValueError as exc:
        raise AssemblyError("scout_summary", str(exc)) from exc


def _load_allocator_decision(path: Path) -> AllocatorDecision:
    if not path.is_file():
        raise AssemblyError("allocator_decision", f"validated allocator not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssemblyError("allocator_decision", f"invalid validated allocator JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise AssemblyError("allocator_decision", "validated allocator must be a JSON object")
    try:
        return AllocatorDecision.model_validate(payload)
    except ValueError as exc:
        raise AssemblyError("allocator_decision", str(exc)) from exc


def _load_analysis_decision(path: Path) -> AnalysisDecision:
    if not path.is_file():
        raise AssemblyError("analysis_decision", f"validated analysis not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssemblyError("analysis_decision", f"invalid validated analysis JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise AssemblyError("analysis_decision", "validated analysis must be a JSON object")
    try:
        return AnalysisDecision.model_validate(payload)
    except ValueError as exc:
        raise AssemblyError("analysis_decision", str(exc)) from exc


def _load_date_md_ids(date_md_path: Path) -> frozenset[str]:
    if not date_md_path.is_file():
        raise AssemblyError("date_md", f"Date.md not found: {date_md_path}")
    try:
        sections = parse_date_md_sections(date_md_path.read_text(encoding="utf-8"))
    except SmokeError as exc:
        raise AssemblyError("date_md", exc.message) from exc
    return frozenset(section.date_id for section in sections)


def _verify_store_consistency(*, date_md_path: Path, store_path: Path) -> None:
    if not store_path.is_file():
        raise AssemblyError("store", f"store not found: {store_path}")
    date_md_ids = _load_date_md_ids(date_md_path)
    store = SQLiteDateIdSourceStore(store_path)
    try:
        store_records = store.list_records()
    finally:
        store.close()
    store_ids = {record.date_id.value for record in store_records}
    missing = sorted(date_md_ids - store_ids)
    if missing:
        raise AssemblyError("store", f"Date.md date_id missing from store: {', '.join(missing)}")


def _collect_cited_date_id_values(
    *,
    scout_summary: ScoutSummary | None,
    allocator_decision: AllocatorDecision,
    analysis_decision: AnalysisDecision,
) -> tuple[str, ...]:
    cited: set[str] = set()
    if scout_summary is not None:
        cited.update(date_id.value for date_id in extract_date_ids_from_scout_summary(scout_summary))
    cited.update(date_id.value for date_id in extract_date_ids_from_allocator_decision(allocator_decision))
    cited.update(date_id.value for date_id in extract_date_ids_from_analysis_decision(analysis_decision))
    return tuple(sorted(cited))


def _verify_cited_in_date_md(
    *,
    cited: tuple[str, ...],
    date_md_ids: frozenset[str],
    source_label: str,
) -> None:
    for date_id in cited:
        if date_id not in date_md_ids:
            raise AssemblyError("membership", f"{source_label} cited date_id missing from Date.md: {date_id}")


def _verify_universe_consistency(
    *,
    scout_summary: ScoutSummary | None,
    allocator_decision: AllocatorDecision,
    analysis_decision: AnalysisDecision,
) -> str:
    universe = allocator_decision.universe
    if analysis_decision.universe != universe:
        raise AssemblyError(
            "membership",
            "universe mismatch between AllocatorDecision and AnalysisDecision: "
            f"{universe!r} vs {analysis_decision.universe!r}",
        )
    if scout_summary is not None and scout_summary.universe != universe:
        raise AssemblyError(
            "membership",
            "universe mismatch between ScoutSummary and AllocatorDecision: "
            f"{scout_summary.universe!r} vs {universe!r}",
        )
    return universe


def _verify_symbol_market_consistency(
    *,
    analysis_decision: AnalysisDecision,
    context: PaperLoopContextBundle,
) -> tuple[str, str]:
    market = analysis_decision.market
    symbol = analysis_decision.symbol

    if context.market_price.symbol != symbol:
        raise AssemblyError(
            "membership",
            "market_price.symbol must match AnalysisDecision.symbol: "
            f"{context.market_price.symbol!r} vs {symbol!r}",
        )
    expected_market = Market(market.upper())
    if context.market_price.market != expected_market:
        raise AssemblyError(
            "membership",
            "market_price.market must match AnalysisDecision.market: "
            f"{context.market_price.market.value!r} vs {market!r}",
        )

    if context.risk_context.market is not None and context.risk_context.market != expected_market:
        raise AssemblyError(
            "context",
            "risk_context.market must match AnalysisDecision.market when provided",
        )

    expected_currency = Currency.KRW if expected_market == Market.KR else Currency.USD
    if context.market_price.currency != expected_currency:
        raise AssemblyError(
            "context",
            f"market_price.currency must be {expected_currency.value} for market {market!r}",
        )

    return market, symbol


def _build_loop_metadata(
    *,
    context: PaperLoopContextBundle,
    scout_summary: ScoutSummary | None,
    allocator_decision: AllocatorDecision,
    analysis_decision: AnalysisDecision,
    portfolio_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "foundation": "8H",
        "allocator_decision_id": allocator_decision.decision_id.value,
        "analysis_decision_id": analysis_decision.decision_id.value,
        "context_source": context.metadata.get("source"),
        "context_created_by": context.metadata.get("created_by"),
        "paper_only": True,
    }
    if scout_summary is not None:
        metadata["scout_summary_id"] = scout_summary.summary_id.value
    if portfolio_metadata is not None:
        metadata.update(portfolio_metadata)
    notes = context.metadata.get("notes")
    if notes is not None:
        metadata["context_notes"] = notes
    return canonicalize_payload(metadata)


def _build_assembly_txt(
    *,
    loop_input: PaperLoopInput,
    universe: str,
    cited_date_ids: tuple[str, ...],
    output_paths: dict[str, str],
) -> str:
    analysis = loop_input.analysis_decision
    lines = [
        "PaperLoopInput assembly log (Foundation 8H)",
        "",
        "status: ok",
        f"run_id: {loop_input.normalized_run_id.value}",
        f"created_at: {loop_input.created_at.isoformat()}",
        f"universe: {universe}",
        f"market: {analysis.market}",
        f"symbol: {analysis.symbol}",
    ]
    if loop_input.scout_summary is not None:
        lines.append(f"scout_summary_id: {loop_input.scout_summary.summary_id.value}")
    lines.extend(
        [
            f"allocator_decision_id: {loop_input.allocator_decision.decision_id.value}",
            f"analysis_decision_id: {analysis.decision_id.value}",
            f"market_price: symbol={loop_input.market_price.symbol}, "
            f"market={loop_input.market_price.market.value}, "
            f"currency={loop_input.market_price.currency.value}, "
            f"price={loop_input.market_price.price}",
            f"risk_context.mode: {loop_input.risk_context.mode.value}",
            f"broker_account_role: {loop_input.broker_account_role.value}",
            f"cited_date_ids_count: {len(cited_date_ids)}",
            "",
            "checks:",
            "  Date.md membership: PASS",
            "  Store consistency: PASS",
            "  PaperLoopInput model validation: PASS",
            "",
            "execution: NOT RUN",
            "order generation: NOT RUN",
            "broker: NOT CALLED",
            "KIS: NOT CALLED",
            "",
            "output files:",
        ]
    )
    for key, value in output_paths.items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def _preflight_out_dir(out_dir: Path, *, filenames: tuple[str, str, str], force: bool) -> None:
    existing = [name for name in filenames if (out_dir / name).exists()]
    if existing and not force:
        joined = ", ".join(existing)
        raise AssemblyError(
            "write",
            f"output files already exist: {joined} (use --force to overwrite)",
        )


def run_assemble_paper_loop_input(
    *,
    validated_scout_path: Path | None,
    validated_allocator_path: Path,
    validated_analysis_path: Path,
    portfolio_state_path: Path | None,
    paper_loop_context_path: Path,
    date_md_path: Path,
    store_path: Path,
    out_dir: Path,
    now: datetime | None,
    force: bool,
) -> dict[str, Any]:
    """PaperLoopInput을 조립하고 summary dict를 반환한다."""
    context = load_paper_loop_context(paper_loop_context_path)
    allocator_decision = _load_allocator_decision(validated_allocator_path)
    analysis_decision = _load_analysis_decision(validated_analysis_path)

    scout_summary: ScoutSummary | None = None
    if validated_scout_path is not None:
        scout_summary = _load_scout_summary(validated_scout_path)

    portfolio_metadata: dict[str, Any] | None = None
    if portfolio_state_path is not None:
        try:
            portfolio_state = load_portfolio_state(portfolio_state_path)
        except PortfolioPacketError as exc:
            raise AssemblyError("context", f"portfolio state: {exc.message}") from exc
        portfolio_metadata = {
            "portfolio_snapshot_id": portfolio_state.portfolio_snapshot.snapshot_id,
            "nav_snapshot_id": portfolio_state.nav_snapshot.snapshot_id,
        }

    date_md_ids = _load_date_md_ids(date_md_path)
    _verify_store_consistency(date_md_path=date_md_path, store_path=store_path)

    universe = _verify_universe_consistency(
        scout_summary=scout_summary,
        allocator_decision=allocator_decision,
        analysis_decision=analysis_decision,
    )
    market, symbol = _verify_symbol_market_consistency(
        analysis_decision=analysis_decision,
        context=context,
    )

    cited_date_ids = _collect_cited_date_id_values(
        scout_summary=scout_summary,
        allocator_decision=allocator_decision,
        analysis_decision=analysis_decision,
    )
    if scout_summary is not None:
        scout_cited = tuple(
            sorted({d.value for d in extract_date_ids_from_scout_summary(scout_summary)}),
        )
        _verify_cited_in_date_md(cited=scout_cited, date_md_ids=date_md_ids, source_label="ScoutSummary")
    alloc_cited = tuple(
        sorted({d.value for d in extract_date_ids_from_allocator_decision(allocator_decision)}),
    )
    analysis_cited = tuple(
        sorted({d.value for d in extract_date_ids_from_analysis_decision(analysis_decision)}),
    )
    _verify_cited_in_date_md(cited=alloc_cited, date_md_ids=date_md_ids, source_label="AllocatorDecision")
    _verify_cited_in_date_md(cited=analysis_cited, date_md_ids=date_md_ids, source_label="AnalysisDecision")

    created_at = now if now is not None else context.created_at
    loop_metadata = _build_loop_metadata(
        context=context,
        scout_summary=scout_summary,
        allocator_decision=allocator_decision,
        analysis_decision=analysis_decision,
        portfolio_metadata=portfolio_metadata,
    )

    try:
        loop_input = PaperLoopInput(
            run_id=context.run_id,
            created_at=created_at,
            scout_summary=scout_summary,
            allocator_decision=allocator_decision,
            analysis_decision=analysis_decision,
            risk_context=context.risk_context,
            market_price=context.market_price,
            broker_account_role=AccountRole.PAPER,
            correlation_id=context.correlation_id,
            metadata=loop_metadata,
        )
    except ValueError as exc:
        raise AssemblyError("paper_loop_input", str(exc)) from exc

    try:
        loop_input = PaperLoopInput.model_validate(loop_input.model_dump(mode="json"))
    except ValueError as exc:
        raise AssemblyError("paper_loop_input", str(exc)) from exc

    input_name, txt_name, summary_name = output_filenames(market, symbol)
    _preflight_out_dir(out_dir, filenames=(input_name, txt_name, summary_name), force=force)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_paths = {
        "paper_loop_input": str(out_dir / input_name),
        "assembly_txt": str(out_dir / txt_name),
        "summary_json": str(out_dir / summary_name),
    }

    (out_dir / input_name).write_text(
        canonical_json_dumps(canonicalize_payload(loop_input.model_dump(mode="json"))) + "\n",
        encoding="utf-8",
    )
    (out_dir / txt_name).write_text(
        _build_assembly_txt(
            loop_input=loop_input,
            universe=universe,
            cited_date_ids=cited_date_ids,
            output_paths=output_paths,
        ),
        encoding="utf-8",
    )

    summary: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "run_id": loop_input.normalized_run_id.value,
        "universe": universe,
        "market": market,
        "symbol": symbol,
        "allocator_decision_id": allocator_decision.decision_id.value,
        "analysis_decision_id": analysis_decision.decision_id.value,
        "broker_account_role": loop_input.broker_account_role.value,
        "market_price": {
            "symbol": loop_input.market_price.symbol,
            "market": loop_input.market_price.market.value,
            "currency": loop_input.market_price.currency.value,
            "price": str(loop_input.market_price.price),
            "as_of": loop_input.market_price.as_of.isoformat(),
        },
        "risk_mode": loop_input.risk_context.mode.value,
        "cited_date_ids": list(cited_date_ids),
        "cited_date_ids_count": len(cited_date_ids),
        "output_paths": output_paths,
        "paper_loop_input": output_paths["paper_loop_input"],
        "date_md": str(date_md_path),
        "store": str(store_path),
        "execution_run": False,
        "order_generation_run": False,
        "broker_called": False,
        "kis_called": False,
        "created_at": loop_input.created_at.isoformat(),
        "metadata": loop_metadata,
    }
    if scout_summary is not None:
        summary["scout_summary_id"] = scout_summary.summary_id.value

    (out_dir / summary_name).write_text(
        canonical_json_dumps(summary) + "\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "stage": "complete",
        "output_paths": output_paths,
        "run_id": loop_input.normalized_run_id.value,
        "market": market,
        "symbol": symbol,
        "cited_date_ids_count": len(cited_date_ids),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Foundation 8H Production PaperLoopInput assembler.",
    )
    parser.add_argument("--validated-scout", default=None, help="optional validated ScoutSummary JSON path")
    parser.add_argument("--validated-allocator", required=True, help="validated AllocatorDecision JSON path")
    parser.add_argument("--validated-analysis", required=True, help="validated AnalysisDecision JSON path")
    parser.add_argument("--portfolio-state", default=None, help="optional portfolio state JSON path (8F)")
    parser.add_argument("--paper-loop-context", required=True, help="paper loop context JSON path")
    parser.add_argument("--date-md", required=True, help="exported Date.md path")
    parser.add_argument("--store", required=True, help="SQLiteDateIdSourceStore path")
    parser.add_argument("--out-dir", required=True, help="output directory for assembled PaperLoopInput artifacts")
    parser.add_argument("--now", default=None, help="ISO timezone-aware datetime for PaperLoopInput.created_at")
    parser.add_argument("--force", action="store_true", help="overwrite existing assembly output files")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON summary to stdout")
    parser.add_argument("--verbose", action="store_true", help="print non-sensitive metadata to stderr")
    return parser


def _resolve_now(raw_now: str | None) -> datetime | None:
    if raw_now is None:
        return None
    try:
        return parse_timezone_aware_datetime(raw_now, field_name="now")
    except ValueError as exc:
        raise AssemblyError("args", str(exc)) from exc


def _emit_result(payload: dict[str, Any], *, as_json: bool, out: TextIO) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out)
        return
    status = payload.get("status", "error")
    print(f"PaperLoopInput assembly: {status}", file=out)
    for key in ("stage", "output_paths", "run_id", "market", "symbol", "cited_date_ids_count", "error"):
        if key in payload:
            print(f"{key}: {payload[key]}", file=out)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    if args.verbose:
        print(f"verbose: validated_allocator={args.validated_allocator}", file=stderr)
        print(f"verbose: validated_analysis={args.validated_analysis}", file=stderr)
        print(f"verbose: paper_loop_context={args.paper_loop_context}", file=stderr)
        print(f"verbose: out_dir={args.out_dir}", file=stderr)

    try:
        now = _resolve_now(args.now)
        if now is not None:
            require_timezone_aware_datetime(now, field_name="now")
        payload = run_assemble_paper_loop_input(
            validated_scout_path=Path(args.validated_scout) if args.validated_scout else None,
            validated_allocator_path=Path(args.validated_allocator),
            validated_analysis_path=Path(args.validated_analysis),
            portfolio_state_path=Path(args.portfolio_state) if args.portfolio_state else None,
            paper_loop_context_path=Path(args.paper_loop_context),
            date_md_path=Path(args.date_md),
            store_path=Path(args.store),
            out_dir=Path(args.out_dir),
            now=now,
            force=args.force,
        )
    except AssemblyError as exc:
        payload = {"status": "error", "stage": exc.stage, "error": exc.message}
        _emit_result(payload, as_json=args.json, out=stdout)
        return 1

    _emit_result(payload, as_json=args.json, out=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
