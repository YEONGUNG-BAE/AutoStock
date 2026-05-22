from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class LlmSmokeResponse(BaseModel):
    """투자 판단이 아니라 Ollama JSON 고정 구조 출력을 확인하는 smoke schema다."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    action: Literal["HOLD"]
    summary_one_liner: str | None = None
