from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from data.dart_corp_code_resolver import (
    DartCorpCodeEntry,
    DartCorpCodeResolverError,
    normalize_stock_code,
    parse_corp_code_xml_file,
    parse_corp_code_zip_file,
    resolve_corp_code_by_stock_code,
)
from data.provider_mapping_registry import (
    ProviderMappingError,
    load_provider_mapping_toml,
    validate_provider_mappings_cover_universe,
)
from domain.universe import load_universe_toml

StageName = Literal["parse", "resolve", "write", "validate"]

_KR_YFINANCE_SUFFIXES = (".KS", ".KQ")
_CANDIDATE_ROOT_KEYS = frozenset({"version", "name", "description", "candidates"})
_CANDIDATE_ENTRY_KEYS = frozenset(
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


class KrProviderMappingGeneratorError(ValueError):
    """KR universe/provider mapping generator 실패."""

    def __init__(self, stage: StageName, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


@dataclass(frozen=True)
class KrCandidateEntry:
    """operator-curated KR candidate (corp_code 미포함)."""

    symbol: str
    market: str
    enabled: bool
    display_name: str
    stock_code: str
    corp_name: str
    yfinance_provider_symbol: str
    currency: str


@dataclass(frozen=True)
class KrCandidatesDocument:
    """candidate TOML root document."""

    version: int
    name: str
    description: str
    candidates: tuple[KrCandidateEntry, ...]


@dataclass(frozen=True)
class ResolvedKrCandidate:
    """resolver provenance가 반영된 candidate."""

    symbol: str
    market: str
    enabled: bool
    display_name: str
    stock_code: str
    corp_name: str
    yfinance_provider_symbol: str
    currency: str
    corp_code: str
    dart_corp_name: str


def parse_kr_candidates_toml(path: Path) -> KrCandidatesDocument:
    """로컬 candidate TOML을 strict schema로 파싱한다."""
    if not path.is_file():
        raise KrProviderMappingGeneratorError("parse", f"candidate file not found: {path}")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    if not isinstance(raw, dict):
        raise KrProviderMappingGeneratorError("parse", "candidate TOML root must be a table")

    unknown_root = set(raw.keys()) - _CANDIDATE_ROOT_KEYS
    if unknown_root:
        joined = ", ".join(sorted(unknown_root))
        raise KrProviderMappingGeneratorError("parse", f"unknown candidate root fields: {joined}")

    version = raw.get("version")
    if version != 1:
        raise KrProviderMappingGeneratorError("parse", "version must be exactly 1")

    name = _required_text(raw.get("name"), field_name="name")
    description = _required_text(raw.get("description"), field_name="description")

    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list) or not candidates_raw:
        raise KrProviderMappingGeneratorError("parse", "candidates must contain at least one entry")

    candidates: list[KrCandidateEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, entry_raw in enumerate(candidates_raw):
        if not isinstance(entry_raw, dict):
            raise KrProviderMappingGeneratorError("parse", f"candidates[{index}] must be a table")
        candidate = _parse_candidate_entry(entry_raw, index=index)
        key = (candidate.market, candidate.symbol)
        if key in seen:
            raise KrProviderMappingGeneratorError(
                "parse",
                f"duplicate candidate entry: market={candidate.market!r}, symbol={candidate.symbol!r}",
            )
        seen.add(key)
        candidates.append(candidate)

    return KrCandidatesDocument(
        version=1,
        name=name,
        description=description,
        candidates=tuple(candidates),
    )


def load_corp_code_entries(
    *,
    corp_code_xml: Path | None,
    corp_code_zip: Path | None,
) -> tuple[DartCorpCodeEntry, ...]:
    """로컬 corp-code XML 또는 ZIP snapshot을 로드한다."""
    if corp_code_xml is not None:
        try:
            return parse_corp_code_xml_file(corp_code_xml)
        except DartCorpCodeResolverError as exc:
            raise KrProviderMappingGeneratorError("resolve", str(exc)) from exc
    if corp_code_zip is not None:
        try:
            return parse_corp_code_zip_file(corp_code_zip)
        except DartCorpCodeResolverError as exc:
            raise KrProviderMappingGeneratorError("resolve", str(exc)) from exc
    raise KrProviderMappingGeneratorError(
        "resolve",
        "corp-code source is required",
    )


def resolve_kr_candidates(
    candidates: Sequence[KrCandidateEntry],
    *,
    corp_code_entries: Sequence[DartCorpCodeEntry],
) -> tuple[ResolvedKrCandidate, ...]:
    """candidate마다 local corp-code resolver로 DART mapping을 해석한다."""
    resolved: list[ResolvedKrCandidate] = []
    for candidate in candidates:
        try:
            match = resolve_corp_code_by_stock_code(
                corp_code_entries,
                candidate.stock_code,
                corp_name=candidate.corp_name,
            )
        except DartCorpCodeResolverError as exc:
            raise KrProviderMappingGeneratorError(
                "resolve",
                f"failed to resolve corp_code for symbol={candidate.symbol!r}: {exc}",
            ) from exc

        resolved.append(
            ResolvedKrCandidate(
                symbol=candidate.symbol,
                market=candidate.market,
                enabled=candidate.enabled,
                display_name=candidate.display_name,
                stock_code=candidate.stock_code,
                corp_name=candidate.corp_name,
                yfinance_provider_symbol=candidate.yfinance_provider_symbol,
                currency=candidate.currency,
                corp_code=match.corp_code,
                dart_corp_name=match.corp_name,
            )
        )
    return tuple(resolved)


def render_universe_toml(
    *,
    name: str,
    description: str,
    resolved: Sequence[ResolvedKrCandidate],
) -> str:
    """기존 universe schema에 맞는 TOML 문자열을 생성한다."""
    lines = [
        "# Generated KR universe (3F1). Local-file only.",
        "version = 1",
        f"name = {_toml_string(name)}",
        f"description = {_toml_string(description)}",
        'base_market = "KR"',
        "",
    ]
    for entry in resolved:
        lines.extend(
            [
                "[[symbols]]",
                f"symbol = {_toml_string(entry.symbol)}",
                f"market = {_toml_string(entry.market)}",
                f"enabled = {_toml_bool(entry.enabled)}",
                f"display_name = {_toml_string(entry.display_name)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_provider_mapping_toml(
    *,
    name: str,
    description: str,
    resolved: Sequence[ResolvedKrCandidate],
) -> str:
    """기존 provider mapping registry schema에 맞는 TOML 문자열을 생성한다."""
    lines = [
        "# Generated KR provider mapping registry (3F1). Local-file only.",
        "version = 1",
        f"name = {_toml_string(name)}",
        f"description = {_toml_string(description)}",
        "",
    ]
    for entry in resolved:
        lines.extend(
            [
                "[[mappings]]",
                f"symbol = {_toml_string(entry.symbol)}",
                f"market = {_toml_string(entry.market)}",
                f"display_name = {_toml_string(entry.display_name)}",
                f"enabled = {_toml_bool(entry.enabled)}",
                f"stock_code = {_toml_string(entry.stock_code)}",
                "",
                "[mappings.yfinance]",
                f"provider_symbol = {_toml_string(entry.yfinance_provider_symbol)}",
                f"currency = {_toml_string(entry.currency)}",
                "",
                "[mappings.dart]",
                f"corp_code = {_toml_string(entry.corp_code)}",
                f"stock_code = {_toml_string(entry.stock_code)}",
                f"corp_name = {_toml_string(entry.dart_corp_name)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_generated_files(
    *,
    universe_out: Path,
    provider_mapping_out: Path,
    universe_toml: str,
    provider_mapping_toml: str,
    force: bool,
) -> None:
    """universe/provider mapping TOML을 UTF-8로 기록한다."""
    for path, label in (
        (universe_out, "universe"),
        (provider_mapping_out, "provider mapping"),
    ):
        if path.exists() and not force:
            raise KrProviderMappingGeneratorError(
                "write",
                f"{label} output already exists: {path} (use --force to overwrite)",
            )
        path.parent.mkdir(parents=True, exist_ok=True)

    universe_out.write_text(universe_toml, encoding="utf-8")
    provider_mapping_out.write_text(provider_mapping_toml, encoding="utf-8")


def validate_generated_files(
    *,
    universe_out: Path,
    provider_mapping_out: Path,
) -> None:
    """생성된 TOML을 existing loader/registry validator로 self-check한다."""
    try:
        universe = load_universe_toml(universe_out)
    except (FileNotFoundError, ValueError) as exc:
        raise KrProviderMappingGeneratorError("validate", str(exc)) from exc

    try:
        registry = load_provider_mapping_toml(provider_mapping_out)
    except (FileNotFoundError, ProviderMappingError) as exc:
        raise KrProviderMappingGeneratorError("validate", str(exc)) from exc

    try:
        validate_provider_mappings_cover_universe(
            registry,
            universe,
            require_yfinance=True,
            require_dart=True,
        )
    except ProviderMappingError as exc:
        raise KrProviderMappingGeneratorError("validate", str(exc)) from exc


def generate_kr_provider_mapping_files(
    *,
    candidates_path: Path,
    corp_code_xml: Path | None,
    corp_code_zip: Path | None,
    universe_out: Path,
    provider_mapping_out: Path,
    universe_name: str,
    provider_mapping_name: str,
    universe_description: str | None = None,
    provider_mapping_description: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """candidate + local corp-code snapshot → universe/mapping TOML 생성 및 검증."""
    document = parse_kr_candidates_toml(candidates_path)
    corp_entries = load_corp_code_entries(
        corp_code_xml=corp_code_xml,
        corp_code_zip=corp_code_zip,
    )
    resolved = resolve_kr_candidates(document.candidates, corp_code_entries=corp_entries)

    effective_universe_description = (
        universe_description
        if universe_description is not None
        else f"Generated KR universe from candidate file {document.name}."
    )
    effective_mapping_description = (
        provider_mapping_description
        if provider_mapping_description is not None
        else f"Generated KR provider mappings from candidate file {document.name}."
    )

    universe_toml = render_universe_toml(
        name=universe_name,
        description=effective_universe_description,
        resolved=resolved,
    )
    provider_mapping_toml = render_provider_mapping_toml(
        name=provider_mapping_name,
        description=effective_mapping_description,
        resolved=resolved,
    )
    write_generated_files(
        universe_out=universe_out,
        provider_mapping_out=provider_mapping_out,
        universe_toml=universe_toml,
        provider_mapping_toml=provider_mapping_toml,
        force=force,
    )
    validate_generated_files(
        universe_out=universe_out,
        provider_mapping_out=provider_mapping_out,
    )

    enabled_count = sum(1 for entry in resolved if entry.enabled)
    return {
        "status": "ok",
        "stage": "complete",
        "candidates_read": len(resolved),
        "enabled_symbols": enabled_count,
        "universe_out": str(universe_out),
        "provider_mapping_out": str(provider_mapping_out),
        "resolved": [
            {
                "symbol": entry.symbol,
                "market": entry.market,
                "stock_code": entry.stock_code,
                "corp_code": entry.corp_code,
                "corp_name": entry.dart_corp_name,
                "yfinance_provider_symbol": entry.yfinance_provider_symbol,
            }
            for entry in resolved
        ],
    }


def _parse_candidate_entry(raw: dict[str, Any], *, index: int) -> KrCandidateEntry:
    prefix = f"candidates[{index}]"
    if "corp_code" in raw:
        raise KrProviderMappingGeneratorError(
            "parse",
            f"{prefix}: corp_code must not appear in candidate file; use local corp-code snapshot",
        )

    unknown = set(raw.keys()) - _CANDIDATE_ENTRY_KEYS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise KrProviderMappingGeneratorError("parse", f"{prefix}: unknown fields: {joined}")

    market = _required_text(raw.get("market"), field_name=f"{prefix}.market")
    if market != "KR":
        raise KrProviderMappingGeneratorError("parse", f"{prefix}.market must be 'KR'")

    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise KrProviderMappingGeneratorError("parse", f"{prefix}.enabled must be a boolean")

    display_name = _required_text(raw.get("display_name"), field_name=f"{prefix}.display_name")
    corp_name = _required_text(raw.get("corp_name"), field_name=f"{prefix}.corp_name")
    yfinance_provider_symbol = _required_text(
        raw.get("yfinance_provider_symbol"),
        field_name=f"{prefix}.yfinance_provider_symbol",
    )
    if not yfinance_provider_symbol.endswith(_KR_YFINANCE_SUFFIXES):
        raise KrProviderMappingGeneratorError(
            "parse",
            f"{prefix}.yfinance_provider_symbol must end with .KS or .KQ",
        )

    currency = _required_text(raw.get("currency"), field_name=f"{prefix}.currency")
    if currency != "KRW":
        raise KrProviderMappingGeneratorError("parse", f"{prefix}.currency must be 'KRW'")

    try:
        normalized_stock_code = normalize_stock_code(
            _required_text(raw.get("stock_code"), field_name=f"{prefix}.stock_code")
        )
    except DartCorpCodeResolverError as exc:
        raise KrProviderMappingGeneratorError("parse", f"{prefix}.stock_code: {exc}") from exc

    symbol_raw = _required_text(raw.get("symbol"), field_name=f"{prefix}.symbol")
    try:
        normalized_symbol = normalize_stock_code(symbol_raw)
    except DartCorpCodeResolverError as exc:
        raise KrProviderMappingGeneratorError("parse", f"{prefix}.symbol: {exc}") from exc

    if normalized_symbol != normalized_stock_code:
        raise KrProviderMappingGeneratorError(
            "parse",
            f"{prefix}: symbol must match normalized stock_code "
            f"(symbol={normalized_symbol!r}, stock_code={normalized_stock_code!r})",
        )

    return KrCandidateEntry(
        symbol=normalized_symbol,
        market=market,
        enabled=enabled,
        display_name=display_name,
        stock_code=normalized_stock_code,
        corp_name=corp_name,
        yfinance_provider_symbol=yfinance_provider_symbol,
        currency=currency,
    )


def _contains_control_character(value: str) -> bool:
    """ASCII control(0x00–0x1F) 및 DEL(0x7F) 포함 여부."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _required_text(value: Any, *, field_name: str) -> str:
    if value is None:
        raise KrProviderMappingGeneratorError("parse", f"{field_name} is required")
    if not isinstance(value, str):
        raise KrProviderMappingGeneratorError("parse", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise KrProviderMappingGeneratorError("parse", f"{field_name} must not be blank")
    if _contains_control_character(normalized):
        raise KrProviderMappingGeneratorError(
            "parse",
            f"{field_name} contains a control character",
        )
    return normalized


def _toml_string(value: str) -> str:
    # parse/args 검증 우회(programmatic API·resolver 출력) 방어 백스톱
    if _contains_control_character(value):
        raise KrProviderMappingGeneratorError(
            "validate",
            "rendered text contains a control character",
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
