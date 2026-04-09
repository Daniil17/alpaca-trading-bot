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
import time
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
from strategies import StrategyEngine, StrategyPerformanceTracker, BayesianWeightOptimizer
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
    # Guard: skip adding handlers if they already exist (e.g. when called
    # repeatedly from main.py's persistent loop on Railway).
    if log.handlers:
        return log
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

def _record_exit_pnl(state, symbol, pct_return, tracker, optimizer):
    """
    Record a closed position's P&L in the adaptive weight tracker and
    Bayesian trade history. Extracted to avoid the 3-way copy-paste that
    existed in the hard-stop, trailing-stop, and strategy-sell exit paths.
    """
    if tracker is None and optimizer is None:
        return
    position_strategies = state.get("position_strategies", {}).pop(symbol, None)
    if not position_strategies:
        return
    if tracker is not None:
        tracker.record_trade_result(position_strategies, pct_return)
    if optimizer is not None:
        bh = state.setdefault("bayes_trade_history", [])
        bh.append({"strategy_scores": position_strategies, "pct_return": pct_return})
        state["bayes_trade_history"] = bh[-100:]


def get_dynamic_stock_universe(api, base_universe: list, top_n: int = 32) -> list:
    """
    Filter base_universe to the top_n most liquid stocks by dollar volume
    (daily_volume × close_price).  Falls back to the full base_universe
    silently if snapshots are unavailable.
    """
    logger_ref = logging.getLogger("TradingBot")
    try:
        snapshots = api.get_stock_snapshots(base_universe)
        if not snapshots:
            return base_universe

        scored = []
        for symbol in base_universe:
            snap = snapshots.get(symbol)
            if snap is None:
                continue
            try:
                daily_bar = getattr(snap, "daily_bar", None)
                if daily_bar is None:
                    continue
                close = float(daily_bar.close) if daily_bar.close else 0.0
                volume = float(daily_bar.volume) if daily_bar.volume else 0.0
                dollar_volume = close * volume
                scored.append((symbol, dollar_volume))
            except Exception:
                continue

        if not scored:
            return base_universe

        scored.sort(key=lambda x: x[1], reverse=True)
        result = [s for s, _ in scored[:top_n]]
        cutoff_dv = scored[min(top_n - 1, len(scored) - 1)][1]
        logger_ref.info(
            f"Dynamic universe: {len(result)}/{len(base_universe)} symbols selected "
            f"(cutoff ${cutoff_dv / 1e6:.1f}M daily dollar vol)"
        )
        return result

    except Exception as exc:
        logging.getLogger("TradingBot").warning(
            f"Dynamic universe screening failed ({exc}) — using full universe"
        )
        return base_universe


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

    # --- Adaptive strategy weight tracker ---
    # Loads rolling PnL history from state so weights persist across runs.
    tracker = None
    if getattr(config, "USE_ADAPTIVE_WEIGHTS", True):
        tracker = StrategyPerformanceTracker(config.STRATEGY_WEIGHTS)
        saved_pnl = state.get("strategy_pnl", {})
        for k in tracker.strategy_pnl:
            if k in saved_pnl:
                tracker.strategy_pnl[k] = saved_pnl[k]

    # --- Bayesian weight optimizer ---
    # Runs GP optimisation every 10 cycles; result cached in state and loaded
    # into engine._bayesian_weights so per-stock analyze() calls are fast.
    optimizer = None
    if getattr(config, "USE_BAYES_WEIGHTS", False):
        optimizer = BayesianWeightOptimizer(
            strategy_names=list(config.STRATEGY_WEIGHTS.keys()),
            n_calls=getattr(config, "BAYES_WEIGHT_N_CALLS", 15),
            window=getattr(config, "BAYES_WEIGHT_WINDOW", 30),
        )

    engine = StrategyEngine(
        weights=config.STRATEGY_WEIGHTS,
        tracker=tracker,
        optimizer=optimizer,
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
        vpt_lookback=getattr(config, "VPT_LOOKBACK", 20),
    )

    risk = RiskManager(config)
    # Restore peak value from state with sanity checks to guard against
    # state loss (Railway redeploy, corrupted file) silently disabling the
    # drawdown breaker.
    risk.peak_portfolio_value = state.get("peak_portfolio_value", 0.0)

    # Restore cached Bayesian weights — but only if they match current strategy names.
    # If we renamed a strategy (e.g. vwap → volume_flow) the cached keys are stale.
    if optimizer is not None:
        cached_weights = state.get("bayes_weights")
        if cached_weights:
            expected_keys = set(config.STRATEGY_WEIGHTS.keys())
            if expected_keys == set(cached_weights.keys()):
                engine._bayesian_weights = cached_weights
            else:
                logger.info("Strategy names changed — discarding stale Bayesian weights cache")

    # Re-run Bayesian optimisation every 10 cycles (GP is slow — don't run every cycle)
    run_count = state.get("run_count", 1)
    if optimizer is not None and run_count % 10 == 0:
        trade_history = state.get("bayes_trade_history", [])
        if len(trade_history) >= 10:
            new_weights = optimizer.optimise(trade_history)
            engine._bayesian_weights = new_weights
            state["bayes_weights"] = new_weights
            logger.info(
                "Bayesian weights updated: "
                + ", ".join(f"{k}={v:.3f}" for k, v in new_weights.items())
            )

    news = NewsScanner()
    telegram = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)

    # --- Process Telegram commands (respond to button presses / messages) ---
    commander = TelegramCommander(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, api)

    # Register bot commands in Telegram menu on first run and every 50 cycles
    # (keeps the menu in sync after code deploys without spamming the API)
    if state.get("run_count", 0) % 50 == 1:
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

    # Sanity-check restored peak value — a stale or zeroed peak silently
    # disables the drawdown circuit breaker until a new high is reached.
    if risk.peak_portfolio_value == 0 and portfolio_value > 0:
        logger.warning(
            f"⚠️  Peak portfolio value was 0 (state lost or first run) — "
            f"initialising to current value ${portfolio_value:,.2f}"
        )
        risk.peak_portfolio_value = portfolio_value
    elif risk.peak_portfolio_value > 0 and portfolio_value > risk.peak_portfolio_value * 1.5:
        logger.warning(
            f"⚠️  Stored peak ${risk.peak_portfolio_value:,.2f} looks stale "
            f"(current ${portfolio_value:,.2f} is 50%+ higher) — resetting peak."
        )
        risk.peak_portfolio_value = portfolio_value

    # Update peak value
    if portfolio_value > risk.peak_portfolio_value:
        risk.peak_portfolio_value = portfolio_value

    if account.get("trading_blocked"):
        logger.error("Trading blocked on this account!")
        save_state(state)
        return

    # --- PDT (Pattern Day Trader) protection ---
    # Alpaca blocks accounts with <$25k equity after >3 day trades in a rolling 5-day window.
    # Check BOTH our counter AND Alpaca's own flag.
    equity = account.get("equity", portfolio_value)
    daytrade_count = account.get("daytrade_count", 0)
    alpaca_pdt_flagged = account.get("pattern_day_trader", False)
    pdt_protected = equity < 25_000
    pdt_limit_reached = pdt_protected and (daytrade_count >= 3 or alpaca_pdt_flagged)
    if pdt_limit_reached:
        msg = (f"PDT LIMIT: {daytrade_count}/3 day trades used "
               f"(equity ${equity:,.0f} < $25k). Stock buys blocked.")
        logger.warning(msg)
        telegram.send(f"⚠️ <b>PDT Protection Active</b>\n{msg}")

    # --- Get positions ---
    all_positions = api.get_all_positions()
    # Only stock positions for the stock cycle (exclude crypto)
    bot_positions = [p for p in all_positions if not api.is_crypto(p["symbol"])]
    sold_symbols = set()   # Track symbols sold this cycle to sync local list

    # --- Prune stale trailing_high entries (positions closed by Alpaca stop orders) ---
    # Normalize to no-slash form so "BCH/USD" and "BCHUSD" map to the same key.
    active_symbols = {p["symbol"].replace("/", "") for p in all_positions}
    stale = [s for s in state.get("trailing_high", {}) if s not in active_symbols]
    for s in stale:
        state["trailing_high"].pop(s, None)
    if stale:
        logger.debug(f"Pruned trailing_high for closed positions: {stale}")

    if run_stock_cycle:
        # ============================================================
        # DYNAMIC UNIVERSE SCREENING
        # ============================================================
        # Filter to the top-N most liquid stocks by dollar volume each cycle.
        # Held positions are always included regardless of liquidity rank.
        if getattr(config, "USE_DYNAMIC_UNIVERSE", True):
            top_n = getattr(config, "DYNAMIC_UNIVERSE_TOP_N", 32)
            stock_universe = get_dynamic_stock_universe(api, config.STOCK_UNIVERSE, top_n)
        else:
            stock_universe = config.STOCK_UNIVERSE

        # ============================================================
        # BAR CACHE — fetch once, reuse in exit and buy phases
        # ============================================================
        universe_symbols = (
            {p["symbol"] for p in bot_positions} | set(stock_universe)
        )
        bars_cache = {}
        logger.info(f"Pre-fetching bars for {len(universe_symbols)} symbols...")
        for sym in universe_symbols:
            b = api.get_bars(sym, "1Day", 100)
            if b is not None and not b.empty:
                bars_cache[sym] = b

        # ============================================================
        # PHASE 1: EXITS
        # ============================================================
        logger.info("--- Phase 1: Checking exits ---")

        # --- Trailing stop high-water mark update (stocks) ---
        trail_activation = getattr(config, "TRAILING_STOP_ACTIVATION_PCT", 0.05)
        trail_pct = getattr(config, "TRAILING_STOP_PCT", 0.07)
        trailing_high = state.setdefault("trailing_high", {})

        for pos in bot_positions:
            symbol = pos["symbol"]
            entry = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            if entry > 0 and current > 0:
                # Update high-water mark
                prev_high = trailing_high.get(symbol, entry)
                trailing_high[symbol] = max(prev_high, current)

        # Software-side safety backup for hard stop-loss and take-profit.
        # When USE_BRACKET_ORDERS is True, Alpaca's matching engine handles
        # SL/TP atomically at entry — this check only fires if the bracket
        # legs were somehow cancelled or the position was opened without brackets.
        exits = risk.check_positions_for_exit(bot_positions)
        exited_symbols = {pos["symbol"] for pos in exits}

        for pos in exits:
            symbol = pos["symbol"]
            reason = pos.get("exit_reason", "Risk exit")
            unrealized_pl = float(pos.get("unrealized_pl", 0))
            current_price = float(pos.get("current_price", 0))
            pct_return = float(pos.get("unrealized_plpc", 0)) * 100
            logger.info(f"EXIT: {symbol} — {reason}")
            result = api.close_position(symbol)
            if result:
                log_trade(state, "SELL", symbol, float(pos.get("market_value", 0)),
                          current_price, pnl=unrealized_pl, reason=reason)
                sold_symbols.add(symbol)
                trailing_high.pop(symbol, None)   # Clear trailing record on exit
                state.setdefault("sell_cooldowns", {})[symbol] = time.time()
                _record_exit_pnl(state, symbol, pct_return, tracker, optimizer)
                if config.NOTIFY_ON_STOP_LOSS and "STOP" in reason:
                    telegram.notify_stop_loss(symbol, pct_return, unrealized_pl)
                elif config.NOTIFY_ON_SELL:
                    telegram.notify_sell(symbol, reason, unrealized_pl)

        # --- Trailing stop check (stocks) ---
        for pos in bot_positions:
            symbol = pos["symbol"]
            if symbol in sold_symbols or symbol in exited_symbols:
                continue
            entry = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            if entry <= 0 or current <= 0:
                continue
            high = trailing_high.get(symbol, entry)
            # Only activate once position is up enough to warrant trailing
            if high >= entry * (1 + trail_activation):
                trail_stop = high * (1 - trail_pct)
                if current <= trail_stop:
                    gain_pct = (high - entry) / entry * 100
                    reason = (f"TRAILING STOP: price fell {trail_pct*100:.0f}% from "
                              f"peak ${high:.2f} (peak gain was +{gain_pct:.1f}%)")
                    logger.info(f"TRAILING STOP: {symbol} — current ${current:.2f} "
                                f"≤ stop ${trail_stop:.2f} (peak ${high:.2f})")
                    unrealized_pl = float(pos.get("unrealized_pl", 0))
                    result = api.close_position(symbol)
                    if result:
                        log_trade(state, "SELL", symbol, float(pos.get("market_value", 0)),
                                  current, pnl=unrealized_pl, reason=reason)
                        sold_symbols.add(symbol)
                        trailing_high.pop(symbol, None)
                        state.setdefault("sell_cooldowns", {})[symbol] = time.time()
                        trail_pct_return = (current - entry) / entry * 100 if entry > 0 else 0.0
                        _record_exit_pnl(state, symbol, trail_pct_return, tracker, optimizer)
                        if config.NOTIFY_ON_SELL:
                            try:
                                telegram.notify_sell(symbol, reason, unrealized_pl)
                            except Exception as _e:
                                logger.warning(f"Telegram notify failed: {_e}")

        # Strategy-based sell signals — use cached bars, sync local list after sell
        for pos in list(bot_positions):   # iterate copy so we can remove mid-loop
            symbol = pos["symbol"]
            if symbol in sold_symbols:
                continue
            bars = bars_cache.get(symbol)
            if bars is None:
                continue
            analysis = engine.analyze(bars)
            if analysis["signal"] in ("STRONG_SELL", "SELL"):
                logger.info(f"STRATEGY SELL: {symbol} (score: {analysis['combined_score']:.3f})")
                unrealized_pl = float(pos.get("unrealized_pl", 0))
                current_price = float(pos.get("current_price", 0))
                pct_return = float(pos.get("unrealized_plpc", 0)) * 100
                result = api.close_position(symbol)
                if result:
                    log_trade(state, "SELL", symbol, float(pos.get("market_value", 0)),
                              current_price, pnl=unrealized_pl,
                              reason=f"Strategy ({analysis['combined_score']:.3f})",
                              score=analysis["combined_score"])
                    sold_symbols.add(symbol)
                    state.setdefault("sell_cooldowns", {})[symbol] = time.time()
                    _record_exit_pnl(state, symbol, pct_return, tracker, optimizer)
                    # Sync local position list immediately
                    bot_positions = [p for p in bot_positions if p["symbol"] != symbol]
                    if config.NOTIFY_ON_SELL:
                        telegram.notify_sell(symbol, f"Strategy ({analysis['combined_score']:.3f})", unrealized_pl)

        # ============================================================
        # PHASE 2: FIND BUYS (use cached bars — no redundant API calls)
        # ============================================================
        logger.info("--- Phase 2: Scanning for opportunities ---")

        held_symbols = {p["symbol"] for p in bot_positions} | sold_symbols

        # --- Daily scan gate ---
        # Strategy scores use 1Day bars that don't change intraday, so running
        # the full buy scan ~78×/day (every 5 min) produces identical signals.
        # Skip buy scanning after the first run of each trading day; exits are
        # always managed regardless (hard stops, trailing stops above).
        today_str = str(date.today())
        if state.get("last_full_scan_date") == today_str:
            logger.info("Buy scan already ran today — skipping (exits still managed)")
            candidates = []
        else:
            # --- Sell cooldown: don't re-buy a stock too soon after selling ---
            cooldown_secs = getattr(config, "SELL_COOLDOWN_HOURS", 24) * 3600
            now_ts = time.time()
            sell_cooldowns = {
                sym: ts for sym, ts in state.get("sell_cooldowns", {}).items()
                if now_ts - ts < cooldown_secs
            }
            state["sell_cooldowns"] = sell_cooldowns

            news_scores = news.get_sentiment_scores(stock_universe, exclude_symbols=held_symbols)

            candidates = []
            for symbol in stock_universe:
                if symbol in held_symbols:
                    continue
                if symbol in sell_cooldowns:
                    hrs = (now_ts - sell_cooldowns[symbol]) / 3600
                    logger.debug(f"COOLDOWN: {symbol} sold {hrs:.1f}h ago — skipping")
                    continue
                bars = bars_cache.get(symbol)
                if bars is None:
                    continue
                sentiment = news_scores.get(symbol, 0.0)
                analysis = engine.analyze(bars, sentiment_score=sentiment)
                if analysis["signal"] in ("STRONG_BUY", "BUY"):
                    candidates.append({"symbol": symbol, "analysis": analysis, "bars": bars})

            candidates.sort(key=lambda c: c["analysis"]["combined_score"], reverse=True)
            state["last_full_scan_date"] = today_str

            if candidates:
                logger.info("Buy candidates: " + ", ".join(
                    f"{c['symbol']} ({c['analysis']['combined_score']:.3f})"
                    for c in candidates[:5]
                ))
            else:
                logger.info("No buy signals this cycle")

        # ============================================================
        # PHASE 3: EXECUTE BUYS + place stop-loss orders
        # ============================================================
        logger.info("--- Phase 3: Executing trades ---")

        if pdt_limit_reached:
            logger.warning("PDT limit reached — skipping all stock buys this cycle")
            candidates = []

        for candidate in candidates:
            symbol = candidate["symbol"]
            analysis = candidate["analysis"]
            bars = candidate["bars"]

            allowed, reason, position_size = risk.can_open_position(
                symbol, portfolio_value, bot_positions, bars, api=api
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

            # Compute ATR-based stop-loss and take-profit levels
            stp = risk.calculate_stop_take_profit(symbol, price, bars)
            stop_loss_price = stp["stop_loss"]
            take_profit_price = stp["take_profit"]

            logger.info(
                f"BUY: {symbol} | ${position_size:.2f} @ ~${price} | "
                f"Score: {analysis['combined_score']:.3f} | "
                f"SL=${stop_loss_price:.2f} TP=${take_profit_price:.2f}"
            )

            # --- Bracket order (atomic SL + TP at Alpaca's matching engine) ---
            # Architectural note: bracket orders submit the stop-loss and take-profit
            # legs atomically at entry.  This eliminates the gap risk of the bot's
            # 5-minute polling cycle — exits fire at the broker level even when the
            # bot is not running.  The software-side check_positions_for_exit above
            # remains as a backup in case a bracket leg gets cancelled.
            if getattr(config, "USE_BRACKET_ORDERS", True):
                result = api.place_bracket_order(
                    symbol, position_size, "buy", stop_loss_price, take_profit_price
                )
                if result is None:
                    # Bracket order failed — fall back to plain market order + stop
                    logger.warning(f"Bracket order failed for {symbol} — falling back to market + stop")
                    result = api.buy_market(symbol, notional=position_size)
                    if result:
                        try:
                            qty_shares = round(position_size / price, 6)
                            if stop_loss_price > 0 and qty_shares > 0:
                                api.sell_stop(symbol, stop_loss_price, qty_shares)
                        except Exception as _e:
                            logger.warning(f"Fallback stop-loss placement failed for {symbol}: {_e}")
            else:
                algo_threshold = getattr(config, "ALGO_ORDER_THRESHOLD", 5000)
                if position_size > algo_threshold:
                    result = api.place_algo_order(symbol, position_size, "buy", algo="twap")
                else:
                    result = api.buy_market(symbol, notional=position_size)
                if result:
                    try:
                        qty_shares = round(position_size / price, 6)
                        if stop_loss_price > 0 and qty_shares > 0:
                            api.sell_stop(symbol, stop_loss_price, qty_shares)
                    except Exception as _e:
                        logger.warning(f"Could not place stop-loss for {symbol}: {_e}")

            if result:
                log_trade(state, "BUY", symbol, position_size, price,
                          score=analysis["combined_score"])
                # Store strategy breakdown for adaptive weight tracking on close
                state.setdefault("position_strategies", {})[symbol] = analysis["strategies"]
                if config.NOTIFY_ON_BUY:
                    try:
                        telegram.notify_buy(symbol, position_size, price, analysis)
                    except Exception as _tg_err:
                        logger.warning(f"Telegram notify_buy failed for {symbol}: {_tg_err}")

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
    # Persist adaptive weight history
    if tracker is not None:
        state["strategy_pnl"] = tracker.strategy_pnl
    # Persist Bayesian weights so next run picks up where we left off
    if optimizer is not None and engine._bayesian_weights is not None:
        state["bayes_weights"] = engine._bayesian_weights
    save_state(state)

    logger.info("Cycle complete.")


def _calculate_atr(bars, period=14):
    """Calculate the Average True Range from a bars DataFrame."""
    try:
        import numpy as np
        high = bars["high"]
        low = bars["low"]
        close = bars["close"]
        tr = (
            (high - low)
            .combine(abs(high - close.shift()), max)
            .combine(abs(low - close.shift()), max)
        )
        return float(tr.rolling(period).mean().iloc[-1])
    except Exception:
        return None


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

    crypto_trail_activation = getattr(config, "CRYPTO_TRAILING_STOP_ACTIVATION_PCT", 0.08)
    crypto_trail_pct = getattr(config, "CRYPTO_TRAILING_STOP_PCT", 0.12)
    trailing_high = state.setdefault("trailing_high", {})

    # Update crypto high-water marks first
    for pos in crypto_positions:
        symbol = pos["symbol"]
        sym_key = symbol.replace("/", "")   # normalize "BCH/USD" → "BCHUSD"
        entry = float(pos.get("avg_entry_price", 0))
        current = float(pos.get("current_price", 0))
        if entry > 0 and current > 0:
            trailing_high[sym_key] = max(trailing_high.get(sym_key, entry), current)

    for pos in crypto_positions:
        symbol = pos["symbol"]
        sym_key = symbol.replace("/", "")   # normalize for trailing_high key lookups
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

        # Trailing stop
        elif entry > 0 and current > 0:
            high = trailing_high.get(sym_key, entry)
            if high >= entry * (1 + crypto_trail_activation):
                trail_stop = high * (1 - crypto_trail_pct)
                if current <= trail_stop:
                    gain_pct = (high - entry) / entry * 100
                    exit_reason = (f"TRAILING STOP: price fell {crypto_trail_pct*100:.0f}% "
                                   f"from peak ${high:.2f} (peak gain was +{gain_pct:.1f}%)")

        # Strategy signal (only if no price-based exit triggered)
        if not exit_reason:
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
                trailing_high.pop(sym_key, None)
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
                try:
                    enriched_analysis = dict(analysis)
                    enriched_analysis["is_crypto"] = True
                    telegram.notify_buy(f"🪙 {symbol}", position_size, price, enriched_analysis)
                except Exception as _tg_err:
                    logger.warning(f"Telegram notify_buy failed for {symbol}: {_tg_err}")

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
