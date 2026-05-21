from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar


ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_LIVE_CONFIRMATION_ENV_VAR = "LIVE_TRADING_CONFIRM"
DEFAULT_LIVE_CONFIRMATION_PHRASE = "ENABLE_LIVE_TRADING"
EnumT = TypeVar("EnumT", bound=StrEnum)


class SettingsError(RuntimeError):
    """설정 로딩과 런타임 안전 게이트 실패를 표현한다."""


class ConfigFileNotFoundError(SettingsError):
    """config.toml 파일이 없을 때 live fallback 없이 즉시 실패한다."""


class ConfigEnvironmentError(SettingsError):
    """config.toml의 환경변수 치환 또는 필수 환경변수 검증 실패를 표현한다."""


class RuntimeGateError(SettingsError):
    """paper/live 모드와 브로커 조합의 안전 게이트 실패를 표현한다."""


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class BrokerAdapterName(StrEnum):
    PAPER = "paper"
    KIS_LIVE = "kis_live"


class ExecutionMode(StrEnum):
    NORMAL = "normal"
    REBALANCING = "rebalancing"
    EMERGENCY_TRIGGER = "emergency_trigger"
    MDD_KILLSWITCH = "mdd_killswitch"
    MANUAL = "manual"


@dataclass(frozen=True)
class TradingSettings:
    mode: TradingMode = TradingMode.PAPER
    allow_live_trading: bool = False
    live_confirmation_env_var: str = DEFAULT_LIVE_CONFIRMATION_ENV_VAR
    live_confirmation_phrase: str = DEFAULT_LIVE_CONFIRMATION_PHRASE


@dataclass(frozen=True)
class KisLiveSettings:
    account_env: str = "KIS_LIVE_ACCOUNT"
    app_key_env: str = "KIS_LIVE_APP_KEY"
    app_secret_env: str = "KIS_LIVE_APP_SECRET"


@dataclass(frozen=True)
class BrokerSettings:
    adapter: BrokerAdapterName = BrokerAdapterName.PAPER
    live: KisLiveSettings = field(default_factory=KisLiveSettings)


@dataclass(frozen=True)
class AppSettings:
    trading: TradingSettings = field(default_factory=TradingSettings)
    broker: BrokerSettings = field(default_factory=BrokerSettings)


def load_settings(
    config_path: str | Path = "config/config.toml",
    *,
    environ: Mapping[str, str] | None = None,
) -> AppSettings:
    """config.toml을 읽고 환경변수 치환과 런타임 안전 게이트를 검증한다."""

    raw_config = read_config_file(config_path)
    expanded_config = expand_environment_variables(raw_config, environ=environ)
    settings = parse_settings(expanded_config)
    assert_runtime_safety(settings, environ=environ)
    return settings


def read_config_file(config_path: str | Path) -> dict[str, Any]:
    """설정 파일이 없거나 파싱할 수 없으면 기본값 부팅 없이 실패한다."""

    path = Path(config_path)
    if not path.exists():
        raise ConfigFileNotFoundError(
            f"Config file not found: config_path={path}. Provide config.toml explicitly; no default or live fallback is allowed."
        )

    try:
        with path.open("rb") as file:
            config = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"Invalid TOML in config_path={path}: {exc}") from exc

    return config


def expand_environment_variables(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
    field_path: str = "config",
) -> Any:
    """문자열 안의 ${ENV_NAME}을 치환하고 누락 시 필드 경로와 변수명을 포함해 실패한다."""

    source = os.environ if environ is None else environ

    if isinstance(value, dict):
        return {
            key: expand_environment_variables(nested_value, environ=source, field_path=f"{field_path}.{key}")
            for key, nested_value in value.items()
        }

    if isinstance(value, list):
        return [
            expand_environment_variables(item, environ=source, field_path=f"{field_path}[{index}]")
            for index, item in enumerate(value)
        ]

    if isinstance(value, str):
        return _expand_string_environment_variables(value, environ=source, field_path=field_path)

    return value


def parse_settings(config: Mapping[str, Any]) -> AppSettings:
    """config.toml의 Phase 0 설정을 명시적인 타입으로 변환한다."""

    trading_section = _optional_table(config, "trading", "config.trading")
    broker_section = _optional_table(config, "broker", "config.broker")
    live_section = _optional_table(broker_section, "live", "config.broker.live")

    _assert_allowed_keys(
        trading_section,
        allowed_keys={"mode", "allow_live_trading", "live_confirmation_env_var", "live_confirmation_phrase"},
        field_path="config.trading",
    )
    _assert_allowed_keys(broker_section, allowed_keys={"adapter", "live"}, field_path="config.broker")
    _assert_allowed_keys(
        live_section,
        allowed_keys={"account_env", "app_key_env", "app_secret_env"},
        field_path="config.broker.live",
    )

    return AppSettings(
        trading=TradingSettings(
            mode=_parse_enum(
                trading_section.get("mode", TradingMode.PAPER.value),
                TradingMode,
                field_path="config.trading.mode",
            ),
            allow_live_trading=_parse_bool(
                trading_section.get("allow_live_trading", False),
                field_path="config.trading.allow_live_trading",
            ),
            live_confirmation_env_var=_parse_str(
                trading_section.get("live_confirmation_env_var", DEFAULT_LIVE_CONFIRMATION_ENV_VAR),
                field_path="config.trading.live_confirmation_env_var",
            ),
            live_confirmation_phrase=_parse_str(
                trading_section.get("live_confirmation_phrase", DEFAULT_LIVE_CONFIRMATION_PHRASE),
                field_path="config.trading.live_confirmation_phrase",
            ),
        ),
        broker=BrokerSettings(
            adapter=_parse_enum(
                broker_section.get("adapter", BrokerAdapterName.PAPER.value),
                BrokerAdapterName,
                field_path="config.broker.adapter",
            ),
            live=KisLiveSettings(
                account_env=_parse_str(
                    live_section.get("account_env", "KIS_LIVE_ACCOUNT"),
                    field_path="config.broker.live.account_env",
                ),
                app_key_env=_parse_str(
                    live_section.get("app_key_env", "KIS_LIVE_APP_KEY"),
                    field_path="config.broker.live.app_key_env",
                ),
                app_secret_env=_parse_str(
                    live_section.get("app_secret_env", "KIS_LIVE_APP_SECRET"),
                    field_path="config.broker.live.app_secret_env",
                ),
            ),
        ),
    )


