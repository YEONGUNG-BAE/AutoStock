from __future__ import annotations

import json
import math
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from data.kr_candidate_pool import (
    KrCandidatePoolDocument,
    KrCandidatePoolEntry,
    KrCandidatePoolError,
    parse_kr_candidate_pool_toml,
    render_selected_candidates_toml,
    select_candidates,
    validate_exported_candidates_toml,
)

StageName = Literal["args", "parse", "rank", "write", "validate", "complete"]

SCORE_PRECISION = 4
_MISSING_PRIORITY_SORT_KEY = 2_147_483_647

_SIGNAL_ROOT_KEYS = frozenset({"version", "name", "description", "as_of", "score_version", "signals"})
_SIGNAL_ENTRY_KEYS = frozenset(
    {
        "symbol",
        "market",
        "liquidity_score",
        "market_cap_score",
        "quality_score",
        "momentum_score",
        "risk_penalty",
        "notes",
    }
)

_SCORE_WEIGHTS: dict[str, float] = {
    "liquidity_score": 0.35,
    "market_cap_score": 0.25,
    "quality_score": 0.20,
    "momentum_score": 0.20,
    "risk_penalty": -0.20,
}

_FORBIDDEN_RANKED_OUTPUT_FIELDS = frozenset(
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


class KrCandidateRankerError(ValueError):
    """KR candidate ranking 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrRankingSignalEntry:
    """fixture ranking signal (local-only; not live market data)."""

    symbol: str
    market: str
    liquidity_score: float
    market_cap_score: float
    quality_score: float
    momentum_score: float
    risk_penalty: float
    notes: str | None


@dataclass(frozen=True)
class KrRankingSignalsDocument:
    """ranking signal TOML root document."""

    version: int
    name: str
    description: str
    as_of: datetime
    score_version: str
    signals: tuple[KrRankingSignalEntry, ...]


@dataclass(frozen=True)
class RankedKrCandidate:
    """ranked candidate with explainable score metadata."""

    rank: int
    candidate: KrCandidatePoolEntry
    score: float
    score_components: dict[str, float]
    score_contributions: dict[str, float]
    explanation: tuple[str, ...]


def parse_ranking_signals_toml(path: Path) -> KrRankingSignalsDocument:
    """로컬 ranking signal TOML을 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrCandidateRankerError("parse", f"ranking signals file not found: {path}")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise KrCandidateRankerError("parse", "ranking signals TOML root must be a table")

    unknown_root = set(raw.keys()) - _SIGNAL_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrCandidateRankerError("parse", f"unknown ranking signals root fields: {joined}")

    version = raw.get("version")
    if version != 1:
        raise KrCandidateRankerError("parse", "version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _required_text(raw.get("description"), field_name="description")
    score_version = _required_text(raw.get("score_version"), field_name="score_version")
    as_of = _parse_timezone_aware_datetime(raw.get("as_of"), field_name="as_of")

    signals_raw = raw.get("signals")
    if not isinstance(signals_raw, list) or not signals_raw:
        raise KrCandidateRankerError("parse", "signals must contain at least one entry")

    signals: list[KrRankingSignalEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, entry_raw in enumerate(signals_raw):
        if not isinstance(entry_raw, dict):
            raise KrCandidateRankerError("parse", f"signals[{index}] must be a table")
        signal = _parse_signal_entry(entry_raw, index=index)
        key = (signal.market, signal.symbol)
        if key in seen:
            raise KrCandidateRankerError(
                "parse",
                f"duplicate ranking signal: market={signal.market!r}, symbol={signal.symbol!r}",
            )
        seen.add(key)
        signals.append(signal)

    return KrRankingSignalsDocument(
        version=1,
        name=name,
        description=description,
        as_of=as_of,
        score_version=score_version,
        signals=tuple(signals),
    )


def compute_ranking_score(signal: KrRankingSignalEntry) -> tuple[float, dict[str, float], dict[str, float], tuple[str, ...]]:
    """deterministic weighted score와 explainable component metadata를 계산한다."""
    components = {
        "liquidity_score": signal.liquidity_score,
        "market_cap_score": signal.market_cap_score,
        "quality_score": signal.quality_score,
        "momentum_score": signal.momentum_score,
        "risk_penalty": signal.risk_penalty,
    }
    contributions: dict[str, float] = {}
    raw_total = 0.0
    for key, component_value in components.items():
        weighted = _SCORE_WEIGHTS[key] * component_value
        rounded_weighted = _round_score(weighted)
        contributions[key] = rounded_weighted
        raw_total += rounded_weighted

    clamped = _round_score(max(0.0, min(1.0, raw_total)))
    explanation = _build_explanation(contributions)
    return clamped, components, contributions, explanation


def rank_selected_candidates(
    candidates: Sequence[KrCandidatePoolEntry],
    signals: KrRankingSignalsDocument,
) -> tuple[RankedKrCandidate, ...]:
    """선택된 candidate subset을 deterministic ranking signal로 rank한다."""
    signal_index = {(entry.market, entry.symbol): entry for entry in signals.signals}
    ranked_rows: list[RankedKrCandidate] = []

    for candidate in candidates:
        signal = signal_index.get((candidate.market, candidate.symbol))
        if signal is None:
            raise KrCandidateRankerError(
                "rank",
                f"missing ranking signal for market={candidate.market!r}, symbol={candidate.symbol!r}",
            )
        score, components, contributions, explanation = compute_ranking_score(signal)
        ranked_rows.append(
            RankedKrCandidate(
                rank=0,
                candidate=candidate,
                score=score,
                score_components=components,
                score_contributions=contributions,
                explanation=explanation,
            )
        )

    ranked_rows.sort(
        key=lambda row: (
            -row.score,
            row.candidate.sector,
            _MISSING_PRIORITY_SORT_KEY if row.candidate.priority is None else row.candidate.priority,
            row.candidate.symbol,
        )
    )
    return tuple(
        RankedKrCandidate(
            rank=index,
            candidate=row.candidate,
            score=row.score,
            score_components=row.score_components,
            score_contributions=row.score_contributions,
            explanation=row.explanation,
        )
        for index, row in enumerate(ranked_rows, start=1)
    )


def build_ranked_output_payload(
    *,
    pool: KrCandidatePoolDocument,
    signals: KrRankingSignalsDocument,
    ranked: Sequence[RankedKrCandidate],
) -> dict[str, Any]:
    """reviewable ranked JSON payload를 구성한다."""
    payload: dict[str, Any] = {
        "status": "ok",
        "stage": "complete",
        "pool_name": pool.name,
        "signals_name": signals.name,
        "score_version": signals.score_version,
        "as_of": signals.as_of.isoformat(),
        "score_precision": SCORE_PRECISION,
        "ranked_count": len(ranked),
        "ranked": [_ranked_entry_to_dict(entry) for entry in ranked],
    }
    _assert_no_forbidden_ranked_fields(payload)
    return payload


def write_ranked_json(
    *,
    ranked_out: Path,
    payload: dict[str, Any],
    force: bool,
) -> None:
    """ranked JSON artifact를 UTF-8로 기록한다."""
    if ranked_out.exists() and not force:
        raise KrCandidateRankerError(
            "write",
            f"ranked output already exists: {ranked_out} (use --force to overwrite)",
        )
    ranked_out.parent.mkdir(parents=True, exist_ok=True)
    ranked_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_ranked_selected_candidates_toml(
    *,
    out_candidates: Path,
    selection_name: str,
    selection_description: str,
    ranked: Sequence[RankedKrCandidate],
    top_n: int | None,
    force: bool,
) -> None:
    """ranked subset을 clean 3F1 candidate TOML로 export한다."""
    if out_candidates.exists() and not force:
        raise KrCandidateRankerError(
            "write",
            f"selected candidates output already exists: {out_candidates} (use --force to overwrite)",
        )

    selected_entries = _select_ranked_entries(ranked, top_n=top_n)
    if not selected_entries:
        raise KrCandidateRankerError("rank", "ranked selection produced zero candidates")

    out_candidates.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_selected_candidates_toml(
        name=selection_name,
        description=selection_description,
        selected=selected_entries,
    )
    out_candidates.write_text(rendered, encoding="utf-8")


def rank_kr_candidates(
    *,
    candidate_pool_path: Path,
    ranking_signals_path: Path,
    ranked_out: Path,
    sectors: set[str] | None = None,
    max_total: int | None = None,
    max_per_sector: int | None = None,
    include_disabled: bool = False,
    include_ineligible: bool = False,
    selected_candidates_out: Path | None = None,
    selection_name: str | None = None,
    selection_description: str | None = None,
    top_n: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """candidate pool + fixture signals → ranked JSON (+ optional 3F1 candidate export)."""
    try:
        pool = parse_kr_candidate_pool_toml(candidate_pool_path)
    except KrCandidatePoolError as exc:
        raise KrCandidateRankerError(exc.stage, exc.message) from exc

    try:
        signals = parse_ranking_signals_toml(ranking_signals_path)
    except KrCandidateRankerError:
        raise
    except Exception as exc:
        raise KrCandidateRankerError("parse", str(exc)) from exc

    try:
        selected = select_candidates(
            pool,
            sectors=sectors,
            max_total=max_total,
            max_per_sector=max_per_sector,
            include_disabled=include_disabled,
            include_ineligible=include_ineligible,
        )
    except KrCandidatePoolError as exc:
        if exc.stage == "select":
            raise KrCandidateRankerError("args", exc.message) from exc
        raise KrCandidateRankerError(exc.stage, exc.message) from exc

    if not selected:
        raise KrCandidateRankerError("rank", "selection produced zero candidates")

    ranked = rank_selected_candidates(selected, signals)
    payload = build_ranked_output_payload(pool=pool, signals=signals, ranked=ranked)
    write_ranked_json(ranked_out=ranked_out, payload=payload, force=force)

    result: dict[str, Any] = {
        **payload,
        "ranked_out": str(ranked_out),
    }

    if selected_candidates_out is not None:
        if selection_name is None:
            raise KrCandidateRankerError("validate", "selection_name is required for selected candidate export")
        effective_description = (
            selection_description
            if selection_description is not None
            else f"Ranked KR candidates exported from pool {pool.name}."
        )
        try:
            write_ranked_selected_candidates_toml(
                out_candidates=selected_candidates_out,
                selection_name=selection_name,
                selection_description=effective_description,
                ranked=ranked,
                top_n=top_n,
                force=force,
            )
            validate_exported_candidates_toml(selected_candidates_out)
        except KrCandidatePoolError as exc:
            raise KrCandidateRankerError(exc.stage, exc.message) from exc
        result["selected_candidates_out"] = str(selected_candidates_out)
        result["selected_count"] = len(_select_ranked_entries(ranked, top_n=top_n))

    return result


def _select_ranked_entries(
    ranked: Sequence[RankedKrCandidate],
    *,
    top_n: int | None,
) -> tuple[KrCandidatePoolEntry, ...]:
    filtered: list[RankedKrCandidate] = list(ranked)
    if top_n is not None:
        filtered = filtered[:top_n]
    return tuple(row.candidate for row in filtered)


def _ranked_entry_to_dict(entry: RankedKrCandidate) -> dict[str, Any]:
    candidate = entry.candidate
    return {
        "rank": entry.rank,
        "symbol": candidate.symbol,
        "market": candidate.market,
        "sector": candidate.sector,
        "industry": candidate.industry,
        "score": entry.score,
        "score_components": dict(entry.score_components),
        "score_contributions": dict(entry.score_contributions),
        "explanation": list(entry.explanation),
    }


def _build_explanation(contributions: dict[str, float]) -> tuple[str, ...]:
    lines: list[str] = []
    for key in ("liquidity_score", "market_cap_score", "quality_score", "momentum_score"):
        lines.append(f"{key} contributed {_format_score(contributions[key])}")
    penalty = contributions["risk_penalty"]
    if penalty < 0:
        lines.append(f"risk_penalty subtracted {_format_score(abs(penalty))}")
    else:
        lines.append(f"risk_penalty contributed {_format_score(penalty)}")
    return tuple(lines)


def _parse_signal_entry(raw: dict[str, Any], *, index: int) -> KrRankingSignalEntry:
    prefix = f"signals[{index}]"
    unknown = set(raw.keys()) - _SIGNAL_ENTRY_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrCandidateRankerError("parse", f"{prefix}: unknown fields: {joined}")

    market = _required_text(raw.get("market"), field_name=f"{prefix}.market")
    if market != "KR":
        raise KrCandidateRankerError("parse", f"{prefix}.market must be 'KR'")

    symbol = _required_text(raw.get("symbol"), field_name=f"{prefix}.symbol")

    notes_raw = raw.get("notes")
    notes: str | None
    if notes_raw is None:
        notes = None
    else:
        notes = _required_text(notes_raw, field_name=f"{prefix}.notes")

    return KrRankingSignalEntry(
        symbol=symbol,
        market=market,
        liquidity_score=_parse_unit_score(raw.get("liquidity_score"), field_name=f"{prefix}.liquidity_score"),
        market_cap_score=_parse_unit_score(raw.get("market_cap_score"), field_name=f"{prefix}.market_cap_score"),
        quality_score=_parse_unit_score(raw.get("quality_score"), field_name=f"{prefix}.quality_score"),
        momentum_score=_parse_unit_score(raw.get("momentum_score"), field_name=f"{prefix}.momentum_score"),
        risk_penalty=_parse_unit_score(raw.get("risk_penalty"), field_name=f"{prefix}.risk_penalty"),
        notes=notes,
    )


def _parse_unit_score(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KrCandidateRankerError("parse", f"{field_name} must be a number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise KrCandidateRankerError("parse", f"{field_name} must be finite")
    if numeric < 0.0 or numeric > 1.0:
        raise KrCandidateRankerError("parse", f"{field_name} must be between 0.0 and 1.0")
    return numeric


def _parse_timezone_aware_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise KrCandidateRankerError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrCandidateRankerError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrCandidateRankerError("parse", f"{field_name} contains a control character")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KrCandidateRankerError("parse", f"{field_name} must be ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise KrCandidateRankerError("parse", f"{field_name} must be timezone-aware")
    return parsed


def _assert_no_forbidden_ranked_fields(payload: dict[str, Any]) -> None:
    def _walk(value: Any, *, path: str) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in _FORBIDDEN_RANKED_OUTPUT_FIELDS:
                    raise KrCandidateRankerError(
                        "validate",
                        f"ranked output contains forbidden field at {path}.{key}",
                    )
                _walk(nested, path=f"{path}.{key}")
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                _walk(nested, path=f"{path}[{index}]")

    _walk(payload, path="payload")


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrCandidateRankerError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrCandidateRankerError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrCandidateRankerError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrCandidateRankerError("parse", f"{field_name} contains a control character")
    return normalized


def _round_score(value: float) -> float:
    return round(value, SCORE_PRECISION)


def _format_score(value: float) -> str:
    return f"{_round_score(value):.{SCORE_PRECISION}f}"
