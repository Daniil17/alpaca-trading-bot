"""
RISK MANAGER
==============
Institutional-grade risk management that controls:
- Position sizing based on volatility (ATR)
- Portfolio-level exposure limits
- Maximum drawdown circuit breaker
- Sector diversification
- Stop-loss and take-profit levels per position

No trade goes through without risk manager approval.
"""

import logging
import numpy as np
from strategies import compute_atr

logger = logging.getLogger("TradingBot")

# Approximate sector mapping for common stocks
SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "AMZN": "Consumer", "NVDA": "Technology", "META": "Technology",
    "TSLA": "Consumer", "JPM": "Finance", "V": "Finance",
    "MA": "Finance", "UNH": "Healthcare", "JNJ": "Healthcare",
    "PG": "Consumer", "HD": "Consumer", "BAC": "Finance",
    "XOM": "Energy", "CVX": "Energy", "ABBV": "Healthcare",
    "KO": "Consumer", "PEP": "Consumer", "COST": "Consumer",
    "MCD": "Consumer", "CRM": "Technology", "ADBE": "Technology",
    "NFLX": "Technology", "AMD": "Technology", "INTC": "Technology",
    "QCOM": "Technology", "AVGO": "Technology", "DIS": "Consumer",
    "NKE": "Consumer", "WMT": "Consumer",
}


