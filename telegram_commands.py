"""
TELEGRAM INTERACTIVE COMMANDS
==============================
Handles incoming Telegram messages and callback queries (button presses).
Called every cycle by run_once.py to process any pending user commands.

Supports:
  /status    — Portfolio overview
  /positions — Open positions with P&L
  /profit    — Total profit/loss breakdown
  /balance   — Account balance details
  /help      — Show all commands

Also provides inline keyboard buttons so the user can tap instead of typing.
"""

import logging
import json
import threading
import time
import requests
from datetime import datetime
import pytz

_LONDON = pytz.timezone("Europe/London")

def _now():
    """Current time in London (handles BST/GMT automatically)."""
    return datetime.now(_LONDON)

try:
    import config as _config
except ImportError:
    _config = None

logger = logging.getLogger("TradingBot")


class TelegramCommander:
    """Processes Telegram commands and sends interactive responses."""

    def __init__(self, bot_token, chat_id, alpaca_api):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.api = alpaca_api
        self.enabled = (
            bot_token != "YOUR_TELEGRAM_BOT_TOKEN_HERE"
            and chat_id != "YOUR_CHAT_ID_HERE"
        )
        self._last_update_id = 0
        # Rate limiting: track last command time to prevent rapid-fire DoS
        self._last_command_time = 0.0
        self._command_rate_limit_sec = 2.0  # minimum seconds between commands

    # ------------------------------------------------------------------
    # CORE: PROCESS PENDING MESSAGES
    # ------------------------------------------------------------------

    def process_updates(self):
        """
        Fetch and process all pending Telegram messages/button presses.
        Called once per trading cycle.
        """
        if not self.enabled:
            return

        try:
            updates = self._get_updates()
            for update in updates:
                self._last_update_id = update["update_id"] + 1

                # Handle button presses (callback queries)
                if "callback_query" in update:
                    self._handle_callback(update["callback_query"])

                # Handle text commands
                elif "message" in update:
                    msg = update["message"]
                    # Only respond to our chat
                    if str(msg.get("chat", {}).get("id")) == self.chat_id:
                        text = msg.get("text", "").strip().lower()
                        self._handle_command(text, msg.get("message_id"))

        except Exception as e:
            logger.warning(f"Telegram commands error: {e}")

    # ------------------------------------------------------------------
    # FETCH UPDATES
    # ------------------------------------------------------------------

    def _get_updates(self):
        """Get new messages from Telegram."""
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self._last_update_id,
                    "timeout": 2,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
                timeout=5,
            )
            data = resp.json()
            return data.get("result", [])
        except Exception as e:
            logger.warning(f"Failed to get Telegram updates: {e}")
            return []

    # ------------------------------------------------------------------
    # COMMAND ROUTER
    # ------------------------------------------------------------------

    def _handle_command(self, text, message_id=None):
        """Route text commands to handlers."""
        # --- Input validation ---
        # Ignore oversized messages (prevents memory/CPU abuse from very long inputs)
        if len(text) > 200:
            logger.debug(f"Ignoring oversized message ({len(text)} chars)")
            return

        # Rate limiting: ignore commands that arrive too quickly in succession
        now = time.monotonic()
        if now - self._last_command_time < self._command_rate_limit_sec:
            logger.debug("Ignoring command — rate limit active")
            return
        self._last_command_time = now

        if text in ("/start", "/help"):
            # Re-register the command menu so Telegram's picker stays current.
            send_startup_menu(self.bot_token, self.chat_id)
            self._send_help()
        elif text == "/status":
            self._send_status()
        elif text == "/positions":
            self._send_positions()
        elif text == "/profit":
            self._send_profit()
        elif text == "/balance":
            self._send_balance()
        elif text == "/dashboard":
            self._send_dashboard()
        elif text == "/trades":
            self._send_trades()
        elif text == "/menu":
            self._send_menu()
        elif text == "/analyse":
            self._send_analyse()
        elif text.startswith("/backtest"):
            # /backtest [SYMBOL|all [DAYS]]
            parts = text.split()
            symbol = parts[1].upper() if len(parts) > 1 else "ALL"
            try:
                days = int(parts[2]) if len(parts) > 2 else 365
                days = max(60, min(days, 1000))  # clamp to sane range
            except ValueError:
                self._send_message(
                    "<b>Usage:</b>\n"
                    "  /backtest all [DAYS] — full portfolio (all stocks)\n"
                    "  /backtest SYMBOL [DAYS] — single symbol\n\n"
                    "Examples:\n"
                    "  <code>/backtest all 365</code>\n"
                    "  <code>/backtest AAPL 252</code>"
                )
                return
            if symbol == "ALL":
                self._run_portfolio_backtest(days)
            else:
                self._run_backtest(symbol, days)
        # Ignore unknown messages silently

    def _handle_callback(self, callback):
        """Handle inline button presses."""
        data = callback.get("data", "")
        callback_id = callback.get("id")

        # Acknowledge the button press
        self._answer_callback(callback_id)

        # Route to handler
        if data == "status":
            self._send_status()
        elif data == "positions":
            self._send_positions()
        elif data == "profit":
            self._send_profit()
        elif data == "balance":
            self._send_balance()
        elif data == "dashboard":
            self._send_dashboard()
        elif data == "trades":
            self._send_trades()
        elif data == "help":
            self._send_help()
        elif data == "analyse":
            self._send_analyse()
        elif data.startswith("backtest:"):
            # callback_data format: "backtest:AAPL:365" or "backtest:ALL:365"
            parts = data.split(":")
            symbol = parts[1] if len(parts) > 1 else "ALL"
            try:
                days = int(parts[2]) if len(parts) > 2 else 365
            except ValueError:
                days = 365
            if symbol.upper() == "ALL":
                self._run_portfolio_backtest(days)
            else:
                self._run_backtest(symbol, days)

    # ------------------------------------------------------------------
    # RESPONSE HANDLERS
    # ------------------------------------------------------------------

    def _send_trades(self):
        """
        Send recent trade history.
        Primary source: Alpaca API (works on Railway without bot_state.json).
        Fallback: local bot_state.json (enriched with bot-recorded P&L).
        """
        import os
        import json as _json

        # --- Primary: fetch from Alpaca API ---
        trade_log = []
        try:
            trade_log = self.api.get_recent_orders(limit=20) or []
        except Exception:
            pass

        # --- Fallback: local bot_state.json (GitHub Actions environment) ---
        if not trade_log:
            try:
                if os.path.exists("bot_state.json"):
                    with open("bot_state.json") as f:
                        state_data = _json.load(f)
                    trade_log = list(reversed(state_data.get("trade_log", [])))
            except Exception:
                pass

        if not trade_log:
            msg = (
                "<b>🔁 Trade History</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "No trades recorded yet. The bot logs every buy and sell here — "
                "check back after the next trading cycle."
            )
            self._send_message(msg)
            return

        recent = trade_log[:20]  # already newest-first from Alpaca API

        msg = f"<b>🔁 Recent Trades (last {len(recent)})</b>\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        for t in recent:
            action = t.get("action", "?")
            symbol = t.get("symbol", "?")
            amount = t.get("amount", 0)
            price = t.get("price", 0)
            pnl = t.get("pnl")
            time_str = t.get("time", "")
            is_crypto = t.get("is_crypto", False)

            icon = "🪙" if is_crypto else "📈"
            side_icon = "🟢" if action == "BUY" else "🔴"

            line = f"{side_icon} <b>{action}</b> {icon} {symbol} — ${amount:,.2f} @ ${price:,.4g}"
            if pnl is not None:
                line += f" | P&L: ${pnl:+,.2f}"
            line += f"\n   <code>{time_str}</code>\n\n"
            msg += line

        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "💰 Profit", "callback_data": "profit"},
            ],
            [
                {"text": "🔄 Refresh", "callback_data": "trades"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_dashboard(self):
        """Send a link to the web dashboard."""
        url = getattr(_config, "DASHBOARD_URL", "") if _config else ""
        if url:
            msg = (
                "<b>📊 Live Dashboard</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Open your trading dashboard here:\n\n"
                f"<a href=\"{url}\">{url}</a>\n\n"
                "<i>The dashboard shows your portfolio, open positions, "
                "trade history, and strategy signals in real-time.</i>"
            )
        else:
            msg = (
                "<b>📊 Dashboard Not Configured</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "The dashboard URL hasn't been set yet.\n\n"
                "<b>To set it up:</b>\n"
                "1. Deploy <code>dashboard.py</code> to Streamlit Cloud (free)\n"
                "2. Copy the URL (e.g. <code>https://your-app.streamlit.app</code>)\n"
                "3. Set <code>DASHBOARD_URL</code> in <code>config.py</code>\n"
                "4. Push to GitHub — the bot will pick it up next cycle"
            )
        self._send_message(msg)

    def _send_help(self):
        """Send help message with command buttons."""
        msg = (
            "<b>Trading Bot Commands</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tap a button below or type a command:\n\n"
            "/status — Portfolio overview (open + closed P&amp;L)\n"
            "/positions — Open positions with live P&amp;L\n"
            "/profit — Full profit &amp; loss breakdown\n"
            "/trades — Recent trade history\n"
            "/balance — Account balance\n"
            "/dashboard — Open web dashboard\n"
            "/analyse — Performance analysis for strategy review\n"
            "/backtest all [DAYS] — Full portfolio backtest\n"
            "/backtest SYMBOL [DAYS] — Single symbol backtest\n"
            "/menu — Show buttons\n"
        )
        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📈 Positions", "callback_data": "positions"},
            ],
            [
                {"text": "💰 Profit", "callback_data": "profit"},
                {"text": "🔁 Trades", "callback_data": "trades"},
            ],
            [
                {"text": "🏦 Balance", "callback_data": "balance"},
                {"text": "🖥️ Dashboard", "callback_data": "dashboard"},
            ],
            [
                {"text": "🔬 Analyse Performance", "callback_data": "analyse"},
            ],
            [
                {"text": "📦 Full Portfolio Backtest", "callback_data": "backtest:ALL:365"},
                {"text": "🧪 Backtest AAPL", "callback_data": "backtest:AAPL:365"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_menu(self):
        """Send just the button menu."""
        msg = "<b>What would you like to see?</b>"
        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📈 Positions", "callback_data": "positions"},
            ],
            [
                {"text": "💰 Profit", "callback_data": "profit"},
                {"text": "🔁 Trades", "callback_data": "trades"},
            ],
            [
                {"text": "🏦 Balance", "callback_data": "balance"},
                {"text": "🖥️ Dashboard", "callback_data": "dashboard"},
            ],
            [
                {"text": "🔬 Analyse Performance", "callback_data": "analyse"},
            ],
            [
                {"text": "📦 Full Portfolio Backtest", "callback_data": "backtest:ALL:365"},
                {"text": "🧪 Backtest AAPL", "callback_data": "backtest:AAPL:365"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_status(self):
        """Send full portfolio overview including both open and realized P&L."""
        account = self.api.get_account()
        positions = self.api.get_all_positions()

        if not account:
            self._send_message("<b>Error:</b> Could not reach Alpaca API")
            return

        portfolio = float(account["portfolio_value"])
        cash = float(account["cash"])

        unrealized_pl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        total_invested = sum(float(p.get("market_value", 0)) for p in positions)
        num_positions = len(positions)

        # Realized P&L from closed trades (from state file)
        r = self._load_realized_stats()
        realized_pl = r["total_pnl"] if r else None
        closed_count = r["trade_count"] if r else 0

        total_pl = unrealized_pl + (realized_pl or 0.0)
        trend = "🟢" if total_pl > 0 else "🔴" if total_pl < 0 else "⚪"

        msg = (
            f"<b>{trend} Portfolio Overview</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Portfolio Value:</b> ${portfolio:,.2f}\n"
            f"<b>Cash:</b>           ${cash:,.2f}\n"
            f"<b>Invested:</b>       ${total_invested:,.2f} ({num_positions} positions)\n\n"
        )

        # P&L breakdown
        msg += f"<b>Unrealized P&L:</b>  ${unrealized_pl:+,.2f}  <i>(open positions)</i>\n"
        if realized_pl is not None:
            msg += f"<b>Realized P&L:</b>    ${realized_pl:+,.2f}  <i>({closed_count} closed trades)</i>\n"
            msg += f"<b>Total P&L:</b>       ${total_pl:+,.2f}\n"
        else:
            msg += "<i>Realized P&L: no closed trades yet</i>\n"

        msg += f"\n<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"

        buttons = [
            [
                {"text": "📈 Positions", "callback_data": "positions"},
                {"text": "💰 Profit", "callback_data": "profit"},
            ],
            [
                {"text": "🔬 Analyse", "callback_data": "analyse"},
                {"text": "🔄 Refresh", "callback_data": "status"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_positions(self):
        """Send detailed open positions list."""
        positions = self.api.get_all_positions()

        if not positions:
            msg = (
                "<b>📈 Open Positions</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "No open positions right now."
            )
            buttons = [[{"text": "📊 Status", "callback_data": "status"}]]
            self._send_message(msg, buttons)
            return

        # Sort by unrealized P&L (best first)
        positions.sort(key=lambda p: float(p.get("unrealized_pl", 0)), reverse=True)

        msg = (
            f"<b>📈 Open Positions ({len(positions)})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        for pos in positions:
            symbol = pos["symbol"]
            qty = float(pos.get("qty", 0))
            entry = float(pos.get("avg_entry_price", 0))
            current = float(pos.get("current_price", 0))
            pl = float(pos.get("unrealized_pl", 0))
            pl_pct = float(pos.get("unrealized_plpc", 0)) * 100
            market_val = float(pos.get("market_value", 0))

            icon = "🟢" if pl >= 0 else "🔴"

            msg += (
                f"{icon} <b>{symbol}</b>\n"
                f"   Qty: {qty:.4g} | Entry: ${entry:.2f} | Now: ${current:.2f}\n"
                f"   Value: ${market_val:,.2f} | P&L: ${pl:+,.2f} ({pl_pct:+.1f}%)\n\n"
            )

        msg += f"<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"

        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "🔄 Refresh", "callback_data": "positions"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_profit(self):
        """Send full P&L report: open positions + all realized closed trades."""
        account = self.api.get_account()
        positions = self.api.get_all_positions()

        if not account:
            self._send_message("<b>Error:</b> Could not reach Alpaca API")
            return

        # ---- Open positions (unrealized) ----
        total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        total_invested = sum(float(p.get("market_value", 0)) for p in positions)
        total_cost = sum(
            float(p.get("avg_entry_price", 0)) * float(p.get("qty", 0))
            for p in positions
        )
        open_winners = [p for p in positions if float(p.get("unrealized_pl", 0)) > 0]
        open_losers  = [p for p in positions if float(p.get("unrealized_pl", 0)) < 0]
        open_flat    = [p for p in positions if float(p.get("unrealized_pl", 0)) == 0]

        best  = max(positions, key=lambda p: float(p.get("unrealized_pl", 0))) if positions else None
        worst = min(positions, key=lambda p: float(p.get("unrealized_pl", 0))) if positions else None

        icon = "🟢" if total_unrealized >= 0 else "🔴"

        msg = (
            f"<b>{icon} Profit &amp; Loss Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>OPEN POSITIONS ({len(positions)})</b>\n"
            f"Unrealized P&amp;L: ${total_unrealized:+,.2f}\n"
            f"Invested: ${total_invested:,.2f}  |  Cost basis: ${total_cost:,.2f}\n"
            f"Winners: {len(open_winners)}  |  Losers: {len(open_losers)}  |  Flat: {len(open_flat)}\n"
        )
        if best and len(positions) > 1:
            bp = float(best.get("unrealized_plpc", 0)) * 100
            wp = float(worst.get("unrealized_plpc", 0)) * 100
            msg += f"Best: {best['symbol']} ({bp:+.1f}%)  |  Worst: {worst['symbol']} ({wp:+.1f}%)\n"

        # ---- Closed trades (realized) from state file ----
        r = self._load_realized_stats()
        if r:
            pf_str = f"{r['profit_factor']:.2f}x" if r["profit_factor"] != float("inf") else "∞"
            msg += (
                f"\n<b>CLOSED TRADES ({r['trade_count']})</b>\n"
                f"Realized P&amp;L: ${r['total_pnl']:+,.2f}\n"
                f"Win rate: {r['win_rate']:.1f}%  |  Profit factor: {pf_str}\n"
                f"Gross wins: ${r['gross_win']:+,.2f}  |  Gross losses: ${-r['gross_loss']:,.2f}\n"
                f"Avg/trade: {r['avg_pct_return']:+.2f}%\n"
            )
            total_combined = total_unrealized + r["total_pnl"]
            combined_icon = "🟢" if total_combined >= 0 else "🔴"
            msg += (
                f"\n<b>COMBINED ({combined_icon})</b>\n"
                f"Total P&amp;L: ${total_combined:+,.2f}\n"
            )
        else:
            msg += "\n<i>No closed trades recorded yet.</i>\n"

        msg += f"\n<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"

        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📈 Positions", "callback_data": "positions"},
            ],
            [
                {"text": "🔬 Analyse", "callback_data": "analyse"},
                {"text": "🔄 Refresh", "callback_data": "profit"},
            ],
        ]
        self._send_message(msg, buttons)

    # ------------------------------------------------------------------
    # REALIZED STATS HELPER
    # ------------------------------------------------------------------

    def _load_realized_stats(self):
        """
        Compute realized P&L stats from the bot_state.json trade log.
        Returns a dict with trade metrics, or None if no data is available
        (e.g. no state file, or no closed trades yet).
        """
        import os
        import json as _json

        try:
            if not os.path.exists("bot_state.json"):
                return None
            with open("bot_state.json") as f:
                state_data = _json.load(f)
        except Exception as e:
            logger.debug(f"Could not read bot_state.json for stats: {e}")
            return None

        sells = [
            t for t in state_data.get("trade_log", [])
            if t.get("action") == "SELL" and "pnl" in t
        ]
        if not sells:
            return None

        total_pnl  = sum(t["pnl"] for t in sells)
        wins       = [t for t in sells if t["pnl"] > 0]
        losses     = [t for t in sells if t["pnl"] <= 0]
        win_rate   = len(wins) / len(sells) * 100
        gross_win  = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

        # Approximate % return per trade: pnl / entry_value
        pct_returns = []
        for t in sells:
            entry_val = t.get("amount", 0) - t["pnl"]
            if entry_val > 1:
                pct_returns.append(t["pnl"] / entry_val * 100)

        avg_pct = sum(pct_returns) / len(pct_returns) if pct_returns else 0.0

        # Simplified annualised Sharpe (assumes avg 5-day hold)
        sharpe = 0.0
        if len(pct_returns) >= 2:
            mean_r = avg_pct
            var_r  = sum((r - mean_r) ** 2 for r in pct_returns) / len(pct_returns)
            std_r  = var_r ** 0.5
            if std_r > 0:
                sharpe = (mean_r / std_r) * (252 / 5) ** 0.5

        # Exit-reason categories
        reasons = {}
        for t in sells:
            r = t.get("reason", "").lower()
            if "hard_stop" in r or ("stop" in r and "trail" not in r):
                cat = "hard_stop"
            elif "take_profit" in r or "take profit" in r:
                cat = "take_profit"
            elif "trail" in r:
                cat = "trailing_stop"
            else:
                cat = "strategy"
            reasons[cat] = reasons.get(cat, 0) + 1

        # Per-symbol stats
        by_symbol = {}
        for t in sells:
            sym = t["symbol"]
            s = by_symbol.setdefault(sym, {"pnl": 0.0, "trades": 0, "wins": 0})
            s["pnl"]    += t["pnl"]
            s["trades"] += 1
            if t["pnl"] > 0:
                s["wins"] += 1

        # Recent 10 trades vs all-time
        recent = sells[-10:]
        recent_wins    = sum(1 for t in recent if t["pnl"] > 0)
        recent_wr      = recent_wins / len(recent) * 100 if recent else 0.0
        recent_pcts    = []
        for t in recent:
            ev = t.get("amount", 0) - t["pnl"]
            if ev > 1:
                recent_pcts.append(t["pnl"] / ev * 100)
        recent_avg_pct = sum(recent_pcts) / len(recent_pcts) if recent_pcts else 0.0

        return {
            "total_pnl":      total_pnl,
            "trade_count":    len(sells),
            "win_rate":       win_rate,
            "profit_factor":  pf,
            "gross_win":      gross_win,
            "gross_loss":     gross_loss,
            "avg_pct_return": avg_pct,
            "sharpe":         sharpe,
            "reasons":        reasons,
            "by_symbol":      by_symbol,
            "recent_win_rate":  recent_wr,
            "recent_avg_pct":   recent_avg_pct,
            "recent_count":     len(recent),
        }

    # ------------------------------------------------------------------
    # PERFORMANCE ANALYSIS
    # ------------------------------------------------------------------

    def _send_analyse(self):
        """
        Send a comprehensive performance analysis designed for strategy review.
        Covers metrics, trends, exit quality, per-symbol P&L, and
        auto-generated analyst flags with specific config change suggestions.
        """
        import os
        import json as _json

        stats   = self._load_realized_stats()
        account = self.api.get_account()
        positions = self.api.get_all_positions() or []

        portfolio_val = float(account.get("portfolio_value", 0)) if account else 0.0
        cash_val      = float(account.get("cash", 0)) if account else 0.0
        invested_val  = sum(float(p.get("market_value", 0)) for p in positions)
        unrealized_pl = sum(float(p.get("unrealized_pl", 0)) for p in positions)

        # Peak value for drawdown
        peak_val = portfolio_val
        try:
            if os.path.exists("bot_state.json"):
                with open("bot_state.json") as f:
                    sd = _json.load(f)
                peak_val = sd.get("peak_portfolio_value") or portfolio_val
        except Exception:
            pass
        drawdown_pct = (peak_val - portfolio_val) / peak_val * 100 if peak_val > 0 else 0.0

        # ---- No trade history yet ----
        if not stats:
            msg = (
                "<b>📊 Bot Performance Analysis</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "<i>No closed trades yet — run the bot for a while, then check back.</i>\n\n"
            )
            if account:
                cash_pct = cash_val / portfolio_val * 100 if portfolio_val > 0 else 0
                inv_pct  = invested_val / portfolio_val * 100 if portfolio_val > 0 else 0
                msg += (
                    f"<b>Current Exposure</b>\n"
                    f"Portfolio: ${portfolio_val:,.0f} | Drawdown: {drawdown_pct:.1f}%\n"
                    f"Positions: {len(positions)}\n"
                    f"Invested: {inv_pct:.0f}%  |  Cash: {cash_pct:.0f}%\n"
                    f"Unrealized P&amp;L: ${unrealized_pl:+,.2f}"
                )
            self._send_message(msg)
            return

        # ---- Core metrics ----
        n        = stats["trade_count"]
        wr       = stats["win_rate"]
        pf       = stats["profit_factor"]
        sharpe   = stats["sharpe"]
        avg_pct  = stats["avg_pct_return"]
        total_pl = stats["total_pnl"]

        def _grade(val, ok, good, label_low="low", label_ok="ok", label_good="good"):
            return label_good if val >= good else label_ok if val >= ok else label_low

        wr_grade     = _grade(wr, 50, 60, "⚠️ low", "ok", "✅ good")
        pf_grade     = _grade(pf if pf != float("inf") else 99, 1.2, 1.5, "⚠️ low", "ok", "✅ good")
        sharpe_grade = _grade(sharpe, 0.5, 1.0, "⚠️ low", "ok", "✅ good")

        pf_str = f"{pf:.2f}x" if pf != float("inf") else "∞"

        # ---- Trend: last N vs all-time ----
        nc       = stats["recent_count"]
        t_wr     = stats["recent_win_rate"]
        t_avg    = stats["recent_avg_pct"]
        wr_arr   = "▲" if t_wr  > wr + 2     else "▼" if t_wr  < wr - 2     else "→"
        avg_arr  = "▲" if t_avg > avg_pct + 0.2 else "▼" if t_avg < avg_pct - 0.2 else "→"

        # ---- Exit reason breakdown ----
        reasons   = stats["reasons"]
        total_ex  = sum(reasons.values())
        strat_pct = reasons.get("strategy", 0) / total_ex * 100 if total_ex else 0
        stop_pct  = reasons.get("hard_stop", 0) / total_ex * 100 if total_ex else 0
        tp_pct    = reasons.get("take_profit", 0) / total_ex * 100 if total_ex else 0
        trail_pct = reasons.get("trailing_stop", 0) / total_ex * 100 if total_ex else 0

        # ---- Per-symbol P&L ----
        ranked = sorted(stats["by_symbol"].items(), key=lambda x: x[1]["pnl"], reverse=True)
        top3 = ranked[:3]
        bot3 = ranked[-3:]

        def _sym_line(sym, s):
            wr_s = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            pnl_s = f"${s['pnl']:+,.0f}"
            return f"  {sym:<7} {pnl_s:<10} {s['trades']} trades  {wr_s:.0f}% win\n"

        top_lines = "".join(_sym_line(s, d) for s, d in top3)
        bot_lines = "".join(_sym_line(s, d) for s, d in reversed(bot3))

        # ---- Analyst flags (actionable) ----
        cfg  = _config
        stop_thresh  = getattr(cfg, "HARD_STOP_LOSS_PERCENT", 8)
        tp_thresh    = getattr(cfg, "HARD_TAKE_PROFIT_PERCENT", 15)
        rsi_oversold = getattr(cfg, "RSI_OVERSOLD", 30)

        flags = []
        if wr < 45:
            flags.append(f"Win rate {wr:.0f}% is critical — raise entry score threshold above 0.35 or tighten RSI_OVERSOLD from {rsi_oversold} → {rsi_oversold + 5}")
        elif wr < 50:
            flags.append(f"Win rate {wr:.0f}% is below breakeven — monitor; consider raising RSI_OVERSOLD")
        if pf < 1.2 and pf != float("inf"):
            flags.append(f"Profit factor {pf:.2f}x — losers eat gains; reduce HARD_STOP_LOSS_PERCENT from {stop_thresh}% → {max(4, stop_thresh - 2)}%")
        if sharpe < 0.5:
            flags.append(f"Sharpe {sharpe:.2f} is very low — returns too choppy; lower MAX_POSITION_WEIGHT or reduce RISK_PER_TRADE")
        if stop_pct > 40:
            flags.append(f"Hard stops at {stop_pct:.0f}% of exits — strategy enters too early; raise BUY score threshold or lower ATR stop multiplier")
        if tp_pct < 5 and n >= 15:
            flags.append(f"Only {tp_pct:.0f}% of exits hit take-profit — targets too wide; consider lowering HARD_TAKE_PROFIT_PERCENT from {tp_thresh}% → {max(8, tp_thresh - 3)}%")
        if wr_arr == "▼" and avg_arr == "▼":
            flags.append("Recent win rate AND avg return both declining — possible regime shift; check STRATEGY_WEIGHTS (increase mean_reversion weight?)")
        if not flags:
            flags.append("No critical issues detected — strategy operating within healthy parameters")

        flags_str = "\n".join(f"• {f}" for f in flags)

        # ---- Current exposure ----
        cash_pct = cash_val / portfolio_val * 100 if portfolio_val > 0 else 0
        inv_pct  = invested_val / portfolio_val * 100 if portfolio_val > 0 else 0

        msg = (
            f"<b>📊 Bot Performance Analysis</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Based on {n} closed trades  |  {_now().strftime('%d %b %H:%M %Z')}\n\n"

            f"<b>📈 Performance</b>\n"
            f"Win rate:        {wr:.1f}%  ({wr_grade})\n"
            f"Profit factor:   {pf_str}  ({pf_grade})\n"
            f"Realized P&amp;L:    ${total_pl:+,.2f}\n"
            f"Avg return/trade: {avg_pct:+.2f}%\n"
            f"Sharpe (est.):   {sharpe:.2f}  ({sharpe_grade})\n\n"

            f"<b>📅 Trend  (last {nc} vs all-time)</b>\n"
            f"Win rate:   {t_wr:.0f}% vs {wr:.0f}%  {wr_arr}\n"
            f"Avg/trade:  {t_avg:+.1f}% vs {avg_pct:+.1f}%  {avg_arr}\n\n"

            f"<b>🚪 Exit Breakdown</b>\n"
            f"Strategy: {strat_pct:.0f}%  |  Hard stop: {stop_pct:.0f}%"
            + (f"  |  Trail: {trail_pct:.0f}%" if trail_pct > 0 else "")
            + f"  |  Take-profit: {tp_pct:.0f}%\n\n"

            f"<b>🏆 Best Symbols</b>\n<code>{top_lines}</code>"
            f"<b>💔 Weakest Symbols</b>\n<code>{bot_lines}</code>\n"

            f"<b>⚠️ Analyst Flags</b>\n{flags_str}\n\n"

            f"<b>📊 Current Exposure</b>\n"
            f"Portfolio: ${portfolio_val:,.0f}  |  Drawdown: {drawdown_pct:.1f}%\n"
            f"Invested: {inv_pct:.0f}%  |  Cash: {cash_pct:.0f}%  |  Positions: {len(positions)}\n"
            f"Unrealized: ${unrealized_pl:+,.2f}  |  Total P&amp;L: ${total_pl + unrealized_pl:+,.2f}"
        )

        buttons = [
            [
                {"text": "🔄 Refresh", "callback_data": "analyse"},
                {"text": "📊 Status", "callback_data": "status"},
            ],
            [
                {"text": "📦 Full Backtest", "callback_data": "backtest:ALL:365"},
            ],
        ]
        self._send_message(msg, buttons)

    def _run_backtest(self, symbol: str, days: int):
        """Run walk-forward backtest in a background thread and send results to Telegram."""
        self._send_message(
            f"<b>🧪 Running backtest…</b>\n"
            f"Symbol: <b>{symbol}</b> | Period: <b>{days} days</b>\n"
            "<i>This may take 10–30 seconds.</i>"
        )

        def _worker():
            try:
                from backtest import backtest as _backtest
                _df, m = _backtest(symbol, days, print_results=False, api=self.api)
            except Exception as exc:
                logger.warning(f"Backtest error for {symbol}: {exc}")
                self._send_message(f"<b>Backtest failed</b> for {symbol}:\n<code>{exc}</code>")
                return

            self._send_backtest_result(symbol, days, m)

        t = threading.Thread(target=_worker, daemon=False, name=f"backtest-{symbol}")
        t.start()

    def _send_backtest_result(self, symbol: str, days: int, m):
        """Format and send the backtest result message."""

        if m is None:
            self._send_message(
                f"<b>No trades</b> generated for <b>{symbol}</b> over {days} days.\n"
                "Try a longer period or a different symbol."
            )
            return

        # Format exit reasons
        reasons = m["exit_reasons"]
        reason_parts = []
        for label, key in [("Strategy", "strategy"), ("Stop", "hard_stop"), ("TP", "take_profit")]:
            n = reasons.get(key, 0)
            if n:
                reason_parts.append(f"{label}: {n}")
        reason_str = " | ".join(reason_parts) if reason_parts else "—"

        # Format regime breakdown
        regime_lines = ""
        for regime, stats in sorted(m["regime_stats"].items()):
            regime_lines += f"  {regime:<10} {stats['trades']:>3} trades  {stats['win_rate']:.0f}% win\n"

        pnl_sign = "+" if m["total_pnl"] >= 0 else ""
        pf_str = f"{m['profit_factor']:.2f}x" if m["profit_factor"] != float("inf") else "∞"

        msg = (
            f"<b>🧪 Backtest — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Period: {days} days | Capital: ${m['initial_capital']:,.0f}\n\n"
            f"<b>📊 Summary</b>\n"
            f"Trades:        {m['trade_count']}\n"
            f"Win rate:      {m['win_rate']:.1f}%\n"
            f"Profit factor: {pf_str}\n"
            f"Total P&amp;L:     {pnl_sign}${m['total_pnl']:,.2f} ({pnl_sign}{m['total_pnl_pct']:.1f}%)\n"
            f"Avg winner:    +{m['avg_win_pct']:.2f}%\n"
            f"Avg loser:     {m['avg_loss_pct']:.2f}%\n"
            f"Sharpe:        {m['sharpe']:.2f}\n"
            f"Max drawdown:  ${m['max_drawdown']:,.2f}\n"
            f"Avg hold:      {m['avg_hold_days']:.1f} days\n"
            f"Final capital: ${m['final_capital']:,.2f}\n\n"
            f"<b>🚪 Exit reasons</b>\n{reason_str}\n\n"
            f"<b>🌊 By regime</b>\n<code>{regime_lines}</code>"
        )
        buttons = [
            [
                {"text": "🔄 Re-run", "callback_data": f"backtest:{symbol}:{days}"},
                {"text": "📊 Status", "callback_data": "status"},
            ],
        ]
        self._send_message(msg, buttons)

    def _run_portfolio_backtest(self, days: int):
        """
        Run walk-forward backtest for ALL symbols in the stock universe in parallel.
        Sends an aggregate summary showing best/worst performers and overall metrics.
        """
        universe = getattr(_config, "STOCK_UNIVERSE", []) if _config else []
        if not universe:
            self._send_message("<b>Error:</b> No STOCK_UNIVERSE defined in config.")
            return

        count = len(universe)
        self._send_message(
            f"<b>📦 Running Full Portfolio Backtest…</b>\n"
            f"Testing <b>{count} symbols</b> over <b>{days} days</b>\n"
            f"<i>Running in parallel — expect results in ~60 seconds.</i>"
        )

        def _worker():
            from backtest import backtest as _backtest
            from concurrent.futures import ThreadPoolExecutor, as_completed

            results = {}

            def _run_one(sym):
                try:
                    _df, m = _backtest(sym, days, print_results=False, api=self.api)
                    return sym, m
                except Exception as exc:
                    logger.debug(f"Portfolio backtest skipped {sym}: {exc}")
                    return sym, None

            # 5 parallel workers — stays well within Alpaca rate limits
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(_run_one, sym): sym for sym in universe}
                for future in as_completed(futures):
                    sym, m = future.result()
                    if m is not None:
                        results[sym] = m

            self._send_portfolio_backtest_result(results, days, universe)

        t = threading.Thread(target=_worker, daemon=False, name="portfolio-backtest")
        t.start()

    def _send_portfolio_backtest_result(self, results: dict, days: int, universe: list):
        """Format and send the portfolio-level backtest summary."""
        if not results:
            self._send_message(
                "<b>No portfolio backtest results.</b>\n"
                "All symbols had insufficient data. Try a longer period."
            )
            return

        tested = len(results)
        total_symbols = len(universe)
        no_data = total_symbols - tested

        # Aggregate across all symbols
        all_trades = sum(m["trade_count"] for m in results.values())
        all_wins = sum(
            round(m["trade_count"] * m["win_rate"] / 100)
            for m in results.values()
        )
        agg_win_rate = (all_wins / all_trades * 100) if all_trades > 0 else 0.0
        avg_pnl_pct = sum(m["total_pnl_pct"] for m in results.values()) / tested
        avg_sharpe = sum(m["sharpe"] for m in results.values()) / tested
        avg_maxdd_pct = sum(
            m["max_drawdown"] / m["initial_capital"] * 100
            for m in results.values()
        ) / tested
        profitable_count = sum(1 for m in results.values() if m["total_pnl_pct"] > 0)

        # Rank symbols by P&L%
        ranked = sorted(results.items(), key=lambda x: x[1]["total_pnl_pct"], reverse=True)
        top5 = ranked[:5]
        bottom5 = ranked[-5:]

        top_lines = ""
        for sym, m in top5:
            top_lines += f"  {sym:<6} {m['total_pnl_pct']:+.1f}%  win={m['win_rate']:.0f}%  n={m['trade_count']}\n"

        bottom_lines = ""
        for sym, m in reversed(bottom5):
            bottom_lines += f"  {sym:<6} {m['total_pnl_pct']:+.1f}%  win={m['win_rate']:.0f}%  n={m['trade_count']}\n"

        no_data_str = f" ({no_data} had no data)" if no_data else ""

        msg = (
            f"<b>📦 Portfolio Backtest — {days} days</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Symbols tested: {tested}/{total_symbols}{no_data_str}\n\n"
            f"<b>📊 Aggregate Results</b>\n"
            f"Total trades:    {all_trades}\n"
            f"Win rate:        {agg_win_rate:.1f}%\n"
            f"Avg P&amp;L/symbol: {avg_pnl_pct:+.1f}%\n"
            f"Avg Sharpe:      {avg_sharpe:.2f}\n"
            f"Avg max DD:      {avg_maxdd_pct:.1f}%\n"
            f"Profitable:      {profitable_count}/{tested} symbols\n\n"
            f"<b>🏆 Top 5 Performers</b>\n<code>{top_lines}</code>\n"
            f"<b>💀 Bottom 5 Performers</b>\n<code>{bottom_lines}</code>"
            f"\n<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"
        )
        buttons = [
            [
                {"text": "🔄 Re-run", "callback_data": f"backtest:ALL:{days}"},
                {"text": "📊 Status", "callback_data": "status"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_balance(self):
        """Send detailed account balance."""
        account = self.api.get_account()
        positions = self.api.get_all_positions()

        if not account:
            self._send_message("<b>Error:</b> Could not reach Alpaca API")
            return

        portfolio = account["portfolio_value"]
        cash = account["cash"]
        buying_power = account["buying_power"]
        equity = account["equity"]

        total_invested = sum(float(p.get("market_value", 0)) for p in positions)
        cash_pct = (cash / portfolio * 100) if portfolio > 0 else 0
        invested_pct = (total_invested / portfolio * 100) if portfolio > 0 else 0

        mode = "PAPER" if account.get("trading_blocked") is False else "UNKNOWN"
        pdt = "Yes" if account.get("pattern_day_trader") else "No"

        msg = (
            f"<b>🏦 Account Balance</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Portfolio Value:</b> ${portfolio:,.2f}\n"
            f"<b>Equity:</b> ${equity:,.2f}\n"
            f"<b>Cash:</b> ${cash:,.2f} ({cash_pct:.0f}%)\n"
            f"<b>Invested:</b> ${total_invested:,.2f} ({invested_pct:.0f}%)\n"
            f"<b>Buying Power:</b> ${buying_power:,.2f}\n\n"
            f"<b>Positions:</b> {len(positions)}\n"
            f"<b>Pattern Day Trader:</b> {pdt}\n"
            f"\n<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"
        )

        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📈 Positions", "callback_data": "positions"},
            ],
            [
                {"text": "🔄 Refresh", "callback_data": "balance"},
            ],
        ]
        self._send_message(msg, buttons)

    # ------------------------------------------------------------------
    # TELEGRAM API HELPERS
    # ------------------------------------------------------------------

    def _send_message(self, text, buttons=None):
        """Send a message with optional inline keyboard."""
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        if buttons:
            payload["reply_markup"] = json.dumps({
                "inline_keyboard": buttons
            })

        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Telegram send failed: {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram send error: {e}")

    def _answer_callback(self, callback_id):
        """Acknowledge a callback query (removes loading spinner on button)."""
        try:
            requests.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=5,
            )
        except Exception:
            pass


def send_startup_menu(bot_token, chat_id):
    """
    Register bot commands and send the interactive menu on bot startup.
    Can be called standalone to set up the menu.
    Returns True on success, False on failure.
    """
    if not bot_token or bot_token in ("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE"):
        logger.debug("send_startup_menu: token not configured — skipping")
        return False

    base_url = f"https://api.telegram.org/bot{bot_token}"

    # Set bot commands (shows in Telegram's command menu)
    commands = [
        {"command": "status", "description": "Portfolio overview"},
        {"command": "positions", "description": "Open positions with P&L"},
        {"command": "profit", "description": "Profit & loss breakdown"},
        {"command": "trades", "description": "Recent buy/sell history"},
        {"command": "balance", "description": "Account balance details"},
        {"command": "dashboard", "description": "Open web dashboard"},
        {"command": "analyse", "description": "Performance analysis for strategy review"},
        {"command": "backtest", "description": "Backtest: /backtest all 365 or /backtest AAPL 252"},
        {"command": "menu", "description": "Show button menu"},
        {"command": "help", "description": "List all commands"},
    ]

    try:
        resp = requests.post(
            f"{base_url}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            logger.info("Telegram command menu registered successfully (/backtest and others active)")
            return True
        else:
            logger.warning(f"setMyCommands failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"send_startup_menu error: {e}")
        return False
