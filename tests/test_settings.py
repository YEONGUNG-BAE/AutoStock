"""Phase 0 startup-gate tests for `src.config.settings`.

Test isolation contract:

- An `autouse` fixture deletes every env var the loader can touch, so the
  host shell never bleeds into the test (no real KIS keys are read).
- Tests that need credentials use `DUMMY_*` / `TEST_*` placeholder values
  only. No real API keys, secrets, or account numbers appear in this file.
- We additionally pass `env=...` explicitly to `load_settings` in most
  tests to make the contract obvious and resilient to fixture changes.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.config.settings import (
    BrokerAdapterName,
    ConfigError,
    ExecutionMode,
    TradingMode,
    load_settings,
)


_TOUCHED_ENV_VARS: tuple[str, ...] = (
    "LIVE_TRADING_CONFIRM",
    "KIS_MOCK_APP_KEY",
    "KIS_MOCK_APP_SECRET",
    "KIS_MOCK_ACCOUNT",
    "KIS_LIVE_APP_KEY",
    "KIS_LIVE_APP_SECRET",
    "KIS_LIVE_ACCOUNT",
    "AUTOSTOCK_TEST_VAR",
)


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var the loader could read before each test."""
    for name in _TOUCHED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_config(tmp_path: Path, body: str) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return config_path


def _mock_creds_env() -> dict[str, str]:
    return {
        "KIS_MOCK_APP_KEY": "DUMMY_MOCK_APP_KEY",
        "KIS_MOCK_APP_SECRET": "DUMMY_MOCK_APP_SECRET",
        "KIS_MOCK_ACCOUNT": "DUMMY_MOCK_ACCOUNT",
    }


def _live_creds_env() -> dict[str, str]:
    return {
        "KIS_LIVE_APP_KEY": "DUMMY_LIVE_APP_KEY",
        "KIS_LIVE_APP_SECRET": "DUMMY_LIVE_APP_SECRET",
        "KIS_LIVE_ACCOUNT": "DUMMY_LIVE_ACCOUNT",
    }


def test_minimal_config_defaults_to_paper(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, "")

    settings = load_settings(config_path, env={})

    assert settings.trading.mode is TradingMode.PAPER
    assert settings.broker.adapter is BrokerAdapterName.PAPER
    assert settings.trading.allow_live_trading is False


def test_missing_config_file_raises_explicit_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "does-not-exist.toml"

    with pytest.raises(ConfigError, match="Config file not found"):
        load_settings(missing_path, env={})


def test_paper_with_paper_adapter_needs_no_secrets(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"

        [broker]
        adapter = "paper"
        """,
    )

    settings = load_settings(config_path, env={})

    assert settings.broker.adapter is BrokerAdapterName.PAPER


def test_paper_with_kis_mock_passes_when_creds_present(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"

        [broker]
        adapter = "kis_mock"
        """,
    )

    settings = load_settings(config_path, env=_mock_creds_env())

    assert settings.broker.adapter is BrokerAdapterName.KIS_MOCK


def test_paper_with_kis_mock_fails_without_creds(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"

        [broker]
        adapter = "kis_mock"
        """,
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings(config_path, env={})

    message = str(exc_info.value)
    assert "kis_mock" in message
    assert "KIS_MOCK_APP_KEY" in message


def test_paper_with_kis_live_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"

        [broker]
        adapter = "kis_live"
        """,
    )

    fully_loaded_env = {
        "LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING",
        **_live_creds_env(),
    }
    with pytest.raises(ConfigError, match="paper.*kis_live"):
        load_settings(config_path, env=fully_loaded_env)


def test_live_with_paper_adapter_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "live"
        allow_live_trading = true

        [broker]
        adapter = "paper"
        """,
    )

    with pytest.raises(ConfigError, match="kis_live"):
        load_settings(
            config_path,
            env={"LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING", **_live_creds_env()},
        )


def test_live_with_kis_mock_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "live"
        allow_live_trading = true

        [broker]
        adapter = "kis_mock"
        """,
    )

    with pytest.raises(ConfigError, match="kis_live"):
        load_settings(
            config_path,
            env={
                "LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING",
                **_mock_creds_env(),
                **_live_creds_env(),
            },
        )


