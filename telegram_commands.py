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
        elif text.startswith("/backtest"):
            # /backtest [SYMBOL [DAYS]]
            parts = text.split()
            symbol = parts[1].upper() if len(parts) > 1 else "AAPL"
            try:
                days = int(parts[2]) if len(parts) > 2 else 365
                days = max(60, min(days, 1000))  # clamp to sane range
            except ValueError:
                self._send_message("<b>Usage:</b> /backtest SYMBOL [DAYS]\nExample: <code>/backtest AAPL 365</code>")
                return
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
        elif data.startswith("backtest:"):
            # callback_data format: "backtest:AAPL:365"
            parts = data.split(":")
            symbol = parts[1] if len(parts) > 1 else "AAPL"
            try:
                days = int(parts[2]) if len(parts) > 2 else 365
            except ValueError:
                days = 365
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
            "/status — Portfolio overview\n"
            "/positions — Open positions\n"
            "/profit — Profit &amp; loss\n"
            "/trades — Recent trade history\n"
            "/balance — Account balance\n"
            "/dashboard — Open web dashboard\n"
            "/backtest SYMBOL [DAYS] — Walk-forward backtest\n"
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
                {"text": "🧪 Backtest AAPL", "callback_data": "backtest:AAPL:365"},
            ],
        ]
        self._send_message(msg, buttons)

    def _send_status(self):
        """Send full portfolio overview."""
        account = self.api.get_account()
        positions = self.api.get_all_positions()

        if not account:
            self._send_message("<b>Error:</b> Could not reach Alpaca API")
            return

        portfolio = account["portfolio_value"]
        cash = account["cash"]
        equity = account["equity"]

        total_pl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        total_invested = sum(float(p.get("market_value", 0)) for p in positions)
        num_positions = len(positions)

        # Determine overall trend
        if total_pl > 0:
            trend = "🟢"
        elif total_pl < 0:
            trend = "🔴"
        else:
            trend = "⚪"

        msg = (
            f"<b>{trend} Portfolio Overview</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Portfolio Value:</b> ${portfolio:,.2f}\n"
            f"<b>Cash Available:</b> ${cash:,.2f}\n"
            f"<b>Invested:</b> ${total_invested:,.2f}\n"
            f"<b>Open Positions:</b> {num_positions}\n"
            f"<b>Unrealized P&L:</b> ${total_pl:+,.2f}\n"
            f"\n<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"
        )

        buttons = [
            [
                {"text": "📈 Positions", "callback_data": "positions"},
                {"text": "💰 Profit", "callback_data": "profit"},
            ],
            [
                {"text": "🏦 Balance", "callback_data": "balance"},
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
        """Send profit and loss summary."""
        account = self.api.get_account()
        positions = self.api.get_all_positions()

        if not account:
            self._send_message("<b>Error:</b> Could not reach Alpaca API")
            return

        portfolio = account["portfolio_value"]
        cash = account["cash"]

        # Calculate totals
        total_unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        total_invested = sum(float(p.get("market_value", 0)) for p in positions)
        total_cost = sum(
            float(p.get("avg_entry_price", 0)) * float(p.get("qty", 0))
            for p in positions
        )

        # Winners vs losers
        winners = [p for p in positions if float(p.get("unrealized_pl", 0)) > 0]
        losers = [p for p in positions if float(p.get("unrealized_pl", 0)) < 0]
        flat = [p for p in positions if float(p.get("unrealized_pl", 0)) == 0]

        total_wins = sum(float(p["unrealized_pl"]) for p in winners)
        total_losses = sum(float(p["unrealized_pl"]) for p in losers)

        # Best and worst
        best = max(positions, key=lambda p: float(p.get("unrealized_pl", 0))) if positions else None
        worst = min(positions, key=lambda p: float(p.get("unrealized_pl", 0))) if positions else None

        icon = "🟢" if total_unrealized >= 0 else "🔴"

        msg = (
            f"<b>{icon} Profit & Loss Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Unrealized P&L:</b> ${total_unrealized:+,.2f}\n"
            f"<b>Total Invested:</b> ${total_invested:,.2f}\n"
            f"<b>Total Cost Basis:</b> ${total_cost:,.2f}\n\n"
            f"<b>Winners:</b> {len(winners)} (+${total_wins:,.2f})\n"
            f"<b>Losers:</b> {len(losers)} (${total_losses:,.2f})\n"
            f"<b>Flat:</b> {len(flat)}\n"
        )

        if best:
            bp = float(best.get("unrealized_plpc", 0)) * 100
            msg += f"\n<b>Best:</b> {best['symbol']} ({bp:+.1f}%)"
        if worst:
            wp = float(worst.get("unrealized_plpc", 0)) * 100
            msg += f"\n<b>Worst:</b> {worst['symbol']} ({wp:+.1f}%)"

        msg += f"\n\n<code>{_now().strftime('%Y-%m-%d %H:%M %Z')}</code>"

        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📈 Positions", "callback_data": "positions"},
            ],
            [
                {"text": "🔄 Refresh", "callback_data": "profit"},
            ],
        ]
        self._send_message(msg, buttons)

    def _run_backtest(self, symbol: str, days: int):
        """Run walk-forward backtest and send results to Telegram."""
        self._send_message(
            f"<b>🧪 Running backtest…</b>\n"
            f"Symbol: <b>{symbol}</b> | Period: <b>{days} days</b>\n"
            "<i>This may take 10–30 seconds.</i>"
        )
        try:
            from backtest import backtest as _backtest
            _df, m = _backtest(symbol, days, print_results=False, api=self.api)
        except Exception as exc:
            logger.warning(f"Backtest error for {symbol}: {exc}")
            self._send_message(f"<b>Backtest failed</b> for {symbol}:\n<code>{exc}</code>")
            return

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
    Send the interactive menu on bot startup.
    Can be called standalone to set up the menu.
    """
    base_url = f"https://api.telegram.org/bot{bot_token}"

    # Set bot commands (shows in Telegram's command menu)
    commands = [
        {"command": "status", "description": "Portfolio overview"},
        {"command": "positions", "description": "Open positions with P&L"},
        {"command": "profit", "description": "Profit & loss breakdown"},
        {"command": "trades", "description": "Recent buy/sell history"},
        {"command": "balance", "description": "Account balance details"},
        {"command": "dashboard", "description": "Open web dashboard"},
        {"command": "backtest", "description": "Walk-forward backtest: /backtest AAPL 365"},
        {"command": "menu", "description": "Show button menu"},
        {"command": "help", "description": "List all commands"},
    ]

    try:
        requests.post(
            f"{base_url}/setMyCommands",
            json={"commands": commands},
            timeout=10,
        )
    except Exception:
        pass
