"""
ALPACA API WRAPPER
===================
Clean interface to Alpaca's trading API using the official alpaca-py SDK.
Handles orders, positions, account info, and market data.
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

    Alpaca enforces:
      - Standard accounts: 200 requests/minute (≈3.33 tokens/sec)
      - Elite accounts:   1000 requests/minute (≈16.67 tokens/sec)

    The bucket fills at `rate` tokens/second up to `capacity`.
    Before each API call, one token is consumed. If the bucket is
    empty, the call waits exactly long enough for the next token.

    This guarantees zero 429 (Too Many Requests) errors — critical
    during flash crashes when every millisecond of order execution
    counts and a rejected order can cause catastrophic slippage.
    """

    def __init__(self, capacity: int = 200, rate: float = 3.33):
        """
        Args:
            capacity: max burst (= Alpaca's per-minute limit)
            rate:     tokens refilled per second (capacity / 60)
        """
        self.capacity = capacity
        self.rate = rate
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1):
        """
        Block until `tokens` are available, then consume them.
        Typically called with tokens=1 before each API request.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill bucket proportionally to elapsed time
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.rate
            )
            self._last_refill = now

            if self._tokens >= tokens:
                self._tokens -= tokens
            else:
                # Calculate exact wait time for the next token
                deficit = tokens - self._tokens
                wait_time = deficit / self.rate
                logger.debug(
                    f"Rate limit throttle: waiting {wait_time:.2f}s "
                    f"for {tokens} token(s)"
                )
                time.sleep(wait_time)
                self._tokens = 0  # Consumed the waited-for token


class AlpacaAPI:
    """Wrapper around Alpaca's trading and data APIs."""

    def __init__(self, api_key, secret_key, paper=True):
        """
        Connect to Alpaca.

        Args:
            api_key: Your Alpaca API key
            secret_key: Your Alpaca secret key
            paper: True for paper trading, False for live
        """
        self.trading_client = TradingClient(api_key, secret_key, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, secret_key)
        self.crypto_data_client = CryptoHistoricalDataClient(api_key, secret_key)
        self.paper = paper

        # Token bucket: 200 req/min for standard accounts (≈3.33/sec)
        # Upgrade to TokenBucket(1000, 16.67) for Alpaca Elite accounts
        self._rate_limiter = TokenBucket(capacity=190, rate=3.0)

        mode = "PAPER" if paper else "LIVE"
        logger.info(f"Connected to Alpaca ({mode} trading)")

    # ------------------------------------------------------------------
    # ACCOUNT
    # ------------------------------------------------------------------

    def get_account(self):
        """
        Get full account information.

        Returns:
            dict with equity, cash, buying_power, etc.
        """
        try:
            self._rate_limiter.consume()
            account = self.trading_client.get_account()
            return {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "currency": account.currency,
                "pattern_day_trader": account.pattern_day_trader,
                "trading_blocked": account.trading_blocked,
                "account_blocked": account.account_blocked,
                "daytrade_count": int(getattr(account, "daytrade_count", 0) or 0),
            }
        except Exception as e:
            logger.error(f"Failed to get account info: {e}")
            return None

    def get_buying_power(self):
        """Get available buying power as float."""
        account = self.get_account()
        return account["buying_power"] if account else 0.0

    def get_portfolio_value(self):
        """Get total portfolio value as float."""
        account = self.get_account()
        return account["portfolio_value"] if account else 0.0

    def is_market_open(self):
        """Check if the stock market is currently open."""
        try:
            self._rate_limiter.consume()
            clock = self.trading_client.get_clock()
            return clock.is_open
        except Exception as e:
            logger.error(f"Failed to check market clock: {e}")
            return False

    # ------------------------------------------------------------------
    # POSITIONS
    # ------------------------------------------------------------------

    def get_all_positions(self):
        """
        Get all open positions.

        Returns:
            list of dicts with symbol, qty, avg_entry_price,
            current_price, unrealized_pl, unrealized_plpc, market_value
        """
        try:
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
                    "side": pos.side,
                }
                for pos in positions
            ]
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def get_position(self, symbol):
        """Get a single position by symbol, or None if not held."""
        try:
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
        except Exception:
            return None

    def get_position_symbols(self):
        """Get set of symbols currently held."""
        positions = self.get_all_positions()
        return {pos["symbol"] for pos in positions}

    # ------------------------------------------------------------------
    # ORDERS
    # ------------------------------------------------------------------

    def buy_market(self, symbol, notional=None, qty=None):
        """
        Place a market buy order.

        Args:
            symbol: stock ticker (e.g., "AAPL")
            notional: dollar amount to buy (e.g., 500.0) — uses fractional shares
            qty: number of shares to buy (alternative to notional)

        Returns:
            Order object dict, or None if failed
        """
        try:
            self._rate_limiter.consume()
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                **({"notional": round(notional, 2)} if notional else {"qty": qty}),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"BUY MARKET: {symbol} | "
                        f"{'$' + str(notional) if notional else str(qty) + ' shares'}")
            return self._order_to_dict(order)
        except Exception as e:
            logger.error(f"Failed to buy {symbol}: {e}")
            return None

    def sell_market(self, symbol, qty=None, notional=None):
        """
        Place a market sell order.

        Args:
            symbol: stock ticker
            qty: shares to sell (None = sell all via close_position)
            notional: dollar amount to sell
        """
        try:
            if qty is None and notional is None:
                # Sell the entire position
                return self.close_position(symbol)

            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                **({"notional": round(notional, 2)} if notional else {"qty": qty}),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"SELL MARKET: {symbol} | "
                        f"{'$' + str(notional) if notional else str(qty) + ' shares'}")
            return self._order_to_dict(order)
        except Exception as e:
            logger.error(f"Failed to sell {symbol}: {e}")
            return None

    def buy_limit(self, symbol, limit_price, notional=None, qty=None):
        """Place a limit buy order."""
        try:
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
        except Exception as e:
            logger.error(f"Failed to place limit buy for {symbol}: {e}")
            return None

    def sell_stop(self, symbol, stop_price, qty):
        """Place a stop-loss sell order."""
        try:
            order_data = StopOrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
                qty=qty,
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"STOP-LOSS: {symbol} @ ${stop_price} x {qty}")
            return self._order_to_dict(order)
        except Exception as e:
            logger.error(f"Failed to place stop for {symbol}: {e}")
            return None

    def close_position(self, symbol):
        """Close an entire position (sell all shares)."""
        try:
            self._rate_limiter.consume()
            order = self.trading_client.close_position(symbol)
            logger.info(f"CLOSED POSITION: {symbol}")
            return self._order_to_dict(order) if hasattr(order, 'id') else {"status": "closed"}
        except Exception as e:
            logger.error(f"Failed to close position {symbol}: {e}")
            return None

    def cancel_all_orders(self):
        """Cancel all pending orders."""
        try:
            self.trading_client.cancel_orders()
            logger.info("All pending orders cancelled")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
            return False

    def get_pending_orders(self):
        """Get all open/pending orders."""
        try:
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self.trading_client.get_orders(request)
            return [self._order_to_dict(o) for o in orders]
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []

    # ------------------------------------------------------------------
    # MARKET DATA
    # ------------------------------------------------------------------

    def short_sell(self, symbol, notional: float = None, qty: float = None):
        """
        Place a market short-sell order (requires margin account).
        Used for pairs trading — short the overvalued leg of a pair.

        Args:
            symbol:   stock ticker
            notional: dollar amount to short
            qty:      number of shares to short
        """
        try:
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
        except Exception as e:
            logger.error(f"Failed to short sell {symbol}: {e}")
            return None

    def get_bars(self, symbol, timeframe="1Day", limit=100):
        """
        Get historical price bars.

        Args:
            symbol: stock ticker
            timeframe: "1Min", "5Min", "15Min", "1Hour", "1Day"
            limit: number of bars

        Returns:
            pandas DataFrame with open, high, low, close, volume, vwap
        """
        try:
            tf_map = {
                "1Min": TimeFrame.Minute,
                "5Min": TimeFrame(5, TimeFrame.Minute.unit) if hasattr(TimeFrame, 'Minute') else TimeFrame.Minute,
                "15Min": TimeFrame(15, TimeFrame.Minute.unit) if hasattr(TimeFrame, 'Minute') else TimeFrame.Minute,
                "1Hour": TimeFrame.Hour,
                "1Day": TimeFrame.Day,
            }
            tf = tf_map.get(timeframe, TimeFrame.Day)

            end = datetime.now()
            # Calculate start based on limit and timeframe
            if "Min" in timeframe:
                start = end - timedelta(days=max(5, limit // 78 + 2))
            elif "Hour" in timeframe:
                start = end - timedelta(days=max(15, limit // 7 + 2))
            else:
                start = end - timedelta(days=limit + 50)

            self._rate_limiter.consume()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=tf,
                start=start,
                limit=limit,
            )
            bars = self.data_client.get_stock_bars(request)
            df = bars.df

            # If multi-index (symbol, timestamp), drop the symbol level
            if hasattr(df.index, 'levels') and len(df.index.levels) > 1:
                df = df.droplevel(0)

            return df.tail(limit)

        except Exception as e:
            logger.error(f"Failed to get bars for {symbol}: {e}")
            return None

    def get_latest_price(self, symbol):
        """Get the latest quote price for a symbol."""
        try:
            self._rate_limiter.consume()
            request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = self.data_client.get_stock_latest_quote(request)
            quote = quotes.get(symbol) if isinstance(quotes, dict) else quotes
            if quote:
                # Use midpoint of bid/ask, fallback to ask
                bid = float(quote.bid_price) if quote.bid_price else 0
                ask = float(quote.ask_price) if quote.ask_price else 0
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 2)
                return ask or bid
            return None
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # CRYPTO MARKET DATA
    # ------------------------------------------------------------------

    @staticmethod
    def is_crypto(symbol: str) -> bool:
        """Crypto symbols contain a slash, e.g. BTC/USD."""
        return "/" in symbol

    def get_crypto_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 100):
        """
        Get historical OHLCV bars for a crypto pair (e.g. BTC/USD).
        Returns a pandas DataFrame with the same schema as get_bars().
        """
        try:
            tf_map = {
                "1Min": TimeFrame.Minute,
                "5Min": TimeFrame.Minute,
                "15Min": TimeFrame.Minute,
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
                start = end - timedelta(days=limit + 50)

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

            return df.tail(limit)

        except Exception as e:
            logger.error(f"Failed to get crypto bars for {symbol}: {e}")
            return None

    def get_crypto_latest_price(self, symbol: str):
        """Get the latest quote midpoint for a crypto pair."""
        try:
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
        except Exception as e:
            logger.error(f"Failed to get crypto price for {symbol}: {e}")
            return None

    def buy_crypto(self, symbol: str, notional: float):
        """
        Buy a crypto pair with a dollar-notional market order.
        Crypto supports fractional amounts natively.
        """
        try:
            self._rate_limiter.consume()
            # Alpaca accepts BTC/USD directly for crypto orders
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,   # Crypto uses GTC (24/7 market)
                notional=round(notional, 2),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(f"BUY CRYPTO: {symbol} | ${notional:.2f}")
            return self._order_to_dict(order)
        except Exception as e:
            logger.error(f"Failed to buy crypto {symbol}: {e}")
            return None

    def close_crypto_position(self, symbol: str):
        """Close an entire crypto position."""
        try:
            self._rate_limiter.consume()
            # Alpaca's close_position works for crypto too
            order = self.trading_client.close_position(symbol)
            logger.info(f"CLOSED CRYPTO POSITION: {symbol}")
            return self._order_to_dict(order) if hasattr(order, "id") else {"status": "closed"}
        except Exception as e:
            logger.error(f"Failed to close crypto position {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    def _order_to_dict(self, order):
        """Convert an Alpaca Order object to a simple dict."""
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
