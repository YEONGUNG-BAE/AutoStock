"""RTM-6 — official KIS real-time frame parser tests (network/credential/broker-free)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from domain.enums import Currency, Market
from market_data.kis_official_ws_parser import (
    PROVIDER,
    TR_QUOTE,
    TR_TRADE,
    KisOfficialWsFrameParser,
    KisOfficialWsParseError,
)
from market_data.models import NormalizedBestBidAsk, NormalizedTradeTick

_KST = ZoneInfo("Asia/Seoul")
# 2026-06-12 10:00:00 KST == 01:00:00 UTC
_RECEIVED = datetime(2026, 6, 12, 1, 0, 0, tzinfo=UTC)

_QUOTE_LEN = 59
_TRADE_LEN = 46


def _quote_record(
    *,
    symbol: str = "005930",
    bsop_hour: str = "095959",
    askp1: str = "70100",
    bidp1: str = "69900",
    askp_rsqn1: str = "120",
    bidp_rsqn1: str = "0",
) -> list[str]:
    record = ["0"] * _QUOTE_LEN
    record[0] = symbol
    record[1] = bsop_hour
    record[3] = askp1
    record[13] = bidp1
    record[23] = askp_rsqn1
    record[33] = bidp_rsqn1
    return record


def _trade_record(
    *,
    symbol: str = "005930",
    cntg_hour: str = "095959",
    prpr: str = "70000",
    cntg_vol: str = "10",
    acml_vol: str = "123456",
    bsop_date: str = "20260612",
) -> list[str]:
    record = ["0"] * _TRADE_LEN
    record[0] = symbol
    record[1] = cntg_hour
    record[2] = prpr
    record[12] = cntg_vol
    record[13] = acml_vol
    record[33] = bsop_date
    return record


def _frame(tr_id: str, records: list[list[str]], *, flag: str = "0") -> str:
    body = "^".join(field for record in records for field in record)
    return f"{flag}|{tr_id}|{len(records)}|{body}"


def test_parses_single_trade() -> None:
    parser = KisOfficialWsFrameParser()
    events = parser.parse_frame(_frame(TR_TRADE, [_trade_record()]), received_at=_RECEIVED)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, NormalizedTradeTick)
    assert event.provider == PROVIDER
    assert event.symbol == "005930"
    assert event.market is Market.KR
    assert event.currency is Currency.KRW
    assert event.price == Decimal("70000")
    assert event.quantity == Decimal("10")
    assert event.cumulative_volume == Decimal("123456")
    assert event.trade_at == datetime(2026, 6, 12, 9, 59, 59, tzinfo=_KST)
    assert event.received_at == _RECEIVED
    assert event.provider_sequence.channel == "H0STCNT0|005930"
    assert event.provider_sequence.sequence == 1


def test_parses_single_quote() -> None:
    parser = KisOfficialWsFrameParser()
    events = parser.parse_frame(_frame(TR_QUOTE, [_quote_record()]), received_at=_RECEIVED)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, NormalizedBestBidAsk)
    assert event.symbol == "005930"
    assert event.ask_price == Decimal("70100")
    assert event.bid_price == Decimal("69900")
    assert event.ask_quantity == Decimal("120")
    assert event.bid_quantity == Decimal("0")
    # quote frame에는 날짜가 없으므로 received_at의 KST 날짜를 사용한다.
    assert event.quote_at == datetime(2026, 6, 12, 9, 59, 59, tzinfo=_KST)
    assert event.provider_sequence.channel == "H0STASP0|005930"
    assert event.provider_sequence.sequence == 1


def test_multi_record_frame_yields_sequenced_events() -> None:
    parser = KisOfficialWsFrameParser()
    records = [
        _trade_record(cntg_hour="095959", prpr="70000"),
        _trade_record(cntg_hour="100000", prpr="70100"),
    ]
    # received_at must be >= latest trade time (10:00:00 KST)
    received = datetime(2026, 6, 12, 1, 0, 1, tzinfo=UTC)  # 10:00:01 KST
    events = parser.parse_frame(_frame(TR_TRADE, records), received_at=received)
    assert [e.provider_sequence.sequence for e in events] == [1, 2]
    assert [e.price for e in events] == [Decimal("70000"), Decimal("70100")]


def test_sequence_is_per_tr_id_and_symbol() -> None:
    parser = KisOfficialWsFrameParser()
    t1 = parser.parse_frame(_frame(TR_TRADE, [_trade_record(symbol="005930")]), received_at=_RECEIVED)
    q1 = parser.parse_frame(_frame(TR_QUOTE, [_quote_record(symbol="005930")]), received_at=_RECEIVED)
    t2 = parser.parse_frame(_frame(TR_TRADE, [_trade_record(symbol="000660")]), received_at=_RECEIVED)
    t3 = parser.parse_frame(_frame(TR_TRADE, [_trade_record(symbol="005930")]), received_at=_RECEIVED)
    assert t1[0].provider_sequence.sequence == 1
    assert q1[0].provider_sequence.sequence == 1  # independent channel
    assert t2[0].provider_sequence.sequence == 1  # independent symbol
    assert t3[0].provider_sequence.sequence == 2  # same (tr_id, symbol) increments


def test_fresh_parser_resets_sequence() -> None:
    first = KisOfficialWsFrameParser()
    first.parse_frame(_frame(TR_TRADE, [_trade_record()]), received_at=_RECEIVED)
    second = KisOfficialWsFrameParser()
    events = second.parse_frame(_frame(TR_TRADE, [_trade_record()]), received_at=_RECEIVED)
    assert events[0].provider_sequence.sequence == 1


def test_encrypted_flag_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="encrypted"):
        parser.parse_frame(_frame(TR_TRADE, [_trade_record()], flag="1"), received_at=_RECEIVED)


def test_unknown_flag_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="flag"):
        parser.parse_frame(_frame(TR_TRADE, [_trade_record()], flag="9"), received_at=_RECEIVED)


def test_unknown_tr_id_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="tr_id"):
        parser.parse_frame(_frame("H0STXXX0", [_trade_record()]), received_at=_RECEIVED)


def test_field_count_mismatch_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    bad = f"0|{TR_TRADE}|1|{'^'.join(['0'] * 10)}"
    with pytest.raises(KisOfficialWsParseError, match="field count mismatch"):
        parser.parse_frame(bad, received_at=_RECEIVED)


def test_layout_without_pipes_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="layout"):
        parser.parse_frame("not-a-frame", received_at=_RECEIVED)


def test_empty_required_field_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="empty"):
        parser.parse_frame(
            _frame(TR_TRADE, [_trade_record(prpr="")]), received_at=_RECEIVED
        )


def test_crossed_book_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    # bid > ask is a crossed book; model rejects it.
    with pytest.raises(KisOfficialWsParseError, match="validation failed"):
        parser.parse_frame(
            _frame(TR_QUOTE, [_quote_record(askp1="69000", bidp1="70000")]),
            received_at=_RECEIVED,
        )


def test_future_quote_time_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    # received at 10:00 KST but quote claims 10:05 KST -> future.
    with pytest.raises(KisOfficialWsParseError, match="future"):
        parser.parse_frame(
            _frame(TR_QUOTE, [_quote_record(bsop_hour="100500")]),
            received_at=_RECEIVED,
        )


def test_future_trade_time_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="future"):
        parser.parse_frame(
            _frame(TR_TRADE, [_trade_record(cntg_hour="100500")]),
            received_at=_RECEIVED,
        )


def test_bad_count_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    body = "^".join(_trade_record())
    with pytest.raises(KisOfficialWsParseError, match="data_count"):
        parser.parse_frame(f"0|{TR_TRADE}|x|{body}", received_at=_RECEIVED)


def test_bad_hhmmss_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="HHMMSS"):
        parser.parse_frame(
            _frame(TR_TRADE, [_trade_record(cntg_hour="9999")]), received_at=_RECEIVED
        )


def test_bad_bsop_date_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(KisOfficialWsParseError, match="BSOP_DATE"):
        parser.parse_frame(
            _frame(TR_TRADE, [_trade_record(bsop_date="2026")]), received_at=_RECEIVED
        )


def test_naive_received_at_rejected() -> None:
    parser = KisOfficialWsFrameParser()
    with pytest.raises(Exception):  # require_timezone_aware_datetime fails closed
        parser.parse_frame(
            _frame(TR_TRADE, [_trade_record()]),
            received_at=datetime(2026, 6, 12, 10, 0, 0),  # naive
        )


def test_error_message_does_not_leak_raw_frame() -> None:
    parser = KisOfficialWsFrameParser()
    sentinel = "SENSITIVE_SENTINEL_VALUE"
    record = _trade_record(prpr=sentinel)
    frame = _frame(TR_TRADE, [record])
    with pytest.raises(KisOfficialWsParseError) as excinfo:
        parser.parse_frame(frame, received_at=_RECEIVED)
    message = str(excinfo.value)
    assert sentinel not in message
    assert frame not in message
