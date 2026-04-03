"""
STATE PERSISTENCE
==================
Saves and loads bot state between GitHub Actions runs.
Each run is a fresh process, so we store things like:
  - peak_portfolio_value (for drawdown tracking)
  - manual_symbols (stocks to never touch)
  - last_summary_date (to avoid duplicate daily summaries)
  - trade_log (all buys/sells — queryable via /trades in Telegram)

State is stored in bot_state.json, which gets committed
back to the GitHub repo after each run.
"""

import json
import os
import logging
from datetime import date, datetime

STATE_FILE = "bot_state.json"
MAX_TRADE_LOG_ENTRIES = 200   # Keep last 200 trades

logger = logging.getLogger("TradingBot")


def log_trade(state: dict, action: str, symbol: str, amount: float,
              price: float, pnl: float = None, reason: str = None,
              score: float = None, is_crypto: bool = False):
    """
    Append a trade event to the state's trade_log list.
    Called from run_once.py on every buy or sell.
    """
    if "trade_log" not in state:
        state["trade_log"] = []

    entry = {
        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "action": action,        # "BUY" or "SELL"
        "symbol": symbol,
        "amount": round(amount, 2),
        "price": round(price, 6),
        "is_crypto": is_crypto,
    }
    if pnl is not None:
        entry["pnl"] = round(pnl, 2)
    if reason:
        entry["reason"] = reason
    if score is not None:
        entry["score"] = round(score, 3)

    state["trade_log"].append(entry)

    # Keep list bounded
    if len(state["trade_log"]) > MAX_TRADE_LOG_ENTRIES:
        state["trade_log"] = state["trade_log"][-MAX_TRADE_LOG_ENTRIES:]


def load_state():
    """
    Load persisted state from JSON file.
    Returns a dict with default values if file doesn't exist.
    """
    defaults = {
        "peak_portfolio_value": 0.0,
        "manual_symbols": [],
        "last_summary_date": None,
        "run_count": 0,
        "trade_log": [],
        "open_pairs": {},    # Active pairs trading positions {pair_key: trade_info}
    }

    if not os.path.exists(STATE_FILE):
        logger.info("No state file found — starting fresh")
        return defaults

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        # Merge with defaults so new keys are always present
        for key, val in defaults.items():
            state.setdefault(key, val)
        trade_count = len(state.get("trade_log", []))
        logger.info(f"State loaded: peak=${state['peak_portfolio_value']:,.2f}, "
                    f"run #{state['run_count']}, "
                    f"manual={state['manual_symbols']}, "
                    f"trades logged={trade_count}")
        return state
    except Exception as e:
        logger.warning(f"Failed to load state: {e} — using defaults")
        return defaults


def save_state(state):
    """
    Save state to JSON file so the next run can read it.
    This file is committed back to GitHub by the workflow.
    """
    try:
        # Convert set to list for JSON serialization
        if isinstance(state.get("manual_symbols"), set):
            state["manual_symbols"] = list(state["manual_symbols"])
        if isinstance(state.get("last_summary_date"), date):
            state["last_summary_date"] = str(state["last_summary_date"])

        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        logger.info(f"State saved (run #{state.get('run_count', 0)})")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")
