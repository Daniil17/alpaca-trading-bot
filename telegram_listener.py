"""
Telegram Listener — runs as a persistent 24/7 process on Railway.
Handles all Telegram commands instantly via long-polling.
The trading cycle itself still runs on GitHub Actions every 5 minutes.

Deploy this file to Railway (free tier) to get instant Telegram responses.
"""

import os
import sys
import time
import logging

# Load secrets from environment variables (set in Railway dashboard)
os.environ.setdefault("ALPACA_API_KEY", os.environ.get("ALPACA_API_KEY", ""))
os.environ.setdefault("ALPACA_SECRET_KEY", os.environ.get("ALPACA_SECRET_KEY", ""))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
os.environ.setdefault("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", ""))
os.environ.setdefault("ALPACA_PAPER", os.environ.get("ALPACA_PAPER", "true"))

import config

# Inject secrets into config
config.ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", config.ALPACA_API_KEY)
config.ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", config.ALPACA_SECRET_KEY)
config.TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN)
config.TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", config.TELEGRAM_CHAT_ID)
config.PAPER_TRADING = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

from alpaca_api import AlpacaAPI
from telegram_commands import TelegramCommander, send_startup_menu

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("TelegramListener")


def main():
    logger.info("=" * 55)
    logger.info("  TELEGRAM LISTENER — starting up")
    logger.info(f"  Paper trading: {config.PAPER_TRADING}")
    logger.info("=" * 55)

    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "TELEGRAM_BOT_TOKEN":
        logger.error("TELEGRAM_BOT_TOKEN not set — set it in Railway environment variables")
        sys.exit(1)

    api = AlpacaAPI(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=config.PAPER_TRADING)
    commander = TelegramCommander(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, api)

    send_startup_menu(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    logger.info("Listening for Telegram commands...")

    consecutive_errors = 0
    while True:
        try:
            commander.process_updates()
            consecutive_errors = 0
            time.sleep(1)   # Poll every 1 second for near-instant responses
        except KeyboardInterrupt:
            logger.info("Listener stopped.")
            break
        except Exception as e:
            consecutive_errors += 1
            wait = min(30, consecutive_errors * 2)
            logger.error(f"Error (#{consecutive_errors}): {e} — retrying in {wait}s")
            time.sleep(wait)


if __name__ == "__main__":
    main()
