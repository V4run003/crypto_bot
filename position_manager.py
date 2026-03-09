"""
Tracks the single open trade and handles the full position lifecycle:
  - Restoring state on bot restart
  - Breakeven stop moves (at 50 % of TP distance)
  - ADX-fade early exits (only after minimum trade duration)
  - Detecting SL/TP closures and reporting PnL
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import exchange
import strategy
import trade
import risk
import config
import telegram_bot

logger = logging.getLogger(__name__)

_current_trade:   Optional[dict]     = None
_breakeven_moved: bool               = False
_trade_open_time: Optional[datetime] = None


# ── Public interface ──────────────────────────────────────────────────────────

def is_position_open() -> bool:
    return _current_trade is not None


def get_current_trade() -> Optional[dict]:
    return _current_trade


def set_trade(info: dict):
    """Register a newly opened trade."""
    global _current_trade, _breakeven_moved, _trade_open_time
    _current_trade   = info
    _breakeven_moved = False
    _trade_open_time = datetime.now(timezone.utc)
    logger.info(
        "Trade registered: %s %s  qty=%s  entry=%.4f  sl=%s  tp=%s",
        info["symbol"], info["side"], info["qty"],
        info["entry"], info["sl"], info["tp"],
    )
    try:
        s = risk.get_stats()
        try:
            bal = exchange.get_wallet_balance()
        except Exception:
            bal = 0.0
        telegram_bot.notify_trade_opened(
            symbol=info["symbol"], side=info["side"],
            entry=float(info["entry"]), sl=float(info["sl"]), tp=float(info["tp"]),
            qty=float(info["qty"]), balance=bal,
            trade_count=s["trade_count"],
        )
    except Exception as exc:
        logger.warning("Telegram notify_trade_opened failed: %s", exc)


def clear_trade():
    """Wipe internal trade state (call after position is confirmed closed)."""
    global _current_trade, _breakeven_moved, _trade_open_time
    _current_trade   = None
    _breakeven_moved = False
    _trade_open_time = None


def check_open_position():
    """Query the exchange for the tracked position; returns pos dict or None."""
    if _current_trade is None:
        return None
    return exchange.get_position(_current_trade["symbol"])


def enforce_minimum_trade_time() -> bool:
    """Return True when the position has been open for >= MIN_TRADE_DURATION_SECS."""
    if _trade_open_time is None:
        return True
    elapsed = (datetime.now(timezone.utc) - _trade_open_time).total_seconds()
    if elapsed < config.MIN_TRADE_DURATION_SECS:
        logger.debug(
            "Min trade duration not yet reached (%.0f / %d s)",
            elapsed, config.MIN_TRADE_DURATION_SECS,
        )
        return False
    return True


def move_stop_to_breakeven():
    """Move the open trade's stop-loss to the entry price."""
    global _breakeven_moved
    if _current_trade is None or _breakeven_moved:
        return
    trade.move_to_breakeven(_current_trade["symbol"], _current_trade["entry"])
    _current_trade["sl"] = _current_trade["entry"]
    _breakeven_moved     = True
    logger.info("Stop moved to breakeven on %s", _current_trade["symbol"])


