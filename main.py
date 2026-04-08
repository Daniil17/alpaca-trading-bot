"""
main.py — Persistent Railway Runner
=====================================
Runs the trading bot as a long-lived process on Railway instead of
relying on GitHub Actions cron for each cycle.

Benefits over GitHub Actions:
  - No cold start per cycle (torch / FinBERT stay loaded in memory)
  - Each trading cycle takes ~10-20s instead of 2-3 minutes
  - Telegram listener and trading loop run in the same process

Schedule:
  - Stock market hours  (Mon–Fri 13:30–20:05 UTC): every 5 minutes
  - Off-hours / weekends (crypto only):             every 15 minutes

GitHub Actions remains as a fallback — it still calls run_once.py
directly and is unaffected by this file.

Environment variables (set in Railway dashboard):
  ALPACA_API_KEY     — Alpaca API key
  ALPACA_SECRET_KEY  — Alpaca secret key
  ALPACA_PAPER       — "true" for paper trading, "false" for live
  TELEGRAM_BOT_TOKEN — Telegram bot token
  TELEGRAM_CHAT_ID   — Telegram chat ID
"""

import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone

# ── Inject Railway environment variables into config ─────────────────
# Must happen before any other local import that touches config.
import config

config.ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",     config.ALPACA_API_KEY)
config.ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY",  config.ALPACA_SECRET_KEY)
config.TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN)
config.TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   config.TELEGRAM_CHAT_ID)
config.PAPER_TRADING      = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

# ── Logging (set up once here; run_once.setup_logging is idempotent) ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("TradingBot")

# ── Deferred imports (keep startup fast for Telegram thread) ─────────
from alpaca_api import AlpacaAPI
from telegram_commands import TelegramCommander, send_startup_menu


# ═════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════

def is_stock_market_hours() -> bool:
    """
    Return True when NYSE is open (Mon–Fri, 13:30–20:05 UTC).
    Used to decide cycle interval: 5 min vs 15 min.
    """
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:          # Saturday = 5, Sunday = 6
        return False
    open_  = now.replace(hour=13, minute=30, second=0, microsecond=0)
    close_ = now.replace(hour=20, minute= 5, second=0, microsecond=0)
    return open_ <= now <= close_


def cycle_sleep_seconds() -> int:
    """5 minutes during market hours, 15 minutes otherwise."""
    return 300 if is_stock_market_hours() else 900


# ═════════════════════════════════════════════════════════════════════
#  TELEGRAM LISTENER THREAD
# ═════════════════════════════════════════════════════════════════════

def _telegram_thread() -> None:
    """
    Background daemon thread.
    Long-polls Telegram every second for instant command responses.
    Errors are logged and retried with exponential back-off (max 30 s).
    """
    tg_log = logging.getLogger("TelegramListener")

    if not config.TELEGRAM_BOT_TOKEN or config.TELEGRAM_BOT_TOKEN == "TELEGRAM_BOT_TOKEN":
        tg_log.warning("TELEGRAM_BOT_TOKEN not configured — listener disabled")
        return

    api       = AlpacaAPI(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                          paper=config.PAPER_TRADING)
    commander = TelegramCommander(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, api)

    try:
        send_startup_menu(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    except Exception as e:
        tg_log.warning(f"Could not send startup menu: {e}")

    tg_log.info("Telegram listener running (polling every 1 s)")
    consecutive_errors = 0

    while True:
        try:
            commander.process_updates()
            consecutive_errors = 0
            time.sleep(1)
        except KeyboardInterrupt:
            break
        except Exception as e:
            consecutive_errors += 1
            wait = min(30, consecutive_errors * 2)
            tg_log.error(f"Error #{consecutive_errors}: {e} — retry in {wait}s")
            time.sleep(wait)


# ═════════════════════════════════════════════════════════════════════
#  TRADING LOOP
# ═════════════════════════════════════════════════════════════════════

def _trading_loop() -> None:
    """
    Main thread: calls run_once.run() on a market-aware schedule.
    Catches all exceptions so a single bad cycle never kills the process.
    """
    import run_once  # deferred — keeps import time short for Telegram thread

    cycle = 0
    logger.info("Trading loop started — first cycle begins now")

    while True:
        cycle += 1
        start = time.monotonic()
        logger.info(f"{'─' * 50}")
        logger.info(f"Cycle {cycle} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

        try:
            run_once.run()
        except SystemExit as e:
            # run_once calls sys.exit(1) on credential errors — log but keep running
            logger.error(f"Cycle {cycle}: run_once raised SystemExit({e.code}) "
                         f"— check API credentials in Railway environment variables")
        except Exception as e:
            logger.exception(f"Cycle {cycle} unhandled error: {e}")

        elapsed = time.monotonic() - start
        sleep   = max(0, cycle_sleep_seconds() - elapsed)
        label   = "market hours" if is_stock_market_hours() else "off-hours / crypto only"
        logger.info(f"Cycle {cycle} done in {elapsed:.1f}s — "
                    f"next cycle in {sleep / 60:.1f} min ({label})")
        time.sleep(sleep)


# ═════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("=" * 55)
    logger.info("  ALPACA TRADING BOT — Railway Persistent Service")
    logger.info(f"  Mode   : {'PAPER' if config.PAPER_TRADING else 'LIVE'}")
    logger.info(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("=" * 55)

    # Start Telegram listener as a background daemon thread
    t = threading.Thread(target=_telegram_thread, daemon=True, name="TelegramListener")
    t.start()

    # Run trading loop in the main thread (blocks forever)
    _trading_loop()


if __name__ == "__main__":
    main()
