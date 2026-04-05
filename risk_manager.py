"""
RISK MANAGER
==============
Institutional-grade risk management — upgraded with:

  FRACTIONAL KELLY CRITERION
    Sizes each position using the mathematically optimal Kelly fraction
    (derived from expected return and variance of historical returns).
    Uses quarter-Kelly by default to cap drawdowns, as per Renaissance
    Technologies and Thorp's published methodology.
    f* = (μ - r) / σ²    →    position_size = portfolio × (f* × kelly_fraction)

  VALUE AT RISK (VaR) CIRCUIT BREAKER
    Calculates the portfolio's historical-simulation 1-day VaR at 99%
    confidence. If the portfolio's VaR exceeds the configured limit,
    new buys are blocked until exposure falls within tolerance.

  DRAWDOWN CIRCUIT BREAKER
    Halts all buying if portfolio drops more than MAX_DRAWDOWN_PERCENT
    from its peak (high-water mark).

  ATR-BASED VOLATILITY SIZING (retained as secondary cap)
    More volatile stocks receive smaller allocations to equalise risk.

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

        # Fractional Kelly settings
        # Quarter-Kelly (0.25) is the standard institutional default — it achieves
        # ~75% of full Kelly's growth rate with dramatically smoother drawdowns.
        self.kelly_fraction = getattr(config, "KELLY_FRACTION", 0.25)
        self.kelly_min_pct = getattr(config, "KELLY_MIN_PCT", 0.005)  # floor: 0.5%

        # CVaR (Expected Shortfall) circuit breaker settings.
        # CVaR is the average loss in the worst (1-confidence) tail — inherently larger
        # than VaR, so the limit is set slightly higher (4% vs old 3%).
        self.cvar_limit_pct = getattr(config, "CVAR_LIMIT_PCT",
                                       getattr(config, "VAR_LIMIT_PCT", 4.0))
        self.cvar_confidence = getattr(config, "CVAR_CONFIDENCE",
                                        getattr(config, "VAR_CONFIDENCE", 0.99))

        # Track portfolio high-water mark for drawdown calculation
        self.peak_portfolio_value = 0.0

    def can_open_position(self, symbol, portfolio_value, current_positions,
                          bars_df=None, api=None):
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
        # Only count LONG positions — short legs (pairs trading) are not capital deployed
        total_invested = sum(
            float(p.get("market_value", 0))
            for p in current_positions
            if float(p.get("market_value", 0)) > 0   # long only
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

        # --- Check 6: Portfolio CVaR limit (Expected Shortfall) ---
        if current_positions:
            cvar_pct = self.estimate_portfolio_cvar(
                portfolio_value, current_positions, api=api
            )
            if cvar_pct > self.cvar_limit_pct:
                return False, (
                    f"CVaR CIRCUIT BREAKER: portfolio 1-day CVaR at "
                    f"{cvar_pct:.1f}% (limit {self.cvar_limit_pct:.1f}%) — "
                    f"reducing exposure before adding positions"
                ), 0

        # --- Calculate position size (Fractional Kelly) ---
        position_size = self._calculate_position_size(
            symbol, portfolio_value, bars_df
        )

        if position_size <= 0:
            return False, "Position size too small", 0

        return True, "Risk checks passed", round(position_size, 2)

    def _calculate_position_size(self, symbol, portfolio_value, bars_df):
        """
        Calculate position size using the Fractional Kelly Criterion.

        Full Kelly formula (continuous):
            f* = (μ - r) / σ²
        where μ = mean daily return, r = risk-free rate, σ² = return variance.

        We apply kelly_fraction (e.g. 0.25 = quarter-Kelly) to smooth the
        equity curve and reduce tail risk, as recommended by Thorp and used
        by Renaissance Technologies.

        ATR-based volatility adjustment is then applied as a secondary cap.
        """
        max_size = portfolio_value * self.max_position_weight

        # --- Fractional Kelly sizing ---
        kelly_size = None
        if bars_df is not None and len(bars_df) >= 30:
            try:
                returns = bars_df["close"].pct_change().dropna()
                if len(returns) >= 20:
                    mu = float(returns.mean())          # mean daily return
                    sigma2 = float(returns.var())       # variance of daily returns
                    r = 0.0                              # risk-free rate (daily ≈ 0)

                    if sigma2 > 0 and mu > r:
                        # Full Kelly fraction (as proportion of portfolio)
                        full_kelly = (mu - r) / sigma2
                        # Quarter-Kelly (or configured fraction) for safety
                        fractional_kelly = full_kelly * self.kelly_fraction
                        # Clamp: never below floor, never above max_position_weight
                        fractional_kelly = max(self.kelly_min_pct,
                                               min(self.max_position_weight, fractional_kelly))
                        kelly_size = portfolio_value * fractional_kelly
                        logger.debug(
                            f"Kelly sizing {symbol}: full_kelly={full_kelly:.4f}, "
                            f"fractional={fractional_kelly:.4f}, "
                            f"size=${kelly_size:,.2f}"
                        )
                    else:
                        # Negative or zero expected return → use risk_per_trade floor
                        kelly_size = portfolio_value * self.kelly_min_pct
            except Exception as e:
                logger.warning(f"Kelly sizing failed for {symbol}: {e}")

        # Fall back to simple risk_per_trade if Kelly not available
        if kelly_size is None:
            kelly_size = portfolio_value * self.risk_per_trade

        position_size = min(kelly_size, max_size)

        # --- ATR volatility adjustment (secondary cap) ---
        if bars_df is not None and len(bars_df) > self.atr_period + 5:
            try:
                atr = compute_atr(
                    bars_df["high"], bars_df["low"], bars_df["close"],
                    self.atr_period
                )
                current_atr = float(atr.iloc[-1])
                current_price = float(bars_df["close"].iloc[-1])

                if current_price > 0 and current_atr > 0:
                    volatility_pct = current_atr / current_price
                    # Scale: target 2% risk per ATR unit
                    vol_adj = 0.02 / max(volatility_pct, 0.005)
                    vol_adj = max(0.3, min(1.5, vol_adj))
                    position_size = min(position_size * vol_adj, max_size)

            except Exception as e:
                logger.warning(f"ATR sizing adjustment failed for {symbol}: {e}")

        return max(0, position_size)

    def estimate_portfolio_cvar(self, portfolio_value: float,
                                positions: list,
                                api=None) -> float:
        """
        Estimate the portfolio's 1-day CVaR (Expected Shortfall) as a % of
        portfolio value, using historical simulation where possible.

        Historical simulation (preferred when api is provided):
            - Fetch 60 days of daily bars per position
            - Sort returns ascending; take worst (1 - confidence) fraction
            - CVaR = average of those tail losses × position weight

        Parametric fallback (when bars unavailable or < 20 bars):
            CVaR ≈ vol × 2.326 × market_value / portfolio_value

        CVaR is inherently larger than VaR (it measures the AVERAGE loss in
        the tail, not just the threshold), hence CVAR_LIMIT_PCT > VAR_LIMIT_PCT.

        Returns CVaR as a percentage (e.g. 3.5 = 3.5% of portfolio).
        """
        if not positions or portfolio_value <= 0:
            return 0.0

        try:
            # Z-score for parametric fallback: 99% → 2.326, 95% → 1.645
            z = 2.326 if self.cvar_confidence >= 0.99 else 1.645

            total_cvar_pct = 0.0

            for pos in positions:
                market_val = abs(float(pos.get("market_value", 0)))
                if market_val <= 0:
                    continue
                weight = market_val / portfolio_value
                symbol = pos.get("symbol", "")

                pos_cvar_contribution = None

                # --- Historical simulation (requires live API access) ---
                if api is not None and symbol:
                    try:
                        if api.is_crypto(symbol):
                            bars = api.get_crypto_bars(symbol, "1Day", 60)
                        else:
                            bars = api.get_bars(symbol, "1Day", 60)

                        if bars is not None and not bars.empty and len(bars) >= 20:
                            returns = bars["close"].pct_change().dropna().values
                            sorted_returns = np.sort(returns)   # ascending: worst first
                            # Worst (1 - confidence) fraction; minimum 1 observation
                            n_tail = max(1, int(len(sorted_returns) * (1 - self.cvar_confidence)))
                            cvar_return = float(np.mean(sorted_returns[:n_tail]))
                            # CVaR contribution = |avg tail loss| × portfolio weight
                            pos_cvar_contribution = abs(cvar_return) * weight
                    except Exception as exc:
                        logger.debug(f"CVaR historical fetch failed for {symbol}: {exc}")

                # --- Parametric fallback ---
                if pos_cvar_contribution is None:
                    # Baseline: 2% daily vol for large-cap stocks
                    daily_vol = 0.02
                    plpc = abs(float(pos.get("unrealized_plpc", 0)))
                    if plpc > 0:
                        # Back out implied daily move from unrealized P&L
                        # (assumes P&L accumulated over ~5 trading days)
                        implied_daily = plpc / np.sqrt(5)
                        daily_vol = max(0.01, min(0.15, implied_daily))
                    pos_cvar_contribution = daily_vol * z * weight

                total_cvar_pct += pos_cvar_contribution

            # Linear sum = conservative estimate (assumes correlated tail losses)
            return round(total_cvar_pct * 100, 2)

        except Exception as e:
            logger.warning(f"CVaR calculation failed: {e}")
            return 0.0

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

        total_invested = sum(
            float(p.get("market_value", 0))
            for p in positions
            if float(p.get("market_value", 0)) > 0  # long positions only
        )
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

        # Parametric CVaR (no api available here — historical data not fetched in summary)
        cvar_pct = self.estimate_portfolio_cvar(portfolio_value, positions, api=None)

        return {
            "total_positions": len(positions),
            "total_invested": round(total_invested, 2),
            "cash_reserve_pct": round((1 - total_invested / portfolio_value) * 100, 1)
                                if portfolio_value > 0 else 0,
            "unrealized_pl": round(unrealized_pl, 2),
            "drawdown_pct": round(max(0, drawdown), 1),
            "peak_value": round(self.peak_portfolio_value, 2),
            "cvar_1day_pct": cvar_pct,
            "cvar_limit_pct": self.cvar_limit_pct,
            "sectors": sectors,
        }
