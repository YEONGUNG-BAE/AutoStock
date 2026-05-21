from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class StrictBaseModel(BaseModel):
    """Base model that rejects unknown config keys at every nesting level."""

    model_config = ConfigDict(extra="forbid")


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


class TradingSettings(StrictBaseModel):
    mode: TradingMode = TradingMode.PAPER
    allow_live_trading: bool = False
    live_confirmation_env_var: str = "LIVE_TRADING_CONFIRM"
    live_confirmation_phrase: str = "ENABLE_LIVE_TRADING"
    initial_paper_cash_krw: int = Field(default=30_000_000, gt=0)


class LLMSettings(StrictBaseModel):
    provider: str = "ollama"
    model: str = "qwen3.6:35b-a3b"
    temperature: float = 0.0

    @field_validator("temperature")
    @classmethod
    def temperature_must_be_zero(cls, value: float) -> float:
        if value != 0.0:
            raise ValueError("LLM temperature must be 0.0 for deterministic trading decisions.")
        return value


class RiskSettings(StrictBaseModel):
    cash_min_percent: int = 10
    cash_max_percent: int = 30
    invested_min_percent: int = 70
    paper_observation_invested_min_percent: int = 50
    invested_max_percent: int = 90
    asset_min_percent: int = 15
    asset_max_percent: int = 55
    gold_normal_min_percent: int = 18
    gold_normal_max_percent: int = 22
    gold_exception_min_percent: int = 15
    gold_exception_max_percent: int = 25
    single_stock_cumulative_buy_cost_cap_percent: int = 5

    @model_validator(mode="after")
    def validate_ranges(self) -> "RiskSettings":
        if self.cash_min_percent + self.invested_max_percent != 100:
            raise ValueError("cash_min_percent + invested_max_percent must equal 100.")
        if self.cash_max_percent + self.invested_min_percent != 100:
            raise ValueError("cash_max_percent + invested_min_percent must equal 100.")
        if not (self.gold_exception_min_percent <= self.gold_normal_min_percent <= self.gold_normal_max_percent <= self.gold_exception_max_percent):
            raise ValueError("Gold normal band must be inside the gold exception band.")
        if not (0 <= self.paper_observation_invested_min_percent <= self.invested_min_percent):
            raise ValueError("paper_observation_invested_min_percent must be between 0 and invested_min_percent.")
        return self


class AllocatorSettings(StrictBaseModel):
    analysis_tolerance_percent: int = Field(default=5, ge=0, le=20)


class SlippageSettings(StrictBaseModel):
    kr_tolerance_percent: float = Field(default=0.5, ge=0)
    us_tolerance_percent: float = Field(default=0.2, ge=0)
    emergency_bypass: bool = True


class MDDSettings(StrictBaseModel):
    level_1_drawdown_percent: int = -10
    level_1_target_cash_percent: int = 50
    level_2_drawdown_percent: int = -15
    level_2_target_cash_percent: int = 80
    level_3_drawdown_percent: int = -20
    level_3_target_cash_percent: int = 95
    stage_cooldown_hours: int = Field(default=4, ge=0)
    same_stage_max_triggers_per_day: int = Field(default=1, ge=1)
    halt_trading_after_level_3: bool = True

    @model_validator(mode="after")
    def validate_mdd_levels(self) -> "MDDSettings":
        drawdowns = [self.level_1_drawdown_percent, self.level_2_drawdown_percent, self.level_3_drawdown_percent]
        cash_targets = [self.level_1_target_cash_percent, self.level_2_target_cash_percent, self.level_3_target_cash_percent]
        if not (drawdowns[0] > drawdowns[1] > drawdowns[2]):
            raise ValueError("MDD drawdown levels must become more severe: -10 > -15 > -20.")
        if not (cash_targets[0] < cash_targets[1] < cash_targets[2]):
            raise ValueError("MDD target cash percentages must increase by stage.")
        return self


class GoldTradingSettings(StrictBaseModel):
    monthly_trade_soft_limit: int = Field(default=2, ge=0)
    quarterly_trade_soft_limit: int = Field(default=4, ge=0)
    micro_adjust_ignore_below_percent: int = Field(default=3, ge=0)
    increase_max_percent: int = Field(default=5, ge=0)
    decrease_max_percent: int = Field(default=2, ge=0)


class EmergencyTriggerSettings(StrictBaseModel):
    stock_drop_percent: float = -3.0
    index_crash_percent: float = -1.5
    portfolio_loss_percent: float = -2.0
    profit_run_levels_percent: list[int] = Field(default_factory=lambda: [10, 15, 20])

    @model_validator(mode="after")
    def validate_triggers(self) -> "EmergencyTriggerSettings":
        if self.stock_drop_percent >= 0 or self.index_crash_percent >= 0 or self.portfolio_loss_percent >= 0:
            raise ValueError("Drop/loss trigger thresholds must be negative percentages.")
        if sorted(self.profit_run_levels_percent) != self.profit_run_levels_percent:
            raise ValueError("profit_run_levels_percent must be sorted ascending.")
        return self


class DateIdStalenessSettings(StrictBaseModel):
    price_max_age_hours: int = Field(default=24, ge=1)
    flow_max_age_hours: int = Field(default=24, ge=1)
    fx_max_age_hours: int = Field(default=24, ge=1)
    news_max_age_hours: int = Field(default=168, ge=1)  # 7 days
    disclosure_max_age_hours: int = Field(default=2160, ge=1)  # 90 days
    macro_default_max_age_hours: int = Field(default=168, ge=1)


