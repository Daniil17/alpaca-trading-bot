"""
MAIN TRADING BOT - Advanced Multi-Strategy Alpaca Trader
=========================================================
Orchestrates all components:
  - Alpaca API for trade execution
  - 4-strategy engine for signal generation
  - Risk manager for position sizing and portfolio protection
  - News scanner for sentiment
  - Telegram for real-time notifications

To run:  python bot.py
To stop: Ctrl+C
"""

import time
import logging
import sys
from datetime import datetime, timezone

import config
from alpaca_api import AlpacaAPI
from strategies import StrategyEngine
from risk_manager import RiskManager
from news_scanner import NewsScanner
from telegram_bot import TelegramNotifier

# ============================================================
# LOGGING
# ============================================================

def setup_logging():
    """Configure logging to console and file."""
    log = logging.getLogger("TradingBot")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    if config.ENABLE_LOGGING:
        fh = logging.FileHandler(config.LOG_FILE)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


# ============================================================
# THE BOT
# ============================================================

class TradingBot:
    """Advanced multi-strategy trading bot."""

    def __init__(self):
        self.logger = logging.getLogger("TradingBot")

        # --- Validate config ---
        if config.ALPACA_API_KEY == "YOUR_API_KEY_HERE":
            self.logger.error("Set your Alpaca API key in config.py first!")
            sys.exit(1)

        # --- Initialize components ---
        self.api = AlpacaAPI(
            config.ALPACA_API_KEY,
            config.ALPACA_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )

        self.strategy = StrategyEngine(
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

        self.risk = RiskManager(config)
        self.news = NewsScanner()

        self.telegram = TelegramNotifier(
            config.TELEGRAM_BOT_TOKEN,
            config.TELEGRAM_CHAT_ID,
        )

        # Track stocks the user already held before bot started
        self.manual_symbols = set()

        # Track last daily summary time
        self.last_summary_date = None

    def start(self):
        """Start the bot."""
        mode = "PAPER" if config.PAPER_TRADING else "LIVE"
        self.logger.info("=" * 60)
        self.logger.info(f"  ADVANCED TRADING BOT — {mode} MODE")
        self.logger.info(f"  Strategies: {list(config.STRATEGY_WEIGHTS.keys())}")
        self.logger.info(f"  Universe: {len(config.STOCK_UNIVERSE)} stocks")
        self.logger.info(f"  Risk per trade: {config.RISK_PER_TRADE*100:.0f}%")
        self.logger.info(f"  Max drawdown: {config.MAX_DRAWDOWN_PERCENT}%")
        self.logger.info("=" * 60)

        # Safety confirmation for live trading
        if not config.PAPER_TRADING:
            print("\n  WARNING: LIVE TRADING with REAL MONEY!")
            print("  Type 'yes' to confirm: ", end="")
            if input().strip().lower() != "yes":
                print("  Cancelled.")
                sys.exit(0)

        # Record existing positions (won't touch these)
        self.manual_symbols = self.api.get_position_symbols()
        if self.manual_symbols:
            self.logger.info(f"Your positions (bot won't touch): "
                             f"{', '.join(self.manual_symbols)}")

        # Send Telegram startup message
        account = self.api.get_account()
        settings_summary = (
            f"<b>Stocks:</b> {len(config.STOCK_UNIVERSE)}\n"
            f"<b>Risk/trade:</b> {config.RISK_PER_TRADE*100:.0f}%\n"
            f"<b>Max positions:</b> {config.MAX_OPEN_POSITIONS}\n"
            f"<b>Portfolio:</b> ${account['portfolio_value']:,.2f}"
        )
        self.telegram.notify_bot_started(config.PAPER_TRADING, settings_summary)

        # Main loop
        self.logger.info(f"Checking every {config.CHECK_INTERVAL_SECONDS}s. Ctrl+C to stop.\n")
        cycle = 0

        try:
            while True:
                cycle += 1
                self.logger.info(f"\n{'='*50}")
                self.logger.info(f"CYCLE #{cycle} — "
                                 f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info(f"{'='*50}")

                try:
                    self._run_cycle()
                except Exception as e:
                    self.logger.error(f"Cycle error: {e}", exc_info=True)
                    if config.NOTIFY_ON_ERROR:
                        self.telegram.notify_error(str(e)[:500])

                # Daily summary check
                self._maybe_send_daily_summary()

                self.logger.info(
                    f"Next cycle in {config.CHECK_INTERVAL_SECONDS}s "
                    f"({config.CHECK_INTERVAL_SECONDS // 60}m)\n"
                )
                time.sleep(config.CHECK_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            self.logger.info("\nBot stopped by user.")
            self.telegram.send("<b>Bot stopped by user.</b>")

    def _run_cycle(self):
        """One full trading cycle."""

        # --- Check market hours ---
        if config.RESPECT_MARKET_HOURS and not self.api.is_market_open():
            self.logger.info("Market closed — skipping cycle")
            return

        # --- Account status ---
        account = self.api.get_account()
        if not account:
            self.logger.error("Cannot reach Alpaca API")
            return

        portfolio_value = account["portfolio_value"]
        self.logger.info(f"Portfolio: ${portfolio_value:,.2f} | "
                         f"Cash: ${account['cash']:,.2f}")

        if account.get("trading_blocked"):
            self.logger.error("Trading is blocked on this account!")
            return

        # --- Get current positions ---
        all_positions = self.api.get_all_positions()
        # Separate bot positions from manual ones
        bot_positions = [
            p for p in all_positions
            if p["symbol"] not in self.manual_symbols
        ]

        # ========================================
        # PHASE 1: CHECK EXITS
        # ========================================
        self.logger.info("--- Phase 1: Checking exits ---")

        # Risk manager hard stop-loss / take-profit check
        exits = self.risk.check_positions_for_exit(bot_positions)
        for pos in exits:
            symbol = pos["symbol"]
            reason = pos.get("exit_reason", "Risk exit")
            unrealized_pl = float(pos.get("unrealized_pl", 0))

            self.logger.info(f"EXIT: {symbol} — {reason}")
            result = self.api.close_position(symbol)

            if result:
                if config.NOTIFY_ON_STOP_LOSS and "STOP" in reason:
                    pct = float(pos.get("unrealized_plpc", 0)) * 100
                    self.telegram.notify_stop_loss(symbol, pct, unrealized_pl)
                elif config.NOTIFY_ON_SELL:
                    self.telegram.notify_sell(symbol, reason, unrealized_pl)

        # Strategy-based sell signals on held positions
        for pos in bot_positions:
            symbol = pos["symbol"]
            # Skip if we already closed it above
            if any(e["symbol"] == symbol for e in exits):
                continue

            bars = self.api.get_bars(symbol, "1Day", 100)
            if bars is None or bars.empty:
                continue

            analysis = self.strategy.analyze(bars)
            if analysis["signal"] in ("STRONG_SELL", "SELL"):
                self.logger.info(f"STRATEGY SELL: {symbol} "
                                 f"(score: {analysis['combined_score']:.3f})")
                unrealized_pl = float(pos.get("unrealized_pl", 0))
                result = self.api.close_position(symbol)
                if result and config.NOTIFY_ON_SELL:
                    self.telegram.notify_sell(
                        symbol,
                        f"Strategy sell (score: {analysis['combined_score']:.3f})",
                        unrealized_pl,
                    )

        # ========================================
        # PHASE 2: SCAN FOR NEW BUYS
        # ========================================
        self.logger.info("--- Phase 2: Scanning for opportunities ---")

        # Refresh positions after sells
        all_positions = self.api.get_all_positions()
        bot_positions = [
            p for p in all_positions
            if p["symbol"] not in self.manual_symbols
        ]
        held_symbols = {p["symbol"] for p in all_positions}

        # Get news sentiment for the stock universe
        news_scores = self.news.get_sentiment_scores(
            config.STOCK_UNIVERSE,
            exclude_symbols=held_symbols,
        )

        # Score all candidates
        candidates = []
        for symbol in config.STOCK_UNIVERSE:
            if symbol in held_symbols:
                continue

            bars = self.api.get_bars(symbol, "1Day", 100)
            if bars is None or bars.empty:
                continue

            sentiment = news_scores.get(symbol, 0.0)
            analysis = self.strategy.analyze(bars, sentiment_score=sentiment)

            if analysis["signal"] in ("STRONG_BUY", "BUY"):
                candidates.append({
                    "symbol": symbol,
                    "analysis": analysis,
                    "bars": bars,
                    "sentiment": sentiment,
                })

        # Sort by combined score (best first)
        candidates.sort(
            key=lambda c: c["analysis"]["combined_score"], reverse=True
        )

        if candidates:
            self.logger.info(
                f"Buy candidates: "
                + ", ".join(
                    f"{c['symbol']} ({c['analysis']['combined_score']:.3f})"
                    for c in candidates[:5]
                )
            )
        else:
            self.logger.info("No buy signals this cycle")

        # ========================================
        # PHASE 3: EXECUTE BUYS (with risk checks)
        # ========================================
        self.logger.info("--- Phase 3: Executing trades ---")

        for candidate in candidates:
            symbol = candidate["symbol"]
            analysis = candidate["analysis"]
            bars = candidate["bars"]

            # Risk manager approval
            allowed, reason, position_size = self.risk.can_open_position(
                symbol, portfolio_value, bot_positions, bars
            )

            if not allowed:
                self.logger.info(f"BLOCKED: {symbol} — {reason}")
                if "DRAWDOWN" in reason:
                    self.telegram.notify_drawdown_breaker(
                        self.risk.peak_portfolio_value - portfolio_value,
                        self.risk.peak_portfolio_value,
                        portfolio_value,
                    )
                continue

            if position_size < 1:
                self.logger.info(f"SKIP: {symbol} — position too small (${position_size})")
                continue

            # Place the buy order
            price = self.api.get_latest_price(symbol)
            if not price:
                continue

            self.logger.info(
                f"BUY: {symbol} | ${position_size:.2f} | "
                f"Price ~${price} | Score: {analysis['combined_score']:.3f}"
            )

            result = self.api.buy_market(symbol, notional=position_size)

            if result:
                if config.NOTIFY_ON_BUY:
                    self.telegram.notify_buy(symbol, position_size, price, analysis)

                # Refresh bot_positions for next iteration's risk checks
                bot_positions.append({
                    "symbol": symbol,
                    "market_value": position_size,
                    "avg_entry_price": price,
                    "current_price": price,
                    "unrealized_pl": 0,
                    "unrealized_plpc": 0,
                })

        # Portfolio risk summary
        summary = self.risk.get_portfolio_summary(portfolio_value, bot_positions)
        self.logger.info(
            f"Portfolio: {summary['total_positions']} positions, "
            f"${summary['total_invested']:,.2f} invested, "
            f"{summary['cash_reserve_pct']:.0f}% cash, "
            f"drawdown {summary['drawdown_pct']:.1f}%"
        )

    def _maybe_send_daily_summary(self):
        """Send a daily Telegram summary after market close."""
        if not config.NOTIFY_DAILY_SUMMARY:
            return

        now = datetime.now()
        today = now.date()

        # Send once per day, after 4:05 PM ET (give market time to settle)
        if self.last_summary_date == today:
            return
        if now.hour < 16:
            return

        self.last_summary_date = today

        account = self.api.get_account()
        if not account:
            return

        positions = self.api.get_all_positions()
        bot_positions = [
            p for p in positions if p["symbol"] not in self.manual_symbols
        ]
        summary = self.risk.get_portfolio_summary(
            account["portfolio_value"], bot_positions
        )

        self.telegram.notify_daily_summary(account, bot_positions, summary)
        self.logger.info("Daily summary sent to Telegram")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logger = setup_logging()

    print("""
    ╔════════════════════════════════════════════════════╗
    ║   ADVANCED MULTI-STRATEGY TRADING BOT (Alpaca)     ║
    ║                                                    ║
    ║   Strategies: Mean Reversion | Momentum | News     ║
    ║               VWAP | Risk Management               ║
    ║                                                    ║
    ║   Configure config.py before running!              ║
    ╚════════════════════════════════════════════════════╝
    """)

    bot = TradingBot()
    bot.start()
