from __future__ import annotations

from .config import TradingMode, load_config


def main() -> None:
    config = load_config("config/config.toml")

    # Keep main thin. Build services through dependency injection.
    if config.trading.mode == TradingMode.PAPER:
        print("Starting in PAPER trading mode.")
        # scheduler = build_paper_scheduler(config)
    else:
        print("Starting in LIVE trading mode.")
        # scheduler = build_live_scheduler(config)

    # scheduler.run()


if __name__ == "__main__":
    main()
