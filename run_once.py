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
from state import load_state, save_state, log_trade
from crypto_strategies import CryptoStrategyEngine
from pairs_trading import PairsTradingEngine


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

    # --- Check market hours ---
    market_open = api.is_market_open()
    run_stock_cycle = not config.RESPECT_MARKET_HOURS or market_open

    if not run_stock_cycle:
        logger.info("Market closed — skipping stock/pairs cycle (crypto will still run)")

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
    bot_positions = all_positions

    if run_stock_cycle:
        # ============================================================
        # PHASE 1: EXITS
        # ============================================================
        logger.info("--- Phase 1: Checking exits ---")

        exits = risk.check_positions_for_exit(bot_positions)
        for pos in exits:
            symbol = pos["symbol"]
            reason = pos.get("exit_reason", "Risk exit")
            unrealized_pl = float(pos.get("unrealized_pl", 0))
            current_price = float(pos.get("current_price", 0))
            logger.info(f"EXIT: {symbol} — {reason}")
            result = api.close_position(symbol)
            if result:
                log_trade(state, "SELL", symbol, float(pos.get("market_value", 0)),
                          current_price, pnl=unrealized_pl, reason=reason)
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
                current_price = float(pos.get("current_price", 0))
                result = api.close_position(symbol)
                if result:
                    log_trade(state, "SELL", symbol, float(pos.get("market_value", 0)),
                              current_price, pnl=unrealized_pl,
                              reason=f"Strategy ({analysis['combined_score']:.3f})",
                              score=analysis["combined_score"])
                    if config.NOTIFY_ON_SELL:
                        telegram.notify_sell(symbol, f"Strategy ({analysis['combined_score']:.3f})", unrealized_pl)

        # ============================================================
        # PHASE 2: FIND BUYS
        # ============================================================
        logger.info("--- Phase 2: Scanning for opportunities ---")

        all_positions = api.get_all_positions()
        bot_positions = all_positions
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
                log_trade(state, "BUY", symbol, position_size, price,
                          score=analysis["combined_score"])
                if config.NOTIFY_ON_BUY:
                    telegram.notify_buy(symbol, position_size, price, analysis)
                bot_positions.append({
                    "symbol": symbol, "market_value": position_size,
                    "avg_entry_price": price, "current_price": price,
                    "unrealized_pl": 0, "unrealized_plpc": 0,
                })

    # ============================================================
    # CRYPTO CYCLE
    # ============================================================
    if getattr(config, "ENABLE_CRYPTO", False):
        logger.info("\n" + "=" * 55)
        logger.info("  CRYPTO TRADING CYCLE")
        logger.info("=" * 55)
        _run_crypto_cycle(api, risk, news, telegram, state, portfolio_value, logger)

    # ============================================================
    # PAIRS TRADING CYCLE (Statistical Arbitrage) — stocks only, needs market hours
    # ============================================================
    if run_stock_cycle and getattr(config, "ENABLE_PAIRS_TRADING", False):
        logger.info("\n" + "=" * 55)
        logger.info("  PAIRS TRADING CYCLE (Statistical Arbitrage)")
        logger.info("=" * 55)
        _run_pairs_cycle(api, telegram, state, portfolio_value, logger)

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
    save_state(state)

    logger.info("Cycle complete.")


