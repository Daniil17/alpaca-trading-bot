# Advanced Multi-Strategy Trading Bot (Alpaca)

An autonomous stock and crypto trading bot combining four hedge fund-style strategies, institutional risk management, real-time Telegram notifications, and an optional live dashboard.

---

## How It Works

Every cycle the bot scores each symbol in the universe from -1 (strong sell) to +1 (strong buy) using four independent strategies. Scores are blended via weighted voting into a single signal that drives buy and sell decisions.

### Equity Strategies

| Strategy | Mechanism | Entry signal |
|---|---|---|
| **Mean Reversion** | RSI + Bollinger Bands | RSI < 30 and price near lower band |
| **Momentum** | EMA crossover + ADX | EMA-12 crosses EMA-26 and ADX > 25 |
| **News Sentiment + Pullback** | RSS keyword scoring | Good news + RSI pullback to 40–55 |
| **VWAP** | Volume-weighted average price | Price > 2% below VWAP |

### Crypto Strategies

Five separate strategies tuned for 24/7 high-volatility markets: Trend+Momentum (EMA+MACD+ADX), Mean Reversion, Breakout, Volatility Mean-Reversion, and Macro Divergence.

### Risk Management

- **Quarter-Kelly position sizing** — scales with conviction and historical Sharpe
- **Max drawdown circuit breaker** — halts buying if portfolio drops 10% from peak
- **CVaR limit** — blocks new positions when tail risk exceeds 4% of portfolio
- **Sector caps** — max 3 positions per sector (Technology, Finance, Healthcare, etc.)
- **Portfolio allocation cap** — max 80% invested, 20% cash reserve
- **Per-position exits** — stop-loss at 2× ATR (hard floor −8%), take-profit at 3× ATR (hard cap +15%)
- **Trailing stops** — lock in gains once a position is up ≥5%; trails 7% below the peak

---

## Setup (Local / GitHub Actions)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get Alpaca API keys

Sign up at [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys. Generate a key pair.

### 3. Set up Telegram bot (optional)

1. Open Telegram, search for **@BotFather**
2. Send `/newbot` and follow the prompts — copy the bot token
3. Send any message to your new bot
4. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` and note your `chat_id`

### 4. Configure

Open `config.py` and fill in:

```python
ALPACA_API_KEY    = "your-key"
ALPACA_SECRET_KEY = "your-secret"
TELEGRAM_BOT_TOKEN = "your-token"   # leave empty to disable notifications
TELEGRAM_CHAT_ID   = "your-chat-id"
```

Or export them as environment variables — `config.py` checks `os.environ` first.

### 5. Run locally

```bash
python main.py          # persistent service (trading + Telegram listener)
python run_once.py      # single cycle — useful for testing
```

---

## GitHub Actions Deployment (free, serverless)

The bot runs on a cron schedule — no server required.

1. Fork / push this repo to GitHub
2. Go to **Settings → Secrets and variables → Actions** and add:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `TELEGRAM_BOT_TOKEN` _(optional)_
   - `TELEGRAM_CHAT_ID` _(optional)_
3. The workflow in `.github/workflows/trading_bot.yml` runs automatically:
   - Every **5 minutes** during NYSE market hours (9:30 AM–4:00 PM ET, Mon–Fri)
   - Every **15 minutes** outside market hours (for crypto and Telegram commands)

State is saved to `bot_state.json` and committed back to the repo after each cycle so nothing is lost between runs.

---

## Railway Deployment (persistent, low-latency)

Railway runs `main.py` as a persistent service — the full trading loop and Telegram listener in a single process, with ~10–20 s per cycle instead of 2–3 min cold start.

### Steps

1. Create a new Railway project and connect your GitHub repo
2. Set these environment variables in the Railway dashboard:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `TELEGRAM_BOT_TOKEN` _(optional)_
   - `TELEGRAM_CHAT_ID` _(optional)_
   - `ALPACA_PAPER` = `true` (change to `false` only for live trading)
3. Railway reads `railway.toml` and automatically starts `python main.py`

> **Note:** `bot_state.json` is not persisted across Railway restarts. For long-running deployments use a Railway Volume or rely on GitHub Actions for state persistence.

---

## Streamlit Dashboard (optional)

The live dashboard (`dashboard.py`) is deployed separately on [Streamlit Cloud](https://share.streamlit.io) — not Railway.

Install dashboard dependencies:

```bash
pip install streamlit plotly
```

Run locally:

```bash
streamlit run dashboard.py
```

For Streamlit Cloud deployment, add your API keys in the **Secrets** section of the dashboard.

---

## Optional Advanced Features

Install `requirements_optional.txt` to enable:

```bash
pip install -r requirements_optional.txt
```

Then toggle features in `config.py`:

| Config flag | Package | Feature |
|---|---|---|
| `USE_ADAPTIVE_WEIGHTS = True` | scikit-optimize | Bayesian strategy weight optimiser |
| `USE_GARCH_CRYPTO_VOL = True` | arch | GARCH(1,1) volatility for crypto CVaR |
| `USE_FINBERT = True` | transformers + torch | Local FinBERT NLP sentiment (~400 MB model) |
| `ENABLE_PAIRS_TRADING = True` | _(core only)_ | Cointegration-based market-neutral trades |
| `ENABLE_CRYPTO = True` | _(core only)_ | 24/7 crypto cycle |

> FinBERT requires `torch`. The model is downloaded on first use and cached. Install the CPU-only wheel to avoid a multi-GB CUDA build:
> ```bash
> pip install torch --extra-index-url https://download.pytorch.org/whl/cpu
> pip install transformers
> ```

---

## File Reference

| File | Purpose |
|---|---|
| `config.py` | All settings — API keys, strategy params, risk limits, feature flags |
| `main.py` | Railway persistent service — trading loop + Telegram listener |
| `run_once.py` | Single-cycle runner used by GitHub Actions |
| `bot.py` | Legacy standalone bot (5-min polling, no Railway/GH Actions) |
| `strategies.py` | Four-strategy equity scoring engine |
| `crypto_strategies.py` | Five crypto-specific strategies |
| `risk_manager.py` | Position sizing, Kelly criterion, drawdown protection |
| `alpaca_api.py` | Alpaca API wrapper — orders, positions, market data |
| `news_scanner.py` | RSS news sentiment analyser (optional FinBERT) |
| `pairs_trading.py` | Cointegration-based statistical arbitrage |
| `telegram_bot.py` | Outbound trade and alert notifications |
| `telegram_commands.py` | Interactive `/status`, `/positions`, `/profit` commands |
| `telegram_listener.py` | Lightweight Telegram-only listener (pairs with GitHub Actions) |
| `dashboard.py` | Streamlit live dashboard — deploy to Streamlit Cloud |
| `state.py` | Atomic JSON state persistence |
| `requirements.txt` | Core dependencies (Railway + GitHub Actions) |
| `requirements_optional.txt` | Optional ML features (FinBERT, GARCH, Bayesian weights) |

---

## Disclaimer

This bot is for educational purposes. It is not financial advice. Always start with paper trading. You are responsible for any trades it makes. Past performance does not guarantee future results.
