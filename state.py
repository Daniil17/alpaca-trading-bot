"""
STATE PERSISTENCE
==================
Saves and loads bot state between GitHub Actions runs.
Each run is a fresh process, so we store things like:
  - peak_portfolio_value (for drawdown tracking)
  - last_summary_date (to avoid duplicate daily summaries)
  - trade_log (all buys/sells — queryable via /trades in Telegram)
  - open_pairs (active pairs trading positions)

State is stored in bot_state.json, which gets committed
back to the GitHub repo after each run.

ATOMIC WRITES: Uses a temp-file + rename pattern so a crash
mid-write never corrupts the state file.
"""

import json
import os
import logging
import tempfile
import shutil
import pytz
from datetime import date, datetime

STATE_FILE = "bot_state.json"
MAX_TRADE_LOG_ENTRIES = 500   # Increased from 200

_LONDON = pytz.timezone("Europe/London")
logger = logging.getLogger("TradingBot")


def _now_str():
    return datetime.now(_LONDON).strftime("%Y-%m-%d %H:%M %Z")


def log_trade(state: dict, action: str, symbol: str, amount: float,
              price: float, pnl: float = None, reason: str = None,
              score: float = None, is_crypto: bool = False):
    """
    Append a trade event to the state's trade_log list.
    Called from run_once.py on every buy or sell.
    """
    if "trade_log" not in state:
        state["trade_log"] = []

    # Sanitise string fields to prevent JSON corruption
    symbol = str(symbol)[:20]
    reason = str(reason)[:200] if reason else None

    entry = {
        "time": _now_str(),
        "action": action,
        "symbol": symbol,
        "amount": round(float(amount), 2),
        "price": round(float(price), 6),
        "is_crypto": bool(is_crypto),
    }
    if pnl is not None:
        entry["pnl"] = round(float(pnl), 2)
    if reason:
        entry["reason"] = reason
    if score is not None:
        entry["score"] = round(float(score), 3)

    state["trade_log"].append(entry)

    # Keep list bounded
    if len(state["trade_log"]) > MAX_TRADE_LOG_ENTRIES:
        state["trade_log"] = state["trade_log"][-MAX_TRADE_LOG_ENTRIES:]


def load_state():
    """
    Load persisted state from JSON file.
    Returns a dict with default values if file doesn't exist or is corrupted.
    Attempts backup recovery before falling back to defaults.
    """
    defaults = {
        "peak_portfolio_value": 0.0,
        "last_summary_date": None,
        "run_count": 0,
        "trade_log": [],
        "open_pairs": {},
    }

    if not os.path.exists(STATE_FILE):
        logger.info("No state file found — starting fresh")
        return defaults

    # Try primary file first, then backup
    for filepath in [STATE_FILE, STATE_FILE + ".bak"]:
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, "r") as f:
                raw = f.read().strip()
            if not raw:
                logger.warning(f"{filepath} is empty — skipping")
                continue
            state = json.loads(raw)
            # Merge with defaults so new keys are always present
            for key, val in defaults.items():
                state.setdefault(key, val)
            trade_count = len(state.get("trade_log", []))
            source = "" if filepath == STATE_FILE else " (from backup)"
            logger.info(f"State loaded{source}: peak=${state['peak_portfolio_value']:,.2f}, "
                        f"run #{state['run_count']}, trades={trade_count}")
            return state
        except Exception as e:
            logger.warning(f"Failed to load {filepath}: {e}")

    logger.error("All state files corrupted — starting with defaults (trade history lost)")
    return defaults


def save_state(state: dict):
    """
    Atomically save state to JSON file.
    Writes to a temp file first, then renames — guarantees no partial writes.
    Also keeps a .bak copy of the previous good state.
    """
    try:
        # Serialise safely
        safe = dict(state)
        if isinstance(safe.get("manual_symbols"), set):
            safe["manual_symbols"] = list(safe["manual_symbols"])
        if isinstance(safe.get("last_summary_date"), date):
            safe["last_summary_date"] = str(safe["last_summary_date"])

        payload = json.dumps(safe, indent=2)

        # Write to temp file in same directory (same filesystem = atomic rename)
        dir_ = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            # Rotate: current → .bak, then temp → current
            if os.path.exists(STATE_FILE):
                shutil.copy2(STATE_FILE, STATE_FILE + ".bak")
            shutil.move(tmp_path, STATE_FILE)
        except Exception:
            # Clean up temp file if something went wrong
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.info(f"State saved (run #{safe.get('run_count', 0)})")
    except Exception as e:
        logger.error(f"Failed to save state: {e}")
