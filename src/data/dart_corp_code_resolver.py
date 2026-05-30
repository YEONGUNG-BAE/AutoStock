from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 3C1: OpenDART corp-code master 로컬 fixture 전용. network/env/API key 없음.

_LIST_TAG = "list"
_CORP_CODE_TAG = "corp_code"
_CORP_NAME_TAG = "corp_name"
_STOCK_CODE_TAG = "stock_code"
_MODIFY_DATE_TAG = "modify_date"


class DartCorpCodeResolverError(ValueError):
    """corp-code master 파싱·stock_code 조회 실패."""


@dataclass(frozen=True)
class DartCorpCodeEntry:
    """OpenDART corp-code master 단일 list 항목."""

    corp_code: str
    corp_name: str
    stock_code: str | None
    modify_date: str | None = None


def parse_corp_code_xml_file(path: Path) -> tuple[DartCorpCodeEntry, ...]:
    """로컬 corp-code XML 파일을 파싱한다."""
    if not path.is_file():
        raise DartCorpCodeResolverError(f"corp-code XML not found: {path}")
    return parse_corp_code_xml_text(path.read_text(encoding="utf-8"))


def parse_corp_code_zip_file(path: Path) -> tuple[DartCorpCodeEntry, ...]:
    """ZIP 내부 XML member를 직접 읽어 파싱한다. extractall 사용 금지."""
    if not path.is_file():
        raise DartCorpCodeResolverError(f"corp-code ZIP not found: {path}")

    try:
        with zipfile.ZipFile(path) as archive:
            xml_members = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".xml") and not name.endswith("/")
            ]
            if not xml_members:
                raise DartCorpCodeResolverError("corp-code ZIP contains no XML member")
            if len(xml_members) != 1:
                raise DartCorpCodeResolverError(
                    "corp-code ZIP must contain exactly one XML member"
                )
            xml_text = archive.read(xml_members[0]).decode("utf-8")
    except zipfile.BadZipFile as exc:
        raise DartCorpCodeResolverError(f"invalid corp-code ZIP: {path}") from exc

    return parse_corp_code_xml_text(xml_text)


def parse_corp_code_xml_text(xml_text: str) -> tuple[DartCorpCodeEntry, ...]:
    """OpenDART corp-code master XML 텍스트를 파싱한다."""
    if not xml_text.strip():
        raise DartCorpCodeResolverError("corp-code XML is empty")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise DartCorpCodeResolverError(f"invalid corp-code XML: {exc}") from exc

    list_nodes = _find_list_nodes(root)
    if not list_nodes:
        raise DartCorpCodeResolverError("corp-code XML has no list entries")

    entries: list[DartCorpCodeEntry] = []
    for index, list_node in enumerate(list_nodes):
        entries.append(_parse_list_node(list_node, index=index))

    _validate_duplicate_listed_stock_codes(entries)
    return tuple(entries)


def resolve_corp_code_by_stock_code(
    entries: Iterable[DartCorpCodeEntry],
    stock_code: str,
    *,
    corp_name: str | None = None,
) -> DartCorpCodeEntry:
    """listed stock_code → corp_code 항목을 반환한다. 비상장(blank stock_code)은 매칭하지 않는다."""
    normalized = normalize_stock_code(stock_code)
    matches = [
        entry
        for entry in entries
        if entry.stock_code is not None and entry.stock_code == normalized
    ]
    if not matches:
        raise DartCorpCodeResolverError(
            f"no corp_code match for stock_code {normalized!r}"
        )

    if len(matches) == 1:
        return matches[0]

    if corp_name is None:
        raise DartCorpCodeResolverError(
            f"ambiguous stock_code {normalized!r}: multiple corp_code entries; "
            "provide corp_name to disambiguate"
        )

    normalized_name = _normalize_required_text(corp_name, field_name="corp_name")
    named_matches = [entry for entry in matches if entry.corp_name == normalized_name]
    if len(named_matches) == 1:
        return named_matches[0]
    if not named_matches:
        raise DartCorpCodeResolverError(
            f"no corp_code match for stock_code {normalized!r} and corp_name {normalized_name!r}"
        )
    raise DartCorpCodeResolverError(
        f"ambiguous stock_code {normalized!r} and corp_name {normalized_name!r}"
    )


def normalize_stock_code(stock_code: str) -> str:
    """KR 종목코드를 6자리 숫자 문자열로 정규화한다."""
    raw = _normalize_required_text(stock_code, field_name="stock_code")
    if ":" in raw:
        prefix, _, suffix = raw.partition(":")
        if prefix.upper() != "KR" or not suffix:
            raise DartCorpCodeResolverError(
                f"invalid market-prefixed stock_code: {stock_code!r}"
            )
        raw = suffix

    if not raw.isdigit():
        raise DartCorpCodeResolverError(f"stock_code must be numeric: {stock_code!r}")
    if len(raw) > 6:
        raise DartCorpCodeResolverError(f"stock_code too long: {stock_code!r}")

    normalized = raw.zfill(6)
    if len(normalized) != 6:
        raise DartCorpCodeResolverError(f"stock_code must normalize to 6 digits: {stock_code!r}")
    return normalized


def _find_list_nodes(root: ET.Element) -> list[ET.Element]:
    if root.tag == _LIST_TAG:
        return [root]
    return list(root.iter(_LIST_TAG))


def _parse_list_node(list_node: ET.Element, *, index: int) -> DartCorpCodeEntry:
    corp_code = _optional_element_text(list_node, _CORP_CODE_TAG)
    if corp_code is None:
        raise DartCorpCodeResolverError(f"list[{index}] corp_code is required")

    corp_name = _optional_element_text(list_node, _CORP_NAME_TAG)
    if corp_name is None:
        raise DartCorpCodeResolverError(f"list[{index}] corp_name is required")

    raw_stock = _optional_element_text(list_node, _STOCK_CODE_TAG)
    stock_code: str | None
    if raw_stock is None or not raw_stock:
        stock_code = None
    else:
        stock_code = normalize_stock_code(raw_stock)

    modify_date = _optional_element_text(list_node, _MODIFY_DATE_TAG)
    return DartCorpCodeEntry(
        corp_code=corp_code,
        corp_name=corp_name,
        stock_code=stock_code,
        modify_date=modify_date,
    )


def _validate_duplicate_listed_stock_codes(entries: Iterable[DartCorpCodeEntry]) -> None:
    """동일 stock_code + 동일 corp_name 중복은 파싱 단계에서 거부한다."""
    seen: dict[str, set[str]] = {}
    for entry in entries:
        if entry.stock_code is None:
            continue
        names_for_code = seen.setdefault(entry.stock_code, set())
        if entry.corp_name in names_for_code:
            raise DartCorpCodeResolverError(
                f"duplicate listed stock_code {entry.stock_code!r} "
                f"with corp_name {entry.corp_name!r}"
            )
        names_for_code.add(entry.corp_name)


def _optional_element_text(parent: ET.Element, tag: str) -> str | None:
    child = parent.find(tag)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text if text else None


def _normalize_required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise DartCorpCodeResolverError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise DartCorpCodeResolverError(f"{field_name} must not be blank")
    return normalized
