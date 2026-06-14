from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
from config.settings import (
    BrokerAdapterName,
    ConfigEnvironmentError,
    ConfigFileNotFoundError,
    ExecutionMode,
    SettingsError,
    TradingMode,
    load_settings,
)


LIVE_ENV_VARS = (
    "LIVE_TRADING_CONFIRM",
    "KIS_LIVE_ACCOUNT",
    "KIS_LIVE_APP_KEY",
    "KIS_LIVE_APP_SECRET",
    "TEST_LIVE_ACCOUNT",
    "TEST_LIVE_APP_KEY",
    "TEST_LIVE_APP_SECRET",
    "TEST_ACCOUNT_ENV_NAME",
    "TEST_MISSING_ENV",
)


@pytest.fixture(autouse=True)
def isolate_live_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # 테스트가 사용자의 실제 셸 환경변수에 의존하지 않도록 라이브 관련 값을 모두 지운다.
    for env_var in LIVE_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)


def write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def test_minimal_config_defaults_to_paper_mode(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path, ""))

    assert settings.trading.mode == TradingMode.PAPER
    assert settings.broker.adapter == BrokerAdapterName.PAPER
    assert settings.trading.allow_live_trading is False


def test_minimal_config_defaults_to_ollama_smoke_settings(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path, ""))

    assert settings.llm.provider == "ollama"
    assert settings.llm.model == "qwen3.6:35b"
    assert settings.llm.default_think is False
    assert settings.llm.temperature == 0
    assert settings.llm.seed == 42


def test_llm_temperature_must_be_zero_for_deterministic_decisions(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[llm]
temperature = 1
""",
    )

    with pytest.raises(
        SettingsError,
        match="field_path=config.llm.temperature.*must be 0 for deterministic trading decisions",
    ):
        load_settings(config_path)


def test_kis_ws_read_only_defaults(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path, ""))
    ws = settings.broker.kis_ws_read_only
    assert ws.enabled is False
    assert ws.environment == "prod"
    assert ws.websocket_url == "ws://ops.koreainvestment.com:21000"
    assert ws.app_key_env == "KIS_LIVE_APP_KEY"
    assert ws.app_secret_env == "KIS_LIVE_APP_SECRET"
    assert ws.connect_timeout_seconds == 10.0
    assert ws.receive_timeout_seconds == 30.0
    assert ws.max_subscriptions == 4
    assert ws.confirmation_env_var == "KIS_WS_READONLY_CONFIRM"
    assert ws.confirmation_phrase == "ENABLE_KIS_WS_READONLY"


def test_kis_ws_read_only_overrides(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
enabled = true
environment = "vps"
websocket_url = "ws://example.invalid:21000"
max_subscriptions = 2
receive_timeout_seconds = 12
""",
    )
    settings = load_settings(config_path)
    ws = settings.broker.kis_ws_read_only
    assert ws.enabled is True
    assert ws.environment == "vps"
    assert ws.websocket_url == "ws://example.invalid:21000"
    assert ws.max_subscriptions == 2
    assert ws.receive_timeout_seconds == 12.0


def test_kis_ws_read_only_rejects_unknown_environment(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
environment = "test"
""",
    )
    with pytest.raises(SettingsError, match="environment must be one of"):
        load_settings(config_path)


def test_kis_ws_read_only_rejects_non_https_approval_url(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
approval_base_url = "http://openapi.koreainvestment.com:9443"
""",
    )
    with pytest.raises(SettingsError, match="approval_base_url"):
        load_settings(config_path)


def test_kis_ws_read_only_rejects_non_ws_websocket_url(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
websocket_url = "https://ops.koreainvestment.com:21000"
""",
    )
    with pytest.raises(SettingsError, match="websocket_url"):
        load_settings(config_path)


def test_kis_ws_read_only_rejects_url_without_host(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
websocket_url = "ws://"
""",
    )
    with pytest.raises(SettingsError, match="websocket_url"):
        load_settings(config_path)


def test_kis_ws_read_only_rejects_unknown_key(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
allow_orders = true
""",
    )
    with pytest.raises(SettingsError, match="config.broker.kis_ws_read_only"):
        load_settings(config_path)


def test_kis_ws_read_only_rejects_non_positive_max_subscriptions(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.kis_ws_read_only]
max_subscriptions = 0
""",
    )
    with pytest.raises(SettingsError, match="config.broker.kis_ws_read_only.max_subscriptions"):
        load_settings(config_path)


def test_kis_ws_read_only_enabled_allowed_in_paper_mode(tmp_path: Path) -> None:
    # read-only WS는 주문이 없으므로 paper gate를 위반하지 않는다.
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "paper"
allow_live_trading = false

[broker.kis_ws_read_only]
enabled = true
""",
    )
    settings = load_settings(config_path)
    assert settings.broker.kis_ws_read_only.enabled is True
    assert settings.trading.allow_live_trading is False


def test_missing_config_file_fails_without_fallback(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-config.toml"

    with pytest.raises(ConfigFileNotFoundError, match="Config file not found.*no default or live fallback"):
        load_settings(missing_path)


def test_paper_mode_with_paper_adapter_passes_without_secrets(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "paper"

[broker]
adapter = "paper"
""",
    )

    settings = load_settings(config_path)

    assert settings.trading.mode == TradingMode.PAPER
    assert settings.broker.adapter == BrokerAdapterName.PAPER


