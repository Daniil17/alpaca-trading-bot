"""
WALK-FORWARD BACKTESTER
========================
Uses the live StrategyEngine to replay historical data and report
basic performance metrics.  Gives you a baseline to compare against
when you change strategy parameters, weights, or add new strategies.

Usage:
    python backtest.py --symbol AAPL --days 365
    python backtest.py --symbol MSFT --days 252 --capital 50000

How it works:
    - Fetches (days + 100) daily bars from Alpaca so the first analysis
      window has the full indicator lookback available.
    - Slides a 100-bar analysis window one day at a time.
    - Opens a position when the engine signals BUY or STRONG_BUY.
    - Closes when: strategy signals SELL/STRONG_SELL, hard stop, or take-profit.
    - Reports win rate, total P&L, Sharpe, max drawdown.

Limitations:
    - Single-symbol at a time (portfolio simulation out of scope).
    - No slippage or commission modelling (add ESTIMATED_SLIPPAGE below).
    - Uses market-close fills; real fills may differ slightly.
"""

import argparse
import logging
import sys

import numpy as np
import pandas as pd

import config
from alpaca_api import AlpacaAPI
from strategies import StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("Backtest")

# Estimated round-trip cost: spread + slippage (0.1% is a reasonable
# large-cap conservative estimate for market orders).
ESTIMATED_SLIPPAGE = 0.001


def _max_drawdown(equity_curve: np.ndarray) -> float:
    """Peak-to-trough drawdown of a cumulative P&L series."""
    if len(equity_curve) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity_curve)
    drawdown = peak - equity_curve
    return float(np.max(drawdown))


