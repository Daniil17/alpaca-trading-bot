"""
SINGLE-CYCLE RUNNER
=====================
Called by GitHub Actions on every scheduled trigger.
Runs exactly one trading cycle, saves state, then exits.

Environment variables (set as GitHub Secrets):
  ALPACA_API_KEY       — Alpaca API key
  ALPACA_SECRET_KEY    — Alpaca secret key
  ALPACA_PAPER         — "true" for paper, "false" for live
  TELEGRAM_BOT_TOKEN   — Telegram bot token
  TELEGRAM_CHAT_ID     — Telegram chat ID
"""

import os
import sys
import logging
from datetime import date, datetime

# ============================================================
# INJECT ENVIRONMENT VARIABLES INTO CONFIG
# ============================================================
# This reads secrets from GitHub Actions environment, so the
# real config.py never contains actual credentials.

import config

# Override config with environment variables if present
if os.environ.get("ALPACA_API_KEY"):
    config.ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
if os.environ.get("ALPACA_SECRET_KEY"):
    config.ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
if os.environ.get("ALPACA_PAPER"):
    config.PAPER_TRADING = os.environ["ALPACA_PAPER"].lower() == "true"
if os.environ.get("TELEGRAM_BOT_TOKEN"):
    config.TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
if os.environ.get("TELEGRAM_CHAT_ID"):
    config.TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ============================================================
# IMPORTS (after config patching)
# ============================================================

from alpaca_api import AlpacaAPI
from strategies import StrategyEngine
from risk_manager import RiskManager
from news_scanner import NewsScanner
from telegram_bot import TelegramNotifier
from telegram_commands import TelegramCommander, send_startup_menu
from state import load_state, save_state


# ============================================================
# LOGGING
# ============================================================

def setup_logging():
    log = logging.getLogger("TradingBot")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    if config.ENABLE_LOGGING:
        fh = logging.FileHandler(config.LOG_FILE)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


# ============================================================
# MAIN SINGLE-CYCLE RUN
# ============================================================