def _run_pairs_cycle(api, telegram, state, portfolio_value, logger):
    """
    Statistical arbitrage cycle using cointegration-based pairs trading.

    Strategy: long the undervalued leg, short the overvalued leg when the
    Z-score spread between two cointegrated stocks exceeds 2 standard deviations.
    Market-neutral — profits regardless of broad market direction.

    Requires: margin account with >$2,000 equity for short selling.
    """
    pairs_engine = PairsTradingEngine(
        pairs=config.PAIRS_UNIVERSE,
        zscore_entry=config.PAIRS_ZSCORE_ENTRY,
        zscore_exit=config.PAIRS_ZSCORE_EXIT,
        lookback=config.PAIRS_LOOKBACK,
        max_pair_allocation=config.PAIRS_MAX_ALLOCATION,
    )

    max_open_pairs = getattr(config, "PAIRS_MAX_OPEN", 3)

    # Track open pair trades in state
    open_pairs = state.get("open_pairs", {})   # {pair_key: {signal, symbols, ...}}

    # ---- Phase 1: Check exits on existing pair trades ----
    logger.info("--- Pairs Phase 1: Checking exits ---")
    pairs_to_close = []

    for pair_key, trade in list(open_pairs.items()):
        symbol_x = trade.get("symbol_x")
        symbol_y = trade.get("symbol_y")
        if not symbol_x or not symbol_y:
            continue

        bars_x = api.get_bars(symbol_x, "1Day", 120)
        bars_y = api.get_bars(symbol_y, "1Day", 120)
        if bars_x is None or bars_y is None:
            continue

        result = pairs_engine.analyser.analyse(bars_x, bars_y, symbol_x, symbol_y)

        if result["signal"] == "EXIT" or not result["cointegrated"]:
            logger.info(f"PAIRS EXIT: {symbol_x}/{symbol_y} — {result['reason']}")
            # Close both legs
            api.close_position(symbol_x)
            api.close_position(symbol_y)
            log_trade(state, "SELL", f"{symbol_x}↔{symbol_y}", 0, 0,
                      reason=f"Pair exit: {result['reason']}")
            if config.NOTIFY_ON_SELL:
                telegram.notify_sell(
                    f"📊 PAIR {symbol_x}/{symbol_y}",
                    result["reason"], None
                )
            pairs_to_close.append(pair_key)

    for k in pairs_to_close:
        open_pairs.pop(k, None)

    # ---- Phase 2: Scan for new pair opportunities ----
    logger.info("--- Pairs Phase 2: Scanning for pair signals ---")

    if len(open_pairs) >= max_open_pairs:
        logger.info(f"Pairs: max open pairs reached ({max_open_pairs})")
        state["open_pairs"] = open_pairs
        return

    signals = pairs_engine.scan_all_pairs(api, logger_ref=logger)

    for sig in signals:
        if len(open_pairs) >= max_open_pairs:
            break

        symbol_x = sig["symbol_x"]
        symbol_y = sig["symbol_y"]
        pair_key = f"{symbol_x}_{symbol_y}"
        trade_signal = sig["signal"]
        hedge_ratio = sig["hedge_ratio"]

        # Skip if this pair is already open
        if pair_key in open_pairs:
            continue

        # Determine which symbol to long and which to short
        if trade_signal == "LONG_X_SHORT_Y":
            long_sym, short_sym = symbol_x, symbol_y
        elif trade_signal == "LONG_Y_SHORT_X":
            long_sym, short_sym = symbol_y, symbol_x
        else:
            continue

        # Get prices
        long_price = api.get_latest_price(long_sym)
        short_price = api.get_latest_price(short_sym)
        if not long_price or not short_price:
            continue

        long_notional, short_notional = pairs_engine.calculate_leg_sizes(
            portfolio_value, long_price, short_price, hedge_ratio
        )

        if long_notional < 10 or short_notional < 10:
            logger.info(f"PAIRS SKIP: {pair_key} — position too small")
            continue

        logger.info(
            f"PAIRS TRADE: long {long_sym} (${long_notional:.2f}) | "
            f"short {short_sym} (${short_notional:.2f}) | "
            f"Z={sig['zscore']:.2f} | hedge_ratio={hedge_ratio:.3f}"
        )

        # Execute both legs
        long_result = api.buy_market(long_sym, notional=long_notional)
        short_result = api.short_sell(short_sym, notional=short_notional)

        if long_result and short_result:
            open_pairs[pair_key] = {
                "symbol_x": symbol_x,
                "symbol_y": symbol_y,
                "long_sym": long_sym,
                "short_sym": short_sym,
                "signal": trade_signal,
                "hedge_ratio": hedge_ratio,
                "entry_zscore": sig["zscore"],
            }
            log_trade(state, "BUY", f"{long_sym}↔{short_sym}",
                      long_notional + short_notional,
                      (long_price + short_price) / 2,
                      score=sig["zscore"])
            if config.NOTIFY_ON_BUY:
                telegram.send(
                    f"<b>📊 PAIRS TRADE OPENED</b>\n"
                    f"Long: {long_sym} (${long_notional:.2f})\n"
                    f"Short: {short_sym} (${short_notional:.2f})\n"
                    f"Z-score: {sig['zscore']:.2f}\n"
                    f"Hedge ratio: {hedge_ratio:.3f}\n"
                    f"Reason: {sig['reason']}"
                )
        else:
            logger.warning(f"PAIRS: one leg failed for {pair_key} — closing both")
            if long_result:
                api.close_position(long_sym)
            if short_result:
                api.close_position(short_sym)

    state["open_pairs"] = open_pairs
    logger.info(f"Pairs cycle complete. Open pairs: {len(open_pairs)}")


