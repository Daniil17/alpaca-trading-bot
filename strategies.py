"""
MULTI-STRATEGY ENGINE
======================
Implements four hedge fund-style strategies that each score stocks
independently. The bot combines scores using weighted voting to
make final buy/sell decisions.

Strategies:
1. Mean Reversion  — RSI + Bollinger Bands (buy oversold, sell overbought)
2. Momentum        — EMA crossover + ADX trend strength
3. News Sentiment  — News-driven with pullback entry (avoids buying spikes)
4. Volume Flow     — Volume-Price Trend (VPT) on daily bars; replaces VWAP
                     which is an intraday indicator meaningless on daily bars.

Regime Detection (applied to all strategies):
   RegimeDetector uses ADX to classify the market as TRENDING or RANGING
   and adjusts ensemble weights accordingly — dampening mean reversion in
   trends and momentum in ranges — so the two strategies stop cancelling
   each other out.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("TradingBot")


# ============================================================
# TECHNICAL INDICATOR CALCULATIONS
# ============================================================

def compute_rsi(prices, period=14):
    """
    Relative Strength Index.
    Returns a pandas Series of RSI values (0-100).
    """
    deltas = prices.diff()
    gains = deltas.clip(lower=0)
    losses = (-deltas).clip(lower=0)

    avg_gain = gains.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1/period, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_bollinger_bands(prices, period=20, std_dev=2.0):
    """
    Bollinger Bands: middle (SMA), upper, lower bands.
    Returns (middle, upper, lower) as pandas Series.
    """
    middle = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std()
    upper = middle + (std_dev * std)
    lower = middle - (std_dev * std)
    return middle, upper, lower


def compute_ema(prices, period):
    """Exponential Moving Average."""
    return prices.ewm(span=period, adjust=False).mean()


def compute_adx(high, low, close, period=14):
    """
    Average Directional Index — measures trend strength (0-100).
    ADX > 25 indicates a strong trend.
    """
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)

    # Only keep the larger of +DM or -DM
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1/period, min_periods=period).mean()

    return adx, plus_di, minus_di


def compute_atr(high, low, close, period=14):
    """
    Average True Range — measures volatility.
    Used for position sizing and stop-loss placement.
    """
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1/period, min_periods=period).mean()


def compute_vpt(close, volume):
    """
    Volume-Price Trend (VPT) — daily-timeframe alternative to VWAP.

    VWAP is an *intraday* indicator that resets each day; applying cumsum()
    across daily bars produces a long-run weighted average that diverges
    further from price every day and carries no fair-value meaning.

    VPT is designed for daily data and captures the same institutional-flow
    intuition: large volume on up-days signals accumulation (bullish), large
    volume on down-days signals distribution (bearish).

    Returns a pandas Series of cumulative VPT values.
    """
    pct_change = close.pct_change().fillna(0)
    vpt = (pct_change * volume).cumsum()
    return vpt


# ============================================================
# STRATEGY 1: MEAN REVERSION (RSI + Bollinger Bands)
# ============================================================

class MeanReversionStrategy:
    """
    Buys when a stock is oversold (RSI low + price near lower Bollinger Band).
    Sells when overbought (RSI high + price near upper Bollinger Band).

    This is a contrarian strategy: bet that extremes revert to the mean.
    """

    def __init__(self, rsi_period=14, rsi_oversold=30, rsi_overbought=70,
                 bb_period=20, bb_std=2.0):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_period = bb_period
        self.bb_std = bb_std

    def score(self, bars_df):
        """
        Score a stock from -1 (strong sell) to +1 (strong buy).

        Args:
            bars_df: DataFrame with 'close' column (from Alpaca bars)

        Returns:
            dict with score, rsi, bb_position, reason
        """
        close = bars_df["close"]

        if len(close) < max(self.rsi_period, self.bb_period) + 5:
            return {"score": 0, "reason": "Not enough data"}

        rsi = compute_rsi(close, self.rsi_period)
        _, bb_upper, bb_lower = compute_bollinger_bands(close, self.bb_period, self.bb_std)

        current_rsi = float(rsi.iloc[-1])
        current_price = float(close.iloc[-1])
        current_upper = float(bb_upper.iloc[-1])
        current_lower = float(bb_lower.iloc[-1])

        # Calculate how far price is from Bollinger Bands (0 = lower, 1 = upper)
        bb_range = current_upper - current_lower
        if bb_range > 0:
            bb_position = (current_price - current_lower) / bb_range
        else:
            bb_position = 0.5

        # Score logic
        score = 0.0
        reasons = []

        # RSI component (-0.5 to +0.5)
        if current_rsi < self.rsi_oversold:
            rsi_score = 0.5 * ((self.rsi_oversold - current_rsi) / self.rsi_oversold)
            score += rsi_score
            reasons.append(f"RSI oversold ({current_rsi:.0f})")
        elif current_rsi > self.rsi_overbought:
            rsi_score = -0.5 * ((current_rsi - self.rsi_overbought) / (100 - self.rsi_overbought))
            score += rsi_score
            reasons.append(f"RSI overbought ({current_rsi:.0f})")

        # Bollinger Band component (-0.5 to +0.5)
        if bb_position < 0.1:
            score += 0.5
            reasons.append("Price at lower Bollinger Band")
        elif bb_position > 0.9:
            score -= 0.5
            reasons.append("Price at upper Bollinger Band")
        elif bb_position < 0.3:
            score += 0.2
            reasons.append("Price near lower band")
        elif bb_position > 0.7:
            score -= 0.2
            reasons.append("Price near upper band")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 3),
            "rsi": round(current_rsi, 1),
            "bb_position": round(bb_position, 3),
            "reason": " | ".join(reasons) if reasons else "Neutral",
        }


# ============================================================
# STRATEGY 2: MOMENTUM / TREND FOLLOWING (EMA + ADX)
# ============================================================

class MomentumStrategy:
    """
    Rides strong trends using EMA crossovers confirmed by ADX.
    Buy when fast EMA crosses above slow EMA AND ADX > 25 (strong trend).
    Sell when fast EMA crosses below slow EMA.

    This is a trend-following strategy: bet that winners keep winning.
    """

    def __init__(self, ema_fast=12, ema_slow=26, adx_period=14,
                 adx_threshold=25):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold

    def score(self, bars_df):
        """
        Score from -1 (strong sell) to +1 (strong buy).
        """
        close = bars_df["close"]
        high = bars_df["high"]
        low = bars_df["low"]

        if len(close) < self.ema_slow + self.adx_period + 5:
            return {"score": 0, "reason": "Not enough data"}

        ema_f = compute_ema(close, self.ema_fast)
        ema_s = compute_ema(close, self.ema_slow)
        adx, plus_di, minus_di = compute_adx(high, low, close, self.adx_period)

        current_ema_f = float(ema_f.iloc[-1])
        current_ema_s = float(ema_s.iloc[-1])
        prev_ema_f = float(ema_f.iloc[-2])
        prev_ema_s = float(ema_s.iloc[-2])
        current_adx = float(adx.iloc[-1])
        current_plus_di = float(plus_di.iloc[-1])
        current_minus_di = float(minus_di.iloc[-1])

        score = 0.0
        reasons = []

        # EMA crossover direction
        currently_above = current_ema_f > current_ema_s
        previously_above = prev_ema_f > prev_ema_s

        # Fresh crossover = stronger signal
        if currently_above and not previously_above:
            score += 0.6
            reasons.append("Bullish EMA crossover (fresh)")
        elif currently_above:
            # Already in uptrend
            spread_pct = (current_ema_f - current_ema_s) / current_ema_s * 100
            score += min(0.3, spread_pct * 0.1)
            reasons.append(f"Uptrend (EMA spread {spread_pct:.1f}%)")
        elif not currently_above and previously_above:
            score -= 0.6
            reasons.append("Bearish EMA crossover (fresh)")
        else:
            spread_pct = (current_ema_s - current_ema_f) / current_ema_s * 100
            score -= min(0.3, spread_pct * 0.1)
            reasons.append(f"Downtrend (EMA spread {spread_pct:.1f}%)")

        # ADX confirms trend strength
        if current_adx > self.adx_threshold:
            # Strong trend — amplify the signal
            adx_boost = min(0.4, (current_adx - self.adx_threshold) / 50)
            if score > 0:
                score += adx_boost
                reasons.append(f"Strong trend (ADX {current_adx:.0f})")
            elif score < 0:
                score -= adx_boost
                reasons.append(f"Strong downtrend (ADX {current_adx:.0f})")
        else:
            # Weak trend — dampen the signal
            score *= 0.5
            reasons.append(f"Weak trend (ADX {current_adx:.0f})")

        # Directional index confirmation
        if current_plus_di > current_minus_di and score > 0:
            score += 0.1
        elif current_minus_di > current_plus_di and score < 0:
            score -= 0.1

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 3),
            "adx": round(current_adx, 1),
            "ema_fast": round(current_ema_f, 2),
            "ema_slow": round(current_ema_s, 2),
            "reason": " | ".join(reasons) if reasons else "Neutral",
        }


# ============================================================
# STRATEGY 3: NEWS SENTIMENT + PULLBACK ENTRY
# ============================================================

class NewsSentimentStrategy:
    """
    Uses news sentiment for stock SELECTION, but uses RSI pullback
    detection for TIMING. Avoids buying at news-spike peaks.

    When positive news breaks → stock spikes → RSI > 70.
    Classic RSI would say "don't buy" (overbought).
    This strategy waits for the pullback to 40-55 RSI range.
    """

    def __init__(self, pullback_rsi_low=40, pullback_rsi_high=55,
                 spike_confirm=65, rsi_period=14):
        self.pullback_rsi_low = pullback_rsi_low
        self.pullback_rsi_high = pullback_rsi_high
        self.spike_confirm = spike_confirm
        self.rsi_period = rsi_period

    def score(self, bars_df, sentiment_score=0.0):
        """
        Score from -1 to +1.

        Args:
            bars_df: DataFrame with price data
            sentiment_score: news sentiment from NewsScanner (-1 to +1)
        """
        close = bars_df["close"]

        if len(close) < self.rsi_period + 10:
            return {"score": 0, "reason": "Not enough data"}

        if sentiment_score <= 0:
            return {
                "score": 0,
                "reason": "No positive news sentiment",
                "sentiment": sentiment_score,
            }

        rsi = compute_rsi(close, self.rsi_period)
        current_rsi = float(rsi.iloc[-1])

        # Check for recent spike in last 10 days
        lookback = min(10, len(rsi))
        recent_max_rsi = float(rsi.iloc[-lookback:].max())

        score = 0.0
        reasons = []

        had_spike = recent_max_rsi >= self.spike_confirm

        if had_spike:
            # News DID move the price — look for pullback
            if current_rsi > self.spike_confirm:
                # Still at the peak — wait
                score = 0.0
                reasons.append(f"News spike active (RSI {current_rsi:.0f}) — waiting for pullback")
            elif self.pullback_rsi_low <= current_rsi <= self.pullback_rsi_high:
                # Sweet spot: pullback entry
                score = 0.7 * sentiment_score
                reasons.append(f"Pullback entry! RSI {recent_max_rsi:.0f}->{current_rsi:.0f}")
            elif current_rsi < self.pullback_rsi_low:
                # Deep pullback — oversold bounce play
                score = 0.5 * sentiment_score
                reasons.append(f"Deep pullback (RSI {current_rsi:.0f} from {recent_max_rsi:.0f})")
            else:
                # Between pullback_high and spike — cooling but not enough
                score = 0.2 * sentiment_score
                reasons.append(f"Cooling (RSI {current_rsi:.0f}) — waiting for deeper dip")
        else:
            # News didn't spike the stock — weaker signal
            if current_rsi < 50:
                score = 0.4 * sentiment_score
                reasons.append(f"Positive news + low RSI ({current_rsi:.0f})")
            else:
                score = 0.1 * sentiment_score
                reasons.append(f"Positive news but RSI neutral ({current_rsi:.0f})")

        reasons.append(f"Sentiment: {sentiment_score:.2f}")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 3),
            "rsi": round(current_rsi, 1),
            "recent_max_rsi": round(recent_max_rsi, 1),
            "sentiment": sentiment_score,
            "reason": " | ".join(reasons),
        }


# ============================================================
# STRATEGY 4: VOLUME FLOW (Volume-Price Trend on daily bars)
# ============================================================

class VolumeFlowStrategy:
    """
    Replaces VWAPStrategy. Uses Volume-Price Trend (VPT) on daily bars.

    VWAP is an intraday indicator — applying it across 100 daily bars
    produces a long-run price average that diverges further from current
    price each day and carries no fair-value meaning at this timeframe.

    VPT captures the same institutional-flow intuition on daily data:
    - Rising VPT slope = accumulation (institutions buying on up-days) → bullish
    - Falling VPT slope = distribution (heavy selling on down-days) → bearish
    Volume ratio confirms conviction.
    """

    def __init__(self, lookback=20):
        self.lookback = lookback

    def score(self, bars_df):
        """Score from -1 to +1."""
        close = bars_df["close"]
        volume = bars_df["volume"]

        if len(close) < self.lookback + 5:
            return {"score": 0, "reason": "Not enough data"}

        vpt = compute_vpt(close, volume)

        # VPT slope over lookback window (normalised so it's scale-invariant)
        vpt_recent = vpt.iloc[-self.lookback:]
        vpt_start = float(vpt_recent.iloc[0])
        vpt_end = float(vpt_recent.iloc[-1])
        slope = (vpt_end - vpt_start) / (abs(vpt_start) + 1e-8)

        # Volume ratio for confirmation
        avg_vol = float(volume.rolling(self.lookback).mean().iloc[-1])
        cur_vol = float(volume.iloc[-1])
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

        score = 0.0
        reasons = []

        if slope > 0.05:
            score = min(1.0, slope * 2)
            reasons.append(f"VPT rising (accumulation, slope {slope:.3f})")
        elif slope < -0.05:
            score = max(-1.0, slope * 2)
            reasons.append(f"VPT falling (distribution, slope {slope:.3f})")
        else:
            reasons.append(f"VPT flat ({slope:+.3f})")

        # Volume confirmation: high volume strengthens signal, low volume dampens it
        if score != 0:
            if vol_ratio > 1.5:
                score = max(-1.0, min(1.0, score * 1.3))
                reasons.append(f"High volume ({vol_ratio:.1f}x avg)")
            elif vol_ratio < 0.5:
                score = max(-1.0, min(1.0, score * 0.7))
                reasons.append(f"Low volume ({vol_ratio:.1f}x avg)")

        score = max(-1.0, min(1.0, score))

        return {
            "score": round(score, 3),
            "vpt_slope": round(slope, 4),
            "vol_ratio": round(vol_ratio, 2),
            "reason": " | ".join(reasons) if reasons else "Neutral",
        }


# ============================================================
# REGIME DETECTOR
# ============================================================

class RegimeDetector:
    """
    Classifies the current market regime using ADX:
      - TRENDING (ADX > trend_threshold): favour momentum, dampen mean reversion.
        In strong trends mean reversion trades against the prevailing flow.
      - RANGING  (ADX < range_threshold): favour mean reversion, dampen momentum.
        In choppy markets momentum produces whipsaws.
      - MIXED    (between thresholds): use base weights unchanged.

    Returns weight multipliers that are applied to the ensemble before
    normalisation, so they always sum to 1.0.
    """

    def __init__(self, adx_period=14, trend_threshold=25, range_threshold=20):
        self.adx_period = adx_period
        self.trend_threshold = trend_threshold
        self.range_threshold = range_threshold

    def detect(self, bars_df) -> dict:
        """Returns regime info and per-strategy weight multipliers."""
        if len(bars_df) < self.adx_period + 10:
            return {"regime": "MIXED", "adx": None, "adjustments": {}}

        adx, _, _ = compute_adx(
            bars_df["high"], bars_df["low"], bars_df["close"],
            self.adx_period,
        )
        current_adx = float(adx.iloc[-1])

        if current_adx > self.trend_threshold:
            return {
                "regime": "TRENDING",
                "adx": round(current_adx, 1),
                "adjustments": {
                    "mean_reversion": 0.5,   # halve: don't fight the trend
                    "momentum": 1.5,          # boost: ride the trend
                },
            }
        elif current_adx < self.range_threshold:
            return {
                "regime": "RANGING",
                "adx": round(current_adx, 1),
                "adjustments": {
                    "mean_reversion": 1.5,   # boost: mean reversion shines in ranges
                    "momentum": 0.5,          # dampen: momentum whipsaws in ranges
                },
            }
        else:
            return {
                "regime": "MIXED",
                "adx": round(current_adx, 1),
                "adjustments": {},
            }


# ============================================================
# PERFORMANCE-ADAPTIVE STRATEGY WEIGHT TRACKER
# ============================================================

class StrategyPerformanceTracker:
    """
    Tracks each strategy's recent performance and adjusts ensemble weights
    dynamically based on a 20-trade rolling window.

    Weights shift toward strategies with better recent Sharpe-like performance.
    Each strategy is bounded between MIN_WEIGHT and MAX_WEIGHT to prevent
    any single strategy from dominating or being completely sidelined.

    Usage:
        tracker = StrategyPerformanceTracker(base_weights)
        # After a position closes:
        tracker.record_trade_result(analysis["strategies"], pct_return)
        # In StrategyEngine.analyze():
        weights = tracker.get_adjusted_weights()
    """
    MIN_WEIGHT = 0.10   # No strategy gets less than 10%
    MAX_WEIGHT = 0.50   # No strategy gets more than 50%

    def __init__(self, base_weights: dict):
        self.base_weights = base_weights
        # Rolling list of signed returns attributed to each strategy
        self.strategy_pnl: dict = {k: [] for k in base_weights}

    def record_trade_result(self, strategy_scores: dict, pct_return: float):
        """
        Called after a position is closed.  Attributes the P&L to the strategy
        that had the highest absolute score at entry (the "dominant" strategy).

        Args:
            strategy_scores: analysis["strategies"] dict from entry analysis
            pct_return:       realised % return (positive = profit, negative = loss)
        """
        if not strategy_scores:
            return

        def _abs_score(v):
            if isinstance(v, dict):
                return abs(float(v.get("score", 0)))
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                return 0.0

        dominant = max(strategy_scores, key=lambda k: _abs_score(strategy_scores[k]))

        for k in self.strategy_pnl:
            if k == dominant:
                self.strategy_pnl[k].append(pct_return)
            # Trim to rolling window
            if len(self.strategy_pnl[k]) > 20:
                self.strategy_pnl[k] = self.strategy_pnl[k][-20:]

    def get_adjusted_weights(self) -> dict:
        """
        Returns adapted weights.  Falls back to base_weights if no strategy
        has accumulated at least 5 trades yet.
        """
        if all(len(v) < 5 for v in self.strategy_pnl.values()):
            return self.base_weights.copy()

        # Sharpe-like score per strategy
        scores = {}
        for k, returns in self.strategy_pnl.items():
            if len(returns) < 3:
                scores[k] = 0.0
            else:
                arr = np.array(returns)
                std = arr.std() + 1e-6
                scores[k] = arr.mean() / std

        # Shift to positive (min score → 0.01)
        min_score = min(scores.values())
        shifted = {k: v - min_score + 0.01 for k, v in scores.items()}
        total = sum(shifted.values())
        raw_weights = {k: shifted[k] / total for k in shifted}

        # Clip to [MIN_WEIGHT, MAX_WEIGHT] and renormalise
        clipped = {
            k: max(self.MIN_WEIGHT, min(self.MAX_WEIGHT, raw_weights[k]))
            for k in raw_weights
        }
        total_clipped = sum(clipped.values())
        return {k: v / total_clipped for k, v in clipped.items()}


# ============================================================
# BAYESIAN WEIGHT OPTIMIZER
# ============================================================

class BayesianWeightOptimizer:
    """
    Uses a Gaussian Process surrogate model to find the optimal strategy weights
    that maximise the Sharpe-like objective on recent trade history.

    Wraps scikit-optimize's gp_minimize. Falls back gracefully if skopt is not
    installed.

    The 'action space' is the weight vector for N strategies (simplex-constrained).
    The 'reward' is the portfolio Sharpe ratio achieved when those weights would have
    been applied to the last window of trades.
    """

    def __init__(self, strategy_names: list, n_calls: int = 15, window: int = 30):
        self.strategy_names = strategy_names
        self.n_calls = n_calls       # GP iterations per optimisation run
        self.window = window         # recent trade window to evaluate weights on
        self.best_weights = None
        self._skopt_available = False
        try:
            from skopt import gp_minimize  # noqa: F401
            from skopt.space import Real   # noqa: F401
            self._skopt_available = True
        except ImportError:
            pass

    def _objective(self, raw_weights, trade_history: list) -> float:
        """
        Given a weight vector and trade history (list of dicts with
        'strategy_scores' and 'pct_return'), compute negative Sharpe.
        Lower is better for minimisation.
        """
        n = len(self.strategy_names)
        w = np.array(raw_weights)
        w = np.clip(w, 0.01, 1.0)
        w = w / w.sum()
        weights = dict(zip(self.strategy_names, w))

        returns = []
        for trade in trade_history[-self.window:]:
            scores = trade.get("strategy_scores", {})
            if not scores:
                continue
            composite = sum(
                weights.get(k, 0) * (v.get("score", 0) if isinstance(v, dict) else float(v))
                for k, v in scores.items()
            )
            pct = trade.get("pct_return", 0)
            if composite > 0 and pct > 0:
                returns.append(pct)
            elif composite > 0 and pct <= 0:
                returns.append(pct)
            elif composite < 0 and pct < 0:
                returns.append(abs(pct))  # correct short signal
            else:
                returns.append(-abs(pct))

        if len(returns) < 5:
            return 0.0

        arr = np.array(returns)
        sharpe = arr.mean() / (arr.std() + 1e-8)
        return -sharpe  # minimise negative Sharpe

    def optimise(self, trade_history: list) -> dict:
        """
        Run Bayesian Optimisation. Returns the best weight dict found.
        Falls back to equal weights if skopt is unavailable or data is insufficient.
        """
        n = len(self.strategy_names)
        equal = {k: 1.0 / n for k in self.strategy_names}

        if not self._skopt_available or len(trade_history) < 10:
            return equal

        try:
            from skopt import gp_minimize
            from skopt.space import Real

            space = [Real(0.05, 0.60, name=k) for k in self.strategy_names]

            result = gp_minimize(
                lambda w: self._objective(w, trade_history),
                space,
                n_calls=self.n_calls,
                random_state=42,
                noise=0.01,
                verbose=False,
            )

            raw = np.array(result.x)
            raw = np.clip(raw, 0.05, 0.60)
            raw = raw / raw.sum()
            self.best_weights = dict(zip(self.strategy_names, raw.tolist()))
            return self.best_weights

        except Exception as e:
            logger.warning(f"BayesianWeightOptimizer failed: {e} — using equal weights")
            return equal


# ============================================================
# COMBINED STRATEGY SCORER
# ============================================================

class StrategyEngine:
    """
    Combines all four strategies using weighted voting.
    Each strategy scores independently, then scores are blended.
    """

    def __init__(self, weights, tracker: "StrategyPerformanceTracker" = None,
                 optimizer: "BayesianWeightOptimizer" = None, **kwargs):
        """
        Args:
            weights:   dict of strategy name -> weight (should sum to 1.0)
            tracker:   optional StrategyPerformanceTracker for adaptive weights
            optimizer: optional BayesianWeightOptimizer; when set, its cached
                       weights (stored in _bayesian_weights) override the tracker
            **kwargs:  strategy-specific parameters from config
        """
        self.weights = weights
        self.tracker = tracker
        self.optimizer = optimizer
        self._bayesian_weights = None  # populated externally every N cycles

        self.mean_reversion = MeanReversionStrategy(
            rsi_period=kwargs.get("rsi_period", 14),
            rsi_oversold=kwargs.get("rsi_oversold", 30),
            rsi_overbought=kwargs.get("rsi_overbought", 70),
            bb_period=kwargs.get("bb_period", 20),
            bb_std=kwargs.get("bb_std", 2.0),
        )
        self.momentum = MomentumStrategy(
            ema_fast=kwargs.get("ema_fast", 12),
            ema_slow=kwargs.get("ema_slow", 26),
            adx_period=kwargs.get("adx_period", 14),
            adx_threshold=kwargs.get("adx_threshold", 25),
        )
        self.news_sentiment = NewsSentimentStrategy(
            pullback_rsi_low=kwargs.get("news_pullback_rsi_low", 40),
            pullback_rsi_high=kwargs.get("news_pullback_rsi_high", 55),
            spike_confirm=kwargs.get("news_rsi_spike_confirm", 65),
        )
        self.volume_flow = VolumeFlowStrategy(
            lookback=kwargs.get("vpt_lookback", 20),
        )
        self.regime_detector = RegimeDetector(
            adx_period=kwargs.get("adx_period", 14),
            trend_threshold=kwargs.get("adx_threshold", 25),
            range_threshold=kwargs.get("regime_range_threshold", 20),
        )

    def analyze(self, bars_df, sentiment_score=0.0):
        """
        Run all four strategies and return a combined score.

        Args:
            bars_df: DataFrame from Alpaca with OHLCV data
            sentiment_score: news sentiment for this stock (-1 to +1)

        Returns:
            dict with combined_score, individual scores, and final signal
        """
        # Score each strategy independently
        mr_result = self.mean_reversion.score(bars_df)
        mom_result = self.momentum.score(bars_df)
        news_result = self.news_sentiment.score(bars_df, sentiment_score)
        vf_result = self.volume_flow.score(bars_df)

        # Weight priority: Bayesian optimizer > adaptive tracker > base weights.
        # _bayesian_weights is populated externally every N cycles to avoid
        # re-running the GP on every per-stock call.
        weights = self.weights
        if self._bayesian_weights is not None:
            weights = self._bayesian_weights
        elif self.tracker is not None:
            weights = self.tracker.get_adjusted_weights()

        # --- Regime-aware weight adjustment ---
        # ADX classifies the market as TRENDING or RANGING and multiplies the
        # per-strategy weights accordingly, then re-normalises to sum=1.
        # This stops momentum and mean reversion constantly cancelling each other.
        regime = self.regime_detector.detect(bars_df)
        adjustments = regime.get("adjustments", {})
        if adjustments:
            adjusted = {k: weights.get(k, 0) * adjustments.get(k, 1.0) for k in weights}
            total = sum(adjusted.values())
            if total > 0:
                weights = {k: v / total for k, v in adjusted.items()}

        # Weighted combination
        combined = (
            weights.get("mean_reversion", 0.30) * mr_result["score"]
            + weights.get("momentum", 0.30) * mom_result["score"]
            + weights.get("news_sentiment", 0.10) * news_result["score"]
            + weights.get("volume_flow", 0.30) * vf_result["score"]
        )

        # Determine signal with thresholds
        if combined >= 0.3:
            signal = "STRONG_BUY"
        elif combined >= 0.15:
            signal = "BUY"
        elif combined <= -0.3:
            signal = "STRONG_SELL"
        elif combined <= -0.15:
            signal = "SELL"
        else:
            signal = "HOLD"

        return {
            "combined_score": round(combined, 3),
            "signal": signal,
            "regime": regime.get("regime", "MIXED"),
            "strategies": {
                "mean_reversion": mr_result,
                "momentum": mom_result,
                "news_sentiment": news_result,
                "volume_flow": vf_result,
            },
        }
