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

# What to send notifications for
NOTIFY_ON_BUY = True
NOTIFY_ON_SELL = True
NOTIFY_ON_STOP_LOSS = True
NOTIFY_DAILY_SUMMARY = True       # End-of-day P&L report
NOTIFY_ON_ERROR = True

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
# LOGGING
# ============================================================
ENABLE_LOGGING = True
LOG_FILE = "bot_activity.log"
