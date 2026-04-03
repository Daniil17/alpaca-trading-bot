"""
TELEGRAM NOTIFICATIONS
=======================
Sends trade alerts, daily summaries, and error notifications
to your Telegram chat. Uses the Telegram Bot API directly
(no extra libraries needed beyond 'requests').
"""

import logging
import requests
from datetime import datetime

logger = logging.getLogger("TradingBot")


class TelegramNotifier:
    """Sends formatted messages to Telegram."""

    def __init__(self, bot_token, chat_id):
        """
        Args:
            bot_token: Telegram bot token from @BotFather
            chat_id: Your Telegram chat ID
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = (
            bot_token != "YOUR_TELEGRAM_BOT_TOKEN_HERE"
            and chat_id != "YOUR_CHAT_ID_HERE"
        )

        if not self.enabled:
            logger.warning("Telegram not configured — notifications disabled")

    def send(self, message, parse_mode="HTML"):
        """
        Send a message to Telegram.

        Args:
            message: text to send (supports HTML formatting)
            parse_mode: "HTML" or "Markdown"
        """
        if not self.enabled:
            return

        try:
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if response.status_code != 200:
                logger.warning(f"Telegram send failed: {response.text}")
        except Exception as e:
            logger.warning(f"Telegram error: {e}")

    # ------------------------------------------------------------------
    # FORMATTED NOTIFICATIONS
    # ------------------------------------------------------------------

    def notify_buy(self, symbol, qty_or_notional, price, strategy_info):
        """Send a buy order notification."""
        msg = (
            f"<b>BUY ORDER PLACED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> {symbol}\n"
            f"<b>Amount:</b> ${qty_or_notional:.2f}\n"
            f"<b>Price:</b> ${price:.2f}\n"
            f"<b>Signal:</b> {strategy_info.get('signal', 'N/A')}\n"
            f"<b>Score:</b> {strategy_info.get('combined_score', 0):.3f}\n"
            f"\n<i>Strategy breakdown:</i>\n"
        )

        strategies = strategy_info.get("strategies", {})
        for name, data in strategies.items():
            score = data.get("score", 0)
            emoji = "+" if score > 0 else ""
            msg += f"  {name}: {emoji}{score:.2f}\n"

        msg += f"\n<code>{datetime.now().strftime('%H:%M:%S')}</code>"
        self.send(msg)

    def notify_sell(self, symbol, reason, pnl=None):
        """Send a sell/close notification."""
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "pending"
        emoji = "+" if pnl and pnl > 0 else ""
        msg = (
            f"<b>POSITION CLOSED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> {symbol}\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>P&L:</b> {pnl_str}\n"
            f"\n<code>{datetime.now().strftime('%H:%M:%S')}</code>"
        )
        self.send(msg)

    def notify_stop_loss(self, symbol, loss_pct, unrealized_pl):
        """Send a stop-loss trigger notification."""
        msg = (
            f"<b>STOP-LOSS TRIGGERED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> {symbol}\n"
            f"<b>Loss:</b> {loss_pct:.1f}%\n"
            f"<b>P&L:</b> ${unrealized_pl:.2f}\n"
            f"\n<code>{datetime.now().strftime('%H:%M:%S')}</code>"
        )
        self.send(msg)

    def notify_daily_summary(self, account_info, positions, risk_summary):
        """Send end-of-day portfolio summary."""
        msg = (
            f"<b>DAILY SUMMARY</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Portfolio:</b> ${account_info.get('portfolio_value', 0):,.2f}\n"
            f"<b>Cash:</b> ${account_info.get('cash', 0):,.2f}\n"
            f"<b>Positions:</b> {risk_summary.get('total_positions', 0)}\n"
            f"<b>Unrealized P&L:</b> ${risk_summary.get('unrealized_pl', 0):+,.2f}\n"
            f"<b>Drawdown:</b> {risk_summary.get('drawdown_pct', 0):.1f}%\n"
            f"<b>Cash Reserve:</b> {risk_summary.get('cash_reserve_pct', 0):.0f}%\n"
        )

        if positions:
            msg += f"\n<i>Open Positions:</i>\n"
            for pos in positions[:10]:
                symbol = pos.get("symbol", "?")
                pl = float(pos.get("unrealized_pl", 0))
                pl_pct = float(pos.get("unrealized_plpc", 0)) * 100
                emoji = "+" if pl >= 0 else ""
                msg += f"  {symbol}: {emoji}{pl_pct:.1f}% (${pl:+.2f})\n"

        sectors = risk_summary.get("sectors", {})
        if sectors:
            msg += f"\n<i>Sector Exposure:</i>\n"
            for sector, count in sorted(sectors.items()):
                msg += f"  {sector}: {count} position(s)\n"

        msg += (
            f"\n<b>Peak Value:</b> ${risk_summary.get('peak_value', 0):,.2f}\n"
            f"<code>{datetime.now().strftime('%Y-%m-%d %H:%M')}</code>"
        )
        self.send(msg)

    def notify_error(self, error_message):
        """Send an error notification."""
        msg = (
            f"<b>BOT ERROR</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{error_message}\n"
            f"\n<code>{datetime.now().strftime('%H:%M:%S')}</code>"
        )
        self.send(msg)

    def notify_bot_started(self, mode, settings_summary):
        """Send bot startup notification."""
        msg = (
            f"<b>BOT STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"<b>Mode:</b> {'PAPER' if mode else 'LIVE'}\n"
            f"{settings_summary}\n"
            f"\n<code>{datetime.now().strftime('%Y-%m-%d %H:%M')}</code>"
        )
        self.send(msg)

    def notify_drawdown_breaker(self, drawdown_pct, peak_value, current_value):
        """Send drawdown circuit breaker alert."""
        msg = (
            f"<b>DRAWDOWN CIRCUIT BREAKER</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Trading PAUSED — portfolio drawdown exceeded limit.\n"
            f"<b>Drawdown:</b> {drawdown_pct:.1f}%\n"
            f"<b>Peak:</b> ${peak_value:,.2f}\n"
            f"<b>Current:</b> ${current_value:,.2f}\n"
            f"\nBot will resume buying when portfolio recovers.\n"
            f"\n<code>{datetime.now().strftime('%H:%M:%S')}</code>"
        )
        self.send(msg)