def test_paper_mode_with_allow_live_trading_true_fails(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "paper"
allow_live_trading = true

[broker]
adapter = "paper"
""",
    )

    with pytest.raises(SettingsError, match="trading.mode=paper.*trading.allow_live_trading=true"):
        load_settings(config_path)


def test_paper_mode_with_kis_live_adapter_fails(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "paper"

[broker]
adapter = "kis_live"
""",
    )

    with pytest.raises(SettingsError, match="trading.mode=paper requires broker.adapter=paper"):
        load_settings(config_path)


def test_live_mode_with_paper_adapter_fails(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "live"
allow_live_trading = true

[broker]
adapter = "paper"
""",
    )

    with pytest.raises(SettingsError, match="trading.mode=live requires broker.adapter=kis_live"):
        load_settings(config_path)


def test_live_kis_live_with_allow_live_trading_false_fails(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "live"
allow_live_trading = false

[broker]
adapter = "kis_live"
""",
    )

    with pytest.raises(SettingsError, match="broker.adapter=kis_live requires trading.allow_live_trading=true"):
        load_settings(config_path)


def test_live_kis_live_with_missing_credentials_fails(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "live"
allow_live_trading = true

[broker]
adapter = "kis_live"
""",
    )

    with pytest.raises(ConfigEnvironmentError, match="KIS_LIVE_ACCOUNT.*KIS_LIVE_APP_KEY.*KIS_LIVE_APP_SECRET"):
        load_settings(config_path, environ={"LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING"})


def test_live_kis_live_with_confirmation_mismatch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "TEST_WRONG_CONFIRMATION")
    monkeypatch.setenv("KIS_LIVE_ACCOUNT", "TEST_ACCOUNT")
    monkeypatch.setenv("KIS_LIVE_APP_KEY", "TEST_APP_KEY")
    monkeypatch.setenv("KIS_LIVE_APP_SECRET", "TEST_APP_SECRET")
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "live"
allow_live_trading = true

[broker]
adapter = "kis_live"
""",
    )

    with pytest.raises(ConfigEnvironmentError, match="env_var=LIVE_TRADING_CONFIRM.*expected=ENABLE_LIVE_TRADING"):
        load_settings(config_path)


def test_live_kis_live_with_missing_confirmation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIS_LIVE_ACCOUNT", "TEST_ACCOUNT")
    monkeypatch.setenv("KIS_LIVE_APP_KEY", "TEST_APP_KEY")
    monkeypatch.setenv("KIS_LIVE_APP_SECRET", "TEST_APP_SECRET")
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "live"
allow_live_trading = true

[broker]
adapter = "kis_live"
""",
    )

    with pytest.raises(ConfigEnvironmentError, match="env_var=LIVE_TRADING_CONFIRM.*expected=ENABLE_LIVE_TRADING"):
        load_settings(config_path)


def test_live_kis_live_passes_only_when_all_gates_pass(
    tmp_path: Path,
) -> None:
    config_path = write_config(
        tmp_path,
        """
[trading]
mode = "live"
allow_live_trading = true

[broker]
adapter = "kis_live"
""",
    )

    settings = load_settings(
        config_path,
        environ={
            "LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING",
            "KIS_LIVE_ACCOUNT": "TEST_ACCOUNT",
            "KIS_LIVE_APP_KEY": "TEST_APP_KEY",
            "KIS_LIVE_APP_SECRET": "TEST_APP_SECRET",
        },
    )

    assert settings.trading.mode == TradingMode.LIVE
    assert settings.broker.adapter == BrokerAdapterName.KIS_LIVE


def test_config_environment_substitution_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_ACCOUNT_ENV_NAME", "TEST_LIVE_ACCOUNT")
    config_path = write_config(
        tmp_path,
        """
[broker.live]
account_env = "${TEST_ACCOUNT_ENV_NAME}"
""",
    )

    settings = load_settings(config_path)

    assert settings.broker.live.account_env == "TEST_LIVE_ACCOUNT"


def test_missing_config_environment_substitution_fails_with_field_path_and_env_name(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[broker.live]
app_key_env = "${TEST_MISSING_ENV}"
""",
    )

    with pytest.raises(ConfigEnvironmentError, match="field_path=config.broker.live.app_key_env.*env_var=TEST_MISSING_ENV"):
        load_settings(config_path)


def test_execution_mode_enum_contains_required_values() -> None:
    assert {mode.value for mode in ExecutionMode} == {
        "normal",
        "rebalancing",
        "emergency_trigger",
        "mdd_killswitch",
        "manual",
    }


