"""Attended one-shot activation stage model (RTM-7c.4f design freeze).

순수 enum/dataclass만 허용 — side effect 없음. 실제 activation caller는 미구현.
``docs/PAPER_FAST_LOOP_ATTENDED_ACTIVATION_CONTRACT.md`` 참조.
"""

from __future__ import annotations

from enum import StrEnum


class AttendedActivationStage(StrEnum):
    """Attended one-shot activation의 논리 단계.

    현재 코드베이스는 ``ACTIVATION_NOT_IMPLEMENTED``에서 종료한다.
    machine precheck PASS나 receipt VALID는 approval/freshness/authenticity가 아니다.
    """

    DISABLED = "disabled"
    PRECHECK_MACHINE_PASS = "precheck_machine_pass"
    RECEIPT_STRUCTURALLY_VALID = "receipt_structurally_valid"
    WRITER_STOP_CONFIRMATION_REQUIRED = "writer_stop_confirmation_required"
    OPERATOR_APPROVAL_REQUIRED = "operator_approval_required"
    ACTIVATION_NOT_IMPLEMENTED = "activation_not_implemented"
