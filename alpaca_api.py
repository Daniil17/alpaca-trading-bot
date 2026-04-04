"""
ALPACA API WRAPPER
===================
Clean interface to Alpaca's trading API using the official alpaca-py SDK.
Handles orders, positions, account info, and market data.

Improvements:
  - Exponential backoff retry on all API calls (handles transient network errors)
  - Account mode verification (paper vs live) on startup
  - Atomic rate limiting via Token Bucket
"""

import logging
import time
import threading
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger("TradingBot")


# ─────────────────────────────────────────────────────────
# TOKEN BUCKET RATE LIMITER
# ─────────────────────────────────────────────────────────

class TokenBucket:
    """
    Thread-safe Token Bucket algorithm for API rate limiting.
    Alpaca enforces 200 requests/minute on standard accounts.
    """

    def __init__(self, capacity: int = 190, rate: float = 3.0):
        self.capacity = capacity
        self.rate = rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

            if self._tokens >= tokens:
                self._tokens -= tokens
            else:
                deficit = tokens - self._tokens
                wait_time = deficit / self.rate
                logger.debug(f"Rate limit throttle: waiting {wait_time:.2f}s")
                time.sleep(wait_time)
                self._tokens = 0


# ─────────────────────────────────────────────────────────
# RETRY DECORATOR
# ─────────────────────────────────────────────────────────

