from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from domain._strings import normalize_required_string


class ValidationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ValidationIssue(BaseModel):
    """단일 validation 이슈를 표현한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    severity: ValidationSeverity
    path: str | None = None

    @field_validator("code", "message", mode="before")
    @classmethod
    def validate_required_strings(cls, value: Any, info) -> str:
        return normalize_required_string(value, field_name=info.field_name)

    @field_validator("path", mode="before")
    @classmethod
    def validate_optional_path(cls, value: Any) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name="path")


class ValidationResult(BaseModel):
    """schema/Date-ID/rule validation 결과를 공통 구조로 보존한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    issues: tuple[ValidationIssue, ...]
    schema_name: str | None = None
    validator_version: str | None = None

    @field_validator("schema_name", "validator_version", mode="before")
    @classmethod
    def validate_optional_metadata(cls, value: Any, info) -> str | None:
        if value is None:
            return None
        return normalize_required_string(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        if self.passed and any(issue.severity == ValidationSeverity.ERROR for issue in self.issues):
            raise ValueError("ValidationResult with passed=True must not contain ERROR issues.")

        if not self.passed and not self.issues:
            raise ValueError("ValidationResult with passed=False must contain at least one issue.")

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """deterministic replay/저장을 위한 canonical dict 표현을 반환한다."""
        return {
            "passed": self.passed,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity.value,
                    **({"path": issue.path} if issue.path is not None else {}),
                }
                for issue in self.issues
            ],
            **({"schema_name": self.schema_name} if self.schema_name is not None else {}),
            **(
                {"validator_version": self.validator_version}
                if self.validator_version is not None
                else {}
            ),
        }