def backtest(symbol: str, days: int = 365, initial_capital: float = 100_000.0):
    """
    Walk-forward backtest for a single symbol.

    Returns a DataFrame of individual trades, or None if insufficient data.
    """
    api = AlpacaAPI(
        config.ALPACA_API_KEY,
        config.ALPACA_SECRET_KEY,
        paper=True,
    )

    # Fetch extra bars so the first window has full indicator lookback
    total_bars = days + 120
    logger.info(f"Fetching {total_bars} daily bars for {symbol}...")
    bars = api.get_bars(symbol, "1Day", total_bars)

    if bars is None or len(bars) < 120:
        logger.error(f"Not enough historical data for {symbol} (got {len(bars) if bars is not None else 0} bars)")
        return None

    engine = StrategyEngine(
        weights=config.STRATEGY_WEIGHTS,
        rsi_period=config.RSI_PERIOD,
        rsi_oversold=config.RSI_OVERSOLD,
        rsi_overbought=config.RSI_OVERBOUGHT,
        bb_period=config.BOLLINGER_PERIOD,
        bb_std=config.BOLLINGER_STD_DEV,
        ema_fast=config.EMA_FAST,
        ema_slow=config.EMA_SLOW,
        adx_period=config.ADX_PERIOD,
        adx_threshold=config.ADX_TREND_THRESHOLD,
        news_pullback_rsi_low=config.NEWS_PULLBACK_RSI_LOW,
        news_pullback_rsi_high=config.NEWS_PULLBACK_RSI_HIGH,
        news_rsi_spike_confirm=config.NEWS_RSI_SPIKE_CONFIRM,
        vpt_lookback=getattr(config, "VPT_LOOKBACK", 20),
    )

    # Walk-forward simulation
    capital = initial_capital
    position = None   # {"entry_price": float, "shares": float, "entry_idx": int}
    trades = []

    lookback = 100   # bars to feed the engine per step

    for i in range(lookback, len(bars)):
        window = bars.iloc[i - lookback: i + 1]
        price = float(window["close"].iloc[-1])
        date_str = str(window.index[-1])[:10] if hasattr(window.index[-1], 'strftime') else str(window.index[-1])[:10]

        analysis = engine.analyze(window)
        signal = analysis["signal"]

        if position is None and signal in ("BUY", "STRONG_BUY"):
            # Size: risk_per_trade of current capital, adjusted for slippage
            fill_price = price * (1 + ESTIMATED_SLIPPAGE)
            invest = capital * config.RISK_PER_TRADE
            invest = min(invest, capital * config.MAX_POSITION_WEIGHT)
            shares = invest / fill_price
            capital -= shares * fill_price
            position = {
                "entry_price": fill_price,
                "shares": shares,
                "entry_idx": i,
                "entry_date": date_str,
                "score": analysis["combined_score"],
                "regime": analysis.get("regime", "MIXED"),
            }
            logger.debug(f"[{date_str}] BUY  {symbol} @ ${fill_price:.2f}  "
                         f"score={analysis['combined_score']:.3f}  regime={analysis.get('regime')}")

        elif position is not None:
            pct_change = (price - position["entry_price"]) / position["entry_price"]

            # --- Exit conditions ---
            exit_reason = None

            if signal in ("SELL", "STRONG_SELL"):
                exit_reason = "strategy"
            elif pct_change <= -(config.HARD_STOP_LOSS_PERCENT / 100):
                exit_reason = "hard_stop"
            elif pct_change >= (config.HARD_TAKE_PROFIT_PERCENT / 100):
                exit_reason = "take_profit"

            if exit_reason:
                fill_price = price * (1 - ESTIMATED_SLIPPAGE)
                pnl = position["shares"] * (fill_price - position["entry_price"])
                capital += position["shares"] * fill_price
                holding_days = i - position["entry_idx"]
                pct_return = (fill_price - position["entry_price"]) / position["entry_price"] * 100

                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": date_str,
                    "symbol": symbol,
                    "entry_price": round(position["entry_price"], 4),
                    "exit_price": round(fill_price, 4),
                    "pct_return": round(pct_return, 3),
                    "pnl": round(pnl, 2),
                    "holding_days": holding_days,
                    "reason": exit_reason,
                    "entry_score": position["score"],
                    "regime": position["regime"],
                })

                logger.debug(f"[{date_str}] SELL {symbol} @ ${fill_price:.2f}  "
                             f"P&L={pct_return:+.1f}%  reason={exit_reason}")
                position = None

    # --- Results ---
    if not trades:
        print(f"\nNo trades generated for {symbol} over {days} days.")
        print("Try a longer period or check that the symbol has sufficient data.")
        return None

    df = pd.DataFrame(trades)
    winners = df[df["pnl"] > 0]
    losers = df[df["pnl"] <= 0]

    total_pnl = df["pnl"].sum()
    win_rate = len(winners) / len(df) * 100
    avg_win = float(winners["pct_return"].mean()) if len(winners) > 0 else 0.0
    avg_loss = float(losers["pct_return"].mean()) if len(losers) > 0 else 0.0
    profit_factor = (
        winners["pnl"].sum() / abs(losers["pnl"].sum())
        if len(losers) > 0 and losers["pnl"].sum() != 0 else float("inf")
    )

    # Annualised Sharpe (using per-trade returns)
    returns_arr = df["pct_return"].values
    sharpe = (
        float(returns_arr.mean() / returns_arr.std()) * np.sqrt(252 / max(df["holding_days"].mean(), 1))
        if returns_arr.std() > 0 else 0.0
    )

    # Max drawdown on cumulative P&L curve
    cumulative_pnl = df["pnl"].cumsum().values
    max_dd = _max_drawdown(cumulative_pnl)

    # Exit reason breakdown
    reason_counts = df["reason"].value_counts().to_dict()
    # Regime breakdown (win rate per regime)
    regime_stats = df.groupby("regime")["pnl"].agg(
        trades="count", wins=lambda x: (x > 0).sum()
    ).assign(win_rate=lambda r: r["wins"] / r["trades"] * 100)

    width = 54
    print(f"\n{'=' * width}")
    print(f"  BACKTEST RESULTS — {symbol} ({days} days)")
    print(f"{'=' * width}")
    print(f"  Trades:           {len(df)}")
    print(f"  Win rate:         {win_rate:.1f}%")
    print(f"  Profit factor:    {profit_factor:.2f}x")
    print(f"  Total P&L:        ${total_pnl:+,.2f}")
    print(f"  Avg winner:       +{avg_win:.2f}%")
    print(f"  Avg loser:        {avg_loss:.2f}%")
    print(f"  Sharpe (ann.):    {sharpe:.2f}")
    print(f"  Max drawdown:     ${max_dd:,.2f}")
    print(f"  Avg hold:         {df['holding_days'].mean():.1f} days")
    print(f"  Final capital:    ${capital:,.2f}  (started ${initial_capital:,.2f})")
    print(f"  Exit reasons:     {reason_counts}")
    print(f"\n  Regime breakdown:")
    for regime, row in regime_stats.iterrows():
        print(f"    {regime:<12} {int(row['trades']):>3} trades  {row['win_rate']:.0f}% win rate")
    print(f"{'=' * width}\n")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-forward backtest using the live StrategyEngine")
    parser.add_argument("--symbol", default="AAPL", help="Ticker symbol (default: AAPL)")
    parser.add_argument("--days", type=int, default=365, help="Trading days to test (default: 365)")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Starting capital (default: 100000)")
    args = parser.parse_args()

    backtest(args.symbol, args.days, args.capital)
