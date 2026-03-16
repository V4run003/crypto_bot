"""
cache_manager.py — CLI tool to manage the backtest candle cache.

Usage:
    python cache_manager.py --refresh                 # all coins, all TFs, 180d
    python cache_manager.py --refresh ZECUSDT         # one coin, all TFs
    python cache_manager.py --refresh ZECUSDT 90      # one coin, 90d
    python cache_manager.py --status                  # show inventory
    python cache_manager.py --clear                   # delete all cache files
    python cache_manager.py --clear ZECUSDT           # delete one coin's files
"""

import sys

import exchange
from data_cache import DataCache


def _usage():
    print(__doc__)


def main():
    args = sys.argv[1:]
    if not args:
        _usage()
        return

    cache = DataCache()
    cmd   = args[0]

    # ── --refresh ────────────────────────────────────────────────────────────
    if cmd == "--refresh":
        # Optional positional: coin symbol
        symbol = None
        days   = 180
        for a in args[1:]:
            if a.isdigit():
                days = int(a)
            elif not a.startswith("--"):
                symbol = a
        symbols = [symbol] if symbol else None
        cache.refresh_all(exchange.session, symbols=symbols, days=days)

    # ── --status ─────────────────────────────────────────────────────────────
    elif cmd == "--status":
        cache.status()

    # ── --clear ──────────────────────────────────────────────────────────────
    elif cmd == "--clear":
        symbol = None
        for a in args[1:]:
            if not a.startswith("--"):
                symbol = a
                break
        if symbol:
            print(f"Clearing cache for {symbol} ...")
        else:
            print("Clearing all cache files ...")
        cache.clear(symbol=symbol)

    else:
        print(f"Unknown argument: {cmd}")
        _usage()


if __name__ == "__main__":
    main()
