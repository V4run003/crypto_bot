"""
Main scan cycle — called on each 5-minute candle close.

Delegates to:
  position_manager  — open trade lifecycle
  market_scanner    — dynamic symbol discovery
  strategy          — WR exhaustion signal detection
  strategy_rsi      — RSI pullback signal detection (RSI_APPROVED_COINS only)
  trade             — order placement
  risk              — daily limits and cooldown

Hybrid logic:
  • RSI_APPROVED_COINS  → try RSI first; fall back to WR if RSI doesn't fire
  • All other coins     → WR exhaustion only
  • Same-bar tie        → RSI wins (higher backtest PF on those coins)
"""
import logging

import exchange
import strategy
import strategy_rsi
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

            sig   = None
            entry = sl = tp = None

            # ── RSI-preferred coins: try RSI first ────────────────────────────
            if config.RSI_STRATEGY and symbol in config.RSI_APPROVED_COINS:
                rsi_result = strategy_rsi.check_signal(candles_5m, candles_1h)
                if rsi_result["signal"]:
                    sig   = rsi_result["signal"]
                    entry = rsi_result["entry"]
                    sl    = rsi_result["sl"]
                    tp    = rsi_result["tp"]
                    logger.info("RSI pullback signal on %s: %s", symbol, sig)

            # ── WR exhaustion: all coins (fallback for RSI coins, primary for rest) ─
            if sig is None:
                wr_result = strategy.check_signal(candles_5m, candles_1h)
                if wr_result["signal"]:
                    sig   = wr_result["signal"]
                    entry = wr_result["entry"]
                    sl    = wr_result["sl"]
                    tp    = wr_result["tp"]
                    logger.info("WR exhaustion signal on %s: %s", symbol, sig)

            # ── Regime filter — skip counter-trend signals ────────────────────
            if sig and config.REGIME_FILTER:
                candles_1d = exchange.get_candles_daily(symbol)
                regime = strategy.get_regime(candles_1d, symbol)
                if sig == "long" and regime == "bear":
                    logger.info("REGIME: skipping %s LONG (bear regime)", symbol)
                    sig = None
                elif sig == "short" and regime == "bull":
                    logger.info("REGIME: skipping %s SHORT (bull regime)", symbol)
                    sig = None

            if sig == "long":
                logger.info("Bullish signal detected on %s", symbol)
                info = trade.open_long(symbol, entry, sl, tp)
                if info:
                    risk.record_trade()
                    position_manager.set_trade({**info, "original_tp": tp})
                    logger.info("Opening LONG %s", symbol)
                    break

            elif sig == "short":
                logger.info("Bearish signal detected on %s", symbol)
                info = trade.open_short(symbol, entry, sl, tp)
                if info:
                    risk.record_trade()
                    position_manager.set_trade({**info, "original_tp": tp})
                    logger.info("Opening SHORT %s", symbol)
                    break

        except Exception as exc:
            logger.error("Error scanning %s: %s", symbol, exc)