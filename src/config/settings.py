from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

KIS_WS_ALLOWED_ENVIRONMENTS = frozenset({"prod", "vps"})


def _require_url(value: str, *, schemes: tuple[str, ...], field_name: str) -> None:
    """URL scheme/host를 fail-closed로 검증한다. 자격증명·시세 endpoint 오설정을 막는다."""
    parsed = urlsplit(value)
    if parsed.scheme not in schemes:
        raise SettingsError(
            f"broker.kis_ws_read_only.{field_name} must use scheme in {sorted(schemes)} (got {parsed.scheme!r})."
        )
    if not parsed.hostname:
        raise SettingsError(f"broker.kis_ws_read_only.{field_name} must include a host.")


ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_LIVE_CONFIRMATION_ENV_VAR = "LIVE_TRADING_CONFIRM"
DEFAULT_LIVE_CONFIRMATION_PHRASE = "ENABLE_LIVE_TRADING"
DEFAULT_TINY_LIVE_CONFIRMATION_ENV_VAR = "TINY_LIVE_CONFIRM"
DEFAULT_TINY_LIVE_CONFIRMATION_PHRASE = "ENABLE_TINY_LIVE"
DEFAULT_MAX_TINY_LIVE_NOTIONAL_KRW = 100_000
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
    tiny_live_confirmation_env_var: str = DEFAULT_TINY_LIVE_CONFIRMATION_ENV_VAR
    tiny_live_confirmation_phrase: str = DEFAULT_TINY_LIVE_CONFIRMATION_PHRASE
    max_tiny_live_notional_krw: int = DEFAULT_MAX_TINY_LIVE_NOTIONAL_KRW


@dataclass(frozen=True)
class KisLiveSettings:
    account_env: str = "KIS_LIVE_ACCOUNT"
    app_key_env: str = "KIS_LIVE_APP_KEY"
    app_secret_env: str = "KIS_LIVE_APP_SECRET"
    base_url: str = "https://openapi.koreainvestment.com:9443"


@dataclass(frozen=True)
class BrokerAccountRoleSettings:
    """KIS 계좌 역할 매핑. env var 이름만 저장하고 실제 계좌번호는 환경변수에서 읽는다."""

    use_isa_for_kr_and_gold: bool = True
    use_cma_for_order_execution: bool = False
    kr_tax_advantaged_account_env: str = "KIS_ISA_ACCOUNT"
    us_regular_account_env: str = "KIS_US_REGULAR_ACCOUNT"
    cash_buffer_account_env: str = "KIS_CMA_ACCOUNT"


@dataclass(frozen=True)
class KisReadOnlySettings:
    """KIS read-only smoke 경로 설정. 스케줄러/자동 주문과 연결하지 않는다."""

    enabled: bool = False
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class KisWsReadOnlySettings:
    """KIS 국내 실시간 websocket read-only 경로 설정.

    공개 호가/체결 시세만 구독한다. 주문/체결통보/잔고와 연결되지 않으며 live-order
    flag(allow_live_trading)와 완전히 분리된 confirmation gate를 쓴다. 자격증명은 env var
    이름만 보관한다(실제 값은 환경변수에서만 읽는다).
    """

    enabled: bool = False
    environment: str = "prod"
    approval_base_url: str = "https://openapi.koreainvestment.com:9443"
    websocket_url: str = "ws://ops.koreainvestment.com:21000"
    app_key_env: str = "KIS_LIVE_APP_KEY"
    app_secret_env: str = "KIS_LIVE_APP_SECRET"
    connect_timeout_seconds: float = 10.0
    receive_timeout_seconds: float = 30.0
    max_subscriptions: int = 4
    confirmation_env_var: str = "KIS_WS_READONLY_CONFIRM"
    confirmation_phrase: str = "ENABLE_KIS_WS_READONLY"

    def __post_init__(self) -> None:
        if self.environment not in KIS_WS_ALLOWED_ENVIRONMENTS:
            raise SettingsError(
                "broker.kis_ws_read_only.environment must be one of "
                f"{sorted(KIS_WS_ALLOWED_ENVIRONMENTS)} (got {self.environment!r})."
            )
        # approval은 자격증명을 보내므로 https만, websocket은 ws/wss만 허용한다.
        _require_url(self.approval_base_url, schemes=("https",), field_name="approval_base_url")
        _require_url(self.websocket_url, schemes=("ws", "wss"), field_name="websocket_url")
        if self.max_subscriptions < 1:
            raise SettingsError("broker.kis_ws_read_only.max_subscriptions must be >= 1.")
        if self.connect_timeout_seconds <= 0:
            raise SettingsError("broker.kis_ws_read_only.connect_timeout_seconds must be > 0.")
        if self.receive_timeout_seconds <= 0:
            raise SettingsError("broker.kis_ws_read_only.receive_timeout_seconds must be > 0.")


