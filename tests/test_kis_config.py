from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from config.settings import (
    BrokerAccountRoleSettings,
    BrokerAdapterName,
    KisReadOnlySettings,
    SettingsError,
    TradingMode,
    load_settings,
)


def test_config_toml_example_loads_as_paper() -> None:
    settings = load_settings("config/config.toml.example")

    assert settings.trading.mode == TradingMode.PAPER
    assert settings.broker.adapter == BrokerAdapterName.PAPER
    assert settings.broker.kis_read_only.enabled is False


def test_broker_account_role_env_names_parse_correctly() -> None:
    settings = load_settings("config/config.toml.example")

    roles = settings.broker.account_roles
    assert roles.kr_tax_advantaged_account_env == "KIS_ISA_ACCOUNT"
    assert roles.us_regular_account_env == "KIS_US_REGULAR_ACCOUNT"
    assert roles.cash_buffer_account_env == "KIS_CMA_ACCOUNT"
    assert roles.use_isa_for_kr_and_gold is True
    assert roles.use_cma_for_order_execution is False


def test_kis_read_only_settings_parse_correctly() -> None:
    settings = load_settings("config/config.toml.example")

    assert settings.broker.kis_read_only.enabled is False
    assert settings.broker.kis_read_only.timeout_seconds == 10.0


def test_unknown_broker_keys_still_reject(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[broker]
adapter = "paper"
unexpected_broker_key = true
""",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="Unknown settings keys.*config.broker"):
        load_settings(config_path)


def test_unknown_account_roles_keys_still_reject(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[broker.account_roles]
kr_tax_advantaged_account_env = "KIS_ISA_ACCOUNT"
legacy_isa_flag = true
""",
        encoding="utf-8",
    )

    with pytest.raises(SettingsError, match="Unknown settings keys.*config.broker.account_roles"):
        load_settings(config_path)


def test_settings_dataclasses_not_duplicated() -> None:
    """Phase 14는 기존 BrokerSettings/KisLiveSettings를 확장하고 중복 dataclass를 만들지 않는다."""

    from config import settings as settings_module

    assert hasattr(settings_module, "BrokerAccountRoleSettings")
    assert hasattr(settings_module, "KisReadOnlySettings")
    assert hasattr(settings_module, "KisLiveSettings")
    assert hasattr(settings_module, "BrokerSettings")

    roles = BrokerAccountRoleSettings()
    assert isinstance(roles, BrokerAccountRoleSettings)
    assert isinstance(KisReadOnlySettings(), KisReadOnlySettings)
