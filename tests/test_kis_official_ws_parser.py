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


def _frame_with_count(tr_id: str, records: list[list[str]], count: str) -> str:
    body = "^".join(field for record in records for field in record)
    return f"0|{tr_id}|{count}|{body}"


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


def test_count_field_allows_zero_padded_digits() -> None:
    parser = KisOfficialWsFrameParser()
    events = parser.parse_frame(
        _frame_with_count(TR_QUOTE, [_quote_record()], "001"),
        received_at=_RECEIVED,
    )
    assert len(events) == 1
    assert isinstance(events[0], NormalizedBestBidAsk)


def test_quote_frame_allows_trailing_empty_delimiter_variant() -> None:
    parser = KisOfficialWsFrameParser()
    frame = _frame(TR_QUOTE, [_quote_record()]) + "^"
    events = parser.parse_frame(frame, received_at=_RECEIVED)
    assert len(events) == 1
    assert isinstance(events[0], NormalizedBestBidAsk)
    assert events[0].provider_sequence.channel == "H0STASP0|005930"


def test_trade_frame_allows_trailing_empty_delimiter_variant() -> None:
    parser = KisOfficialWsFrameParser()
    frame = _frame(TR_TRADE, [_trade_record()]) + "^"
    events = parser.parse_frame(frame, received_at=_RECEIVED)
    assert len(events) == 1
    assert isinstance(events[0], NormalizedTradeTick)


def test_multi_record_quote_allows_global_trailing_empty_delimiter() -> None:
    parser = KisOfficialWsFrameParser()
    records = [
        _quote_record(bsop_hour="095958", askp1="70100"),
        _quote_record(bsop_hour="095959", askp1="70200"),
    ]
    frame = _frame(TR_QUOTE, records) + "^"
    events = parser.parse_frame(frame, received_at=_RECEIVED)
    assert [e.provider_sequence.sequence for e in events] == [1, 2]
    assert [e.ask_price for e in events] == [Decimal("70100"), Decimal("70200")]


def test_frame_allows_surrounding_transport_whitespace() -> None:
    parser = KisOfficialWsFrameParser()
    frame = "\r\n" + _frame(TR_QUOTE, [_quote_record()]) + "\n"
    events = parser.parse_frame(frame, received_at=_RECEIVED)
    assert len(events) == 1


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


def test_non_empty_extra_field_still_rejected_without_raw_leak() -> None:
    parser = KisOfficialWsFrameParser()
    frame = _frame(TR_QUOTE, [_quote_record()]) + "^SENSITIVE_EXTRA_FIELD"
    with pytest.raises(KisOfficialWsParseError) as excinfo:
        parser.parse_frame(frame, received_at=_RECEIVED)
    message = str(excinfo.value)
    assert "field count mismatch" in message
    assert "SENSITIVE_EXTRA_FIELD" not in message
    assert frame not in message


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


# --- parser_stage + sanitized metadata --------------------------------------

_ALLOWED_METADATA_KEYS = {
    "parser_stage",
    "tr_id",
    "expected_field_count",
    "observed_field_count",
    "declared_count",
    "record_len",
    "has_trailing_empty_extra",
}


def _raise(parser: KisOfficialWsFrameParser, frame: str) -> KisOfficialWsParseError:
    with pytest.raises(KisOfficialWsParseError) as excinfo:
        parser.parse_frame(frame, received_at=_RECEIVED)
    return excinfo.value


def test_quote_field_count_mismatch_metadata() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, "0|H0STASP0|1|005930^095959")
    assert exc.parser_stage == "field_count"
    assert exc.parser_metadata == {
        "parser_stage": "field_count",
        "tr_id": TR_QUOTE,
        "expected_field_count": _QUOTE_LEN,
        "observed_field_count": 2,
        "declared_count": 1,
        "record_len": _QUOTE_LEN,
        "has_trailing_empty_extra": False,
    }


def test_trade_field_count_mismatch_metadata() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, f"0|{TR_TRADE}|1|{'^'.join(['0'] * 10)}")
    assert exc.parser_stage == "field_count"
    assert exc.parser_metadata["tr_id"] == TR_TRADE
    assert exc.parser_metadata["expected_field_count"] == _TRADE_LEN
    assert exc.parser_metadata["observed_field_count"] == 10
    assert exc.parser_metadata["declared_count"] == 1
    assert exc.parser_metadata["has_trailing_empty_extra"] is False


