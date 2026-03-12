import logging
import math
import ssl
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from pybit.unified_trading import HTTP
import config

logger = logging.getLogger(__name__)


class _TLS12Adapter(HTTPAdapter):
    """Force TLS 1.2 — fixes OpenSSL 1.1.1 handshake resets on Windows."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        ctx = create_urllib3_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **kwargs)


session = HTTP(
    api_key=config.API_KEY,
    api_secret=config.API_SECRET,
    testnet=config.TESTNET,
    demo=config.DEMO,
    recv_window=10000,
)
# Apply TLS 1.2 immediately after session creation
session.client.mount("https://", _TLS12Adapter())

_instrument_cache = {}


# ── Market discovery ────────────────────────────────────────────────────────

def get_top_symbols(n=30):
    """Return the top N USDT perpetual futures sorted by 24-hour USD turnover."""
    try:
        resp    = session.get_tickers(category="linear")
        tickers = resp["result"]["list"]
        usdt    = [t for t in tickers if t["symbol"].endswith("USDT")]
        usdt.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        symbols = [t["symbol"] for t in usdt[:n]]
        logger.debug("Top %d symbols: %s", n, symbols)
        return symbols
    except Exception as exc:
        logger.error("get_top_symbols failed, using fallback: %s", exc)
        return config.FALLBACK_COINS


# ── Instrument metadata ─────────────────────────────────────────────────────

def get_instrument_info(symbol):
    """Return instrument metadata dict (tick size, qty step, min qty)."""
    if symbol not in _instrument_cache:
        resp = session.get_instruments_info(category="linear", symbol=symbol)
        _instrument_cache[symbol] = resp["result"]["list"][0]
    return _instrument_cache[symbol]


def round_qty(symbol, qty):
    """Round qty *down* to exchange precision; return None if below minimum."""
    info    = get_instrument_info(symbol)
    step    = float(info["lotSizeFilter"]["qtyStep"])
    min_qty = float(info["lotSizeFilter"]["minOrderQty"])
    rounded = math.floor(qty / step) * step
    rounded = round(rounded, _decimal_places(step))
    return rounded if rounded >= min_qty else None


def round_price(symbol, price):
    """Round a price to the instrument's tick size."""
    info = get_instrument_info(symbol)
    tick = float(info["priceFilter"]["tickSize"])
    return round(round(price / tick) * tick, _decimal_places(tick))


def _decimal_places(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[-1]) if "." in s else 0


# ── Market data ─────────────────────────────────────────────────────────────

def get_candles(symbol):
    """Fetch the latest 250 5-minute candles (newest first)."""
    data = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=config.TIMEFRAME,
        limit=250,
    )
    return data["result"]["list"]


def get_candles_1h(symbol):
    """Fetch the latest 250 1-hour candles (newest first) for HTF trend filter."""
    data = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=60,
        limit=250,
    )
    return data["result"]["list"]


def get_candles_4h(symbol):
    """Fetch the latest 250 4-hour candles for the 4H EMA200 trend filter."""
    data = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=240,
        limit=250,
    )
    return data["result"]["list"]


def get_candles_daily(symbol):
    """Fetch the latest 250 daily candles for regime detection."""
    data = session.get_kline(
        category="linear",
        symbol=symbol,
        interval="D",
        limit=250,
    )
    return data["result"]["list"]


# ── Account info ────────────────────────────────────────────────────────────

def get_wallet_balance():
    """Return available USDT balance from the Unified account."""
    resp = session.get_wallet_balance(accountType="UNIFIED")
    for coin in resp["result"]["list"][0]["coin"]:
        if coin["coin"] == "USDT":
            return float(coin["walletBalance"])
    return 0.0


def get_today_pnl():
    """Return today's total realized PnL from all closed trades (UTC day)."""
    try:
        day_start_ms = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
        resp = session.get_closed_pnl(
            category="linear", startTime=day_start_ms, limit=50
        )
        return sum(float(e["closedPnl"]) for e in resp["result"]["list"])
    except Exception as exc:
        logger.error("get_today_pnl failed: %s", exc)
        return 0.0


def get_closed_pnl_for_symbol(symbol):
    """Return the most recent closed PnL entry for a specific symbol today."""
    try:
        day_start_ms = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
        resp    = session.get_closed_pnl(
            category="linear", symbol=symbol, startTime=day_start_ms, limit=5
        )
        records = resp["result"]["list"]
        if records:
            return float(records[0]["closedPnl"])
    except Exception as exc:
        logger.error("get_closed_pnl_for_symbol %s failed: %s", symbol, exc)
    return 0.0


# ── Position management ─────────────────────────────────────────────────────

def get_position(symbol):
    """Return the open position dict for symbol, or None if flat."""
    resp = session.get_positions(category="linear", symbol=symbol)
    for pos in resp["result"]["list"]:
        if float(pos.get("size", 0)) > 0:
            return pos
    return None


