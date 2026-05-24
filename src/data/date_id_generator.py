from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from domain._datetime import require_timezone_aware_datetime
from domain.identifiers import DateId
from data.date_id_store import SQLiteDateIdSourceStore

# Date-ID 날짜 부분은 KST 기준으로 고정한다.
_DEFAULT_TIMEZONE = ZoneInfo("Asia/Seoul")


class DateIdGenerator:
    """store state를 기준으로 deterministic YYMMDD-N Date-ID를 생성한다.

    사용 정책:
    - next_id()는 store에 이미 저장된 Date-ID 목록을 읽어 다음 sequence를 계산한다.
    - 같은 KST 날짜에 여러 Date-ID가 필요하면 generate -> save를 반복한다.
    - save 없이 next_id()만 연속 호출하면 동일 id가 반환될 수 있다.
    - batch reservation(한 번에 여러 id 예약)은 Phase 6 범위가 아니다.
    """

    def __init__(
        self,
        store: SQLiteDateIdSourceStore,
        timezone: ZoneInfo = _DEFAULT_TIMEZONE,
    ) -> None:
        self._store = store
        self._timezone = timezone

    def next_id(self, source_timestamp: datetime) -> DateId:
        """source_timestamp의 KST 날짜 기준으로 다음 sequence Date-ID를 반환한다."""
        aware_timestamp = require_timezone_aware_datetime(
            source_timestamp,
            field_name="source_timestamp",
        )
        kst_date = aware_timestamp.astimezone(self._timezone).date()
        return self.next_id_for_date(kst_date)

    def next_id_for_date(self, target_date: date) -> DateId:
        """주어진 calendar date(KST 기준으로 이미 변환된 date)의 다음 sequence Date-ID를 반환한다."""
        date_prefix = target_date.strftime("%y%m%d")
        next_sequence = _next_sequence_for_date_prefix(self._store, date_prefix)
        return DateId(f"{date_prefix}-{next_sequence}")


def _next_sequence_for_date_prefix(store: SQLiteDateIdSourceStore, date_prefix: str) -> int:
    """동일 YYMMDD prefix를 가진 기존 Date-ID 중 최대 sequence 다음 번호를 반환한다."""
    max_sequence = 0
    for record in store.list_records():
        date_id_value = record.date_id.value
        prefix, _, sequence_text = date_id_value.partition("-")
        if prefix != date_prefix:
            continue
        if not sequence_text.isdigit():
            continue
        max_sequence = max(max_sequence, int(sequence_text))
    return max_sequence + 1
