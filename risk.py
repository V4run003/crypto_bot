import logging
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)

_daily_loss:      float                    = 0.0
_trade_count:     int                      = 0
_reset_day:       object                   = None   # datetime.date
_last_close_time: Optional[datetime]       = None   # UTC time of last trade close


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


def cooldown_remaining() -> float:
    """Seconds left in the post-trade cooldown (0 if not in cooldown)."""
    if _last_close_time is None:
        return 0.0
    elapsed   = (datetime.now(timezone.utc) - _last_close_time).total_seconds()
    remaining = config.TRADE_COOLDOWN_SECS - elapsed
    return max(0.0, remaining)


def can_trade() -> bool:
    """Return True only when all daily and cooldown limits are within bounds."""
    if _daily_loss <= -config.MAX_DAILY_LOSS:
        logger.warning(
            "Daily loss limit $%s reached: $%.2f",
            config.MAX_DAILY_LOSS, _daily_loss,
        )
        return False
    if _trade_count >= config.MAX_TRADES_PER_DAY:
        logger.warning("Max daily trades %d reached.", config.MAX_TRADES_PER_DAY)
        return False
    cr = cooldown_remaining()
    if cr > 0:
        logger.info("Trade cooldown active: %.0f s remaining.", cr)
        return False
    return True


def record_trade():
    """Increment the daily trade counter when a new trade opens."""
    global _trade_count
    _trade_count += 1
    logger.info(
        "Trade opened — daily count: %d/%d",
        _trade_count, config.MAX_TRADES_PER_DAY,
    )


def record_trade_closed():
    """Start the 15-minute cooldown timer when a trade closes."""
    global _last_close_time
    _last_close_time = datetime.now(timezone.utc)
    logger.info(
        "Trade closed — %d-minute cooldown started.",
        config.TRADE_COOLDOWN_SECS // 60,
    )


def update_pnl(pnl: float):
    """Add a realized PnL amount (positive = profit, negative = loss)."""
    global _daily_loss
    _daily_loss += pnl
    logger.info("PnL update: $%+.2f  |  daily total: $%.2f", pnl, _daily_loss)


def get_stats() -> dict:
    return {
        "daily_loss":       _daily_loss,
        "trade_count":      _trade_count,
        "day":              _reset_day,
        "cooldown_secs":    cooldown_remaining(),
    }