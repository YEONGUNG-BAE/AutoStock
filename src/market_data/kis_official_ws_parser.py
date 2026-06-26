from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from domain._datetime import require_timezone_aware_datetime
from domain.enums import Currency, Market
from market_data.kis_ws_parser import MarketDataParserError
from market_data.models import (
    MarketEvent,
    NormalizedBestBidAsk,
    NormalizedTradeTick,
    ProviderSequence,
)

# 공식 KIS 국내 실시간 TR ID. 검증 SHA는 docs/KIS_WS_CONTRACT.md 참조.
TR_QUOTE = "H0STASP0"  # 호가 (best bid/ask)
TR_TRADE = "H0STCNT0"  # 체결 (trade)

PROVIDER = "kis"
_KST = ZoneInfo("Asia/Seoul")

# 평문 frame flag. "1"(암호화)은 read-only 공개시세 경로에서 거부한다.
_PLAINTEXT_FLAG = "0"
_ENCRYPTED_FLAG = "1"

# 레코드당 필드 수(전체). docs/KIS_WS_CONTRACT.md의 검증된 컬럼 길이와 일치해야 한다.
_QUOTE_FIELD_COUNT = 59
_TRADE_FIELD_COUNT = 46

# H0STASP0 (quote) 필드 인덱스
_Q_SYMBOL = 0
_Q_BSOP_HOUR = 1
_Q_ASKP1 = 3
_Q_BIDP1 = 13
_Q_ASKP_RSQN1 = 23
_Q_BIDP_RSQN1 = 33

# H0STCNT0 (trade) 필드 인덱스
_T_SYMBOL = 0
_T_CNTG_HOUR = 1
_T_PRPR = 2
_T_CNTG_VOL = 12
_T_ACML_VOL = 13
_T_BSOP_DATE = 33


