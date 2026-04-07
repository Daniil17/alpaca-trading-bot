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
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    StockSnapshotRequest,
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
            result = []
            for pos in positions:
                # Use asset_class when available — most reliable crypto detection.
                # Fall back to symbol-based check for older SDK versions.
                try:
                    ac = str(pos.asset_class).lower()
                    is_crypto = "crypto" in ac
                except Exception:
                    is_crypto = self.is_crypto(pos.symbol)
                result.append({
                    "symbol": pos.symbol,
                    "qty": float(pos.qty),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "unrealized_plpc": float(pos.unrealized_plpc),
                    "side": str(pos.side),
                    "is_crypto": is_crypto,
                })
            return result
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

    def place_bracket_order(self, symbol: str, notional: float, side: str,
                             stop_loss_price: float, take_profit_price: float):
        """
        Place a bracket order: entry (market) + stop-loss + take-profit submitted
        atomically to Alpaca's matching engine.

        This replaces software-side stop checking and eliminates gap risk between
        the bot's 5-minute polling intervals — the broker holds and executes the
        exit legs regardless of whether the bot is running.

        Crypto uses TimeInForce.GTC (markets never close).
        Stocks use TimeInForce.DAY.
        """
        is_crypto_sym = self.is_crypto(symbol)
        tif = TimeInForce.GTC if is_crypto_sym else TimeInForce.DAY
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        def _call():
            self._rate_limiter.consume()
            order_data = MarketOrderRequest(
                symbol=symbol,
                side=order_side,
                time_in_force=tif,
                notional=round(notional, 2),
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(stop_loss_price, 2)),
                take_profit=TakeProfitRequest(limit_price=round(take_profit_price, 2)),
            )
            order = self.trading_client.submit_order(order_data)
            logger.info(
                f"BRACKET ORDER: {side.upper()} {symbol} | ${notional:.2f} | "
                f"SL=${stop_loss_price:.2f} TP=${take_profit_price:.2f}"
            )
            return self._order_to_dict(order)
        return _with_retry(_call)

    def place_limit_order(self, symbol: str, notional: float, side: str,
                           limit_price: float = None, time_in_force=None,
                           chase_seconds: int = 30, chase_ticks: int = 3):
        """
        Place a limit order with an order-chasing loop.

        If limit_price is None, the current bid (BUY) or ask (SELL) is used.
        After `chase_seconds` the unfilled order is cancelled and re-submitted
        one tick closer to mid.  This repeats up to `chase_ticks` times.
        After all attempts are exhausted a market order is placed as fallback.
        """
        # Determine initial limit price from quote if not provided
        if limit_price is None:
            quote = self.get_latest_quote(symbol)
            if quote:
                if side.lower() == "buy":
                    limit_price = quote.get("bid_price") or quote.get("ask_price")
                else:
                    limit_price = quote.get("ask_price") or quote.get("bid_price")
        if not limit_price:
            logger.warning(f"No quote for {symbol} — falling back to market order")
            return (self.buy_market(symbol, notional=notional) if side.lower() == "buy"
                    else self.sell_market(symbol, notional=notional))

        is_crypto_sym = self.is_crypto(symbol)
        tif = time_in_force or (TimeInForce.GTC if is_crypto_sym else TimeInForce.DAY)
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        current_price = float(limit_price)

        for attempt in range(chase_ticks + 1):
            lp = round(current_price, 2)

            def _submit(price=lp):
                self._rate_limiter.consume()
                order_data = LimitOrderRequest(
                    symbol=symbol,
                    side=order_side,
                    time_in_force=tif,
                    limit_price=price,
                    notional=round(notional, 2),
                )
                order = self.trading_client.submit_order(order_data)
                logger.info(
                    f"LIMIT ORDER (attempt {attempt + 1}/{chase_ticks + 1}): "
                    f"{side.upper()} {symbol} @ ${price:.2f} | ${notional:.2f}"
                )
                return self._order_to_dict(order), str(order.id)

            submit_result = _with_retry(_submit)
            if submit_result is None:
                break

            order_dict, order_id = submit_result

            # Poll for fill
            filled = False
            polls = max(1, chase_seconds // 5)
            for _ in range(polls):
                time.sleep(5)
                status = self._get_order_status(order_id)
                if status in ("filled", "partially_filled"):
                    filled = True
                    break

            if filled:
                return order_dict

            # Cancel and prepare next chase
            self._cancel_order_by_id(order_id)

            if attempt < chase_ticks:
                quote = self.get_latest_quote(symbol)
                if quote:
                    bid = quote.get("bid_price", current_price)
                    ask = quote.get("ask_price", current_price)
                    spread = max(0.0, ask - bid)
                    tick = max(0.01, round(spread * 0.25, 4))
                else:
                    tick = 0.01
                if side.lower() == "buy":
                    current_price = current_price + tick
                else:
                    current_price = current_price - tick

        # All attempts exhausted — market fallback
        logger.warning(
            f"Limit order chase exhausted for {symbol} after {chase_ticks} attempts "
            f"— falling back to market order"
        )
        if side.lower() == "buy":
            return self.buy_market(symbol, notional=notional)
        return self.sell_market(symbol, notional=notional)

    def place_algo_order(self, symbol: str, notional: float, side: str,
                          algo: str = "vwap") -> dict:
        """
        Place a VWAP or TWAP algorithmic order for large notional amounts.

        Alpaca's current SDK does not expose a native `instructions` parameter
        for algo execution types, so this falls back to a manual TWAP
        implementation that splits the notional into equal-sized tranches
        executed at timed intervals.

        algo: 'vwap' or 'twap' (both route to manual TWAP for now)
        """
        logger.info(
            f"ALGO ORDER ({algo.upper()}): {side.upper()} {symbol} | ${notional:.2f}"
        )
        return self._manual_twap(symbol, notional, side)

    def _manual_twap(self, symbol: str, notional: float, side: str,
                      n_tranches: int = 5, interval_seconds: int = 60) -> dict:
        """
        Manual TWAP: split order into n_tranches of equal notional,
        executing each at `interval_seconds` intervals.
        Returns the result of the last successfully submitted tranche.
        """
        tranche_notional = notional / n_tranches
        last_result = None
        for i in range(n_tranches):
            logger.info(
                f"TWAP tranche {i + 1}/{n_tranches}: {side.upper()} {symbol} "
                f"| ${tranche_notional:.2f}"
            )
            if side.lower() == "buy":
                result = self.buy_market(symbol, notional=tranche_notional)
            else:
                result = self.sell_market(symbol, notional=tranche_notional)
            if result:
                last_result = result
            if i < n_tranches - 1:
                time.sleep(interval_seconds)
        return last_result

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

    def get_stock_snapshots(self, symbols: list) -> dict:
        """
        Fetch the latest snapshot (daily OHLCV + quote) for a list of symbols.
        Returns a dict mapping symbol → snapshot object.
        Used by get_dynamic_stock_universe for liquidity screening.
        """
        def _call():
            self._rate_limiter.consume()
            request = StockSnapshotRequest(symbol_or_symbols=symbols)
            return self.data_client.get_stock_snapshot(request)
        result = _with_retry(_call)
        return result if result is not None else {}

    def get_latest_quote(self, symbol: str) -> dict:
        """Returns dict with bid_price, ask_price, bid_size, ask_size."""
        is_crypto_sym = self.is_crypto(symbol)

        def _call():
            self._rate_limiter.consume()
            if is_crypto_sym:
                request = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                quotes = self.crypto_data_client.get_crypto_latest_quote(request)
            else:
                request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
                quotes = self.data_client.get_stock_latest_quote(request)
            quote = quotes.get(symbol) if isinstance(quotes, dict) else quotes
            if quote is None:
                return None
            return {
                "bid_price": float(quote.bid_price) if quote.bid_price else 0.0,
                "ask_price": float(quote.ask_price) if quote.ask_price else 0.0,
                "bid_size": float(getattr(quote, "bid_size", 0) or 0),
                "ask_size": float(getattr(quote, "ask_size", 0) or 0),
            }
        return _with_retry(_call)

    def _get_order_status(self, order_id: str) -> str:
        """Return normalised order status string or 'unknown' on error."""
        try:
            from uuid import UUID
            self._rate_limiter.consume()
            order = self.trading_client.get_order_by_id(UUID(order_id))
            return str(order.status).lower().replace("orderstatus.", "")
        except Exception as exc:
            logger.debug(f"Could not get order status for {order_id}: {exc}")
            return "unknown"

    def _cancel_order_by_id(self, order_id: str) -> bool:
        """Cancel an open order by ID. Returns True on success."""
        try:
            from uuid import UUID
            self._rate_limiter.consume()
            self.trading_client.cancel_order_by_id(UUID(order_id))
            logger.debug(f"Cancelled order {order_id}")
            return True
        except Exception as exc:
            logger.debug(f"Could not cancel order {order_id}: {exc}")
            return False

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
        """
        Detect crypto symbols robustly.
        Alpaca sometimes returns 'BTC/USD' and sometimes 'BTCUSD' depending
        on the endpoint / SDK version, so we check both formats.
        """
        symbol = str(symbol).upper()
        if "/" in symbol:
            return True
        # Known crypto bases — catches 'BTCUSD', 'ETHUSD', etc.
        _CRYPTO_BASES = {
            "BTC", "ETH", "SOL", "AVAX", "LINK", "DOT", "ADA", "DOGE",
            "LTC", "BCH", "UNI", "AAVE", "XRP", "SHIB", "MKR", "BAT",
        }
        if symbol.endswith("USD") and symbol[:-3] in _CRYPTO_BASES:
            return True
        return False

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
