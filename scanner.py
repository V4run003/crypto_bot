"""
Main scan cycle — called on each 5-minute candle close.

Delegates to:
  position_manager  — open trade lifecycle
  market_scanner    — dynamic symbol discovery
  strategy          — signal detection (closed-candle, anti-repaint)
  trade             — order placement
  risk              — daily limits and cooldown
"""
import logging

import exchange
import strategy
import trade
import risk
import config
import market_scanner
import position_manager

logger = logging.getLogger(__name__)


def scan():
    """
    One complete scan cycle:
    - If a position is open, manage it (breakeven / early exit) and return.
    - Otherwise iterate through the top-volume symbol list looking for the
      first valid WR exhaustion signal.
    """
    risk.reset_if_new_day()

    # ── Manage existing trade ─────────────────────────────────────────────────
    if position_manager.is_position_open():
        position_manager.manage()
        return

    # ── Check all risk / cooldown limits ──────────────────────────────────────
    if not risk.can_trade():
        return

    # ── Scan for a new entry signal ───────────────────────────────────────────
    symbols = market_scanner.get_symbols()
    for symbol in symbols:
        logger.info("Scanning %s", symbol)
        try:
            candles_5m = exchange.get_candles(symbol)
            candles_1h = exchange.get_candles_1h(symbol)
            result     = strategy.check_signal(candles_5m, candles_1h)
            sig        = result["signal"]

            if sig == "long":
                logger.info("Bullish exhaustion detected on %s", symbol)
                info = trade.open_long(
                    symbol, result["entry"], result["sl"], result["tp"])
                if info:
                    position_manager.set_trade({**info, "original_tp": result["tp"]})
                    risk.record_trade()
                    logger.info("Opening LONG %s", symbol)
                    break

            elif sig == "short":
                logger.info("Bearish exhaustion detected on %s", symbol)
                info = trade.open_short(
                    symbol, result["entry"], result["sl"], result["tp"])
                if info:
                    position_manager.set_trade({**info, "original_tp": result["tp"]})
                    risk.record_trade()
                    logger.info("Opening SHORT %s", symbol)
                    break

        except Exception as exc:
            logger.error("Error scanning %s: %s", symbol, exc)