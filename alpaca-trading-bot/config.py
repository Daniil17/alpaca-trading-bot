"""
CONFIGURATION - Advanced Alpaca Trading Bot
=============================================
Fill in your credentials and tweak settings below.
"""

# ============================================================
# ALPACA API CREDENTIALS
# ============================================================
# Get these from https://app.alpaca.markets → Paper Trading → API Keys
# (or Live Trading → API Keys for real money)

ALPACA_API_KEY = "ALPACA_API_KEY"
ALPACA_SECRET_KEY = "ALPACA_SECRET_KEY"

# True = paper trading (fake money), False = real money
PAPER_TRADING = True

# ============================================================
# TELEGRAM NOTIFICATIONS
# ============================================================
# 1. Open Telegram, search for @BotFather
# 2. Send /newbot, follow the steps, copy the token
# 3. Start a chat with your bot, then get your chat_id:
#    Visit https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
#    after sending a message to your bot

TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"

# Automatic push notifications — all OFF by default.
# The bot logs every trade to bot_state.json so you can check
# history any time by typing /trades or /profit in Telegram.
# Set any of these to True if you want automatic push alerts.
NOTIFY_ON_BUY = False
NOTIFY_ON_SELL = False
NOTIFY_ON_STOP_LOSS = False
NOTIFY_DAILY_SUMMARY = False
NOTIFY_ON_ERROR = False

# ============================================================
# PORTFOLIO & RISK MANAGEMENT (Hedge Fund Style)
# ============================================================

# What fraction of your total portfolio to risk per trade (Kelly-inspired)
# 0.02 = 2% of portfolio per position — conservative institutional default
RISK_PER_TRADE = 0.02

# Maximum % of portfolio in a single stock
MAX_POSITION_WEIGHT = 0.10  # 10%

# Maximum number of open positions at once
MAX_OPEN_POSITIONS = 10

# Maximum total portfolio allocation (rest stays cash as buffer)
# 0.80 = invest up to 80%, keep 20% cash reserve
MAX_PORTFOLIO_ALLOCATION = 0.80

# Maximum portfolio drawdown before bot pauses ALL buying
# If portfolio drops 10% from its peak, stop trading until recovery
MAX_DRAWDOWN_PERCENT = 10.0

# Maximum correlated positions in same sector
MAX_SAME_SECTOR_POSITIONS = 3

# ============================================================
# STRATEGY WEIGHTS
# ============================================================
# The bot runs multiple strategies simultaneously.
# Each strategy scores stocks from -1 (strong sell) to +1 (strong buy).
# These weights control how much influence each strategy has.
# Total should equal 1.0

STRATEGY_WEIGHTS = {
    "mean_reversion": 0.25,    # Buy oversold, sell overbought (RSI + Bollinger)
    "momentum": 0.25,          # Ride strong trends (EMA crossover + ADX)
    "news_sentiment": 0.25,    # News-driven with pullback entry
    "vwap": 0.25,              # Institutional volume-price analysis
}

# ============================================================
# MEAN REVERSION SETTINGS (Strategy 1)
# ============================================================
RSI_PERIOD = 14
RSI_OVERSOLD = 30             # Buy signal
RSI_OVERBOUGHT = 70           # Sell signal
BOLLINGER_PERIOD = 20         # Bollinger Band lookback
BOLLINGER_STD_DEV = 2.0       # Standard deviations for bands

# ============================================================
# MOMENTUM / TREND FOLLOWING SETTINGS (Strategy 2)
# ============================================================
EMA_FAST = 12                 # Fast exponential moving average
EMA_SLOW = 26                 # Slow exponential moving average
ADX_PERIOD = 14               # Average Directional Index period
ADX_TREND_THRESHOLD = 25      # Minimum ADX to confirm a trend

