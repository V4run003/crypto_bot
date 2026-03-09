import logging

import exchange
import strategy
import trade
import risk
import config

logger = logging.getLogger(__name__)

# Tracks the currently open trade; None when flat
_current_trade   = None
_breakeven_moved = False


def init_from_exchange():
    """
    Sync internal state with any positions already open on the exchange.
    Call once on startup so a bot restart resumes without opening a duplicate.
    """
    global _current_trade, _breakeven_moved
    positions = exchange.get_open_positions()
    if positions:
        pos = positions[0]
        _current_trade = {
            "symbol": pos["symbol"],
            "side":   pos["side"],
            "qty":    float(pos["size"]),
            "entry":  float(pos["avgPrice"]),
            "sl":     float(pos.get("stopLoss")  or 0),
            "tp":     float(pos.get("takeProfit") or 0),
        }
        _breakeven_moved = False
        logger.info(
            "Resumed open position: %s  %s  qty=%s  entry=%s",
            _current_trade["symbol"], _current_trade["side"],
            _current_trade["qty"],   _current_trade["entry"],
        )
    else:
        logger.info("No existing positions found on startup.")


def scan():
    """
    Single scan cycle.
    - Manages the open trade when one exists (breakeven, early exit).
    - Otherwise scans the top-volume market list for a new entry signal.
    """
    global _current_trade, _breakeven_moved

    risk.reset_if_new_day()

    if _current_trade:
        pos = exchange.get_position(_current_trade["symbol"])
        if pos is None:
            _on_position_closed()
        else:
            _manage_open_trade(pos)
        return

    if not risk.can_trade():
        logger.info("Trading halted: daily risk limits reached.")
        return

    symbols = exchange.get_top_symbols(config.TOP_SYMBOLS_COUNT)
    for symbol in symbols:
        logger.info("Scanning %s", symbol)
        try:
            candles = exchange.get_candles(symbol)
            result  = strategy.check_signal(candles)
            sig     = result["signal"]

            if sig == "long":
                logger.info("LONG signal on %s", symbol)
                info = trade.open_long(
                    symbol, result["entry"], result["sl"], result["tp"])
                if info:
                    _current_trade   = {**info, "original_tp": result["tp"]}
                    _breakeven_moved = False
                    risk.record_trade()
                    logger.info("Trade opened: LONG  %s", symbol)
                    break

            elif sig == "short":
                logger.info("SHORT signal on %s", symbol)
                info = trade.open_short(
                    symbol, result["entry"], result["sl"], result["tp"])
                if info:
                    _current_trade   = {**info, "original_tp": result["tp"]}
                    _breakeven_moved = False
                    risk.record_trade()
                    logger.info("Trade opened: SHORT  %s", symbol)
                    break

        except Exception as exc:
            logger.error("Error scanning %s: %s", symbol, exc)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _on_position_closed():
    """Handle a trade that was closed by SL, TP, or manual action."""
    global _current_trade, _breakeven_moved
    symbol = _current_trade["symbol"]
    logger.info("Position closed (SL/TP/manual): %s", symbol)
    pnl = exchange.get_closed_pnl_for_symbol(symbol)
    if pnl != 0:
        risk.update_pnl(pnl)
    _current_trade   = None
    _breakeven_moved = False


def _manage_open_trade(pos):
    """Apply breakeven logic and ADX-based early exit to the live trade."""
    global _current_trade, _breakeven_moved
    symbol = _current_trade["symbol"]
    side   = _current_trade["side"]
    entry  = _current_trade["entry"]
    tp     = _current_trade.get("original_tp") or _current_trade["tp"]

    try:
        candles       = exchange.get_candles(symbol)
        current_price = float(candles[0][4])   # close of newest candle (newest-first list)
        adx_now       = strategy.check_adx(candles)

        # Move SL to breakeven once price has travelled 50 % toward TP
        if not _breakeven_moved:
            halfway   = entry + (tp - entry) * 0.5
            triggered = (
                (side == "Buy"  and current_price >= halfway) or
                (side == "Sell" and current_price <= halfway)
            )
            if triggered:
                trade.move_to_breakeven(symbol, entry)
                _current_trade["sl"] = entry
                _breakeven_moved     = True

        # Early exit when ADX drops below 20 (trend is fading)
        if adx_now is not None and adx_now < 20:
            logger.info("ADX faded to %.2f on %s — closing early", adx_now, symbol)
            trade.close_trade(symbol, side, float(pos["size"]))
            pnl = _estimate_pnl(side, entry, current_price, float(pos["size"]))
            risk.update_pnl(pnl)
            _current_trade   = None
            _breakeven_moved = False

    except Exception as exc:
        logger.error("Error managing trade %s: %s", symbol, exc)


def _estimate_pnl(side, entry, price, qty):
    """Quick unrealized PnL estimate used when closing a position early."""
    return (price - entry) * qty if side == "Buy" else (entry - price) * qty