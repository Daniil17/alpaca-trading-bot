# Advanced Multi-Strategy Trading Bot (Alpaca)

An autonomous stock trading bot that combines four hedge fund-style strategies, institutional risk management, and real-time Telegram notifications.

## How It Works

The bot runs four strategies simultaneously, each scoring every stock from -1 (sell) to +1 (buy). Scores are blended using weighted voting to make final decisions.

### Strategies

**1. Mean Reversion (RSI + Bollinger Bands)** — Contrarian approach. Buys when stocks are oversold (RSI < 30, price near lower Bollinger Band). Sells when overbought. Bets that extremes revert to the mean.

**2. Momentum (EMA Crossover + ADX)** — Trend following. Buys when the fast EMA crosses above the slow EMA, confirmed by strong ADX (>25). Rides winners.

**3. News Sentiment + Pullback** — Uses news for stock selection, RSI for timing. When good news spikes a stock, it waits for the RSI pullback to 40-55 range instead of buying at the peak.

**4. VWAP (Volume Weighted Average Price)** — Institutional benchmark. Buys when price is below VWAP (discount), sells when above (premium). Confirmed by volume.

### Risk Management

- ATR-based position sizing (volatile stocks get smaller positions)
- Portfolio-level allocation caps (20% cash reserve)
- Maximum drawdown circuit breaker (pauses buying if portfolio drops 10%)
- Sector diversification limits (max 3 positions per sector)
- Per-position stop-loss (2x ATR) and take-profit (3x ATR)
- Hard stop at -8% and hard take-profit at +15% as safety nets

## Setup

### 1. Install Python
Download Python 3.10+ from https://www.python.org/downloads/
Check "Add Python to PATH" during installation.

### 2. Install Dependencies
Open a terminal in this folder and run:
```
pip install -r requirements.txt
```

### 3. Get Alpaca API Keys
1. Sign up at https://alpaca.markets (free)
2. Go to Paper Trading > API Keys
3. Generate a new key pair

### 4. Set Up Telegram Bot
1. Open Telegram, search for @BotFather
2. Send /newbot and follow the prompts
3. Copy the bot token
4. Send any message to your new bot
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
6. Find your `chat_id` in the response

### 5. Configure
Open `config.py` and fill in:
- `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`

### 6. Run
```
python bot.py
```

## Files

| File | Purpose |
|------|---------|
| `config.py` | All settings — API keys, strategy params, risk limits |
| `bot.py` | Main orchestrator — run this |
| `alpaca_api.py` | Alpaca API wrapper (orders, positions, market data) |
| `strategies.py` | Four-strategy engine with weighted scoring |
| `risk_manager.py` | Position sizing, drawdown protection, sector limits |
| `news_scanner.py` | RSS news sentiment analyzer |
| `telegram_bot.py` | Telegram notification system |

## Disclaimer

This bot is for educational purposes. It is not financial advice. Always start with paper trading. You are responsible for any trades it makes. Past performance does not guarantee future results.