# ============================================================
# NEWS SENTIMENT SETTINGS (Strategy 3)
# ============================================================
MIN_SENTIMENT_SCORE = 0.3     # Minimum positive sentiment to consider
NEWS_PULLBACK_RSI_LOW = 40    # Buy zone lower bound after news spike
NEWS_PULLBACK_RSI_HIGH = 55   # Buy zone upper bound after news spike
NEWS_RSI_SPIKE_CONFIRM = 65   # RSI must have recently hit this

# ============================================================
# VWAP SETTINGS (Strategy 4)
# ============================================================
# VWAP = Volume Weighted Average Price
# Institutions use VWAP as a benchmark. Price below VWAP = potential buy.
VWAP_BUY_THRESHOLD = -0.02    # Buy when price is 2%+ below VWAP
VWAP_SELL_THRESHOLD = 0.02    # Sell when price is 2%+ above VWAP

# ============================================================
# STOCK UNIVERSE
# ============================================================
# The bot picks from these stocks. Focuses on liquid large-caps
# that have tight spreads and reliable data.

STOCK_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
    "JPM", "V", "MA", "UNH", "JNJ", "PG", "HD",
    "BAC", "XOM", "CVX", "ABBV", "KO", "PEP",
    "COST", "MCD", "CRM", "ADBE", "NFLX", "AMD",
    "INTC", "QCOM", "AVGO", "DIS", "NKE", "WMT",
]

# ============================================================
# TIMING
# ============================================================
# How often to run the full analysis cycle (in seconds)
CHECK_INTERVAL_SECONDS = 300   # 5 minutes

# Only trade during market hours? (9:30 AM - 4:00 PM ET)
RESPECT_MARKET_HOURS = True

# ============================================================
# STOP-LOSS & TAKE-PROFIT
# ============================================================
# ATR-based (adapts to each stock's volatility)
# Multiplier * ATR = distance from entry price
ATR_PERIOD = 14
STOP_LOSS_ATR_MULTIPLIER = 2.0    # Stop-loss at 2x ATR below entry
TAKE_PROFIT_ATR_MULTIPLIER = 3.0  # Take-profit at 3x ATR above entry

# Hard limits as safety net (override ATR if exceeded)
HARD_STOP_LOSS_PERCENT = 8.0      # Never lose more than 8% on a trade
HARD_TAKE_PROFIT_PERCENT = 15.0   # Always take profit at 15%

# ============================================================
# CRYPTO TRADING
# ============================================================
# Enable/disable the crypto trading cycle entirely
ENABLE_CRYPTO = True

# All liquid crypto pairs available on Alpaca
CRYPTO_UNIVERSE = [
    "BTC/USD",   # Bitcoin
    "ETH/USD",   # Ethereum
    "SOL/USD",   # Solana
    "AVAX/USD",  # Avalanche
    "LINK/USD",  # Chainlink
    "DOT/USD",   # Polkadot
    "ADA/USD",   # Cardano
    "DOGE/USD",  # Dogecoin
    "LTC/USD",   # Litecoin
    "BCH/USD",   # Bitcoin Cash
    "UNI/USD",   # Uniswap
    "AAVE/USD",  # Aave
    "XRP/USD",   # Ripple
    "SHIB/USD",  # Shiba Inu
    "MKR/USD",   # Maker
    "BAT/USD",   # Basic Attention Token
]

# Max fraction of portfolio allocated to ALL crypto combined (20%)
MAX_CRYPTO_PORTFOLIO_ALLOCATION = 0.20

# Max number of simultaneous crypto positions
MAX_CRYPTO_POSITIONS = 5

# Max position size per crypto as fraction of portfolio (5%)
MAX_CRYPTO_POSITION_WEIGHT = 0.05

# Dollar amount to risk per crypto trade (1.5% of portfolio)
CRYPTO_RISK_PER_TRADE = 0.015

# ATR multipliers for crypto (wider than stocks — crypto is more volatile)
CRYPTO_STOP_LOSS_ATR_MULTIPLIER = 3.0
CRYPTO_TAKE_PROFIT_ATR_MULTIPLIER = 4.5