def manage() -> bool:
    """
    Called each scan cycle while a trade is open.

    Returns True  — position still open.
    Returns False — position was closed (SL/TP or early exit).
    """
    global _current_trade

    pos = check_open_position()
    if pos is None:
        _on_position_closed()
        return False

    symbol = _current_trade["symbol"]
    side   = _current_trade["side"]
    entry  = _current_trade["entry"]
    tp     = _current_trade.get("original_tp") or _current_trade["tp"]

    try:
        candles_5m    = exchange.get_candles(symbol)
        # Use the latest close as current market price (management, not signal)
        current_price = float(candles_5m[0][4])
        adx_now       = strategy.check_adx(candles_5m)

        # ── Breakeven at 50 % of TP distance ─────────────────────────────────
        if not _breakeven_moved:
            halfway = entry + (tp - entry) * 0.5
            triggered = (
                (side == "Buy"  and current_price >= halfway) or
                (side == "Sell" and current_price <= halfway)
            )
            if triggered:
                move_stop_to_breakeven()

        # ── ADX-fade early exit (prop-firm minimum time respected) ────────────
        if adx_now is not None and adx_now < 20 and enforce_minimum_trade_time():
            logger.info(
                "ADX faded to %.2f on %s — closing early", adx_now, symbol
            )
            qty_now = float(pos["size"])
            trade.close_trade(symbol, side, qty_now)
            pnl = _estimate_pnl(side, entry, current_price, qty_now)
            risk.update_pnl(pnl)
            risk.record_trade_closed()
            try:
                duration_mins = (
                    (datetime.now(timezone.utc) - _trade_open_time).total_seconds() / 60
                    if _trade_open_time else 0
                )
                s = risk.get_stats()
                try:
                    bal = exchange.get_wallet_balance()
                except Exception:
                    bal = 0.0
                telegram_bot.notify_trade_closed(
                    symbol=symbol, side=side, entry=entry,
                    exit_price=current_price, pnl=pnl,
                    duration_mins=duration_mins, daily_pnl=s["daily_loss"],
                    balance=bal, dd_left=s["trailing_dd_left"],
                    close_reason="ADX fade",
                )
            except Exception as exc:
                logger.warning("Telegram notify_trade_closed failed: %s", exc)
            clear_trade()
            return False

    except Exception as exc:
        logger.error("position_manager.manage error on %s: %s", symbol, exc)

    return True


def init_from_exchange():
    """
    Sync internal state with any positions open on the exchange.
    Call once on startup so a restart doesn't re-open a duplicate trade.
    """
    global _current_trade, _breakeven_moved, _trade_open_time
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
        _trade_open_time = datetime.now(timezone.utc)   # conservative — assume just opened
        logger.info(
            "Resumed open position: %s %s  qty=%s  entry=%s",
            _current_trade["symbol"], _current_trade["side"],
            _current_trade["qty"],   _current_trade["entry"],
        )
    else:
        logger.info("No existing positions found on startup.")


# ── Private helpers ───────────────────────────────────────────────────────────

def _on_position_closed():
    """Handle a trade closed by SL, TP, or manual action on the exchange."""
    symbol    = _current_trade["symbol"]
    side      = _current_trade["side"]
    entry     = float(_current_trade["entry"])
    qty       = float(_current_trade["qty"])
    open_time = _trade_open_time
    logger.info("Position closed (SL/TP/manual): %s", symbol)
    pnl = exchange.get_closed_pnl_for_symbol(symbol)
    if pnl != 0:
        risk.update_pnl(pnl)
    risk.record_trade_closed()
    risk.update_peak_balance()
    try:
        duration_mins = (
            (datetime.now(timezone.utc) - open_time).total_seconds() / 60
            if open_time else 0
        )
        # Approximate exit price from PnL (exchange fee not included but close enough for notification)
        if qty > 0:
            exit_price = (entry + pnl / qty) if side == "Buy" else (entry - pnl / qty)
        else:
            exit_price = entry
        s = risk.get_stats()
        try:
            bal = exchange.get_wallet_balance()
        except Exception:
            bal = 0.0
        telegram_bot.notify_trade_closed(
            symbol=symbol, side=side, entry=entry,
            exit_price=exit_price, pnl=pnl,
            duration_mins=duration_mins, daily_pnl=s["daily_loss"],
            balance=bal, dd_left=s["trailing_dd_left"],
            close_reason="SL/TP",
        )
    except Exception as exc:
        logger.warning("Telegram notify_trade_closed failed: %s", exc)
    clear_trade()


def _estimate_pnl(side: str, entry: float, price: float, qty: float) -> float:
    """Rough unrealized PnL for early exits (before exchange confirms)."""
    return (price - entry) * qty if side == "Buy" else (entry - price) * qty