@dataclass(frozen=True)
class BrokerSettings:
    adapter: BrokerAdapterName = BrokerAdapterName.PAPER
    live: KisLiveSettings = field(default_factory=KisLiveSettings)
    account_roles: BrokerAccountRoleSettings = field(default_factory=BrokerAccountRoleSettings)
    kis_read_only: KisReadOnlySettings = field(default_factory=KisReadOnlySettings)
    kis_ws_read_only: KisWsReadOnlySettings = field(default_factory=KisWsReadOnlySettings)


@dataclass(frozen=True)
class LlmSettings:
    provider: str = "ollama"
    model: str = "qwen3.6:35b"
    host: str = "http://localhost:11434"
    temperature: float = 0
    seed: int = 42
    keep_alive: str = "24h"
    default_num_ctx: int = 4096
    default_think: bool = False
    timeout_seconds: float = 120
    retry_count: int = 0


RUNTIME_PAPER_FAST_LOOP_MARKET = "KR"
_RUNTIME_ROOT_DIR = "runtime"
_KRX_SYMBOL_PATTERN = re.compile(r"\A\d{6}\Z")


def _validate_runtime_relative_path(value: str, *, field_path: str) -> str:
    """runtime/ 하위 상대경로만 허용한다. 경로 탈출·심볼릭링크를 fail-closed로 막고,
    파싱 단계에서 파일이나 디렉터리를 생성하지 않는다."""

    pure = Path(value)
    if pure.is_absolute():
        raise SettingsError(
            f"Invalid runtime path: field_path={field_path} must be a relative path under '{_RUNTIME_ROOT_DIR}/'."
        )
    parts = pure.parts
    if not parts or parts[0] != _RUNTIME_ROOT_DIR:
        raise SettingsError(
            f"Invalid runtime path: field_path={field_path} must start with '{_RUNTIME_ROOT_DIR}/'."
        )
    if any(part == ".." for part in parts):
        raise SettingsError(
            f"Invalid runtime path: field_path={field_path} must not contain '..' path traversal."
        )
    if any(part == "." for part in parts):
        raise SettingsError(
            f"Invalid runtime path: field_path={field_path} must not contain '.' path segments."
        )
    if len(parts) < 2:
        raise SettingsError(
            f"Invalid runtime path: field_path={field_path} must point at a file under '{_RUNTIME_ROOT_DIR}/'."
        )

    # 심볼릭 링크 컴포넌트 거부(읽기만 수행, 생성하지 않음).
    accumulated = Path(parts[0])
    for part in parts[1:]:
        accumulated = accumulated / part
        if accumulated.is_symlink():
            raise SettingsError(
                f"Invalid runtime path: field_path={field_path} must not traverse a symlink "
                f"(component={accumulated})."
            )

    return os.path.normpath(value)


@dataclass(frozen=True)
class RuntimePaperFastLoopSettings:
    """오프라인 paper fast-loop composition의 runtime 경로 설정.

    기본 비활성(enabled=false)이며 단일 KR 6자리 종목만 허용한다. 네 개의 runtime 경로는
    runtime/ 하위 상대경로여야 하고 경로 탈출·심볼릭링크·중복을 fail-closed로 거부한다.
    파싱 단계에서 파일·디렉터리를 생성하지 않는다.
    """

    enabled: bool = False
    market: str = RUNTIME_PAPER_FAST_LOOP_MARKET
    symbol: str = "000000"
    snapshot_path: str = "runtime/paper_fast_loop/execution_inputs_snapshot.json"
    active_decision_store_path: str = "runtime/paper_fast_loop/active_decision_store.sqlite3"
    ledger_path: str = "runtime/paper_fast_loop/ledger.sqlite3"
    trigger_journal_path: str = "runtime/paper_fast_loop/trigger_journal.sqlite3"

    def __post_init__(self) -> None:
        if self.market != RUNTIME_PAPER_FAST_LOOP_MARKET:
            raise SettingsError(
                "Invalid runtime market: "
                f"config.runtime.paper_fast_loop.market must be {RUNTIME_PAPER_FAST_LOOP_MARKET!r} "
                f"(got {self.market!r})."
            )
        if not _KRX_SYMBOL_PATTERN.match(self.symbol):
            raise SettingsError(
                "Invalid runtime symbol: "
                "config.runtime.paper_fast_loop.symbol must be a 6-digit KRX symbol "
                f"(got {self.symbol!r})."
            )

        path_fields = {
            "snapshot_path": self.snapshot_path,
            "active_decision_store_path": self.active_decision_store_path,
            "ledger_path": self.ledger_path,
            "trigger_journal_path": self.trigger_journal_path,
        }
        normalized_by_field: dict[str, str] = {}
        for name, raw in path_fields.items():
            normalized_by_field[name] = _validate_runtime_relative_path(
                raw, field_path=f"config.runtime.paper_fast_loop.{name}"
            )

        seen: dict[str, str] = {}
        for name, normalized in normalized_by_field.items():
            if normalized in seen:
                raise SettingsError(
                    "Invalid runtime path: "
                    f"config.runtime.paper_fast_loop.{name} collides with "
                    f"config.runtime.paper_fast_loop.{seen[normalized]} (both resolve to {normalized})."
                )
            seen[normalized] = name


