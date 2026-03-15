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
    - Manage ALL open positions (SL/TP/BE/ADX fade).
    - If a free slot exists and risk allows, scan for a new entry signal.
    - Correlation rule: two same-direction trades on coins in the same
      SLOT_CLUSTERS bucket are rejected.
    """
    risk.reset_if_new_day()

    # ── Manage existing trades (all slots) ────────────────────────────────────
    position_manager.manage()

    # ── Gate: need a free slot AND all risk limits green ─────────────────────
    if not position_manager.has_free_slot() or not risk.can_trade():
        return

    # ── Scan for a new entry signal ───────────────────────────────────────────
    open_symbols  = position_manager.get_open_symbols()
    open_trades   = position_manager.get_open_trades()
    clusters      = getattr(config, "SLOT_CLUSTERS", [])
    symbols = market_scanner.get_symbols()
    for symbol in symbols:
        if symbol in open_symbols:
            continue   # already holding this coin

        logger.info("Scanning %s", symbol)
        try:
            candles_5m = exchange.get_candles(symbol)
            candles_1h = exchange.get_candles_1h(symbol)
            candles_4h = None
            excl_4h = getattr(config, "HTF_4H_FILTER_EXCLUDED", [])
            if getattr(config, "HTF_4H_FILTER", False) and symbol not in excl_4h:
                candles_4h = exchange.get_candles_4h(symbol)

            sig           = None
            entry = sl = tp = None
            strategy_name = None

            # ── RSI-preferred coins: try RSI first ────────────────────────────
            if config.RSI_STRATEGY and symbol in config.RSI_APPROVED_COINS:
                rsi_result = strategy_rsi.check_signal(
                    candles_5m, candles_1h, candles_4h=candles_4h, symbol=symbol)
                if rsi_result["signal"]:
                    sig           = rsi_result["signal"]
                    entry         = rsi_result["entry"]
                    sl            = rsi_result["sl"]
                    tp            = rsi_result["tp"]
                    strategy_name = "RSI"
                    logger.info("RSI pullback signal on %s: %s", symbol, sig)

            # ── WR exhaustion fallback ────────────────────────────────────────
            if sig is None:
                wr_result = strategy.check_signal(
                    candles_5m, candles_1h, candles_4h=candles_4h, symbol=symbol)
                if wr_result["signal"]:
                    sig           = wr_result["signal"]
                    entry         = wr_result["entry"]
                    sl            = wr_result["sl"]
                    tp            = wr_result["tp"]
                    strategy_name = "WR"
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

            # ── Correlation gate ──────────────────────────────────────────────
            if sig:
                new_side = "Buy" if sig == "long" else "Sell"
                for ot in open_trades:
                    if ot.get("side", "") == new_side:
                        for cluster in clusters:
                            if ot["symbol"] in cluster and symbol in cluster:
                                logger.info(
                                    "CORRELATION: skipping %s %s (same cluster as %s)",
                                    symbol, sig, ot["symbol"])
                                sig = None
                                break
                    if sig is None:
                        break

            if sig == "long":
                logger.info("Bullish signal detected on %s", symbol)
                info = trade.open_long(symbol, entry, sl, tp)
                if info:
                    risk.record_trade()
                    position_manager.set_trade(
                        {**info, "original_tp": tp, "strategy": strategy_name or "WR"}
                    )
                    logger.info("Opening LONG %s", symbol)
                    return True

            elif sig == "short":
                logger.info("Bearish signal detected on %s", symbol)
                info = trade.open_short(symbol, entry, sl, tp)
                if info:
                    risk.record_trade()
                    position_manager.set_trade(
                        {**info, "original_tp": tp, "strategy": strategy_name or "WR"}
                    )
                    logger.info("Opening SHORT %s", symbol)
                    return True

        except Exception as exc:
            logger.error("Error scanning %s: %s", symbol, exc)

    return False