def _optional_table(config: Mapping[str, Any], key: str, field_path: str) -> Mapping[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise SettingsError(f"Invalid settings type: field_path={field_path} must be a TOML table.")
    return value


def _assert_allowed_keys(config: Mapping[str, Any], *, allowed_keys: set[str], field_path: str) -> None:
    unknown_keys = sorted(set(config) - allowed_keys)
    if unknown_keys:
        raise SettingsError(f"Unknown settings keys: field_path={field_path}, keys={', '.join(unknown_keys)}.")


def _parse_enum(value: Any, enum_type: type[EnumT], *, field_path: str) -> EnumT:
    if not isinstance(value, str):
        raise SettingsError(f"Invalid enum value: field_path={field_path} must be a string; got {type(value).__name__}.")

    try:
        return enum_type(value)
    except ValueError as exc:
        allowed_values = ", ".join(item.value for item in enum_type)
        raise SettingsError(
            f"Invalid enum value: field_path={field_path}, value={value}, allowed_values={allowed_values}."
        ) from exc


def _parse_bool(value: Any, *, field_path: str) -> bool:
    if not isinstance(value, bool):
        raise SettingsError(f"Invalid boolean value: field_path={field_path} must be true or false.")
    return value


def _parse_str(value: Any, *, field_path: str) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"Invalid string value: field_path={field_path} must be a string.")
    if value == "":
        raise SettingsError(f"Invalid string value: field_path={field_path} must not be empty.")
    return value


def assert_runtime_safety(settings: AppSettings, *, environ: Mapping[str, str] | None = None) -> None:
    """브로커 연결이나 스케줄러 시작 전에 paper/live 안전 조건을 fail-closed로 검증한다."""

    source = os.environ if environ is None else environ

    if settings.trading.mode == TradingMode.PAPER:
        _assert_paper_runtime_safety(settings)
        return

    _assert_live_runtime_safety(settings, environ=source)


def _expand_string_environment_variables(value: str, *, environ: Mapping[str, str], field_path: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        env_name = match.group("name")
        if env_name not in environ:
            raise ConfigEnvironmentError(
                f"Missing environment variable for config substitution: field_path={field_path}, env_var={env_name}."
            )
        return environ[env_name]

    return ENV_PATTERN.sub(replace_match, value)


def _assert_paper_runtime_safety(settings: AppSettings) -> None:
    if settings.trading.allow_live_trading:
        raise RuntimeGateError(
            "Invalid paper gate: trading.mode=paper cannot set trading.allow_live_trading=true."
        )

    if settings.broker.adapter != BrokerAdapterName.PAPER:
        raise RuntimeGateError(
            "Invalid paper gate: trading.mode=paper requires broker.adapter=paper; "
            f"got broker.adapter={settings.broker.adapter}."
        )


def _assert_live_runtime_safety(settings: AppSettings, *, environ: Mapping[str, str]) -> None:
    if settings.broker.adapter != BrokerAdapterName.KIS_LIVE:
        raise RuntimeGateError(
            "Invalid live gate: trading.mode=live requires broker.adapter=kis_live; "
            f"got broker.adapter={settings.broker.adapter}."
        )

    if not settings.trading.allow_live_trading:
        raise RuntimeGateError(
            "Invalid live gate: broker.adapter=kis_live requires trading.allow_live_trading=true."
        )

    _assert_live_confirmation(settings, environ=environ)
    _assert_live_credentials(settings, environ=environ)


def _assert_live_confirmation(settings: AppSettings, *, environ: Mapping[str, str]) -> None:
    env_var = settings.trading.live_confirmation_env_var
    expected_phrase = settings.trading.live_confirmation_phrase
    actual_phrase = environ.get(env_var)

    if actual_phrase != expected_phrase:
        raise ConfigEnvironmentError(
            "Invalid live confirmation: "
            f"trading.mode=live, broker.adapter=kis_live, env_var={env_var}, expected={expected_phrase}."
        )


def _assert_live_credentials(settings: AppSettings, *, environ: Mapping[str, str]) -> None:
    required_envs = (
        settings.broker.live.account_env,
        settings.broker.live.app_key_env,
        settings.broker.live.app_secret_env,
    )
    missing_envs = [env_name for env_name in required_envs if not environ.get(env_name)]

    if missing_envs:
        raise ConfigEnvironmentError(
            "Missing live broker credentials: "
            "trading.mode=live, broker.adapter=kis_live, "
            f"required_env_vars={', '.join(missing_envs)}."
        )
