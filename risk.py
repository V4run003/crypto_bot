import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)

_daily_loss  = 0.0
_trade_count = 0
_reset_day   = None   # datetime.date


def reset_if_new_day():
    """Reset daily counters when the UTC calendar day changes."""
    global _daily_loss, _trade_count, _reset_day
    today = datetime.now(timezone.utc).date()
    if _reset_day != today:
        if _reset_day is not None:
            logger.info(
                "UTC day rollover — daily reset.  prev loss=$%.2f  prev trades=%d",
                _daily_loss, _trade_count,
            )
        _daily_loss  = 0.0
        _trade_count = 0
        _reset_day   = today


def can_trade():
    """Return True only when all daily risk limits are within bounds."""
    if _daily_loss <= -config.MAX_DAILY_LOSS:
        logger.warning("Daily loss limit $%s reached: $%.2f",
                       config.MAX_DAILY_LOSS, _daily_loss)
        return False
    if _trade_count >= config.MAX_TRADES_PER_DAY:
        logger.warning("Max daily trades %d reached", config.MAX_TRADES_PER_DAY)
        return False
    return True


def record_trade():
    """Increment the daily trade counter."""
    global _trade_count
    _trade_count += 1
    logger.info("Trade recorded — daily count: %d/%d",
                _trade_count, config.MAX_TRADES_PER_DAY)


def update_pnl(pnl):
    """Add a realized PnL amount (positive = profit, negative = loss)."""
    global _daily_loss
    _daily_loss += pnl
    logger.info("PnL update: $%+.2f  |  daily total: $%.2f", pnl, _daily_loss)


def get_stats():
    return {
        "daily_loss":  _daily_loss,
        "trade_count": _trade_count,
        "day":         _reset_day,
    }