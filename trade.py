import logging

import exchange
import config

logger = logging.getLogger(__name__)


def calculate_qty(symbol, entry, sl, balance):
    """
    Position sizing — three-layer safety model:

    1. Risk layer   : qty = RISK_PER_TRADE / SL_distance
                      → max loss on a full stop-out = $25
    2. Margin layer : qty capped so position notional ≤
                      balance × MAX_LEVERAGE × MAX_MARGIN_PCT
                      → never uses more than 20 % of the account as margin
    3. Exchange min : rounded down to the qty step; returns None if sub-minimum
    """
    sl_distance = abs(entry - sl)
    if sl_distance == 0:
        return None

    # Layer 1 — risk-based (fee-adjusted)
    # Bake round-trip fees into the denominator so that when SL is hit,
    # price_loss + entry_fee + exit_fee == RISK_PER_TRADE exactly.
    # Formula: qty * (sl_dist + 2 * entry * fee_rate) == RISK_PER_TRADE
    fee_rate  = getattr(config, "TAKER_FEE_RATE", 0.0)
    fee_adj   = 2 * entry * fee_rate   # extra cost per coin for round-trip fees
    risk_qty  = config.RISK_PER_TRADE / (sl_distance + fee_adj)

    # Layer 2 — margin cap
    max_notional     = balance * config.MAX_LEVERAGE * config.MAX_MARGIN_PCT
    margin_cap_qty   = max_notional / entry if entry > 0 else risk_qty

    raw_qty = min(risk_qty, margin_cap_qty)
    logger.debug(
        "%s  risk_qty=%.6f  margin_cap_qty=%.6f  chosen=%.6f",
        symbol, risk_qty, margin_cap_qty, raw_qty,
    )
    return exchange.round_qty(symbol, raw_qty)


def open_long(symbol, entry, sl, tp, use_limit: bool = False):
    """Set leverage, size the position safely, then place a Buy order."""
    exchange.set_leverage(symbol, config.MAX_LEVERAGE)
    balance = exchange.get_wallet_balance()
    sl_r    = exchange.round_price(symbol, sl)
    tp_r    = exchange.round_price(symbol, tp)
    qty     = calculate_qty(symbol, entry, sl_r, balance)
    if qty is None:
        logger.warning("%s: calculated qty below exchange minimum — skipping long", symbol)
        return None
    entry_r = exchange.round_price(symbol, entry)
    limit_r = None
    if use_limit:
        offset  = getattr(config, "LIMIT_ENTRY_OFFSET_PCT", 0.0)
        limit_r = exchange.round_price(symbol, entry * (1 - offset))
    order_info = exchange.place_order(symbol, "Buy", qty, sl=sl_r, tp=tp_r, limit_price=limit_r)
    entry_type = order_info.get("entry_type", "market")
    wait_secs  = order_info.get("wait_secs", 0.0)
    logger.info(
        "LONG  opened: %s  qty=%s  entry≈%.4f  sl=%s  tp=%s  leverage=%dx  balance=$%.2f  entry_type=%s",
        symbol, qty, entry, sl_r, tp_r, config.MAX_LEVERAGE, balance, entry_type,
    )
    return {"symbol": symbol, "side": "Buy",
            "qty": qty, "entry": entry, "sl": sl_r, "tp": tp_r,
            "entry_type": entry_type, "wait_secs": wait_secs}


def open_short(symbol, entry, sl, tp, use_limit: bool = False):
    """Set leverage, size the position safely, then place a Sell order."""
    exchange.set_leverage(symbol, config.MAX_LEVERAGE)
    balance = exchange.get_wallet_balance()
    sl_r    = exchange.round_price(symbol, sl)
    tp_r    = exchange.round_price(symbol, tp)
    qty     = calculate_qty(symbol, entry, sl_r, balance)
    if qty is None:
        logger.warning("%s: calculated qty below exchange minimum — skipping short", symbol)
        return None
    entry_r = exchange.round_price(symbol, entry)
    limit_r = None
    if use_limit:
        offset  = getattr(config, "LIMIT_ENTRY_OFFSET_PCT", 0.0)
        limit_r = exchange.round_price(symbol, entry * (1 + offset))
    order_info = exchange.place_order(symbol, "Sell", qty, sl=sl_r, tp=tp_r, limit_price=limit_r)
    entry_type = order_info.get("entry_type", "market")
    wait_secs  = order_info.get("wait_secs", 0.0)
    logger.info(
        "SHORT opened: %s  qty=%s  entry≈%.4f  sl=%s  tp=%s  leverage=%dx  balance=$%.2f  entry_type=%s",
        symbol, qty, entry, sl_r, tp_r, config.MAX_LEVERAGE, balance, entry_type,
    )
    return {"symbol": symbol, "side": "Sell",
            "qty": qty, "entry": entry, "sl": sl_r, "tp": tp_r,
            "entry_type": entry_type, "wait_secs": wait_secs}


def move_to_breakeven(symbol, entry):
    """Move the position's stop-loss to the entry price (breakeven)."""
    be = exchange.round_price(symbol, entry)
    exchange.set_trading_stop(symbol, stop_loss=be)
    logger.info("Breakeven set:  %s  @  %s", symbol, be)


def close_trade(symbol, side, qty):
    """Close an open position at market price."""
    exchange.close_position(symbol, side, qty)
    logger.info("Position closed: %s  %s  qty=%s", symbol, side, qty)