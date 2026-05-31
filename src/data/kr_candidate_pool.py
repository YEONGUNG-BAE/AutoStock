from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from data.dart_corp_code_resolver import DartCorpCodeResolverError, normalize_stock_code
from data.kr_provider_mapping_generator import KrProviderMappingGeneratorError, parse_kr_candidates_toml

StageName = Literal["parse", "select", "write", "validate"]

_KR_YFINANCE_SUFFIXES = (".KS", ".KQ")
_SECTOR_SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_POOL_ROOT_KEYS = frozenset({"version", "name", "description", "base_market", "candidates"})
_POOL_ENTRY_KEYS = frozenset(
    {
        "symbol",
        "market",
        "display_name",
        "stock_code",
        "corp_name",
        "yfinance_provider_symbol",
        "currency",
        "sector",
        "industry",
        "enabled",
        "eligible",
        "priority",
        "notes",
    }
)
_EXPORT_ROOT_KEYS = frozenset({"version", "name", "description", "candidates"})
_EXPORT_ENTRY_KEYS = frozenset(
    {
        "symbol",
        "market",
        "enabled",
        "display_name",
        "stock_code",
        "corp_name",
        "yfinance_provider_symbol",
        "currency",
    }
)
_MISSING_PRIORITY_SORT_KEY = 2_147_483_647