class KisOfficialWsParseError(MarketDataParserError):
    """공식 KIS 실시간 frame 파싱 실패. 메시지에 raw frame/credential을 담지 않는다.

    실패 ``parser_stage``와 sanitized ``parser_metadata``(비밀이 아닌 수치·불리언·tr_id만)를
    함께 운반한다. source 계층이 이를 읽어 세분화된 reason_subcode로 매핑한다. metadata에는
    절대 raw body/field 값/credential/URL/traceback을 넣지 않는다.
    """

    def __init__(
        self,
        message: str,
        *,
        parser_stage: str = "unknown",
        parser_metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.parser_stage = parser_stage
        metadata: dict[str, object] = {"parser_stage": parser_stage}
        if parser_metadata:
            metadata.update(parser_metadata)
        self.parser_metadata = metadata

    def enrich(self, **defaults: object) -> None:
        """stage 무관 sanitized 식별자(tr_id/record_len 등)를 빠진 키에만 채운다."""
        for key, value in defaults.items():
            if value is not None:
                self.parser_metadata.setdefault(key, value)


class KisOfficialWsUnsupportedTrIdError(KisOfficialWsParseError):
    """공식 KIS 실시간 frame의 TR ID가 지원 목록 밖인 경우."""


class KisOfficialWsFrameParser:
    """공식 KIS 국내 실시간 text frame(`flag|tr_id|count|body`)을 정규화 MarketEvent로 변환한다.

    network/env/filesystem 접근이 없는 순수 파서다. 접속(epoch)마다 fresh 인스턴스로
    생성되어 `(tr_id, symbol)`별 수신순서(receive-order) sequence를 1부터 부여하고,
    재접속 시 sequence가 자연히 reset된다. clock은 주입된 received_at으로만 들어온다.
    """

    def __init__(self) -> None:
        self._sequence: dict[tuple[str, str], int] = {}

    def parse_frame(self, raw: str, *, received_at: datetime) -> list[MarketEvent]:
        """한 text frame을 0개 이상의 정규화 MarketEvent로 변환한다.

        제어(PINGPONG/JSON) frame은 이 파서가 다루지 않는다. flag가 `0`/`1`이 아닌
        frame은 시세 frame이 아니므로 호출 측에서 분기해야 한다.
        """
        aware_received = require_timezone_aware_datetime(received_at, field_name="received_at")
        if not isinstance(raw, str) or not raw:
            raise KisOfficialWsParseError(
                "frame must be a non-empty string.", parser_stage="layout"
            )

        frame = raw.strip()
        parts = frame.split("|", 3)
        if len(parts) != 4:
            raise KisOfficialWsParseError(
                "frame must have flag|tr_id|count|body layout.", parser_stage="layout"
            )
        flag, tr_id, count_str, body = parts

        if flag == _ENCRYPTED_FLAG:
            raise KisOfficialWsParseError(
                "encrypted frame (flag=1) is not supported on the read-only public path.",
                parser_stage="layout",
            )
        if flag != _PLAINTEXT_FLAG:
            raise KisOfficialWsParseError(
                "frame flag must be '0' (plaintext) on this path.", parser_stage="layout"
            )

        if tr_id == TR_QUOTE:
            record_len = _QUOTE_FIELD_COUNT
        elif tr_id == TR_TRADE:
            record_len = _TRADE_FIELD_COUNT
        else:
            raise KisOfficialWsUnsupportedTrIdError(
                "unsupported tr_id; expected H0STASP0 or H0STCNT0.", parser_stage="unsupported_tr_id"
            )

        try:
            count = _parse_count(count_str)
        except KisOfficialWsParseError as exc:
            exc.enrich(tr_id=tr_id, record_len=record_len)
            raise
        expected = record_len * count
        fields, observed, has_trailing_empty_extra = _split_body_fields(body, expected=expected)
        if len(fields) != expected:
            raise KisOfficialWsParseError(
                "frame field count mismatch.",
                parser_stage="field_count",
                parser_metadata={
                    "tr_id": tr_id,
                    "expected_field_count": expected,
                    "observed_field_count": observed,
                    "declared_count": count,
                    "record_len": record_len,
                    "has_trailing_empty_extra": has_trailing_empty_extra,
                },
            )

        events: list[MarketEvent] = []
        for index in range(count):
            record = fields[index * record_len : (index + 1) * record_len]
            if tr_id == TR_QUOTE:
                events.append(self._build_quote(record, aware_received))
            else:
                events.append(self._build_trade(record, aware_received))
        return events

    def _next_sequence(self, tr_id: str, symbol: str) -> int:
        key = (tr_id, symbol)
        nxt = self._sequence.get(key, 0) + 1
        self._sequence[key] = nxt
        return nxt

    def _build_quote(self, record: list[str], received_at: datetime) -> NormalizedBestBidAsk:
        try:
            symbol = _require_field(record, _Q_SYMBOL, "symbol")
            quote_at = _kst_from_received_date(
                received_at, _require_field(record, _Q_BSOP_HOUR, "BSOP_HOUR")
            )
            if quote_at > received_at:
                raise KisOfficialWsParseError(
                    "quote time is in the future relative to received_at.", parser_stage="control"
                )
            channel = f"{TR_QUOTE}|{symbol}"
            sequence = self._next_sequence(TR_QUOTE, symbol)
            provider_sequence = _build_provider_sequence(channel, sequence, received_at)
            return _build_model(
                NormalizedBestBidAsk,
                provider=PROVIDER,
                symbol=symbol,
                market=Market.KR,
                currency=Currency.KRW,
                bid_price=_require_field(record, _Q_BIDP1, "BIDP1"),
                ask_price=_require_field(record, _Q_ASKP1, "ASKP1"),
                bid_quantity=_require_field(record, _Q_BIDP_RSQN1, "BIDP_RSQN1"),
                ask_quantity=_require_field(record, _Q_ASKP_RSQN1, "ASKP_RSQN1"),
                quote_at=quote_at,
                received_at=received_at,
                provider_sequence=provider_sequence,
            )
        except KisOfficialWsParseError as exc:
            exc.enrich(tr_id=TR_QUOTE, record_len=_QUOTE_FIELD_COUNT)
            raise

    def _build_trade(self, record: list[str], received_at: datetime) -> NormalizedTradeTick:
        try:
            symbol = _require_field(record, _T_SYMBOL, "symbol")
            trade_at = _kst_from_date_time(
                _require_field(record, _T_BSOP_DATE, "BSOP_DATE"),
                _require_field(record, _T_CNTG_HOUR, "STCK_CNTG_HOUR"),
            )
            if trade_at > received_at:
                raise KisOfficialWsParseError(
                    "trade time is in the future relative to received_at.", parser_stage="control"
                )
            channel = f"{TR_TRADE}|{symbol}"
            sequence = self._next_sequence(TR_TRADE, symbol)
            provider_sequence = _build_provider_sequence(channel, sequence, received_at)
            return _build_model(
                NormalizedTradeTick,
                provider=PROVIDER,
                symbol=symbol,
                market=Market.KR,
                currency=Currency.KRW,
                price=_require_field(record, _T_PRPR, "STCK_PRPR"),
                quantity=_require_field(record, _T_CNTG_VOL, "CNTG_VOL"),
                trade_at=trade_at,
                received_at=received_at,
                provider_sequence=provider_sequence,
                cumulative_volume=_require_field(record, _T_ACML_VOL, "ACML_VOL"),
            )
        except KisOfficialWsParseError as exc:
            exc.enrich(tr_id=TR_TRADE, record_len=_TRADE_FIELD_COUNT)
            raise


def _build_provider_sequence(channel: str, sequence: int, received_at: datetime) -> ProviderSequence:
    return _build_model(
        ProviderSequence,
        provider=PROVIDER,
        channel=channel,
        sequence=sequence,
        received_at=received_at,
    )


def _parse_count(count_str: str) -> int:
    if not count_str.isdigit():
        raise KisOfficialWsParseError(
            "frame data_count must be a non-negative integer.", parser_stage="count"
        )
    count = int(count_str)
    if count < 1:
        raise KisOfficialWsParseError("frame data_count must be >= 1.", parser_stage="count")
    return count


def _split_body_fields(body: str, *, expected: int) -> tuple[list[str], int, bool]:
    """body를 `^`로 분할한다.

    transport가 끝에 붙인 trailing `^`로 생기는 empty field만은 documented 컬럼 밖의
    payload가 아니므로 제거한 뒤 검증한다. 반환: (검증용 fields, raw observed count,
    trailing empty extra가 있었는지). has_trailing_empty_extra는 sanitized boolean이며
    raw 값은 담지 않는다.
    """
    raw_fields = body.split("^")
    observed = len(raw_fields)
    extra = raw_fields[expected:]
    has_trailing_empty_extra = bool(extra) and any(item == "" for item in extra)
    if extra and all(item == "" for item in extra):
        return raw_fields[:expected], observed, has_trailing_empty_extra
    return raw_fields, observed, has_trailing_empty_extra


def _require_field(record: list[str], index: int, name: str) -> str:
    value = record[index].strip()
    if not value:
        raise KisOfficialWsParseError(f"field {name} is empty.", parser_stage="required_field")
    return value


def _kst_from_date_time(date_str: str, time_str: str) -> datetime:
    if len(date_str) != 8 or not date_str.isdigit():
        raise KisOfficialWsParseError("BSOP_DATE must be YYYYMMDD digits.", parser_stage="control")
    hour, minute, second = _parse_hhmmss(time_str)
    try:
        return datetime(
            int(date_str[0:4]),
            int(date_str[4:6]),
            int(date_str[6:8]),
            hour,
            minute,
            second,
            tzinfo=_KST,
        )
    except ValueError as exc:
        raise KisOfficialWsParseError(
            "invalid trade date/time fields.", parser_stage="control"
        ) from exc


def _kst_from_received_date(received_at: datetime, time_str: str) -> datetime:
    hour, minute, second = _parse_hhmmss(time_str)
    kst_now = received_at.astimezone(_KST)
    try:
        return datetime(
            kst_now.year,
            kst_now.month,
            kst_now.day,
            hour,
            minute,
            second,
            tzinfo=_KST,
        )
    except ValueError as exc:
        raise KisOfficialWsParseError("invalid quote time fields.", parser_stage="control") from exc


def _parse_hhmmss(time_str: str) -> tuple[int, int, int]:
    if len(time_str) != 6 or not time_str.isdigit():
        raise KisOfficialWsParseError("time field must be HHMMSS digits.", parser_stage="control")
    return int(time_str[0:2]), int(time_str[2:4]), int(time_str[4:6])


def _build_model(model: type, **fields: object) -> object:
    try:
        return model(**fields)
    except ValidationError as exc:
        raise KisOfficialWsParseError(
            _sanitize_validation_error(model.__name__, exc), parser_stage="model"
        ) from None


def _sanitize_validation_error(model_name: str, exc: ValidationError) -> str:
    # loc + msg만 사용한다. input 값(raw frame/credential)은 절대 포함하지 않는다.
    parts: list[str] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error.get("loc", ()))
        msg = error.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(parts) if parts else "invalid value"
    return f"{model_name} validation failed: {detail}"


__all__ = [
    "PROVIDER",
    "TR_QUOTE",
    "TR_TRADE",
    "KisOfficialWsFrameParser",
    "KisOfficialWsParseError",
    "KisOfficialWsUnsupportedTrIdError",
]
