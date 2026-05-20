"""Phase 0 runtime configuration loader.

Responsibilities (Phase 0 only):

- Load `config.toml` and resolve `${ENV_NAME}` placeholders against
  the provided environment mapping.
- Provide typed enums for trading mode, broker adapter, and execution mode.
- Run startup safety gates before any broker/scheduler component is built.

Out of scope for Phase 0:

- Domain models (Percent, DateId, AllocatorDecision, ...).
- Broker / Ollama / KIS client implementations.
- Scheduler.

Why a separate `ConfigError` instead of `ValueError` / `RuntimeError`:

`config.toml`/env failures are *startup gates*, not arbitrary runtime bugs.
A dedicated exception lets `main.py` catch and refuse to start without
catching unrelated runtime exceptions, which is the explicit rule:
`config.toml` parse failure must never silently fall back to paper or live.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError


_ENV_PLACEHOLDER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}"
)


class ConfigError(RuntimeError):
    """Raised when configuration loading or a startup gate fails.

    Messages must include enough context (field path, env var name, or
    mode/adapter combination) for an operator to debug from logs alone.
    """


class _StrictModel(BaseModel):
    """Base model that rejects unknown keys and is immutable after build."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class BrokerAdapterName(StrEnum):
    PAPER = "paper"
    KIS_MOCK = "kis_mock"
    KIS_LIVE = "kis_live"


class ExecutionMode(StrEnum):
    """Order-validation runtime mode.

    Phase 0 only *defines* this enum so later phases (risk filters, MDD
    killswitch, emergency triggers) can import a single source of truth.
    Phase 0 does not branch on it.
    """

    NORMAL = "normal"
    REBALANCING = "rebalancing"
    EMERGENCY_TRIGGER = "emergency_trigger"
    MDD_KILLSWITCH = "mdd_killswitch"
    MANUAL = "manual"


class TradingSettings(_StrictModel):
    mode: TradingMode = TradingMode.PAPER
    allow_live_trading: bool = False
    live_confirmation_env_var: str = "LIVE_TRADING_CONFIRM"
    live_confirmation_phrase: str = "ENABLE_LIVE_TRADING"


class KisCredentialEnvSettings(_StrictModel):
    """Environment variable *names* for KIS credentials.

    Only env var names live in `config.toml`. The actual secret values must
    never be written to disk, committed, or printed.
    """

    app_key_env: str
    app_secret_env: str
    account_env: str


def _default_kis_mock_env() -> KisCredentialEnvSettings:
    return KisCredentialEnvSettings(
        app_key_env="KIS_MOCK_APP_KEY",
        app_secret_env="KIS_MOCK_APP_SECRET",
        account_env="KIS_MOCK_ACCOUNT",
    )


def _default_kis_live_env() -> KisCredentialEnvSettings:
    return KisCredentialEnvSettings(
        app_key_env="KIS_LIVE_APP_KEY",
        app_secret_env="KIS_LIVE_APP_SECRET",
        account_env="KIS_LIVE_ACCOUNT",
    )


class BrokerSettings(_StrictModel):
    adapter: BrokerAdapterName = BrokerAdapterName.PAPER
    kis_mock: KisCredentialEnvSettings = Field(default_factory=_default_kis_mock_env)
    kis_live: KisCredentialEnvSettings = Field(default_factory=_default_kis_live_env)


class Settings(_StrictModel):
    trading: TradingSettings = Field(default_factory=TradingSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)