def test_live_kis_live_without_allow_flag_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "live"
        allow_live_trading = false

        [broker]
        adapter = "kis_live"
        """,
    )

    with pytest.raises(ConfigError, match="allow_live_trading"):
        load_settings(
            config_path,
            env={"LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING", **_live_creds_env()},
        )


def test_live_kis_live_missing_credentials_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "live"
        allow_live_trading = true

        [broker]
        adapter = "kis_live"
        """,
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings(
            config_path,
            env={"LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING"},
        )

    message = str(exc_info.value)
    assert "KIS_LIVE_APP_KEY" in message
    assert "KIS_LIVE_APP_SECRET" in message
    assert "KIS_LIVE_ACCOUNT" in message


def test_live_kis_live_wrong_confirmation_phrase_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "live"
        allow_live_trading = true

        [broker]
        adapter = "kis_live"
        """,
    )

    with pytest.raises(ConfigError, match="LIVE_TRADING_CONFIRM"):
        load_settings(
            config_path,
            env={"LIVE_TRADING_CONFIRM": "WRONG_PHRASE", **_live_creds_env()},
        )


def test_live_kis_live_missing_confirmation_env_fails(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "live"
        allow_live_trading = true

        [broker]
        adapter = "kis_live"
        """,
    )

    with pytest.raises(ConfigError, match="LIVE_TRADING_CONFIRM"):
        load_settings(config_path, env=_live_creds_env())


def test_live_kis_live_passes_when_every_gate_satisfied(tmp_path: Path) -> None:
    config_path = _write_config(
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
        env={"LIVE_TRADING_CONFIRM": "ENABLE_LIVE_TRADING", **_live_creds_env()},
    )

    assert settings.trading.mode is TradingMode.LIVE
    assert settings.broker.adapter is BrokerAdapterName.KIS_LIVE
    assert settings.trading.allow_live_trading is True


def test_env_placeholder_substitution_resolves(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"
        live_confirmation_phrase = "${AUTOSTOCK_TEST_VAR}"

        [broker]
        adapter = "paper"
        """,
    )

    settings = load_settings(
        config_path,
        env={"AUTOSTOCK_TEST_VAR": "TEST_VALUE_42"},
    )

    assert settings.trading.live_confirmation_phrase == "TEST_VALUE_42"


def test_env_placeholder_missing_variable_fails_with_location(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"
        live_confirmation_phrase = "${AUTOSTOCK_TEST_VAR}"
        """,
    )

    with pytest.raises(ConfigError) as exc_info:
        load_settings(config_path, env={})

    message = str(exc_info.value)
    assert "AUTOSTOCK_TEST_VAR" in message
    assert "trading.live_confirmation_phrase" in message


def test_uses_os_environ_when_env_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_settings` without explicit env falls back to `os.environ`.

    This proves the production code path works with real `os.environ`
    while still being driven by `monkeypatch.setenv` in tests.
    """

    config_path = _write_config(
        tmp_path,
        """
        [trading]
        mode = "paper"

        [broker]
        adapter = "kis_mock"
        """,
    )
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "DUMMY_MOCK_APP_KEY")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "DUMMY_MOCK_APP_SECRET")
    monkeypatch.setenv("KIS_MOCK_ACCOUNT", "DUMMY_MOCK_ACCOUNT")

    settings = load_settings(config_path)

    assert settings.broker.adapter is BrokerAdapterName.KIS_MOCK


def test_execution_mode_enum_exposes_all_phase0_values() -> None:
    """Phase 0 owns the canonical `ExecutionMode` enum for later phases."""
    assert {mode.value for mode in ExecutionMode} == {
        "normal",
        "rebalancing",
        "emergency_trigger",
        "mdd_killswitch",
        "manual",
    }