def _run_crypto_cycle(api, risk, news, telegram, state, portfolio_value, logger):
    """
    Full crypto trading cycle — runs after the stock cycle.
    Uses crypto-specific strategy engine and risk parameters.
    Crypto trades 24/7 so market-hours check is skipped.
    """
    crypto_engine = CryptoStrategyEngine()

    # ---- Current crypto positions ----
    all_positions = api.get_all_positions()
    crypto_positions = [p for p in all_positions if api.is_crypto(p["symbol"])]
    held_crypto = {p["symbol"] for p in crypto_positions}

    max_crypto_positions = getattr(config, "MAX_CRYPTO_POSITIONS", 5)
    max_crypto_alloc = getattr(config, "MAX_CRYPTO_PORTFOLIO_ALLOCATION", 0.20)
    max_pos_weight = getattr(config, "MAX_CRYPTO_POSITION_WEIGHT", 0.05)
    risk_per_trade = getattr(config, "CRYPTO_RISK_PER_TRADE", 0.015)
    hard_stop = getattr(config, "CRYPTO_HARD_STOP_LOSS_PERCENT", 12.0)
    hard_tp = getattr(config, "CRYPTO_HARD_TAKE_PROFIT_PERCENT", 25.0)
    buy_threshold = getattr(config, "CRYPTO_BUY_THRESHOLD", 0.25)

    total_crypto_invested = sum(float(p.get("market_value", 0)) for p in crypto_positions)
    crypto_alloc_pct = total_crypto_invested / portfolio_value if portfolio_value > 0 else 0

    logger.info(f"Crypto: {len(crypto_positions)} positions | "
                f"${total_crypto_invested:,.2f} invested ({crypto_alloc_pct*100:.1f}% of portfolio)")

    # ---- Phase 1: Check crypto exits ----
    logger.info("--- Crypto Phase 1: Checking exits ---")
    for pos in crypto_positions:
        symbol = pos["symbol"]
        entry = float(pos.get("avg_entry_price", 0))
        current = float(pos.get("current_price", 0))
        pl_pct = float(pos.get("unrealized_plpc", 0)) * 100
        unrealized_pl = float(pos.get("unrealized_pl", 0))

        exit_reason = None

        # Hard stop-loss
        if entry > 0 and pl_pct <= -hard_stop:
            exit_reason = f"HARD STOP {pl_pct:.1f}%"

        # Hard take-profit
        elif entry > 0 and pl_pct >= hard_tp:
            exit_reason = f"HARD TAKE-PROFIT {pl_pct:.1f}%"

        # Strategy signal
        else:
            bars = api.get_crypto_bars(symbol, "1Day", 100)
            if bars is not None and not bars.empty:
                analysis = crypto_engine.analyze(bars)
                if analysis["signal"] in ("STRONG_SELL", "SELL"):
                    exit_reason = f"Strategy sell (score: {analysis['combined_score']:.3f})"

        if exit_reason:
            logger.info(f"CRYPTO EXIT: {symbol} — {exit_reason}")
            result = api.close_crypto_position(symbol)
            if result:
                log_trade(state, "SELL", symbol, float(pos.get("market_value", 0)),
                          current, pnl=unrealized_pl, reason=exit_reason, is_crypto=True)
                icon = "🪙"
                if "STOP" in exit_reason.upper():
                    if config.NOTIFY_ON_STOP_LOSS:
                        telegram.notify_stop_loss(f"{icon} {symbol}", pl_pct, unrealized_pl)
                else:
                    if config.NOTIFY_ON_SELL:
                        telegram.notify_sell(f"{icon} {symbol}", exit_reason, unrealized_pl)

    # ---- Phase 2: Scan for crypto buys ----
    logger.info("--- Crypto Phase 2: Scanning for crypto opportunities ---")

    # Refresh after any exits
    all_positions = api.get_all_positions()
    crypto_positions = [p for p in all_positions if api.is_crypto(p["symbol"])]
    held_crypto = {p["symbol"] for p in crypto_positions}
    total_crypto_invested = sum(float(p.get("market_value", 0)) for p in crypto_positions)

    # Check allocation limits
    if len(crypto_positions) >= max_crypto_positions:
        logger.info(f"Crypto: max positions reached ({max_crypto_positions}) — skipping buys")
        return

    if total_crypto_invested / portfolio_value >= max_crypto_alloc:
        logger.info(f"Crypto: max allocation reached ({max_crypto_alloc*100:.0f}%) — skipping buys")
        return

    # Get news scores for crypto (re-uses NewsScanner with crypto symbols)
    crypto_news_scores = news.get_sentiment_scores(
        [s.replace("/USD", "") for s in config.CRYPTO_UNIVERSE],
        exclude_symbols=set(),
    )

    candidates = []
    for symbol in config.CRYPTO_UNIVERSE:
        if symbol in held_crypto:
            continue

        bars = api.get_crypto_bars(symbol, "1Day", 100)
        if bars is None or bars.empty:
            continue

        # Map news score: BTC/USD → BTC
        news_key = symbol.replace("/USD", "")
        sentiment = crypto_news_scores.get(news_key, 0.0)

        analysis = crypto_engine.analyze(bars, sentiment_score=sentiment)
        if analysis["combined_score"] >= buy_threshold:
            candidates.append({
                "symbol": symbol,
                "analysis": analysis,
                "bars": bars,
            })

    candidates.sort(key=lambda c: c["analysis"]["combined_score"], reverse=True)

    if candidates:
        logger.info("Crypto buy candidates: " + ", ".join(
            f"{c['symbol']} ({c['analysis']['combined_score']:.3f})"
            for c in candidates[:5]
        ))
    else:
        logger.info("No crypto buy signals this cycle")

    # ---- Phase 3: Execute crypto buys ----
    logger.info("--- Crypto Phase 3: Executing crypto trades ---")

    for candidate in candidates:
        symbol = candidate["symbol"]
        analysis = candidate["analysis"]

        # Re-check limits each iteration
        if len(crypto_positions) >= max_crypto_positions:
            break
        if total_crypto_invested / portfolio_value >= max_crypto_alloc:
            break

        # Position size: min of (risk_per_trade %, max_pos_weight %)
        position_size = min(
            portfolio_value * risk_per_trade,
            portfolio_value * max_pos_weight,
        )

        # Respect remaining crypto budget
        remaining_budget = portfolio_value * max_crypto_alloc - total_crypto_invested
        position_size = min(position_size, remaining_budget)

        if position_size < 10:   # Minimum $10 per crypto trade
            logger.info(f"CRYPTO SKIP: {symbol} — position too small (${position_size:.2f})")
            continue

        price = api.get_crypto_latest_price(symbol)
        if not price:
            continue

        logger.info(
            f"CRYPTO BUY: {symbol} | ${position_size:.2f} @ ~${price} | "
            f"Score: {analysis['combined_score']:.3f} | Regime: {analysis.get('regime', '?')}"
        )

        result = api.buy_crypto(symbol, notional=position_size)
        if result:
            log_trade(state, "BUY", symbol, position_size, price,
                      score=analysis["combined_score"], is_crypto=True)
            if config.NOTIFY_ON_BUY:
                enriched_analysis = dict(analysis)
                enriched_analysis["is_crypto"] = True
                telegram.notify_buy(f"🪙 {symbol}", position_size, price, enriched_analysis)

            total_crypto_invested += position_size
            crypto_positions.append({
                "symbol": symbol,
                "market_value": position_size,
                "avg_entry_price": price,
                "current_price": price,
                "unrealized_pl": 0,
                "unrealized_plpc": 0,
            })

    logger.info(f"Crypto cycle complete. Positions: {len(crypto_positions)}, "
                f"Invested: ${total_crypto_invested:,.2f}")


if __name__ == "__main__":
    run()
