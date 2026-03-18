"""
Tracks the single open trade and handles the full position lifecycle:
  - Restoring state on bot restart
  - Breakeven stop moves (at 50 % of TP distance)
  - ADX-fade early exits (only after minimum trade duration)
  - Detecting SL/TP closures and reporting PnL
"""
import csv
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import exchange
import strategy
import trade
import risk
import config
import telegram_bot

logger = logging.getLogger(__name__)

_trades:            list               = []   # all currently open trade dicts
_breakeven_moved:   set                = set()  # symbols whose SL was moved to BE
_trade_open_times:  dict               = {}     # symbol -> datetime opened


# ── Public interface ──────────────────────────────────────────────────────────

def is_position_open() -> bool:
    """True if at least one slot is occupied."""
    return len(_trades) > 0


def has_free_slot() -> bool:
    """True if another trade can be opened."""
    max_slots = getattr(config, "MAX_OPEN_SLOTS", 1)
    return len(_trades) < max_slots


def get_open_symbols() -> set:
    """Set of symbols currently held (scanner uses this to skip them)."""
    return {t["symbol"] for t in _trades}


def get_open_trades() -> list:
    """Snapshot of all open trade dicts (for correlation checks)."""
    return list(_trades)


def get_current_trade() -> Optional[dict]:
    """Backward-compat: return first open trade, or None."""
    return _trades[0] if _trades else None


def set_trade(info: dict):
    """Register a newly opened trade (appends to the active slots)."""
    global _trades, _breakeven_moved, _trade_open_times
    sym = info["symbol"]
    _trades.append(info)
    _breakeven_moved.discard(sym)
    _trade_open_times[sym] = datetime.now(timezone.utc)
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
            strategy=info.get("strategy", "WR"),
            entry_type=info.get("entry_type", "market"),
            wait_secs=float(info.get("wait_secs", 0.0)),
        )
    except Exception as exc:
        logger.warning("Telegram notify_trade_opened failed: %s", exc)


def clear_trade(symbol: Optional[str] = None):
    """Remove a trade from the active list.
    Pass symbol to remove a specific trade; omit to clear ALL (legacy).
    """
    global _trades, _breakeven_moved, _trade_open_times
    if symbol is None:
        _trades.clear()
        _breakeven_moved.clear()
        _trade_open_times.clear()
    else:
        _trades = [t for t in _trades if t["symbol"] != symbol]
        _breakeven_moved.discard(symbol)
        _trade_open_times.pop(symbol, None)


def check_open_position(symbol: str):
    """Query the exchange for a specific tracked symbol; returns pos dict or None."""
    return exchange.get_position(symbol)


def enforce_minimum_trade_time(symbol: str) -> bool:
    """Return True when the position has been open for >= MIN_TRADE_DURATION_SECS."""
    open_time = _trade_open_times.get(symbol)
    if open_time is None:
        return True
    elapsed = (datetime.now(timezone.utc) - open_time).total_seconds()
    if elapsed < config.MIN_TRADE_DURATION_SECS:
        logger.debug(
            "Min trade duration not yet reached on %s (%.0f / %d s)",
            symbol, elapsed, config.MIN_TRADE_DURATION_SECS,
        )
        return False
    return True


def move_stop_to_breakeven(symbol: str):
    """Move the open trade's stop-loss to the entry price."""
    trade_info = next((t for t in _trades if t["symbol"] == symbol), None)
    if trade_info is None or symbol in _breakeven_moved:
        return
    trade.move_to_breakeven(symbol, trade_info["entry"])
    trade_info["sl"] = trade_info["entry"]
    _breakeven_moved.add(symbol)
    logger.info("Stop moved to breakeven on %s", symbol)


def manage() -> bool:
    """
    Called each scan cycle.  Manages ALL open slots.
    Returns True if at least one position is still open.
    """
    for t in list(_trades):   # copy: _trades may shrink during iteration
        _manage_one(t)
    return len(_trades) > 0