def run():
    logger = setup_logging()

    # Validate credentials
    if config.ALPACA_API_KEY == "YOUR_API_KEY_HERE":
        logger.error("ALPACA_API_KEY not set — check GitHub Secrets")
        sys.exit(1)

    logger.info("=" * 55)
    logger.info(f"  TRADING BOT — Single Cycle")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info(f"  Mode: {'PAPER' if config.PAPER_TRADING else 'LIVE'}")
    logger.info("=" * 55)

    # --- Load persisted state ---
    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1

    # --- Initialize components ---
    api = AlpacaAPI(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY,
                    paper=config.PAPER_TRADING)

    engine = StrategyEngine(
        weights=config.STRATEGY_WEIGHTS,
        rsi_period=config.RSI_PERIOD,
        rsi_oversold=config.RSI_OVERSOLD,
        rsi_overbought=config.RSI_OVERBOUGHT,
        bb_period=config.BOLLINGER_PERIOD,
        bb_std=config.BOLLINGER_STD_DEV,
        ema_fast=config.EMA_FAST,
        ema_slow=config.EMA_SLOW,
        adx_period=config.ADX_PERIOD,
        adx_threshold=config.ADX_TREND_THRESHOLD,
        news_pullback_rsi_low=config.NEWS_PULLBACK_RSI_LOW,
        news_pullback_rsi_high=config.NEWS_PULLBACK_RSI_HIGH,
        news_rsi_spike_confirm=config.NEWS_RSI_SPIKE_CONFIRM,
        vwap_buy_threshold=config.VWAP_BUY_THRESHOLD,
        vwap_sell_threshold=config.VWAP_SELL_THRESHOLD,
    )

    risk = RiskManager(config)
    # Restore peak value from state
    risk.peak_portfolio_value = state.get("peak_portfolio_value", 0.0)

    news = NewsScanner()
    telegram = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    # --- Process Telegram commands (respond to button presses / messages) ---
    commander = TelegramCommander(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, api)

    # On first run ever, register bot commands in Telegram menu
    if state.get("run_count", 0) <= 1:
        send_startup_menu(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        logger.info("Telegram bot menu registered")

    # Process any pending user commands (button presses, /status, etc.)
    commander.process_updates()
    logger.info("Processed Telegram commands")

    # Restore manual symbols from state
    # On first run, record current positions as "manual"
    manual_symbols = set(state.get("manual_symbols", []))
    if not manual_symbols:
        manual_symbols = api.get_position_symbols()
        logger.info(f"First run — recording manual positions: {manual_symbols or 'none'}")

    # --- Check market hours ---
    if config.RESPECT_MARKET_HOURS and not api.is_market_open():
        logger.info("Market closed — skipping trading cycle")
        state["manual_symbols"] = list(manual_symbols)
        save_state(state)
        return

    # --- Account status ---
    account = api.get_account()
    if not account:
        logger.error("Cannot reach Alpaca API")
        save_state(state)
        sys.exit(1)

    portfolio_value = account["portfolio_value"]
    logger.info(f"Portfolio: ${portfolio_value:,.2f} | Cash: ${account['cash']:,.2f}")

    # Update peak value
    if portfolio_value > risk.peak_portfolio_value:
        risk.peak_portfolio_value = portfolio_value

    if account.get("trading_blocked"):
        logger.error("Trading blocked on this account!")
        save_state(state)
        return

    # --- Get positions ---
    all_positions = api.get_all_positions()
    bot_positions = [p for p in all_positions if p["symbol"] not in manual_symbols]

    # ============================================================
    # PHASE 1: EXITS
    # ============================================================
    logger.info("--- Phase 1: Checking exits ---")

    exits = risk.check_positions_for_exit(bot_positions)
    for pos in exits:
        symbol = pos["symbol"]
        reason = pos.get("exit_reason", "Risk exit")
        unrealized_pl = float(pos.get("unrealized_pl", 0))
        logger.info(f"EXIT: {symbol} — {reason}")
        result = api.close_position(symbol)
        if result:
            if config.NOTIFY_ON_STOP_LOSS and "STOP" in reason:
                pct = float(pos.get("unrealized_plpc", 0)) * 100
                telegram.notify_stop_loss(symbol, pct, unrealized_pl)
            elif config.NOTIFY_ON_SELL:
                telegram.notify_sell(symbol, reason, unrealized_pl)

    # Strategy-based sell signals
    for pos in bot_positions:
        symbol = pos["symbol"]
        if any(e["symbol"] == symbol for e in exits):
            continue
        bars = api.get_bars(symbol, "1Day", 100)
        if bars is None or bars.empty:
            continue
        analysis = engine.analyze(bars)
        if analysis["signal"] in ("STRONG_SELL", "SELL"):
            logger.info(f"STRATEGY SELL: {symbol} (score: {analysis['combined_score']:.3f})")
            unrealized_pl = float(pos.get("unrealized_pl", 0))
            result = api.close_position(symbol)
            if result and config.NOTIFY_ON_SELL:
                telegram.notify_sell(symbol, f"Strategy ({analysis['combined_score']:.3f})", unrealized_pl)

    # ============================================================
    # PHASE 2: FIND BUYS
    # ============================================================
    logger.info("--- Phase 2: Scanning for opportunities ---")

    all_positions = api.get_all_positions()
    bot_positions = [p for p in all_positions if p["symbol"] not in manual_symbols]
    held_symbols = {p["symbol"] for p in all_positions}

    news_scores = news.get_sentiment_scores(config.STOCK_UNIVERSE, exclude_symbols=held_symbols)

    candidates = []
    for symbol in config.STOCK_UNIVERSE:
        if symbol in held_symbols:
            continue
        bars = api.get_bars(symbol, "1Day", 100)
        if bars is None or bars.empty:
            continue
        sentiment = news_scores.get(symbol, 0.0)
        analysis = engine.analyze(bars, sentiment_score=sentiment)
        if analysis["signal"] in ("STRONG_BUY", "BUY"):
            candidates.append({"symbol": symbol, "analysis": analysis, "bars": bars})

    candidates.sort(key=lambda c: c["analysis"]["combined_score"], reverse=True)

    if candidates:
        logger.info("Buy candidates: " + ", ".join(
            f"{c['symbol']} ({c['analysis']['combined_score']:.3f})"
            for c in candidates[:5]
        ))

    # ============================================================
    # PHASE 3: EXECUTE BUYS
    # ============================================================
    logger.info("--- Phase 3: Executing trades ---")

    for candidate in candidates:
        symbol = candidate["symbol"]
        analysis = candidate["analysis"]
        bars = candidate["bars"]

        allowed, reason, position_size = risk.can_open_position(
            symbol, portfolio_value, bot_positions, bars
        )

        if not allowed:
            logger.info(f"BLOCKED: {symbol} — {reason}")
            if "DRAWDOWN" in reason:
                telegram.notify_drawdown_breaker(
                    risk.peak_portfolio_value - portfolio_value,
                    risk.peak_portfolio_value, portfolio_value,
                )
            continue

        if position_size < 1:
            continue

        price = api.get_latest_price(symbol)
        if not price:
            continue

        logger.info(f"BUY: {symbol} | ${position_size:.2f} @ ~${price} | "
                    f"Score: {analysis['combined_score']:.3f}")

        result = api.buy_market(symbol, notional=position_size)
        if result:
            if config.NOTIFY_ON_BUY:
                telegram.notify_buy(symbol, position_size, price, analysis)
            bot_positions.append({
                "symbol": symbol, "market_value": position_size,
                "avg_entry_price": price, "current_price": price,
                "unrealized_pl": 0, "unrealized_plpc": 0,
            })

    # ============================================================
    # DAILY SUMMARY
    # ============================================================
    today = str(date.today())
    last_summary = state.get("last_summary_date")
    now_hour = datetime.now().hour

    if config.NOTIFY_DAILY_SUMMARY and last_summary != today and now_hour >= 16:
        summary = risk.get_portfolio_summary(portfolio_value, bot_positions)
        telegram.notify_daily_summary(account, bot_positions, summary)
        state["last_summary_date"] = today
        logger.info("Daily summary sent")

    # ============================================================
    # SAVE STATE FOR NEXT RUN
    # ============================================================
    state["peak_portfolio_value"] = risk.peak_portfolio_value
    state["manual_symbols"] = list(manual_symbols)
    save_state(state)

    logger.info("Cycle complete.")


if __name__ == "__main__":
    run()