# Hard limits for crypto
CRYPTO_HARD_STOP_LOSS_PERCENT = 12.0
CRYPTO_HARD_TAKE_PROFIT_PERCENT = 25.0

# Minimum combined strategy score to trigger a crypto buy
CRYPTO_BUY_THRESHOLD = 0.25

# ============================================================
# DASHBOARD
# ============================================================
# URL where your Streamlit dashboard is hosted.
# After deploying to Streamlit Cloud (free), paste the URL here.
# Example: "https://alpaca-trading-bot.streamlit.app"
DASHBOARD_URL = ""

# ============================================================
# FRACTIONAL KELLY CRITERION
# ============================================================
# The mathematically optimal position sizing formula derived from
# information theory (Kelly, 1956; Thorp, 1969).
#
# Full Kelly fraction:  f* = (μ - r) / σ²
# Applied fraction:     size = portfolio × (f* × KELLY_FRACTION)
#
# 1.0 = Full Kelly     — maximum theoretical growth, very high drawdowns
# 0.5 = Half Kelly     — ~75% of max growth, much smoother equity curve
# 0.25 = Quarter Kelly — standard institutional default (Renaissance Tech.)
# 0.1  = Tenth Kelly   — very conservative, suitable for live trading start
KELLY_FRACTION = 0.25

# Minimum position size floor as % of portfolio (prevents Kelly from
# sizing positions to near-zero on low-conviction signals)
KELLY_MIN_PCT = 0.005   # 0.5% of portfolio minimum

# ============================================================
# VALUE AT RISK (VaR) CIRCUIT BREAKER
# ============================================================
# If the portfolio's estimated 1-day 99% VaR exceeds VAR_LIMIT_PCT,
# the risk manager blocks ALL new position openings until exposure
# is reduced. This prevents over-exposure during volatile periods.
#
# Example: VAR_LIMIT_PCT = 3.0 means if the portfolio could lose
# more than 3% in a single day at 99% confidence, stop buying.
VAR_LIMIT_PCT = 3.0        # Block new positions if 1-day VaR > 3%
VAR_CONFIDENCE = 0.99      # 99% confidence interval

# ============================================================
# STATISTICAL ARBITRAGE — PAIRS TRADING
# ============================================================
# Market-neutral cointegration strategy. For each pair, the bot:
#   1. Tests for cointegration (Engle-Granger ADF test)
#   2. Monitors the Z-score of the price spread
#   3. When |Z| > PAIRS_ZSCORE_ENTRY → open long/short pair trade
#   4. When |Z| < PAIRS_ZSCORE_EXIT  → close both legs
#
# Requires a margin account (>$2,000) for short selling.
# These pairs are chosen for historically strong cointegration.

ENABLE_PAIRS_TRADING = True

PAIRS_UNIVERSE = [
    # Consumer staples / beverages (classic cointegrated pair)
    ("KO",   "PEP"),
    # Big US banks (same macro drivers)
    ("JPM",  "BAC"),
    # Social media / digital advertising
    ("META", "GOOGL"),
    # Semiconductors
    ("AMD",  "INTC"),
    # Credit card networks
    ("V",    "MA"),
    # Oil majors
    ("XOM",  "CVX"),
    # E-commerce platforms
    ("AMZN", "COST"),
    # Healthcare pharma
    ("JNJ",  "ABBV"),
]

# Z-score thresholds
PAIRS_ZSCORE_ENTRY = 2.0   # Open trade when spread deviates >2σ
PAIRS_ZSCORE_EXIT  = 0.5   # Close trade when spread reverts to 0.5σ

# Rolling window for Z-score normalisation (trading days)
PAIRS_LOOKBACK = 60

# Max portfolio allocation per pair leg (each side independently capped)
PAIRS_MAX_ALLOCATION = 0.04   # 4% per leg = 8% total per active pair

# Max simultaneous open pair trades
PAIRS_MAX_OPEN = 3

# ============================================================
# LOGGING
# ============================================================
ENABLE_LOGGING = True
LOG_FILE = "bot_activity.log"