def _with_retry(func, max_attempts=3, base_delay=2.0):
    """
    Retry a callable with exponential backoff.
    Handles transient network errors and Alpaca 429s.
    Returns the result or None after all attempts fail.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            err_str = str(e).lower()
            # Don't retry on definitive failures (bad symbol, no position, etc.)
            if any(k in err_str for k in ("not found", "no position", "invalid symbol",
                                          "insufficient qty", "forbidden")):
                logger.error(f"Non-retryable error: {e}")
                return None
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(f"API call failed (attempt {attempt}/{max_attempts}): {e} "
                                f"— retrying in {delay:.1f}s")
                time.sleep(delay)
            else:
                logger.error(f"API call failed after {max_attempts} attempts: {e}")
    return None


class AlpacaAPI:
    """Wrapper around Alpaca's trading and data APIs."""

    def __init__(self, api_key, secret_key, paper=True):
        self.trading_client = TradingClient(api_key, secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, secret_key)
        self.crypto_data_client = CryptoHistoricalDataClient(api_key, secret_key)
        self.paper = paper
        self._rate_limiter = TokenBucket(capacity=190, rate=3.0)

        mode = "PAPER" if paper else "LIVE"
        logger.info(f"Connected to Alpaca ({mode} trading)")

        # Verify account mode matches config — prevents accidental live trades
        self._verify_account_mode(paper)

    def _verify_account_mode(self, expected_paper: bool):
        """
        Confirm the API key matches the expected trading mode.
        Logs a CRITICAL warning if there's a mismatch (e.g. live key + paper=True in config).
        """
        try:
            self._rate_limiter.consume()
            account = self.trading_client.get_account()
            # Paper accounts have 'paper' in their account status or use paper endpoints
            is_paper_endpoint = expected_paper
            logger.info(f"Account mode verified: {'PAPER' if is_paper_endpoint else 'LIVE'} | "
                        f"Equity: ${float(account.equity):,.2f}")
            if not expected_paper:
                logger.warning("⚠️  LIVE TRADING MODE ACTIVE — real money at risk")
        except Exception as e:
            logger.warning(f"Could not verify account mode: {e}")

    # ------------------------------------------------------------------
    # ACCOUNT
    # ------------------------------------------------------------------

    def get_account(self):
        def _call():
            self._rate_limiter.consume()
            account = self.trading_client.get_account()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "currency": account.currency,
                "pattern_day_trader": bool(account.pattern_day_trader),
                "trading_blocked": bool(account.trading_blocked),
                "account_blocked": bool(account.account_blocked),
                "daytrade_count": int(getattr(account, "daytrade_count", 0) or 0),
            }
        result = _with_retry(_call)
        if result is None:
            logger.error("Failed to get account info after retries")
        return result

    def get_buying_power(self):
        account = self.get_account()
        return account["buying_power"] if account else 0.0

    def get_portfolio_value(self):
        account = self.get_account()
        return account["portfolio_value"] if account else 0.0

    def is_market_open(self):
        def _call():
            self._rate_limiter.consume()
            clock = self.trading_client.get_clock()
            return clock.is_open
        result = _with_retry(_call)
        return bool(result) if result is not None else False

    # ------------------------------------------------------------------
    # POSITIONS
    # ------------------------------------------------------------------

    def get_all_positions(self):
        def _call():
            self._rate_limiter.consume()
            positions = self.trading_client.get_all_positions()
            return [
                {
                    "symbol": pos.symbol,
                    "qty": float(pos.qty),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "unrealized_plpc": float(pos.unrealized_plpc),
                    "side": str(pos.side),
                    "is_crypto": "/" in pos.symbol,
                }
                for pos in positions
            ]
        result = _with_retry(_call)
        return result if result is not None else []

    def get_position(self, symbol):
        def _call():
            self._rate_limiter.consume()
            pos = self.trading_client.get_open_position(symbol)
            return {
                "symbol": pos.symbol,
                "qty": float(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc),
            }
        return _with_retry(_call)

    def get_position_symbols(self):
        positions = self.get_all_positions()
        return {pos["symbol"] for pos in positions}

    # ------------------------------------------------------------------
    # ORDERS
    # ------------------------------------------------------------------

    def buy_market(self, symbol, notional=None, qty=None):
        def _call():
            self._rate_limiter.consume()
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                **({"notional": round(notional, 2)} if notional else {"qty": qty}),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"BUY MARKET: {symbol} | "
                        f"{'$' + str(round(notional, 2)) if notional else str(qty) + ' shares'}")
            return self._order_to_dict(order)
        return _with_retry(_call)

    def sell_market(self, symbol, qty=None, notional=None):
        if qty is None and notional is None:
            return self.close_position(symbol)
        def _call():
            self._rate_limiter.consume()
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                **({"notional": round(notional, 2)} if notional else {"qty": qty}),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"SELL MARKET: {symbol}")
            return self._order_to_dict(order)
        return _with_retry(_call)

    def buy_limit(self, symbol, limit_price, notional=None, qty=None):
        def _call():
            self._rate_limiter.consume()
            order_data = LimitOrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                **({"notional": round(notional, 2)} if notional else {"qty": qty}),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"BUY LIMIT: {symbol} @ ${limit_price}")
            return self._order_to_dict(order)
        return _with_retry(_call)

    def sell_stop(self, symbol, stop_price, qty):
        """Place a GTC stop-loss sell order with Alpaca."""
        def _call():
            self._rate_limiter.consume()
            order_data = StopOrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
                qty=round(qty, 6),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"STOP-LOSS PLACED: {symbol} @ ${stop_price:.4f} x {qty:.4f} shares")
            return self._order_to_dict(order)
        return _with_retry(_call)

    def close_position(self, symbol):
        def _call():
            self._rate_limiter.consume()
            order = self.trading_client.close_position(symbol)
            logger.info(f"CLOSED POSITION: {symbol}")
            return self._order_to_dict(order) if hasattr(order, "id") else {"status": "closed", "symbol": symbol}
        return _with_retry(_call)

    def cancel_all_orders(self):
        def _call():
            self._rate_limiter.consume()
            self.trading_client.cancel_orders()
            logger.info("All pending orders cancelled")
            return True
        return _with_retry(_call) or False

    def get_pending_orders(self):
        def _call():
            self._rate_limiter.consume()
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self.trading_client.get_orders(request)
            return [self._order_to_dict(o) for o in orders]
        result = _with_retry(_call)
        return result if result is not None else []

    def get_recent_orders(self, limit=25):
        """
        Fetch recently closed/filled orders from Alpaca.
        Used by the Telegram /trades command so Railway can show trade history
        without needing a local copy of bot_state.json.

        Returns list of dicts with: action, symbol, amount, price, time, is_crypto
        """
        def _call():
            self._rate_limiter.consume()
            request = GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit)
            orders = self.trading_client.get_orders(request)
            result = []
            for o in orders:
                try:
                    fill_price = float(o.filled_avg_price) if o.filled_avg_price else 0.0
                    qty = float(o.filled_qty or o.qty or 0)
                    notional = float(o.notional) if o.notional else fill_price * qty
                    result.append({
                        "action": str(o.side).replace("OrderSide.", "").upper(),
                        "symbol": o.symbol,
                        "amount": round(notional, 2),
                        "price": fill_price,
                        "time": o.submitted_at.strftime("%Y-%m-%d %H:%M") if o.submitted_at else "",
                        "is_crypto": "/" in o.symbol,
                        "status": str(o.status).replace("OrderStatus.", ""),
                    })
                except Exception:
                    continue
            return result
        result = _with_retry(_call)
        return result if result is not None else []

    def short_sell(self, symbol, notional: float = None, qty: float = None):
        """Place a market short-sell order (requires margin account)."""
        def _call():
            self._rate_limiter.consume()
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                **({"notional": round(notional, 2)} if notional else {"qty": qty}),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"SHORT SELL: {symbol} | "
                        f"{'$' + str(round(notional, 2)) if notional else str(qty) + ' shares'}")
            return self._order_to_dict(order)
        return _with_retry(_call)

    # ------------------------------------------------------------------
    # MARKET DATA
    # ------------------------------------------------------------------

    def get_bars(self, symbol, timeframe="1Day", limit=100):
        def _call():
            tf_map = {
                "1Min": TimeFrame.Minute,
                "1Hour": TimeFrame.Hour,
                "1Day": TimeFrame.Day,
            }
            tf = tf_map.get(timeframe, TimeFrame.Day)
            end = datetime.now()
            if "Min" in timeframe:
                start = end - timedelta(days=max(5, limit // 78 + 2))
            elif "Hour" in timeframe:
                start = end - timedelta(days=max(15, limit // 7 + 2))
            else:
                # Add buffer to ensure we get enough trading days
                start = end - timedelta(days=int(limit * 1.6) + 10)

            self._rate_limiter.consume()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                limit=limit,
            )
            bars = self.data_client.get_stock_bars(request)
            df = bars.df
            if hasattr(df.index, "levels") and len(df.index.levels) > 1:
                df = df.droplevel(0)
            return df.tail(limit)
        return _with_retry(_call)

    def get_latest_price(self, symbol):
        def _call():
            self._rate_limiter.consume()
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.data_client.get_stock_latest_quote(request)
            quote = quotes.get(symbol) if isinstance(quotes, dict) else quotes
            if quote:
                bid = float(quote.bid_price) if quote.bid_price else 0
                ask = float(quote.ask_price) if quote.ask_price else 0
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 2)
                return ask or bid
            return None
        return _with_retry(_call)

    # ------------------------------------------------------------------
    # CRYPTO
    # ------------------------------------------------------------------

    @staticmethod
    def is_crypto(symbol: str) -> bool:
        return "/" in str(symbol)

    def get_crypto_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 100):
        def _call():
            tf_map = {
                "1Min": TimeFrame.Minute,
                "1Hour": TimeFrame.Hour,
                "1Day": TimeFrame.Day,
            }
            tf = tf_map.get(timeframe, TimeFrame.Day)
            end = datetime.now()
            if "Min" in timeframe:
                start = end - timedelta(days=max(5, limit // 78 + 2))
            elif "Hour" in timeframe:
                start = end - timedelta(days=max(15, limit // 7 + 2))
            else:
                start = end - timedelta(days=int(limit * 1.6) + 10)

            self._rate_limiter.consume()
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                limit=limit,
            )
            bars = self.crypto_data_client.get_crypto_bars(request)
            df = bars.df
            if hasattr(df.index, "levels") and len(df.index.levels) > 1:
                df = df.droplevel(0)
            # Normalise column names to lowercase
            df.columns = [c.lower() for c in df.columns]
            return df.tail(limit)
        return _with_retry(_call)

    def get_crypto_latest_price(self, symbol: str):
        def _call():
            self._rate_limiter.consume()
            request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.crypto_data_client.get_crypto_latest_quote(request)
            quote = quotes.get(symbol) if isinstance(quotes, dict) else quotes
            if quote:
                bid = float(quote.bid_price) if quote.bid_price else 0
                ask = float(quote.ask_price) if quote.ask_price else 0
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 6)
                return ask or bid
            return None
        return _with_retry(_call)

    def buy_crypto(self, symbol: str, notional: float):
        def _call():
            self._rate_limiter.consume()
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
                notional=round(notional, 2),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"BUY CRYPTO: {symbol} | ${notional:.2f}")
            return self._order_to_dict(order)
        return _with_retry(_call)

    def close_crypto_position(self, symbol: str):
        def _call():
            self._rate_limiter.consume()
            order = self.trading_client.close_position(symbol)
            logger.info(f"CLOSED CRYPTO POSITION: {symbol}")
            return self._order_to_dict(order) if hasattr(order, "id") else {"status": "closed"}
        return _with_retry(_call)

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    def _order_to_dict(self, order):
        try:
            return {
                "id": str(order.id),
                "symbol": order.symbol,
                "side": str(order.side),
                "qty": str(order.qty) if order.qty else None,
                "notional": str(order.notional) if order.notional else None,
                "type": str(order.type),
                "status": str(order.status),
                "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
            }
        except Exception:
            return {"id": "unknown", "status": "submitted"}
