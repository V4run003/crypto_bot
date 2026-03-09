"""
Dynamic market discovery.

Fetches all Bybit USDT perpetual futures, filters by 24-hour USD volume
(>= VOLUME_FILTER_USD), sorts by highest volume, and returns the top
TOP_SYMBOLS_COUNT symbols.  The list is cached for SYMBOL_REFRESH_SECS
(30 minutes) to avoid excessive API calls.
"""
import logging
import time

import exchange
import config

logger = logging.getLogger(__name__)

_cached_symbols:    list = []
_last_refresh_time: float = 0.0


def get_symbols() -> list:
    """
    Return a cached list of the top liquid USDT perpetual symbols.
    Automatically refreshes from the exchange every SYMBOL_REFRESH_SECS.
    """
    global _cached_symbols, _last_refresh_time
    now = time.monotonic()
    if not _cached_symbols or (now - _last_refresh_time) >= config.SYMBOL_REFRESH_SECS:
        fresh = _fetch_symbols()
        if fresh:
            _cached_symbols    = fresh
            _last_refresh_time = now
            logger.info("Symbol list refreshed — %d symbols selected.", len(_cached_symbols))
    return _cached_symbols


def _fetch_symbols() -> list:
    try:
        resp    = exchange.session.get_tickers(category="linear")
        tickers = resp["result"]["list"]

        # Only scan APPROVED_COINS that also pass the volume threshold
        approved = set(config.APPROVED_COINS)
        candidates = [
            t for t in tickers
            if t["symbol"] in approved
            and float(t.get("turnover24h", 0)) >= config.VOLUME_FILTER_USD
        ]
        candidates.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        symbols = [t["symbol"] for t in candidates]

        # If volume filter stripped everything, fall back to the full approved list
        if not symbols:
            symbols = list(config.APPROVED_COINS)

        logger.info("Selected symbols: %s", symbols)
        return symbols

    except Exception as exc:
        logger.error(
            "Symbol discovery failed — keeping previous list. Error: %s", exc
        )
        return _cached_symbols or list(config.FALLBACK_COINS)