def load_settings(
    config_path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """Load and validate Phase 0 runtime settings.

    Parameters
    ----------
    config_path:
        Path to a TOML file. The file must exist; Phase 0 refuses to boot
        with defaults or to fall back to live mode on missing/invalid config.
    env:
        Optional mapping used for `${ENV_NAME}` resolution and gate checks.
        Defaults to `os.environ`. Tests pass an explicit dict to stay
        isolated from the host shell environment.
    """

    env_view: Mapping[str, str] = env if env is not None else os.environ
    path = Path(config_path)

    if not path.is_file():
        raise ConfigError(
            f"Config file not found: '{path}'. "
            "Phase 0 will not boot with built-in defaults nor fall back to live mode."
        )

    try:
        with path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Failed to parse TOML at '{path}': {exc}") from exc

    resolved = _resolve_env_placeholders(raw, env=env_view)

    try:
        settings = Settings.model_validate(resolved)
    except ValidationError as exc:
        raise ConfigError(
            f"Invalid config schema in '{path}': {exc.errors()}"
        ) from exc

    _assert_runtime_safety(settings, env=env_view)
    return settings


def _resolve_env_placeholders(
    value: Any,
    *,
    env: Mapping[str, str],
    path: tuple[str, ...] = (),
) -> Any:
    """Recursively replace `${ENV_NAME}` in string leaves of a TOML tree.

    Missing env vars raise `ConfigError` rather than silently substituting
    an empty string, so the failure is anchored to a specific config field
    instead of bubbling up as a confusing downstream validation error.
    """

    if isinstance(value, Mapping):
        return {
            key: _resolve_env_placeholders(item, env=env, path=path + (str(key),))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_env_placeholders(item, env=env, path=path + (f"[{idx}]",))
            for idx, item in enumerate(value)
        ]
    if isinstance(value, str):
        return _substitute_string(value, env=env, path=path)
    return value


def _substitute_string(
    value: str,
    *,
    env: Mapping[str, str],
    path: tuple[str, ...],
) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in env:
            location = ".".join(path) if path else "<root>"
            raise ConfigError(
                f"Config field '{location}' references environment variable "
                f"'${{{name}}}', but the variable is not set."
            )
        return env[name]

    return _ENV_PLACEHOLDER_PATTERN.sub(_replace, value)


def _assert_runtime_safety(settings: Settings, *, env: Mapping[str, str]) -> None:
    """Run all Phase 0 startup gates.

    Gate matrix:

    | trading.mode | broker.adapter | result                                    |
    |--------------|----------------|-------------------------------------------|
    | paper        | paper          | OK, no secrets required                   |
    | paper        | kis_mock       | requires KIS_MOCK_* env vars              |
    | paper        | kis_live       | always fails (adapter/mode mismatch)      |
    | live         | paper          | always fails (adapter/mode mismatch)      |
    | live         | kis_mock       | always fails (adapter/mode mismatch)      |
    | live         | kis_live       | requires allow_live_trading + confirm env |
    |              |                | phrase + KIS_LIVE_* env vars              |
    """

    mode = settings.trading.mode
    adapter = settings.broker.adapter

    if mode is TradingMode.PAPER:
        _gate_paper_mode(settings, env=env, adapter=adapter)
        return

    _gate_live_mode(settings, env=env, adapter=adapter)


def _gate_paper_mode(
    settings: Settings,
    *,
    env: Mapping[str, str],
    adapter: BrokerAdapterName,
) -> None:
    if adapter is BrokerAdapterName.KIS_LIVE:
        raise ConfigError(
            "Adapter/mode mismatch: trading.mode='paper' must not use "
            "broker.adapter='kis_live'. Allowed paper adapters: 'paper', 'kis_mock'."
        )

    if adapter is BrokerAdapterName.KIS_MOCK:
        _require_env_present(
            env=env,
            env_names=(
                settings.broker.kis_mock.app_key_env,
                settings.broker.kis_mock.app_secret_env,
                settings.broker.kis_mock.account_env,
            ),
            context=(
                "trading.mode='paper' with broker.adapter='kis_mock' "
                "requires KIS mock credentials"
            ),
        )


def _gate_live_mode(
    settings: Settings,
    *,
    env: Mapping[str, str],
    adapter: BrokerAdapterName,
) -> None:
    if adapter is not BrokerAdapterName.KIS_LIVE:
        raise ConfigError(
            "Adapter/mode mismatch: trading.mode='live' requires "
            f"broker.adapter='kis_live', got broker.adapter='{adapter.value}'."
        )

    if not settings.trading.allow_live_trading:
        raise ConfigError(
            "Live trading blocked: trading.allow_live_trading is false. "
            "Live mode requires an explicit `allow_live_trading = true` in config.toml."
        )

    confirm_env_name = settings.trading.live_confirmation_env_var
    expected_phrase = settings.trading.live_confirmation_phrase
    actual_phrase = env.get(confirm_env_name)
    if actual_phrase != expected_phrase:
        observed = "<unset>" if actual_phrase is None else "<mismatched-value>"
        raise ConfigError(
            f"Live trading blocked: environment variable '{confirm_env_name}' "
            f"must equal trading.live_confirmation_phrase (got {observed})."
        )

    _require_env_present(
        env=env,
        env_names=(
            settings.broker.kis_live.app_key_env,
            settings.broker.kis_live.app_secret_env,
            settings.broker.kis_live.account_env,
        ),
        context=(
            "trading.mode='live' with broker.adapter='kis_live' "
            "requires KIS live credentials"
        ),
    )


def _require_env_present(
    *,
    env: Mapping[str, str],
    env_names: tuple[str, ...],
    context: str,
) -> None:
    missing = [name for name in env_names if not env.get(name)]
    if missing:
        raise ConfigError(f"{context}; missing environment variables: {missing}.")


__all__ = [
    "BrokerAdapterName",
    "BrokerSettings",
    "ConfigError",
    "ExecutionMode",
    "KisCredentialEnvSettings",
    "Settings",
    "TradingMode",
    "TradingSettings",
    "load_settings",
]