class MarketSettings(StrictBaseModel):
    exchange: str
    timezone: str
    analysis_slots_per_session: int | None = None


class PathSettings(StrictBaseModel):
    data_dir: Path = Path("memory")
    date_file: Path = Path("memory/Date.md")
    daily_summary_dir: Path = Path("memory/daily")
    debug_file: Path = Path("memory/debug/Debug.md")
    weekly_postmortem_dir: Path = Path("memory/postmortem/weekly")
    monthly_postmortem_dir: Path = Path("memory/postmortem/monthly")


class AccountRoleSettings(StrictBaseModel):
    use_isa_for_kr_and_gold: bool = True
    use_cma_for_order_execution: bool = False
    kr_tax_advantaged_account_env: str = "KIS_ISA_ACCOUNT"
    us_regular_account_env: str = "KIS_US_REGULAR_ACCOUNT"
    cash_buffer_account_env: str = "KIS_CMA_ACCOUNT"

    @model_validator(mode="after")
    def cma_must_not_execute_orders_by_default(self) -> "AccountRoleSettings":
        if self.use_cma_for_order_execution:
            raise ValueError("CMA must not be used for order execution in MVP.")
        return self


class PaperBrokerSettings(StrictBaseModel):
    name: str = "paper"
    fill_model: str = "last_price"
    kr_buy_slippage_bps: int = Field(default=10, ge=0)
    kr_sell_slippage_bps: int = Field(default=10, ge=0)
    us_buy_slippage_bps: int = Field(default=5, ge=0)
    us_sell_slippage_bps: int = Field(default=5, ge=0)


class KisBrokerSettings(StrictBaseModel):
    name: str
    endpoint: str
    account_id_env: str
    app_key_env: str
    app_secret_env: str


class BrokerSettings(StrictBaseModel):
    adapter: BrokerAdapterName = BrokerAdapterName.PAPER
    account_roles: AccountRoleSettings = Field(default_factory=AccountRoleSettings)
    paper: PaperBrokerSettings = Field(default_factory=PaperBrokerSettings)
    live: KisBrokerSettings = Field(
        default_factory=lambda: KisBrokerSettings(
            name="kis_live",
            endpoint="https://openapi.koreainvestment.com:9443",
            account_id_env="KIS_LIVE_ACCOUNT",
            app_key_env="KIS_LIVE_APP_KEY",
            app_secret_env="KIS_LIVE_APP_SECRET",
        )
    )


class DataApiSettings(StrictBaseModel):
    fred_api_key_env: str = "FRED_API_KEY"
    dart_api_key_env: str = "DART_API_KEY"
    finnhub_api_key_env: str = "FINNHUB_API_KEY"
    naver_client_id_env: str = "NAVER_CLIENT_ID"
    naver_client_secret_env: str = "NAVER_CLIENT_SECRET"


class AppConfig(StrictBaseModel):
    trading: TradingSettings = Field(default_factory=TradingSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    allocator: AllocatorSettings = Field(default_factory=AllocatorSettings)
    slippage: SlippageSettings = Field(default_factory=SlippageSettings)
    mdd: MDDSettings = Field(default_factory=MDDSettings)
    gold_trading: GoldTradingSettings = Field(default_factory=GoldTradingSettings)
    emergency_triggers: EmergencyTriggerSettings = Field(default_factory=EmergencyTriggerSettings)
    date_id_staleness: DateIdStalenessSettings = Field(default_factory=DateIdStalenessSettings)
    korea_market: MarketSettings
    us_market: MarketSettings
    paths: PathSettings = Field(default_factory=PathSettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)
    data_apis: DataApiSettings = Field(default_factory=DataApiSettings)

    def assert_runtime_safety(self) -> None:
        """Fail closed before any scheduler, broker, or order component starts."""
        if self.trading.mode == TradingMode.PAPER:
            if self.trading.allow_live_trading:
                raise RuntimeError("Paper mode cannot set allow_live_trading=true. Real-money orders are only valid in live mode with kis_live.")
            if self.broker.adapter == BrokerAdapterName.KIS_LIVE:
                raise RuntimeError("Paper mode cannot use the KIS live broker adapter.")
            return

        if self.broker.adapter != BrokerAdapterName.KIS_LIVE:
            raise RuntimeError("Live trading requires broker.adapter = 'kis_live'.")

        if not self.trading.allow_live_trading:
            raise RuntimeError("Live trading blocked: trading.allow_live_trading must be true.")

        actual_phrase = os.environ.get(self.trading.live_confirmation_env_var, "")
        if actual_phrase != self.trading.live_confirmation_phrase:
            raise RuntimeError(
                "Live trading blocked: missing environment confirmation "
                f"{self.trading.live_confirmation_env_var}={self.trading.live_confirmation_phrase}."
            )

        missing_envs = [
            env_name
            for env_name in (
                self.broker.live.account_id_env,
                self.broker.live.app_key_env,
                self.broker.live.app_secret_env,
            )
            if not os.environ.get(env_name)
        ]
        if missing_envs:
            raise RuntimeError(f"Live trading blocked: missing broker environment variables: {missing_envs}")


AppConfig.model_rebuild()

def load_config(path: str | Path = "config/config.toml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}. Copy config.toml.example first.")

    with config_path.open("rb") as file:
        raw_config: dict[str, Any] = tomllib.load(file)

    try:
        config = AppConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise RuntimeError(f"Invalid config: {exc}") from exc

    config.assert_runtime_safety()
    return config