class RiskManager:
    """Controls risk across the entire portfolio."""

    def __init__(self, config):
        """
        Args:
            config: the config module with all risk settings
        """
        self.risk_per_trade = config.RISK_PER_TRADE
        self.max_position_weight = config.MAX_POSITION_WEIGHT
        self.max_open_positions = config.MAX_OPEN_POSITIONS
        self.max_portfolio_allocation = config.MAX_PORTFOLIO_ALLOCATION
        self.max_drawdown_pct = config.MAX_DRAWDOWN_PERCENT
        self.max_same_sector = config.MAX_SAME_SECTOR_POSITIONS
        self.atr_period = config.ATR_PERIOD
        self.sl_atr_mult = config.STOP_LOSS_ATR_MULTIPLIER
        self.tp_atr_mult = config.TAKE_PROFIT_ATR_MULTIPLIER
        self.hard_sl_pct = config.HARD_STOP_LOSS_PERCENT
        self.hard_tp_pct = config.HARD_TAKE_PROFIT_PERCENT

        # Track portfolio high-water mark for drawdown calculation
        self.peak_portfolio_value = 0.0

    def can_open_position(self, symbol, portfolio_value, current_positions,
                          bars_df=None):
        """
        Check if we're allowed to open a new position.
        Returns (allowed: bool, reason: str, position_size: float).

        Args:
            symbol: stock ticker
            portfolio_value: total portfolio value
            current_positions: list of position dicts from AlpacaAPI
            bars_df: price data for the stock (for ATR sizing)
        """
        # Update peak value
        if portfolio_value > self.peak_portfolio_value:
            self.peak_portfolio_value = portfolio_value

        # --- Check 1: Max positions ---
        if len(current_positions) >= self.max_open_positions:
            return False, f"Max positions reached ({self.max_open_positions})", 0

        # --- Check 2: Portfolio allocation limit ---
        total_invested = sum(
            abs(float(p.get("market_value", 0))) for p in current_positions
        )
        allocation_pct = total_invested / portfolio_value if portfolio_value > 0 else 1
        if allocation_pct >= self.max_portfolio_allocation:
            return False, (f"Portfolio allocation at "
                           f"{allocation_pct*100:.0f}% "
                           f"(max {self.max_portfolio_allocation*100:.0f}%)"), 0

        # --- Check 3: Drawdown circuit breaker ---
        if self.peak_portfolio_value > 0:
            drawdown = ((self.peak_portfolio_value - portfolio_value)
                        / self.peak_portfolio_value * 100)
            if drawdown >= self.max_drawdown_pct:
                return False, (f"DRAWDOWN BREAKER: Portfolio down "
                               f"{drawdown:.1f}% from peak "
                               f"(limit {self.max_drawdown_pct}%)"), 0

        # --- Check 4: Sector concentration ---
        symbol_sector = SECTOR_MAP.get(symbol, "Unknown")
        sector_count = sum(
            1 for p in current_positions
            if SECTOR_MAP.get(p.get("symbol", ""), "Unknown") == symbol_sector
        )
        if sector_count >= self.max_same_sector:
            return False, (f"Max {self.max_same_sector} positions in "
                           f"{symbol_sector} sector"), 0

        # --- Check 5: Not already holding this stock ---
        held_symbols = {p.get("symbol", "") for p in current_positions}
        if symbol in held_symbols:
            return False, f"Already holding {symbol}", 0

        # --- Calculate position size ---
        position_size = self._calculate_position_size(
            symbol, portfolio_value, bars_df
        )

        if position_size <= 0:
            return False, "Position size too small", 0

        return True, "Risk checks passed", round(position_size, 2)

    def _calculate_position_size(self, symbol, portfolio_value, bars_df):
        """
        Calculate how much money to allocate to this trade.
        Uses ATR-based volatility sizing:
        - More volatile stocks → smaller positions
        - Less volatile stocks → larger positions

        This normalizes risk across all positions.
        """
        # Base size: risk_per_trade % of portfolio
        base_size = portfolio_value * self.risk_per_trade

        # Cap at max_position_weight
        max_size = portfolio_value * self.max_position_weight
        position_size = min(base_size, max_size)

        # Adjust for volatility using ATR if we have price data
        if bars_df is not None and len(bars_df) > self.atr_period + 5:
            try:
                atr = compute_atr(
                    bars_df["high"], bars_df["low"], bars_df["close"],
                    self.atr_period
                )
                current_atr = float(atr.iloc[-1])
                current_price = float(bars_df["close"].iloc[-1])

                if current_price > 0 and current_atr > 0:
                    # ATR as % of price = volatility measure
                    volatility_pct = current_atr / current_price

                    # Target: 2% portfolio risk per trade
                    # If ATR = 3% of price, reduce position size
                    # If ATR = 1% of price, increase position size
                    volatility_adjustment = 0.02 / max(volatility_pct, 0.005)
                    volatility_adjustment = max(0.3, min(2.0, volatility_adjustment))

                    position_size *= volatility_adjustment
                    position_size = min(position_size, max_size)

            except Exception as e:
                logger.warning(f"ATR sizing failed for {symbol}: {e}")

        return position_size

    def calculate_stop_take_profit(self, symbol, entry_price, bars_df=None):
        """
        Calculate stop-loss and take-profit prices for a position.
        Uses ATR-based levels with hard limits as safety net.

        Args:
            symbol: stock ticker
            entry_price: the price we bought at
            bars_df: price data for ATR calculation

        Returns:
            dict with stop_loss and take_profit prices
        """
        # Start with hard limits
        stop_loss = entry_price * (1 - self.hard_sl_pct / 100)
        take_profit = entry_price * (1 + self.hard_tp_pct / 100)

        # Refine with ATR if available
        if bars_df is not None and len(bars_df) > self.atr_period + 5:
            try:
                atr = compute_atr(
                    bars_df["high"], bars_df["low"], bars_df["close"],
                    self.atr_period
                )
                current_atr = float(atr.iloc[-1])

                atr_stop = entry_price - (current_atr * self.sl_atr_mult)
                atr_take = entry_price + (current_atr * self.tp_atr_mult)

                # Use the TIGHTER stop-loss (higher of ATR or hard limit)
                stop_loss = max(stop_loss, atr_stop)
                # Use the CLOSER take-profit (lower of ATR or hard limit)
                take_profit = min(take_profit, atr_take)

            except Exception as e:
                logger.warning(f"ATR stop/tp calculation failed for {symbol}: {e}")

        return {
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
        }

    def check_positions_for_exit(self, positions):
        """
        Check all held positions against stop-loss and take-profit.
        Uses percentage-based checks (ATR stops are placed as actual
        orders on Alpaca, so this is the hard-limit backup).

        Args:
            positions: list of position dicts from AlpacaAPI

        Returns:
            list of positions that should be closed, with reason
        """
        exit_list = []

        for pos in positions:
            symbol = pos.get("symbol", "")
            avg_price = float(pos.get("avg_entry_price", 0))
            current_price = float(pos.get("current_price", 0))

            if avg_price <= 0 or current_price <= 0:
                continue

            pct_change = ((current_price - avg_price) / avg_price) * 100

            if pct_change <= -self.hard_sl_pct:
                exit_list.append({
                    **pos,
                    "exit_reason": f"HARD STOP-LOSS: {pct_change:.1f}%",
                })
                logger.warning(f"HARD STOP-LOSS: {symbol} at {pct_change:.1f}%")
            elif pct_change >= self.hard_tp_pct:
                exit_list.append({
                    **pos,
                    "exit_reason": f"TAKE-PROFIT: +{pct_change:.1f}%",
                })
                logger.info(f"TAKE-PROFIT: {symbol} at +{pct_change:.1f}%")

        return exit_list

    def get_portfolio_summary(self, portfolio_value, positions):
        """Generate a risk summary of the current portfolio."""
        if not positions:
            return {
                "total_positions": 0,
                "total_invested": 0,
                "cash_reserve_pct": 100,
                "drawdown_pct": 0,
                "sectors": {},
            }

        total_invested = sum(abs(float(p.get("market_value", 0))) for p in positions)
        unrealized_pl = sum(float(p.get("unrealized_pl", 0)) for p in positions)

        if self.peak_portfolio_value > 0:
            drawdown = ((self.peak_portfolio_value - portfolio_value)
                        / self.peak_portfolio_value * 100)
        else:
            drawdown = 0

        # Sector breakdown
        sectors = {}
        for pos in positions:
            sector = SECTOR_MAP.get(pos.get("symbol", ""), "Unknown")
            sectors[sector] = sectors.get(sector, 0) + 1

        return {
            "total_positions": len(positions),
            "total_invested": round(total_invested, 2),
            "cash_reserve_pct": round((1 - total_invested / portfolio_value) * 100, 1)
                                if portfolio_value > 0 else 0,
            "unrealized_pl": round(unrealized_pl, 2),
            "drawdown_pct": round(max(0, drawdown), 1),
            "peak_value": round(self.peak_portfolio_value, 2),
            "sectors": sectors,
        }
