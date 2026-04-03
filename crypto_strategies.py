"""
CRYPTO STRATEGY ENGINE
========================
Five advanced hedge-fund-style strategies optimised for crypto markets.
Crypto behaves very differently from equities:
  - 24/7 trading
  - Higher volatility → tighter position sizing
  - Stronger trend dynamics
  - Sensitive to macro news (ETF, SEC, whale activity)

Each strategy returns a score from -1.0 (strong sell) to +1.0 (strong buy).
Scores are blended using configurable weights to produce a combined signal.
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger("TradingBot")

# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _macd(series: pd.Series, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram)."""
    macd_line = _ema(series, fast) - _ema(series, slow)
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — measures trend strength (0–100)."""
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    overlap = (plus_dm > 0) & (minus_dm > 0)
    mask = plus_dm >= minus_dm
    plus_dm[overlap & ~mask] = 0
    minus_dm[overlap & mask] = 0

    atr_vals = _atr(df, period)
    plus_di = 100 * _ema(plus_dm, period) / atr_vals.replace(0, np.nan)
    minus_di = 100 * _ema(minus_dm, period) / atr_vals.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _ema(dx, period)


def _bollinger(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """Returns (upper_band, middle_band, lower_band)."""
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    return mid + std_dev * std, mid, mid - std_dev * std


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


# ─────────────────────────────────────────────────────────
# STRATEGY 1 — TREND + MOMENTUM (EMA + MACD + ADX)
# ─────────────────────────────────────────────────────────

def strategy_trend_momentum(df: pd.DataFrame) -> float:
    """
    Crypto trends strongly and for longer than equities.
    Entry: EMA-21 crosses above EMA-55, confirmed by MACD histogram
           turning positive AND ADX > 30 (strong trend).
    Exit:  EMA cross reverses or MACD histogram turns negative.
    Score: +1.0 strong buy → -1.0 strong sell.
    """
    if len(df) < 60:
        return 0.0

    close = df["close"]

    ema21 = _ema(close, 21)
    ema55 = _ema(close, 55)
    _, _, hist = _macd(close)
    adx = _adx(df, 14)

    e21, e55 = ema21.iloc[-1], ema55.iloc[-1]
    e21_prev, e55_prev = ema21.iloc[-2], ema55.iloc[-2]
    macd_hist = hist.iloc[-1]
    macd_prev = hist.iloc[-2]
    adx_val = adx.iloc[-1]

    score = 0.0

    # EMA position (trend direction)
    if e21 > e55:
        score += 0.4
    elif e21 < e55:
        score -= 0.4

    # Recent EMA crossover bonus
    if e21 > e55 and e21_prev <= e55_prev:
        score += 0.3   # golden cross
    elif e21 < e55 and e21_prev >= e55_prev:
        score -= 0.3   # death cross

    # MACD momentum
    if macd_hist > 0 and macd_hist > macd_prev:
        score += 0.2   # accelerating up
    elif macd_hist > 0:
        score += 0.1
    elif macd_hist < 0 and macd_hist < macd_prev:
        score -= 0.2
    elif macd_hist < 0:
        score -= 0.1

    # ADX strength gate — halve score if trend is weak
    if adx_val < 20:
        score *= 0.3
    elif adx_val < 30:
        score *= 0.7

    return _clamp(score)


# ─────────────────────────────────────────────────────────
# STRATEGY 2 — MEAN REVERSION WITH VOLATILITY FILTER
# ─────────────────────────────────────────────────────────

def strategy_mean_reversion(df: pd.DataFrame) -> float:
    """
    RSI + Bollinger Bands, but only when ATR is LOW relative to its history.
    Crypto mean reversion works during calm periods; during spikes it fails.
    Score positive when oversold + price at lower BB in calm market.
    """
    if len(df) < 30:
        return 0.0

    close = df["close"]
    rsi = _rsi(close, 14)
    upper, mid, lower = _bollinger(close, 20, 2.0)
    atr = _atr(df, 14)

    rsi_now = rsi.iloc[-1]
    price = close.iloc[-1]
    bb_upper = upper.iloc[-1]
    bb_lower = lower.iloc[-1]
    bb_mid = mid.iloc[-1]
    bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid != 0 else 0

    # ATR volatility check — compare current ATR to 30-day median
    atr_now = atr.iloc[-1]
    atr_median = atr.tail(30).median()
    volatility_ratio = atr_now / atr_median if atr_median else 1.0

    # In high-volatility regimes, mean reversion is unreliable
    vol_multiplier = 1.0
    if volatility_ratio > 2.0:
        vol_multiplier = 0.1   # Nearly disable
    elif volatility_ratio > 1.5:
        vol_multiplier = 0.4
    elif volatility_ratio > 1.2:
        vol_multiplier = 0.7

    score = 0.0

    # Oversold zone
    if rsi_now < 25:
        score += 0.5
    elif rsi_now < 35:
        score += 0.3
    elif rsi_now < 45:
        score += 0.1

    # Overbought zone
    if rsi_now > 75:
        score -= 0.5
    elif rsi_now > 65:
        score -= 0.3
    elif rsi_now > 55:
        score -= 0.1

    # Bollinger Band position
    bb_pos = (price - bb_lower) / (bb_upper - bb_lower) if (bb_upper - bb_lower) > 0 else 0.5
    if bb_pos < 0.1:
        score += 0.4   # Price at/below lower band
    elif bb_pos < 0.25:
        score += 0.2
    elif bb_pos > 0.9:
        score -= 0.4
    elif bb_pos > 0.75:
        score -= 0.2

    return _clamp(score * vol_multiplier)


# ─────────────────────────────────────────────────────────
# STRATEGY 3 — VOLUME ANALYSIS (ACCUMULATION / DISTRIBUTION)
# ─────────────────────────────────────────────────────────

def strategy_volume_analysis(df: pd.DataFrame) -> float:
    """
    Detects institutional accumulation or distribution via volume spikes.
    Volume > 2× 20-day average is a signal. Direction determined by
    whether price closed up or down and its position relative to VWAP.

    Large up-candles on high volume = accumulation (buy signal).
    Large down-candles on high volume = distribution (sell signal).
    """
    if len(df) < 25 or "volume" not in df.columns:
        return 0.0

    close = df["close"]
    volume = df["volume"]
    high = df["high"]
    low = df["low"]

    vol_ma = volume.rolling(20).mean()
    vol_spike_ratio = volume.iloc[-1] / vol_ma.iloc[-1] if vol_ma.iloc[-1] > 0 else 1.0

    # Price move of latest candle
    price_change = (close.iloc[-1] - close.iloc[-2]) / close.iloc[-2] if close.iloc[-2] > 0 else 0
    candle_body = abs(close.iloc[-1] - df["open"].iloc[-1]) / close.iloc[-1] if close.iloc[-1] > 0 else 0

    # On-Balance Volume trend (simplified)
    obv = (volume * np.sign(close.diff())).fillna(0).cumsum()
    obv_ema = _ema(obv, 10)
    obv_trend = 1 if obv.iloc[-1] > obv_ema.iloc[-1] else -1

    score = 0.0

    # Volume spike with direction
    if vol_spike_ratio >= 2.0:
        if price_change > 0.01:       # Up on big volume = accumulation
            score += 0.5
        elif price_change < -0.01:    # Down on big volume = distribution
            score -= 0.5
        else:
            score += 0.1 * np.sign(price_change)  # Indecisive

    elif vol_spike_ratio >= 1.5:
        score += 0.25 * np.sign(price_change)

    # OBV trend alignment
    score += 0.2 * obv_trend

    # Recent volume trend (3-day vs 10-day average)
    recent_avg = volume.tail(3).mean()
    longer_avg = volume.tail(10).mean()
    if longer_avg > 0:
        vol_trend = (recent_avg / longer_avg) - 1
        score += _clamp(vol_trend * 0.3, -0.2, 0.2)

    return _clamp(score)


# ─────────────────────────────────────────────────────────
# STRATEGY 4 — CRYPTO NEWS SENTIMENT
# ─────────────────────────────────────────────────────────

CRYPTO_POSITIVE_KEYWORDS = [
    "bitcoin etf", "spot etf", "sec approves", "institutional adoption",
    "blackrock", "fidelity", "grayscale", "accumulation", "halving",
    "bullish", "rally", "surge", "breakout", "all-time high", "ath",
    "partnership", "integration", "upgrade", "mainnet", "staking",
    "defi growth", "tvl increase", "whale accumulation",
]

CRYPTO_NEGATIVE_KEYWORDS = [
    "sec sues", "sec charges", "ban", "crackdown", "hack", "exploit",
    "rug pull", "exit scam", "regulatory", "lawsuit", "seized",
    "bearish", "crash", "dump", "sell-off", "delisted", "suspended",
    "whale dump", "liquidation", "bankruptcy", "insolvency",
    "federal reserve", "interest rate hike", "inflation spike",
]


def strategy_crypto_sentiment(df: pd.DataFrame, sentiment_score: float = 0.0) -> float:
    """
    Uses the news sentiment score from NewsScanner (already enhanced
    with crypto keywords) combined with price momentum confirmation.

    sentiment_score: float in [-1, +1] from NewsScanner
    Only acts on strong sentiment (>0.3 or <-0.3) confirmed by price.
    """
    if len(df) < 5:
        return 0.0

    close = df["close"]
    price_momentum_3d = (close.iloc[-1] - close.iloc[-4]) / close.iloc[-4] if close.iloc[-4] > 0 else 0

    score = 0.0

    # Strong positive sentiment + price confirms
    if sentiment_score > 0.4:
        score += 0.5
        if price_momentum_3d > 0:
            score += 0.2   # Price is already moving with news
    elif sentiment_score > 0.2:
        score += 0.25

    # Strong negative sentiment
    elif sentiment_score < -0.4:
        score -= 0.5
        if price_momentum_3d < 0:
            score -= 0.2
    elif sentiment_score < -0.2:
        score -= 0.25

    # Neutral — small momentum signal
    else:
        score += _clamp(price_momentum_3d * 2.0, -0.2, 0.2)

    return _clamp(score)


# ─────────────────────────────────────────────────────────
# STRATEGY 5 — REGIME DETECTION
# ─────────────────────────────────────────────────────────

class RegimeDetector:
    """
    Detects whether crypto is in a Bull, Bear, or Sideways regime
    using EMA-50 vs EMA-200 (Golden/Death Cross proxy) and volatility.

    Returns: "bull", "bear", or "sideways"
    """

    def detect(self, df: pd.DataFrame) -> str:
        if len(df) < 60:
            return "sideways"

        close = df["close"]
        ema50 = _ema(close, 50).iloc[-1]
        ema200 = _ema(close, min(200, len(close) - 1)).iloc[-1]
        price = close.iloc[-1]

        # Trend direction
        if ema50 > ema200 * 1.02 and price > ema50:
            return "bull"
        elif ema50 < ema200 * 0.98 and price < ema50:
            return "bear"
        else:
            return "sideways"

    def score(self, df: pd.DataFrame, base_weights: dict) -> float:
        """
        Adjusts strategy confidence based on detected regime.
        In bull: boost trend following, reduce mean reversion.
        In bear: boost mean reversion shorts, reduce momentum longs.
        In sideways: balanced but cautious.
        """
        regime = self.detect(df)
        close = df["close"]

        # Regime score: how strongly are we in this regime?
        if len(df) < 60:
            return 0.0

        ema50 = _ema(close, 50)
        ema200 = _ema(close, min(200, len(close) - 1))
        separation = (ema50.iloc[-1] - ema200.iloc[-1]) / ema200.iloc[-1] if ema200.iloc[-1] else 0

        if regime == "bull":
            return _clamp(separation * 5.0)   # +0.x for bull
        elif regime == "bear":
            return _clamp(separation * 5.0)   # negative for bear
        else:
            return 0.0

    def get_weight_multipliers(self, df: pd.DataFrame) -> dict:
        """
        Returns multipliers to apply to each strategy's weight
        based on the current market regime.
        """
        regime = self.detect(df)

        if regime == "bull":
            return {
                "trend_momentum": 1.4,    # Momentum works great in bull
                "mean_reversion": 0.6,    # Mean reversion less reliable
                "volume_analysis": 1.2,
                "sentiment": 1.2,
                "regime": 1.0,
            }
        elif regime == "bear":
            return {
                "trend_momentum": 0.5,    # Avoid buying dips
                "mean_reversion": 0.5,    # Oversold keeps getting more oversold
                "volume_analysis": 1.3,   # Distribution detection important
                "sentiment": 1.5,         # News drives bear markets
                "regime": 1.0,
            }
        else:  # sideways
            return {
                "trend_momentum": 0.7,
                "mean_reversion": 1.3,    # Range trading works
                "volume_analysis": 1.0,
                "sentiment": 1.0,
                "regime": 1.0,
            }


# ─────────────────────────────────────────────────────────
# MAIN CRYPTO STRATEGY ENGINE
# ─────────────────────────────────────────────────────────

class CryptoStrategyEngine:
    """
    Blends 5 crypto strategies with regime-adjusted weights.
    Default weights (before regime adjustment):
        trend_momentum : 0.25
        mean_reversion : 0.20
        volume_analysis: 0.20
        sentiment      : 0.15
        regime         : 0.20
    """

    BASE_WEIGHTS = {
        "trend_momentum": 0.25,
        "mean_reversion": 0.20,
        "volume_analysis": 0.20,
        "sentiment": 0.15,
        "regime": 0.20,
    }

    BUY_THRESHOLD = 0.25    # Combined score to trigger BUY
    STRONG_BUY_THRESHOLD = 0.45
    SELL_THRESHOLD = -0.25
    STRONG_SELL_THRESHOLD = -0.45

    def __init__(self):
        self.regime_detector = RegimeDetector()

    def analyze(self, df: pd.DataFrame, sentiment_score: float = 0.0) -> dict:
        """
        Run all 5 strategies and return combined signal.

        Returns dict with:
            signal:         STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL
            combined_score: float in [-1, +1]
            regime:         bull | bear | sideways
            strategies:     dict of individual scores
        """
        if df is None or len(df) < 10:
            return {"signal": "HOLD", "combined_score": 0.0, "regime": "unknown", "strategies": {}}

        try:
            # Individual strategy scores
            s1 = strategy_trend_momentum(df)
            s2 = strategy_mean_reversion(df)
            s3 = strategy_volume_analysis(df)
            s4 = strategy_crypto_sentiment(df, sentiment_score)
            s5 = self.regime_detector.score(df, self.BASE_WEIGHTS)

            # Regime-adjusted weights
            multipliers = self.regime_detector.get_weight_multipliers(df)
            regime = self.regime_detector.detect(df)

            weights = {
                k: self.BASE_WEIGHTS[k] * multipliers[k]
                for k in self.BASE_WEIGHTS
            }
            total_weight = sum(weights.values())
            weights = {k: v / total_weight for k, v in weights.items()}  # Normalise

            scores = {
                "trend_momentum": s1,
                "mean_reversion": s2,
                "volume_analysis": s3,
                "sentiment": s4,
                "regime": s5,
            }

            combined = sum(scores[k] * weights[k] for k in scores)
            combined = _clamp(combined)

            if combined >= self.STRONG_BUY_THRESHOLD:
                signal = "STRONG_BUY"
            elif combined >= self.BUY_THRESHOLD:
                signal = "BUY"
            elif combined <= self.STRONG_SELL_THRESHOLD:
                signal = "STRONG_SELL"
            elif combined <= self.SELL_THRESHOLD:
                signal = "SELL"
            else:
                signal = "HOLD"

            return {
                "signal": signal,
                "combined_score": combined,
                "regime": regime,
                "strategies": scores,
                "weights": weights,
            }

        except Exception as e:
            logger.error(f"Crypto strategy error: {e}", exc_info=True)
            return {"signal": "HOLD", "combined_score": 0.0, "regime": "unknown", "strategies": {}}