def _manage_one(trade_info: dict) -> bool:
    """
    Manage a single open trade.
    Returns True — position still open.
    Returns False — position was closed.
    """
    symbol = trade_info["symbol"]
    pos = check_open_position(symbol)
    if pos is None:
        _on_position_closed(trade_info)
        return False

    side   = trade_info["side"]
    entry  = trade_info["entry"]
    tp     = trade_info.get("original_tp") or trade_info.get("tp")

    # ── Unrealized loss cap — catches SL gap / slippage scenarios ────────────
    # Bybit returns unrealisedPnl in the position dict (no extra API call).
    # If price gaps through the SL, this closes the trade before the loss
    # grows beyond 2× the planned risk.
    unrealized_pnl = float(pos.get("unrealisedPnl", 0))
    max_loss = getattr(config, "MAX_UNREALIZED_LOSS", config.RISK_PER_TRADE * 2)
    if unrealized_pnl < -max_loss:
        qty_now = float(pos["size"])
        logger.warning(
            "%s: unrealized loss $%.2f exceeds cap $%.2f — force-closing",
            symbol, unrealized_pnl, max_loss,
        )
        try:
            trade.close_trade(symbol, side, qty_now)
        except Exception as exc:
            logger.error("%s: loss-cap close_trade API call failed: %s — will retry next cycle", symbol, exc)
            return True  # position still open; retry on the next scan cycle
        pnl_est = _estimate_pnl(side, entry, float(pos.get("markPrice", entry)), qty_now)
        risk.update_pnl(pnl_est)
        risk.record_trade_closed()
        try:
            open_time = _trade_open_times.get(symbol)
            duration_mins = (
                (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                if open_time else 0
            )
            s = risk.get_stats()
            try:
                bal = exchange.get_wallet_balance()
            except Exception:
                bal = 0.0
            telegram_bot.notify_trade_closed(
                symbol=symbol, side=side, entry=entry,
                exit_price=float(pos.get("markPrice", entry)),
                pnl=pnl_est, duration_mins=duration_mins,
                daily_pnl=s["daily_loss"], balance=bal,
                dd_left=s["trailing_dd_left"],
                close_reason="loss cap",
            )
            _append_trade_log(
                symbol=symbol, side=side,
                strategy=trade_info.get("strategy", "WR"),
                entry_type=trade_info.get("entry_type", "market"),
                entry=entry, exit_price=float(pos.get("markPrice", entry)),
                pnl=pnl_est, duration_mins=duration_mins,
                close_reason="loss cap",
            )
        except Exception as exc:
            logger.warning("Telegram notify after loss-cap close failed: %s", exc)
        clear_trade(symbol)
        return False

    try:
        candles_5m    = exchange.get_candles(symbol)
        current_price = float(candles_5m[0][4])
        adx_now       = strategy.check_adx(candles_5m)

        # ── Breakeven at 50 % of TP distance ─────────────────────────────────
        if config.BE_ENABLED and symbol not in _breakeven_moved and tp:
            halfway = entry + (tp - entry) * 0.5
            triggered = (
                (side == "Buy"  and current_price >= halfway) or
                (side == "Sell" and current_price <= halfway)
            )
            if triggered:
                move_stop_to_breakeven(symbol)

        # ── ADX-fade early exit ───────────────────────────────────────────────
        if (config.ADX_FADE_ENABLED
                and adx_now is not None
                and adx_now < config.ADX_THRESHOLD
                and enforce_minimum_trade_time(symbol)):
            logger.info("ADX faded to %.2f on %s — closing early", adx_now, symbol)
            qty_now = float(pos["size"])
            try:
                trade.close_trade(symbol, side, qty_now)
            except Exception as exc:
                logger.error("%s: ADX-fade close_trade API call failed: %s — will retry next cycle", symbol, exc)
                return True  # position still open; retry on the next scan cycle
            pnl = _estimate_pnl(side, entry, current_price, qty_now)
            risk.update_pnl(pnl)
            risk.record_trade_closed()
            try:
                open_time = _trade_open_times.get(symbol)
                duration_mins = (
                    (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                    if open_time else 0
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
                _append_trade_log(
                    symbol=symbol, side=side,
                    strategy=trade_info.get("strategy", "WR"),
                    entry_type=trade_info.get("entry_type", "market"),
                    entry=entry, exit_price=current_price,
                    pnl=pnl, duration_mins=duration_mins,
                    close_reason="ADX fade",
                )
            except Exception as exc:
                logger.warning("Telegram notify_trade_closed failed: %s", exc)
            clear_trade(symbol)
            return False

    except Exception as exc:
        logger.error("position_manager._manage_one error on %s: %s", symbol, exc)

    return True


def init_from_exchange():
    """
    Sync internal state with ALL positions open on the exchange.
    Call once on startup so a restart doesn't re-open duplicate trades.
    """
    global _trades, _breakeven_moved, _trade_open_times
    positions = exchange.get_open_positions()
    now = datetime.now(timezone.utc)
    for pos in positions:
        sym = pos["symbol"]
        raw_sl = pos.get("stopLoss")  or "0"
        raw_tp = pos.get("takeProfit") or "0"
        t = {
            "symbol": sym,
            "side":   pos["side"],
            "qty":    float(pos["size"]),
            "entry":  float(pos["avgPrice"]),
            "sl":     float(raw_sl) if float(raw_sl) > 0 else None,
            "tp":     float(raw_tp) if float(raw_tp) > 0 else None,
        }
        _trades.append(t)
        _trade_open_times[sym] = now   # conservative
        risk.record_trade()   # count this slot so daily trade cap is accurate after restart
        logger.info(
            "Resumed open position: %s %s  qty=%s  entry=%s",
            sym, t["side"], t["qty"], t["entry"],
        )
    if not positions:
        logger.info("No existing positions found on startup.")


# ── Private helpers ───────────────────────────────────────────────────────────

def _on_position_closed(trade_info: dict):
    """Handle a trade closed by SL, TP, or manual action on the exchange."""
    symbol    = trade_info["symbol"]
    side      = trade_info["side"]
    entry     = float(trade_info["entry"])
    qty       = float(trade_info["qty"])
    open_time = _trade_open_times.get(symbol)
    logger.info("Position closed (SL/TP/manual): %s", symbol)
    pnl = exchange.get_closed_pnl_for_symbol(symbol)
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
        _append_trade_log(
            symbol=symbol, side=side,
            strategy=trade_info.get("strategy", "WR"),
            entry_type=trade_info.get("entry_type", "market"),
            entry=entry, exit_price=exit_price,
            pnl=pnl, duration_mins=duration_mins,
            close_reason="SL/TP",
        )
    except Exception as exc:
        logger.warning("Telegram notify_trade_closed failed: %s", exc)
    clear_trade(symbol)


def _estimate_pnl(side: str, entry: float, price: float, qty: float) -> float:
    """Rough unrealized PnL for early exits (before exchange confirms)."""
    return (price - entry) * qty if side == "Buy" else (entry - price) * qty


_TRADE_LOG_PATH = "trade_log.csv"
_TRADE_LOG_FIELDS = [
    "datetime", "symbol", "side", "strategy", "entry_type",
    "entry", "exit", "pnl", "duration_mins", "close_reason", "version",
]


def _append_trade_log(
    symbol: str, side: str, strategy: str, entry_type: str,
    entry: float, exit_price: float, pnl: float,
    duration_mins: float, close_reason: str,
):
    """Append one row to the persistent trade log CSV."""
    row = {
        "datetime":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol":       symbol,
        "side":         side,
        "strategy":     strategy,
        "entry_type":   entry_type,
        "entry":        round(entry, 6),
        "exit":         round(exit_price, 6),
        "pnl":          round(pnl, 4),
        "duration_mins": round(duration_mins, 1),
        "close_reason": close_reason,
        "version":      getattr(config, "BOT_VERSION", "?"),
    }
    file_exists = os.path.isfile(_TRADE_LOG_PATH)
    try:
        with open(_TRADE_LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_TRADE_LOG_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:
        logger.warning("trade_log write failed: %s", exc)
