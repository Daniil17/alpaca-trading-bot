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
import requests
from datetime import datetime

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
        elif text == "/menu":
            self._send_menu()
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
        elif data == "help":
            self._send_help()

    # ------------------------------------------------------------------
    # RESPONSE HANDLERS
    # ------------------------------------------------------------------

    def _send_help(self):
        """Send help message with command buttons."""
        msg = (
            "<b>Trading Bot Commands</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tap a button below or type a command:\n\n"
            "/status — Portfolio overview\n"
            "/positions — Open positions\n"
            "/profit — Profit & loss\n"
            "/balance — Account balance\n"
            "/menu — Show buttons\n"
        )
        buttons = [
            [
                {"text": "📊 Status", "callback_data": "status"},
                {"text": "📈 Positions", "callback_data": "positions"},
            ],
            [
                {"text": "💰 Profit", "callback_data": "profit"},
                {"text": "🏦 Balance", "callback_data": "balance"},
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
                {"text": "🏦 Balance", "callback_data": "balance"},
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
            f"\n<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>"
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

        msg += f"<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>"

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

        msg += f"\n\n<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>"

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
            f"\n<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>"
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
        {"command": "balance", "description": "Account balance details"},
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