class KrCandidatePoolError(ValueError):
    """KR sector-tagged candidate pool 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrCandidatePoolEntry:
    """sector-tagged KR candidate pool entry (corp_code 미포함)."""

    symbol: str
    market: str
    display_name: str
    stock_code: str
    corp_name: str
    yfinance_provider_symbol: str
    currency: str
    sector: str
    industry: str
    enabled: bool
    eligible: bool
    priority: int | None
    notes: str | None


@dataclass(frozen=True)
class KrCandidatePoolDocument:
    """candidate pool TOML root document."""

    version: int
    name: str
    description: str
    base_market: str
    candidates: tuple[KrCandidatePoolEntry, ...]


def parse_kr_candidate_pool_toml(path: Path) -> KrCandidatePoolDocument:
    """로컬 sector-tagged candidate pool TOML을 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrCandidatePoolError("parse", f"candidate pool file not found: {path}")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise KrCandidatePoolError("parse", "candidate pool TOML root must be a table")

    unknown_root = set(raw.keys()) - _POOL_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrCandidatePoolError("parse", f"unknown candidate pool root fields: {joined}")

    version = raw.get("version")
    if version != 1:
        raise KrCandidatePoolError("parse", "version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _required_text(raw.get("description"), field_name="description")
    base_market = _required_text(raw.get("base_market"), field_name="base_market")
    if base_market != "KR":
        raise KrCandidatePoolError("parse", "base_market must be 'KR'")

    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise KrCandidatePoolError("parse", "candidates must contain at least one entry")

    candidates: list[KrCandidatePoolEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, entry_raw in enumerate(candidates_raw):
        if not isinstance(entry_raw, dict):
            raise KrCandidatePoolError("parse", f"candidates[{index}] must be a table")
        candidate = _parse_pool_entry(entry_raw, index=index)
        key = (candidate.market, candidate.symbol)
        if key in seen:
            raise KrCandidatePoolError(
                "parse",
                f"duplicate candidate entry: market={candidate.market!r}, symbol={candidate.symbol!r}",
            )
        seen.add(key)
        candidates.append(candidate)

    return KrCandidatePoolDocument(
        version=1,
        name=name,
        description=description,
        base_market=base_market,
        candidates=tuple(candidates),
    )


def select_candidates(
    pool: KrCandidatePoolDocument,
    *,
    sectors: set[str] | None = None,
    max_total: int | None = None,
    max_per_sector: int | None = None,
    include_disabled: bool = False,
    include_ineligible: bool = False,
) -> tuple[KrCandidatePoolEntry, ...]:
    """sector/priority 규칙에 따라 deterministic subset을 선택한다."""
    if max_total is not None and max_total <= 0:
        raise KrCandidatePoolError("select", "max_total must be a positive integer")
    if max_per_sector is not None and max_per_sector <= 0:
        raise KrCandidatePoolError("select", "max_per_sector must be a positive integer")

    filtered: list[KrCandidatePoolEntry] = []
    for candidate in pool.candidates:
        if not include_disabled and not candidate.enabled:
            continue
        if not include_ineligible and not candidate.eligible:
            continue
        if sectors is not None and candidate.sector not in sectors:
            continue
        filtered.append(candidate)

    filtered.sort(
        key=lambda entry: (
            entry.sector,
            _MISSING_PRIORITY_SORT_KEY if entry.priority is None else entry.priority,
            entry.symbol,
        )
    )

    if max_per_sector is not None:
        per_sector: dict[str, list[KrCandidatePoolEntry]] = {}
        for entry in filtered:
            per_sector.setdefault(entry.sector, []).append(entry)
        capped: list[KrCandidatePoolEntry] = []
        for sector in sorted(per_sector):
            capped.extend(per_sector[sector][:max_per_sector])
        filtered = capped

    if max_total is not None:
        filtered = filtered[:max_total]

    return tuple(filtered)


def render_selected_candidates_toml(
    *,
    name: str,
    description: str,
    selected: Sequence[KrCandidatePoolEntry],
) -> str:
    """선택 subset을 3F1 candidate TOML schema로 렌더한다 (pool-only 필드 제외)."""
    lines = [
        "# Selected KR candidates exported from sector-tagged pool (3G1).",
        "version = 1",
        f"name = {_toml_string(name)}",
        f"description = {_toml_string(description)}",
        "",
    ]
    for entry in selected:
        lines.extend(
            [
                "[[candidates]]",
                f"symbol = {_toml_string(entry.symbol)}",
                f"market = {_toml_string(entry.market)}",
                f"enabled = {_toml_bool(entry.enabled)}",
                f"display_name = {_toml_string(entry.display_name)}",
                f"stock_code = {_toml_string(entry.stock_code)}",
                f"corp_name = {_toml_string(entry.corp_name)}",
                f"yfinance_provider_symbol = {_toml_string(entry.yfinance_provider_symbol)}",
                f"currency = {_toml_string(entry.currency)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_selected_candidates_toml(
    *,
    out_candidates: Path,
    name: str,
    description: str,
    selected: Sequence[KrCandidatePoolEntry],
    force: bool,
) -> None:
    """선택 subset을 3F1 candidate TOML로 기록한다."""
    if out_candidates.exists() and not force:
        raise KrCandidatePoolError(
            "write",
            f"output already exists: {out_candidates} (use --force to overwrite)",
        )
    out_candidates.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_selected_candidates_toml(
        name=name,
        description=description,
        selected=selected,
    )
    out_candidates.write_text(rendered, encoding="utf-8")


def validate_exported_candidates_toml(path: Path) -> None:
    """export된 candidate TOML이 3F1 schema만 포함하는지 self-check한다."""
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise KrCandidatePoolError("validate", "exported candidate TOML root must be a table")

    unknown_root = set(raw.keys()) - _EXPORT_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrCandidatePoolError("validate", f"exported root contains forbidden fields: {joined}")

    if "base_market" in raw:
        raise KrCandidatePoolError("validate", "exported root must not contain base_market")

    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list):
        raise KrCandidatePoolError("validate", "exported candidates must be a list")

    forbidden_entry_fields = frozenset(
        {"sector", "industry", "eligible", "priority", "notes", "corp_code"}
    )
    for index, entry_raw in enumerate(candidates_raw):
        if not isinstance(entry_raw, dict):
            raise KrCandidatePoolError("validate", f"exported candidates[{index}] must be a table")
        unknown_entry = set(entry_raw.keys()) - _EXPORT_ENTRY_KEYS
        if unknown_entry:
            joined = ", ".join(sorted(unknown_entry))
            raise KrCandidatePoolError(
                "validate",
                f"exported candidates[{index}] contains forbidden fields: {joined}",
            )
        leaked = forbidden_entry_fields & set(entry_raw.keys())
        if leaked:
            joined = ", ".join(sorted(leaked))
            raise KrCandidatePoolError(
                "validate",
                f"exported candidates[{index}] contains pool-only fields: {joined}",
            )

    try:
        parse_kr_candidates_toml(path)
    except KrProviderMappingGeneratorError as exc:
        raise KrCandidatePoolError("validate", exc.message) from exc


def export_selected_candidates(
    pool: KrCandidatePoolDocument,
    *,
    out_candidates: Path,
    export_name: str,
    export_description: str,
    sectors: set[str] | None = None,
    max_total: int | None = None,
    max_per_sector: int | None = None,
    include_disabled: bool = False,
    include_ineligible: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """pool에서 subset을 선택하고 3F1 candidate TOML로 export/validate한다."""
    selected = select_candidates(
        pool,
        sectors=sectors,
        max_total=max_total,
        max_per_sector=max_per_sector,
        include_disabled=include_disabled,
        include_ineligible=include_ineligible,
    )
    if not selected:
        raise KrCandidatePoolError("select", "selection produced zero candidates")

    write_selected_candidates_toml(
        out_candidates=out_candidates,
        name=export_name,
        description=export_description,
        selected=selected,
        force=force,
    )
    validate_exported_candidates_toml(out_candidates)

    sector_list = sorted({entry.sector for entry in selected})
    return {
        "status": "ok",
        "stage": "complete",
        "pool_name": pool.name,
        "candidates_read": len(pool.candidates),
        "candidates_selected": len(selected),
        "sectors": sector_list,
        "out_candidates": str(out_candidates),
        "selected": [
            {
                "symbol": entry.symbol,
                "market": entry.market,
                "sector": entry.sector,
                "yfinance_provider_symbol": entry.yfinance_provider_symbol,
            }
            for entry in selected
        ],
    }


def _parse_pool_entry(raw: dict[str, Any], *, index: int) -> KrCandidatePoolEntry:
    prefix = f"candidates[{index}]"
    if "corp_code" in raw:
        raise KrCandidatePoolError(
            "parse",
            f"{prefix}: corp_code must not appear in candidate pool; use resolver/generator path",
        )

    unknown = set(raw.keys()) - _POOL_ENTRY_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrCandidatePoolError("parse", f"{prefix}: unknown fields: {joined}")

    market = _required_text(raw.get("market"), field_name=f"{prefix}.market")
    if market != "KR":
        raise KrCandidatePoolError("parse", f"{prefix}.market must be 'KR'")

    display_name = _required_text(raw.get("display_name"), field_name=f"{prefix}.display_name")
    corp_name = _required_text(raw.get("corp_name"), field_name=f"{prefix}.corp_name")
    sector = _required_text(raw.get("sector"), field_name=f"{prefix}.sector")
    _validate_sector_slug(sector, field_name=f"{prefix}.sector")
    industry = _required_text(raw.get("industry"), field_name=f"{prefix}.industry")

    yfinance_provider_symbol = _required_text(
        raw.get("yfinance_provider_symbol"),
        field_name=f"{prefix}.yfinance_provider_symbol",
    )
    if not yfinance_provider_symbol.endswith(_KR_YFINANCE_SUFFIXES):
        raise KrCandidatePoolError(
            "parse",
            f"{prefix}.yfinance_provider_symbol must end with .KS or .KQ",
        )

    currency = _required_text(raw.get("currency"), field_name=f"{prefix}.currency")
    if currency != "KRW":
        raise KrCandidatePoolError("parse", f"{prefix}.currency must be 'KRW'")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise KrCandidatePoolError("parse", f"{prefix}.enabled must be a boolean")

    eligible = raw.get("eligible")
    if not isinstance(eligible, bool):
        raise KrCandidatePoolError("parse", f"{prefix}.eligible must be a boolean")

    priority_raw = raw.get("priority")
    priority: int | None
    if priority_raw is None:
        priority = None
    elif isinstance(priority_raw, bool) or not isinstance(priority_raw, int):
        raise KrCandidatePoolError("parse", f"{prefix}.priority must be an integer")
    else:
        priority = priority_raw

    notes_raw = raw.get("notes")
    notes: str | None
    if notes_raw is None:
        notes = None
    else:
        notes = _required_text(notes_raw, field_name=f"{prefix}.notes")

    try:
        normalized_stock_code = normalize_stock_code(
            _required_text(raw.get("stock_code"), field_name=f"{prefix}.stock_code")
        )
    except DartCorpCodeResolverError as exc:
        raise KrCandidatePoolError("parse", f"{prefix}.stock_code: {exc}") from exc

    symbol_raw = _required_text(raw.get("symbol"), field_name=f"{prefix}.symbol")
    try:
        normalized_symbol = normalize_stock_code(symbol_raw)
    except DartCorpCodeResolverError as exc:
        raise KrCandidatePoolError("parse", f"{prefix}.symbol: {exc}") from exc

    if normalized_symbol != normalized_stock_code:
        raise KrCandidatePoolError(
            "parse",
            f"{prefix}: symbol must match normalized stock_code "
            f"(symbol={normalized_symbol!r}, stock_code={normalized_stock_code!r})",
        )

    return KrCandidatePoolEntry(
        symbol=normalized_symbol,
        market=market,
        display_name=display_name,
        stock_code=normalized_stock_code,
        corp_name=corp_name,
        yfinance_provider_symbol=yfinance_provider_symbol,
        currency=currency,
        sector=sector,
        industry=industry,
        enabled=enabled,
        eligible=eligible,
        priority=priority,
        notes=notes,
    )


def _validate_sector_slug(value: str, *, field_name: str) -> None:
    if value != value.lower():
        raise KrCandidatePoolError("parse", f"{field_name} must be lower-case ASCII slug")
    if not _SECTOR_SLUG_PATTERN.match(value):
        raise KrCandidatePoolError(
            "parse",
            f"{field_name} must contain only [a-z0-9_-] characters",
        )


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrCandidatePoolError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrCandidatePoolError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrCandidatePoolError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrCandidatePoolError("parse", f"{field_name} contains a control character")
    return normalized


def _toml_string(value: str) -> str:
    if _contains_control_character(value):
        raise KrCandidatePoolError("validate", "rendered text contains a control character")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