def test_non_empty_extra_field_count_metadata_flags_no_trailing_empty() -> None:
    parser = KisOfficialWsFrameParser()
    frame = _frame(TR_QUOTE, [_quote_record()]) + "^NON_EMPTY_EXTRA"
    exc = _raise(parser, frame)
    assert exc.parser_stage == "field_count"
    assert exc.parser_metadata["observed_field_count"] == _QUOTE_LEN + 1
    # 마지막 extra가 비어있지 않으므로 trailing-empty 정리가 적용되지 않았다.
    assert exc.parser_metadata["has_trailing_empty_extra"] is False
    assert "NON_EMPTY_EXTRA" not in str(exc)
    assert set(exc.parser_metadata) <= _ALLOWED_METADATA_KEYS


def test_bad_count_metadata_stage_count() -> None:
    parser = KisOfficialWsFrameParser()
    body = "^".join(_trade_record())
    exc = _raise(parser, f"0|{TR_TRADE}|x|{body}")
    assert exc.parser_stage == "count"
    assert exc.parser_metadata == {
        "parser_stage": "count",
        "tr_id": TR_TRADE,
        "record_len": _TRADE_LEN,
    }


def test_layout_metadata_stage_layout_without_tr_id() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, "not-a-frame")
    assert exc.parser_stage == "layout"
    assert exc.parser_metadata == {"parser_stage": "layout"}
    assert "tr_id" not in exc.parser_metadata


def test_required_field_metadata_stage_required_field() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, _frame(TR_TRADE, [_trade_record(prpr="")]))
    assert exc.parser_stage == "required_field"
    assert exc.parser_metadata["tr_id"] == TR_TRADE
    assert exc.parser_metadata["record_len"] == _TRADE_LEN


def test_future_time_metadata_stage_control() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, _frame(TR_QUOTE, [_quote_record(bsop_hour="100500")]))
    assert exc.parser_stage == "control"
    assert exc.parser_metadata["tr_id"] == TR_QUOTE


def test_bad_hhmmss_metadata_stage_control() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, _frame(TR_TRADE, [_trade_record(cntg_hour="9999")]))
    assert exc.parser_stage == "control"
    assert exc.parser_metadata["tr_id"] == TR_TRADE


def test_crossed_book_metadata_stage_model() -> None:
    parser = KisOfficialWsFrameParser()
    exc = _raise(parser, _frame(TR_QUOTE, [_quote_record(askp1="69000", bidp1="70000")]))
    assert exc.parser_stage == "model"
    assert exc.parser_metadata["tr_id"] == TR_QUOTE


def test_all_parser_metadata_keys_are_whitelisted_and_sanitized() -> None:
    parser = KisOfficialWsFrameParser()
    sentinel = "SENSITIVE_SENTINEL_VALUE"
    frames = [
        "not-a-frame",
        "0|H0STASP0|1|005930^095959",
        f"0|{TR_TRADE}|x|{'^'.join(_trade_record())}",
        _frame(TR_TRADE, [_trade_record(prpr=sentinel)]),
        _frame(TR_QUOTE, [_quote_record(bsop_hour="100500")]),
        _frame(TR_QUOTE, [_quote_record(askp1="69000", bidp1="70000")]),
    ]
    for frame in frames:
        exc = _raise(parser, frame)
        assert set(exc.parser_metadata) <= _ALLOWED_METADATA_KEYS
        for value in exc.parser_metadata.values():
            assert isinstance(value, (str, int, bool))
            assert sentinel not in str(value)


# --- H0STASP0 (quote) variants ----------------------------------------------


def test_quote_zero_padded_count_variant() -> None:
    parser = KisOfficialWsFrameParser()
    events = parser.parse_frame(
        _frame_with_count(TR_QUOTE, [_quote_record()], "01"), received_at=_RECEIVED
    )
    assert len(events) == 1
    assert isinstance(events[0], NormalizedBestBidAsk)


def test_quote_allows_empty_optional_non_read_fields() -> None:
    parser = KisOfficialWsFrameParser()
    record = _quote_record()
    # idx 2 is not one of the 6 fields the parser reads; an empty optional column
    # within the documented 59-field record must still parse.
    record[2] = ""
    record[58] = ""
    events = parser.parse_frame(_frame(TR_QUOTE, [record]), received_at=_RECEIVED)
    assert len(events) == 1
    assert isinstance(events[0], NormalizedBestBidAsk)


def test_quote_multi_record_with_trailing_empty_extra() -> None:
    parser = KisOfficialWsFrameParser()
    records = [
        _quote_record(bsop_hour="095958", askp1="70100"),
        _quote_record(bsop_hour="095959", askp1="70200"),
    ]
    frame = _frame(TR_QUOTE, records) + "^^"
    events = parser.parse_frame(frame, received_at=_RECEIVED)
    assert [e.ask_price for e in events] == [Decimal("70100"), Decimal("70200")]