def get_open_positions():
    """Return a list of all open USDT linear positions."""
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return [p for p in resp["result"]["list"] if float(p.get("size", 0)) > 0]


# ── Order placement ──────────────────────────────────────────────────────────

def set_leverage(symbol, leverage):
    """Set buy & sell leverage for a symbol in one-way mode."""
    try:
        session.set_leverage(
            category="linear",
            symbol=symbol,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        logger.debug("Leverage set: %s x%s", symbol, leverage)
    except Exception as exc:
        # Bybit returns an error code when leverage is already at the requested
        # value — safe to ignore; log at DEBUG only.
        logger.debug("set_leverage %s x%s: %s", symbol, leverage, exc)


def place_order(symbol, side, qty, sl=None, tp=None, limit_price=None):
    """
    Place an entry order.  If USE_LIMIT_ENTRY is True and limit_price is given,
    tries a post-only limit first (maker fee 0.02%).  If the limit is rejected
    or not filled within LIMIT_ORDER_TIMEOUT_SECS, cancels it and falls back to
    a market order (taker fee 0.06%).
    """
    if config.USE_LIMIT_ENTRY and limit_price is not None:
        filled = _try_limit_order(symbol, side, qty, limit_price, sl, tp)
        if filled:
            return filled
        logger.info("%s: limit unfilled/rejected — falling back to market", symbol)
    return _place_market_order(symbol, side, qty, sl, tp)


def _place_market_order(symbol, side, qty, sl=None, tp=None):
    """Place a market order with optional stop-loss and take-profit."""
    params = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Market",
        "qty":         str(qty),
        "timeInForce": "GoodTillCancel",
    }
    if sl is not None:
        params["stopLoss"]    = str(sl)
        params["slTriggerBy"] = "LastPrice"
    if tp is not None:
        params["takeProfit"]  = str(tp)
        params["tpTriggerBy"] = "LastPrice"
    return session.place_order(**params)


def _try_limit_order(symbol, side, qty, price, sl, tp):
    """
    Place a post-only limit order at `price`.  Poll until filled or timeout.
    Returns the order response dict on fill, or None on rejection/timeout.
    """
    import time
    params = {
        "category":    "linear",
        "symbol":      symbol,
        "side":        side,
        "orderType":   "Limit",
        "qty":         str(qty),
        "price":       str(price),
        "timeInForce": "PostOnly",
    }
    if sl is not None:
        params["stopLoss"]    = str(sl)
        params["slTriggerBy"] = "LastPrice"
    if tp is not None:
        params["takeProfit"]  = str(tp)
        params["tpTriggerBy"] = "LastPrice"

    try:
        resp     = session.place_order(**params)
        order_id = resp["result"]["orderId"]
        logger.info("%s: PostOnly limit placed  orderId=%s  price=%s", symbol, order_id, price)
    except Exception as exc:
        # PostOnly rejected immediately (would cross the spread) — skip straight to market
        logger.info("%s: PostOnly limit rejected (%s)", symbol, exc)
        return None

    # Poll for fill
    deadline = time.monotonic() + config.LIMIT_ORDER_TIMEOUT_SECS
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            history = session.get_order_history(
                category="linear", symbol=symbol, orderId=order_id, limit=1
            )
            orders = history["result"]["list"]
            if orders:
                status = orders[0]["orderStatus"]
                if status == "Filled":
                    logger.info("%s: limit order filled (maker fee)", symbol)
                    return orders[0]
                if status in ("Cancelled", "Rejected", "Deactivated"):
                    logger.info("%s: limit order %s", symbol, status)
                    return None
        except Exception as exc:
            logger.warning("%s: order status poll failed: %s", symbol, exc)

    # Timeout — cancel the unfilled limit then fall back
    try:
        session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
        logger.info("%s: limit order cancelled (timeout %ds)", symbol, config.LIMIT_ORDER_TIMEOUT_SECS)
    except Exception as exc:
        logger.warning("%s: cancel_order failed: %s", symbol, exc)
    return None


def set_trading_stop(symbol, stop_loss=None, take_profit=None):
    """Modify SL and/or TP on an existing position (one-way mode)."""
    params = {
        "category":    "linear",
        "symbol":      symbol,
        "positionIdx": 0,
    }
    if stop_loss is not None:
        params["stopLoss"] = str(stop_loss)
    if take_profit is not None:
        params["takeProfit"] = str(take_profit)
    return session.set_trading_stop(**params)


def close_position(symbol, side, qty):
    """Close an open position with a reduce-only market order."""
    close_side = "Sell" if side == "Buy" else "Buy"
    return session.place_order(
        category="linear",
        symbol=symbol,
        side=close_side,
        orderType="Market",
        qty=str(qty),
        reduceOnly=True,
        timeInForce="GoodTillCancel",
    )