@dataclass(frozen=True)
class RuntimeSettings:
    paper_fast_loop: RuntimePaperFastLoopSettings = field(default_factory=RuntimePaperFastLoopSettings)


@dataclass(frozen=True)
class AppSettings:
    trading: TradingSettings = field(default_factory=TradingSettings)
    broker: BrokerSettings = field(default_factory=BrokerSettings)
    llm: LlmSettings = field(default_factory=LlmSettings)
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)


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
    account_roles_section = _optional_table(broker_section, "account_roles", "config.broker.account_roles")
    kis_read_only_section = _optional_table(broker_section, "kis_read_only", "config.broker.kis_read_only")
    kis_ws_read_only_section = _optional_table(
        broker_section, "kis_ws_read_only", "config.broker.kis_ws_read_only"
    )
    llm_section = _optional_table(config, "llm", "config.llm")
    runtime_section = _optional_table(config, "runtime", "config.runtime")
    paper_fast_loop_section = _optional_table(
        runtime_section, "paper_fast_loop", "config.runtime.paper_fast_loop"
    )

    _assert_allowed_keys(
        trading_section,
        allowed_keys={
            "mode",
            "allow_live_trading",
            "live_confirmation_env_var",
            "live_confirmation_phrase",
            "tiny_live_confirmation_env_var",
            "tiny_live_confirmation_phrase",
            "max_tiny_live_notional_krw",
        },
        field_path="config.trading",
    )
    _assert_allowed_keys(
        broker_section,
        allowed_keys={"adapter", "live", "account_roles", "kis_read_only", "kis_ws_read_only"},
        field_path="config.broker",
    )
    _assert_allowed_keys(
        live_section,
        allowed_keys={"account_env", "app_key_env", "app_secret_env", "base_url"},
        field_path="config.broker.live",
    )
    _assert_allowed_keys(
        account_roles_section,
        allowed_keys={
            "use_isa_for_kr_and_gold",
            "use_cma_for_order_execution",
            "kr_tax_advantaged_account_env",
            "us_regular_account_env",
            "cash_buffer_account_env",
        },
        field_path="config.broker.account_roles",
    )
    _assert_allowed_keys(
        kis_read_only_section,
        allowed_keys={"enabled", "timeout_seconds"},
        field_path="config.broker.kis_read_only",
    )
    _assert_allowed_keys(
        kis_ws_read_only_section,
        allowed_keys={
            "enabled",
            "environment",
            "approval_base_url",
            "websocket_url",
            "app_key_env",
            "app_secret_env",
            "connect_timeout_seconds",
            "receive_timeout_seconds",
            "max_subscriptions",
            "confirmation_env_var",
            "confirmation_phrase",
        },
        field_path="config.broker.kis_ws_read_only",
    )
    _assert_allowed_keys(
        llm_section,
        allowed_keys={
            "provider",
            "model",
            "host",
            "temperature",
            "seed",
            "keep_alive",
            "default_num_ctx",
            "default_think",
            "timeout_seconds",
            "retry_count",
        },
        field_path="config.llm",
    )
    _assert_allowed_keys(
        runtime_section,
        allowed_keys={"paper_fast_loop"},
        field_path="config.runtime",
    )
    _assert_allowed_keys(
        paper_fast_loop_section,
        allowed_keys={
            "enabled",
            "market",
            "symbol",
            "snapshot_path",
            "active_decision_store_path",
            "ledger_path",
            "trigger_journal_path",
        },
        field_path="config.runtime.paper_fast_loop",
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
            tiny_live_confirmation_env_var=_parse_str(
                trading_section.get("tiny_live_confirmation_env_var", DEFAULT_TINY_LIVE_CONFIRMATION_ENV_VAR),
                field_path="config.trading.tiny_live_confirmation_env_var",
            ),
            tiny_live_confirmation_phrase=_parse_str(
                trading_section.get("tiny_live_confirmation_phrase", DEFAULT_TINY_LIVE_CONFIRMATION_PHRASE),
                field_path="config.trading.tiny_live_confirmation_phrase",
            ),
            max_tiny_live_notional_krw=_parse_positive_int(
                trading_section.get("max_tiny_live_notional_krw", DEFAULT_MAX_TINY_LIVE_NOTIONAL_KRW),
                field_path="config.trading.max_tiny_live_notional_krw",
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
                base_url=_parse_str(
                    live_section.get("base_url", "https://openapi.koreainvestment.com:9443"),
                    field_path="config.broker.live.base_url",
                ),
            ),
            account_roles=BrokerAccountRoleSettings(
                use_isa_for_kr_and_gold=_parse_bool(
                    account_roles_section.get("use_isa_for_kr_and_gold", True),
                    field_path="config.broker.account_roles.use_isa_for_kr_and_gold",
                ),
                use_cma_for_order_execution=_parse_bool(
                    account_roles_section.get("use_cma_for_order_execution", False),
                    field_path="config.broker.account_roles.use_cma_for_order_execution",
                ),
                kr_tax_advantaged_account_env=_parse_str(
                    account_roles_section.get("kr_tax_advantaged_account_env", "KIS_ISA_ACCOUNT"),
                    field_path="config.broker.account_roles.kr_tax_advantaged_account_env",
                ),
                us_regular_account_env=_parse_str(
                    account_roles_section.get("us_regular_account_env", "KIS_US_REGULAR_ACCOUNT"),
                    field_path="config.broker.account_roles.us_regular_account_env",
                ),
                cash_buffer_account_env=_parse_str(
                    account_roles_section.get("cash_buffer_account_env", "KIS_CMA_ACCOUNT"),
                    field_path="config.broker.account_roles.cash_buffer_account_env",
                ),
            ),
            kis_read_only=KisReadOnlySettings(
                enabled=_parse_bool(
                    kis_read_only_section.get("enabled", False),
                    field_path="config.broker.kis_read_only.enabled",
                ),
                timeout_seconds=_parse_positive_number(
                    kis_read_only_section.get("timeout_seconds", 10.0),
                    field_path="config.broker.kis_read_only.timeout_seconds",
                ),
            ),
            kis_ws_read_only=KisWsReadOnlySettings(
                enabled=_parse_bool(
                    kis_ws_read_only_section.get("enabled", False),
                    field_path="config.broker.kis_ws_read_only.enabled",
                ),
                environment=_parse_str(
                    kis_ws_read_only_section.get("environment", "prod"),
                    field_path="config.broker.kis_ws_read_only.environment",
                ),
                approval_base_url=_parse_str(
                    kis_ws_read_only_section.get(
                        "approval_base_url", "https://openapi.koreainvestment.com:9443"
                    ),
                    field_path="config.broker.kis_ws_read_only.approval_base_url",
                ),
                websocket_url=_parse_str(
                    kis_ws_read_only_section.get("websocket_url", "ws://ops.koreainvestment.com:21000"),
                    field_path="config.broker.kis_ws_read_only.websocket_url",
                ),
                app_key_env=_parse_str(
                    kis_ws_read_only_section.get("app_key_env", "KIS_LIVE_APP_KEY"),
                    field_path="config.broker.kis_ws_read_only.app_key_env",
                ),
                app_secret_env=_parse_str(
                    kis_ws_read_only_section.get("app_secret_env", "KIS_LIVE_APP_SECRET"),
                    field_path="config.broker.kis_ws_read_only.app_secret_env",
                ),
                connect_timeout_seconds=_parse_positive_number(
                    kis_ws_read_only_section.get("connect_timeout_seconds", 10.0),
                    field_path="config.broker.kis_ws_read_only.connect_timeout_seconds",
                ),
                receive_timeout_seconds=_parse_positive_number(
                    kis_ws_read_only_section.get("receive_timeout_seconds", 30.0),
                    field_path="config.broker.kis_ws_read_only.receive_timeout_seconds",
                ),
                max_subscriptions=_parse_positive_int(
                    kis_ws_read_only_section.get("max_subscriptions", 4),
                    field_path="config.broker.kis_ws_read_only.max_subscriptions",
                ),
                confirmation_env_var=_parse_str(
                    kis_ws_read_only_section.get("confirmation_env_var", "KIS_WS_READONLY_CONFIRM"),
                    field_path="config.broker.kis_ws_read_only.confirmation_env_var",
                ),
                confirmation_phrase=_parse_str(
                    kis_ws_read_only_section.get("confirmation_phrase", "ENABLE_KIS_WS_READONLY"),
                    field_path="config.broker.kis_ws_read_only.confirmation_phrase",
                ),
            ),
        ),
        llm=LlmSettings(
            provider=_parse_str(llm_section.get("provider", "ollama"), field_path="config.llm.provider"),
            model=_parse_str(llm_section.get("model", "qwen3.6:35b"), field_path="config.llm.model"),
            host=_parse_str(llm_section.get("host", "http://localhost:11434"), field_path="config.llm.host"),
            temperature=_parse_zero_temperature(
                llm_section.get("temperature", 0),
                field_path="config.llm.temperature",
            ),
            seed=_parse_int(llm_section.get("seed", 42), field_path="config.llm.seed"),
            keep_alive=_parse_str(llm_section.get("keep_alive", "24h"), field_path="config.llm.keep_alive"),
            default_num_ctx=_parse_positive_int(
                llm_section.get("default_num_ctx", 4096),
                field_path="config.llm.default_num_ctx",
            ),
            default_think=_parse_bool(
                llm_section.get("default_think", False),
                field_path="config.llm.default_think",
            ),
            timeout_seconds=_parse_positive_number(
                llm_section.get("timeout_seconds", 120),
                field_path="config.llm.timeout_seconds",
            ),
            retry_count=_parse_non_negative_int(
                llm_section.get("retry_count", 0),
                field_path="config.llm.retry_count",
            ),
        ),
        runtime=RuntimeSettings(
            paper_fast_loop=RuntimePaperFastLoopSettings(
                enabled=_parse_bool(
                    paper_fast_loop_section.get("enabled", False),
                    field_path="config.runtime.paper_fast_loop.enabled",
                ),
                market=_parse_str(
                    paper_fast_loop_section.get("market", RUNTIME_PAPER_FAST_LOOP_MARKET),
                    field_path="config.runtime.paper_fast_loop.market",
                ),
                symbol=_parse_str(
                    paper_fast_loop_section.get("symbol", "000000"),
                    field_path="config.runtime.paper_fast_loop.symbol",
                ),
                snapshot_path=_parse_str(
                    paper_fast_loop_section.get(
                        "snapshot_path", "runtime/paper_fast_loop/execution_inputs_snapshot.json"
                    ),
                    field_path="config.runtime.paper_fast_loop.snapshot_path",
                ),
                active_decision_store_path=_parse_str(
                    paper_fast_loop_section.get(
                        "active_decision_store_path",
                        "runtime/paper_fast_loop/active_decision_store.sqlite3",
                    ),
                    field_path="config.runtime.paper_fast_loop.active_decision_store_path",
                ),
                ledger_path=_parse_str(
                    paper_fast_loop_section.get(
                        "ledger_path", "runtime/paper_fast_loop/ledger.sqlite3"
                    ),
                    field_path="config.runtime.paper_fast_loop.ledger_path",
                ),
                trigger_journal_path=_parse_str(
                    paper_fast_loop_section.get(
                        "trigger_journal_path", "runtime/paper_fast_loop/trigger_journal.sqlite3"
                    ),
                    field_path="config.runtime.paper_fast_loop.trigger_journal_path",
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


def _parse_number(value: Any, *, field_path: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise SettingsError(f"Invalid number value: field_path={field_path} must be a number.")
    return value


def _parse_zero_temperature(value: Any, *, field_path: str) -> float:
    parsed_value = _parse_number(value, field_path=field_path)
    if parsed_value != 0:
        raise SettingsError(
            f"Invalid temperature value: field_path={field_path} must be 0 for deterministic trading decisions."
        )
    return parsed_value


def _parse_positive_number(value: Any, *, field_path: str) -> float:
    parsed_value = _parse_number(value, field_path=field_path)
    if parsed_value <= 0:
        raise SettingsError(f"Invalid number value: field_path={field_path} must be greater than 0.")
    return parsed_value


def _parse_int(value: Any, *, field_path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SettingsError(f"Invalid integer value: field_path={field_path} must be an integer.")
    return value


def _parse_positive_int(value: Any, *, field_path: str) -> int:
    parsed_value = _parse_int(value, field_path=field_path)
    if parsed_value <= 0:
        raise SettingsError(f"Invalid integer value: field_path={field_path} must be greater than 0.")
    return parsed_value


def _parse_non_negative_int(value: Any, *, field_path: str) -> int:
    parsed_value = _parse_int(value, field_path=field_path)
    if parsed_value < 0:
        raise SettingsError(f"Invalid integer value: field_path={field_path} must be greater than or equal to 0.")
    return parsed_value


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
