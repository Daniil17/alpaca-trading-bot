"""
STATISTICAL ARBITRAGE — PAIRS TRADING
=======================================
Market-neutral strategy based on cointegration, as used by Renaissance
Technologies and other quant hedge funds.

Core concept:
  Two historically cointegrated stocks (e.g. KO/PEP, JPM/BAC) share
  a long-run equilibrium. When their price SPREAD deviates beyond
  2 standard deviations, it statistically tends to revert.

  → Spread too wide:  SHORT the expensive one, LONG the cheap one.
  → Spread too narrow: close positions.

Because we go long one and short the other simultaneously, the strategy
is market-neutral — broad market moves cancel out.

Alpaca constraints:
  - Short selling requires margin account with >$2,000 equity.
  - 4× intraday buying power on margin accounts.
  - Commission-free for ETB (Easy-to-Borrow) stocks.

Methodology:
  1. Engle–Granger two-step cointegration test (OLS + ADF residuals).
  2. Compute hedge ratio β via OLS regression of log-prices.
  3. Standardise spread into Z-score.
  4. Signal thresholds: |Z| > 2.0 = entry, |Z| < 0.5 = exit.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("TradingBot")


# ─────────────────────────────────────────────────────────
# ADF TEST (simplified — no external dependency needed)
# ─────────────────────────────────────────────────────────

def _adf_pvalue_approx(residuals: np.ndarray) -> float:
    """
    Approximate ADF p-value using a simplified Dickey-Fuller regression.
    Returns p-value; < 0.05 means the series is stationary (cointegrated).

    Uses MacKinnon (1994) critical-value approximation for the ADF test
    without requiring statsmodels.
    """
    y = np.diff(residuals)
    x = residuals[:-1]
    if len(x) < 10:
        return 1.0

    # OLS: Δy = α + β·y_{t-1} + ε
    x_with_const = np.column_stack([np.ones(len(x)), x])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(x_with_const, y, rcond=None)
    except np.linalg.LinAlgError:
        return 1.0

    beta = coeffs[1]
    y_hat = x_with_const @ coeffs
    resid = y - y_hat
    s2 = np.sum(resid ** 2) / max(1, len(resid) - 2)
    x_var = x - x.mean()
    se = np.sqrt(s2 / (np.sum(x_var ** 2) + 1e-10))
    t_stat = beta / se if se > 0 else 0.0

    # MacKinnon approximate p-value mapping (for n >= 25)
    # Critical values for no-constant ADF (tau_1):
    # 1% ≈ -3.43, 5% ≈ -2.86, 10% ≈ -2.57
    if t_stat < -3.43:
        return 0.01
    elif t_stat < -2.86:
        return 0.05
    elif t_stat < -2.57:
        return 0.10
    elif t_stat < -1.94:
        return 0.25
    else:
        return 0.50


# ─────────────────────────────────────────────────────────
# PAIR ANALYSER
# ─────────────────────────────────────────────────────────

class PairAnalyser:
    """
    Tests a pair of stocks for cointegration and computes the Z-score
    spread used to generate trading signals.
    """

    def __init__(self, zscore_entry: float = 2.0,
                 zscore_exit: float = 0.5,
                 lookback: int = 60):
        """
        Args:
            zscore_entry: Z-score threshold to open a trade (default 2σ)
            zscore_exit:  Z-score threshold to close a trade (default 0.5σ)
            lookback:     rolling window for mean/std of spread (days)
        """
        self.zscore_entry = zscore_entry
        self.zscore_exit = zscore_exit
        self.lookback = lookback

    def compute_hedge_ratio(self, log_price_x: pd.Series,
                            log_price_y: pd.Series) -> float:
        """
        OLS regression: log(Y) = α + β·log(X) + ε
        Returns β (hedge ratio).

        β tells us: for every $1 long in X, short $β in Y to be neutral.
        """
        x = log_price_x.values.reshape(-1, 1)
        y = log_price_y.values
        x_with_const = np.column_stack([np.ones(len(x)), x])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(x_with_const, y, rcond=None)
            return float(coeffs[1])
        except np.linalg.LinAlgError:
            return 1.0

    def compute_spread(self, log_price_x: pd.Series,
                       log_price_y: pd.Series,
                       hedge_ratio: float) -> pd.Series:
        """Spread S_t = log(Y_t) - β·log(X_t)."""
        return log_price_y - hedge_ratio * log_price_x

    def compute_zscore(self, spread: pd.Series) -> pd.Series:
        """Normalise spread to Z-score over rolling lookback window."""
        mean = spread.rolling(self.lookback).mean()
        std = spread.rolling(self.lookback).std()
        return (spread - mean) / std.replace(0, np.nan)

    def is_cointegrated(self, log_price_x: pd.Series,
                        log_price_y: pd.Series,
                        pvalue_threshold: float = 0.05) -> bool:
        """
        Engle–Granger step 1: regress Y on X.
        Engle–Granger step 2: ADF test on residuals.
        Returns True if residuals are stationary (cointegrated).
        """
        if len(log_price_x) < 30 or len(log_price_y) < 30:
            return False
        beta = self.compute_hedge_ratio(log_price_x, log_price_y)
        residuals = (log_price_y - beta * log_price_x).dropna().values
        pval = _adf_pvalue_approx(residuals)
        return pval < pvalue_threshold

    def analyse(self, bars_x: pd.DataFrame, bars_y: pd.DataFrame,
                symbol_x: str, symbol_y: str) -> dict:
        """
        Full analysis of a pair.

        Args:
            bars_x, bars_y: DataFrames with 'close' column
            symbol_x, symbol_y: tickers for logging

        Returns dict with:
            signal:       LONG_X_SHORT_Y | LONG_Y_SHORT_X | HOLD | EXIT
            zscore:       current Z-score
            hedge_ratio:  β
            cointegrated: bool
            reason:       human-readable explanation
        """
        # Align on common dates
        close_x = bars_x["close"]
        close_y = bars_y["close"]
        aligned = pd.concat(
            [close_x.rename("x"), close_y.rename("y")], axis=1
        ).dropna()

        if len(aligned) < max(self.lookback + 5, 40):
            return {
                "signal": "HOLD",
                "zscore": 0.0,
                "hedge_ratio": 1.0,
                "cointegrated": False,
                "reason": f"Insufficient data ({len(aligned)} bars)",
            }

        log_x = np.log(aligned["x"])
        log_y = np.log(aligned["y"])

        # Test cointegration (run on full history)
        cointegrated = self.is_cointegrated(log_x, log_y)
        if not cointegrated:
            return {
                "signal": "HOLD",
                "zscore": 0.0,
                "hedge_ratio": 1.0,
                "cointegrated": False,
                "reason": f"{symbol_x}/{symbol_y}: not cointegrated — skipping",
            }

        # Compute hedge ratio and spread
        beta = self.compute_hedge_ratio(log_x, log_y)
        spread = self.compute_spread(log_x, log_y, beta)
        zscore = self.compute_zscore(spread)

        current_z = float(zscore.iloc[-1])
        prev_z = float(zscore.iloc[-2]) if len(zscore) > 1 else current_z

        if np.isnan(current_z):
            return {
                "signal": "HOLD",
                "zscore": 0.0,
                "hedge_ratio": beta,
                "cointegrated": True,
                "reason": "Z-score NaN — insufficient lookback data",
            }

        signal = "HOLD"
        reason = ""

        # --- Entry signals ---
        # Spread too HIGH → Y is expensive vs X → short Y, long X
        if current_z > self.zscore_entry:
            signal = "LONG_X_SHORT_Y"
            reason = (f"Z={current_z:.2f} > {self.zscore_entry}: "
                      f"{symbol_y} overpriced vs {symbol_x} — "
                      f"long {symbol_x}, short {symbol_y}")

        # Spread too LOW → X is expensive vs Y → short X, long Y
        elif current_z < -self.zscore_entry:
            signal = "LONG_Y_SHORT_X"
            reason = (f"Z={current_z:.2f} < -{self.zscore_entry}: "
                      f"{symbol_x} overpriced vs {symbol_y} — "
                      f"long {symbol_y}, short {symbol_x}")

        # --- Exit signals (mean-reversion complete) ---
        elif abs(current_z) < self.zscore_exit:
            signal = "EXIT"
            reason = (f"Z={current_z:.2f} within exit band ±{self.zscore_exit}: "
                      f"spread reverted — close pair positions")

        else:
            reason = (f"Z={current_z:.2f} — within neutral band, no trade")

        return {
            "signal": signal,
            "zscore": round(current_z, 3),
            "prev_zscore": round(prev_z, 3),
            "hedge_ratio": round(beta, 4),
            "cointegrated": True,
            "spread_mean": round(float(spread.rolling(self.lookback).mean().iloc[-1]), 6),
            "spread_std": round(float(spread.rolling(self.lookback).std().iloc[-1]), 6),
            "reason": reason,
        }


# ─────────────────────────────────────────────────────────
# PAIRS TRADING ENGINE (manages multiple pairs)
# ─────────────────────────────────────────────────────────

class PairsTradingEngine:
    """
    Manages a universe of pairs, detects cointegration dynamically,
    and generates market-neutral long/short signals.

    Each pair requires:
      - A long position in the undervalued stock
      - A short position in the overvalued stock (requires margin account)

    Positions are sized to be dollar-neutral: the hedge ratio ensures
    the longs and shorts offset each other's market exposure.
    """

    def __init__(self, pairs: list, zscore_entry: float = 2.0,
                 zscore_exit: float = 0.5, lookback: int = 60,
                 max_pair_allocation: float = 0.05):
        """
        Args:
            pairs: list of (symbol_x, symbol_y) tuples to monitor
            zscore_entry: Z-score to open trade
            zscore_exit:  Z-score to close trade
            lookback:     rolling window for spread normalisation
            max_pair_allocation: max % of portfolio per pair leg (default 5%)
        """
        self.pairs = pairs
        self.analyser = PairAnalyser(zscore_entry, zscore_exit, lookback)
        self.max_pair_allocation = max_pair_allocation

    def scan_all_pairs(self, api, logger_ref=None) -> list:
        """
        Scan all configured pairs and return actionable signals.

        Args:
            api: AlpacaAPI instance (for fetching bar data)

        Returns:
            List of signal dicts for pairs with non-HOLD signals
        """
        log = logger_ref or logger
        actionable = []

        for symbol_x, symbol_y in self.pairs:
            try:
                bars_x = api.get_bars(symbol_x, "1Day", 120)
                bars_y = api.get_bars(symbol_y, "1Day", 120)

                if bars_x is None or bars_y is None:
                    continue
                if bars_x.empty or bars_y.empty:
                    continue

                result = self.analyser.analyse(
                    bars_x, bars_y, symbol_x, symbol_y
                )
                result["symbol_x"] = symbol_x
                result["symbol_y"] = symbol_y

                if result["signal"] != "HOLD":
                    log.info(
                        f"PAIR {symbol_x}/{symbol_y}: "
                        f"signal={result['signal']} Z={result['zscore']:.2f}"
                    )
                    actionable.append(result)
                else:
                    log.info(
                        f"PAIR {symbol_x}/{symbol_y}: "
                        f"Z={result['zscore']:.2f} — {result['reason']}"
                    )

            except Exception as e:
                log.warning(f"Pair {symbol_x}/{symbol_y} analysis failed: {e}")

        return actionable

    def calculate_leg_sizes(self, portfolio_value: float, price_x: float,
                            price_y: float, hedge_ratio: float) -> tuple:
        """
        Calculate dollar-neutral position sizes for both legs.

        Dollar-neutral means: long_value ≈ short_value × hedge_ratio
        so the pair has minimal directional market exposure.

        Returns: (long_notional, short_notional) in USD
        """
        base_notional = portfolio_value * self.max_pair_allocation
        long_notional = base_notional
        short_notional = base_notional * abs(hedge_ratio)
        return round(long_notional, 2), round(short_notional, 2)