def test_config_package_exports_only_phase_0_public_api() -> None:
    assert set(config.__all__) == {
        "AppSettings",
        "BrokerAccountRoleSettings",
        "BrokerAdapterName",
        "BrokerSettings",
        "ConfigEnvironmentError",
        "ConfigFileNotFoundError",
        "ExecutionMode",
        "KisLiveSettings",
        "KisReadOnlySettings",
        "RuntimeGateError",
        "SettingsError",
        "TradingMode",
        "TradingSettings",
        "load_settings",
    }


def test_forbidden_mock_adapter_and_environment_names_are_not_present() -> None:
    forbidden_adapter = "kis" + "_mock"
    forbidden_env = "KIS" + "_MOCK"
    settings_source = Path("src/config/settings.py").read_text(encoding="utf-8")

    assert forbidden_adapter not in settings_source
    assert forbidden_env not in settings_source


def test_config_toml_example_still_loads_as_paper() -> None:
    settings = load_settings("config/config.toml.example")

    assert settings.trading.mode == TradingMode.PAPER
    assert settings.broker.adapter == BrokerAdapterName.PAPER
    assert settings.broker.account_roles.kr_tax_advantaged_account_env == "KIS_ISA_ACCOUNT"


def test_runtime_paper_fast_loop_defaults(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path, ""))
    fl = settings.runtime.paper_fast_loop
    assert fl.enabled is False
    assert fl.market == "KR"
    assert fl.symbol == "000000"
    assert fl.snapshot_path == "runtime/paper_fast_loop/execution_inputs_snapshot.json"
    assert fl.active_decision_store_path == "runtime/paper_fast_loop/active_decision_store.sqlite3"
    assert fl.ledger_path == "runtime/paper_fast_loop/ledger.sqlite3"
    assert fl.trigger_journal_path == "runtime/paper_fast_loop/trigger_journal.sqlite3"


def test_runtime_paper_fast_loop_overrides(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
enabled = true
symbol = "005930"
ledger_path = "runtime/paper_fast_loop/custom_ledger.sqlite3"
""",
    )
    fl = load_settings(config_path).runtime.paper_fast_loop
    assert fl.enabled is True
    assert fl.symbol == "005930"
    assert fl.ledger_path == "runtime/paper_fast_loop/custom_ledger.sqlite3"


def test_runtime_paper_fast_loop_config_example_loads(tmp_path: Path) -> None:
    settings = load_settings("config/config.toml.example")
    fl = settings.runtime.paper_fast_loop
    assert fl.enabled is False
    assert fl.market == "KR"
    assert fl.symbol == "005930"


def test_runtime_paper_fast_loop_rejects_non_kr_market(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
market = "US"
""",
    )
    with pytest.raises(SettingsError, match="config.runtime.paper_fast_loop.market must be 'KR'"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_non_six_digit_symbol(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
symbol = "AAPL"
""",
    )
    with pytest.raises(SettingsError, match="must be a 6-digit KRX symbol"):
        load_settings(config_path)


@pytest.mark.parametrize(
    "symbol",
    ["１２３４５６", "١٢٣٤٥٦", "00593０"],
)
def test_runtime_paper_fast_loop_rejects_unicode_digit_symbol(tmp_path: Path, symbol: str) -> None:
    config_path = write_config(
        tmp_path,
        f"""
[runtime.paper_fast_loop]
symbol = "{symbol}"
""",
    )
    with pytest.raises(SettingsError, match="must be a 6-digit KRX symbol"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_absolute_path(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
ledger_path = "/etc/passwd"
""",
    )
    with pytest.raises(SettingsError, match="must be a relative path under 'runtime/'"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_path_outside_runtime(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
ledger_path = "data/ledger.sqlite3"
""",
    )
    with pytest.raises(SettingsError, match="must start with 'runtime/'"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_path_traversal(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
ledger_path = "runtime/../etc/ledger.sqlite3"
""",
    )
    with pytest.raises(SettingsError, match="must not contain '..' path traversal"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_duplicate_paths(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
ledger_path = "runtime/paper_fast_loop/shared.sqlite3"
trigger_journal_path = "runtime/paper_fast_loop/shared.sqlite3"
""",
    )
    with pytest.raises(SettingsError, match="collides with"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_symlink_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # runtime/paper_fast_loop 의 한 컴포넌트가 심볼릭링크면 fail-closed.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "real_target").mkdir()
    (tmp_path / "runtime" / "paper_fast_loop").symlink_to(tmp_path / "real_target")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[runtime.paper_fast_loop]
ledger_path = "runtime/paper_fast_loop/ledger.sqlite3"
""",
        encoding="utf-8",
    )
    with pytest.raises(SettingsError, match="must not traverse a symlink"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_rejects_unknown_key(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
[runtime.paper_fast_loop]
unexpected = "x"
""",
    )
    with pytest.raises(SettingsError, match="config.runtime.paper_fast_loop"):
        load_settings(config_path)


def test_runtime_paper_fast_loop_parse_creates_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[runtime.paper_fast_loop]\nenabled = true\n", encoding="utf-8")
    load_settings(config_path)
    assert not (tmp_path / "runtime").exists()
