"""
data_cache.py — Universal candle data cache for all backtest scripts.

Cache key  : symbol + interval + days (e.g. ZECUSDT_5m_180d.parquet)
Freshness  : file mtime vs config.CACHE_MAX_AGE_DAYS (default 7 days)
Format     : Apache Parquet via pandas
On refresh : overwrite in place — no stale file accumulation

Bypass rules:
  config.USE_CACHE = False  → always fetch live
  end_offset_days  > 0      → always fetch live (historical-window runs)
  force_refresh    = True   → skip cache check, fetch fresh and overwrite
"""

import os
import time
import pathlib

import pandas as pd

import config


# ── Public exception ──────────────────────────────────────────────────────────

class CacheMissError(Exception):
    """Raised when cache has no entry and no exchange was provided to fetch."""


# ── Interval label helper ─────────────────────────────────────────────────────

def _ivl_str(interval):
    """Normalise interval to a filename-safe label.

    Examples:
        5, "5"         → "5m"
        60             → "60m"
        240            → "240m"
        "D", "d"       → "D"
    """
    s = str(interval).strip().upper()
    if s == "D":
        return "D"
    return f"{s}m"


# ── DataCache ─────────────────────────────────────────────────────────────────

class DataCache:
    """Strategy-agnostic candle cache.  Shared by all backtest scripts."""

    def __init__(self):
        self._dir     = pathlib.Path(getattr(config, "CACHE_DIR",          "cache"))
        self._max_age = getattr(config, "CACHE_MAX_AGE_DAYS", 7) * 86_400
        self._use     = getattr(config, "USE_CACHE",           True)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── Path helpers ──────────────────────────────────────────────────────────

    def _path(self, symbol, interval, days):
        return self._dir / f"{symbol}_{_ivl_str(interval)}_{days}d.parquet"

    def _is_fresh(self, path):
        p = pathlib.Path(path)
        return p.exists() and (time.time() - p.stat().st_mtime) < self._max_age

    # ── Slice a larger cached file to satisfy a smaller request ───────────────

    def _try_slice_larger(self, symbol, interval, days):
        """Look for any fresh cached file for (symbol, interval) with more days
        than requested, and return a slice covering the most recent `days` days.
        Returns a DataFrame or None if no suitable file found.
        """
        ivl_label = _ivl_str(interval)
        pattern   = f"{symbol}_{ivl_label}_*d.parquet"
        candidates = []
        for p in self._dir.glob(pattern):
            if not self._is_fresh(p):
                continue
            stem  = p.stem            # e.g. "ZECUSDT_5m_180d"
            parts = stem.split("_")
            if len(parts) < 3:
                continue
            try:
                cached_days = int(parts[2].rstrip("d"))
            except ValueError:
                continue
            if cached_days > days:
                candidates.append((cached_days, p))

        if not candidates:
            return None

        # Use the smallest file that still covers the window
        candidates.sort(key=lambda x: x[0])
        cached_days, best_path = candidates[0]

        try:
            df       = pd.read_parquet(best_path)
            cutoff   = int(time.time() * 1000) - days * 86_400_000
            sliced   = df[df["timestamp"] >= cutoff].reset_index(drop=True)
            print(f"  [cache] {symbol} {ivl_label} {days}d — "
                  f"{len(sliced):,} bars sliced from {cached_days}d file")
            return sliced
        except Exception as exc:
            print(f"  [cache] slice failed ({best_path.name}): {exc}")
            return None

    # ── Low-level fetch (delegates to backtest_hybrid) ─────────────────────────

    def _do_fetch(self, exchange, symbol, interval, days, end_offset_days=0):
        """Call the canonical fetch functions in backtest_hybrid."""
        import backtest_hybrid as bh
        ivl = str(interval).strip().upper()
        if ivl == "D":
            return bh.fetch_daily_candles(exchange, symbol, days, end_offset_days)
        return bh.fetch_all_candles(exchange, symbol, int(ivl), days, end_offset_days)

    # ── Main entry point ──────────────────────────────────────────────────────

    def get(self, symbol, interval, days,
            exchange=None, end_offset_days=0, force_refresh=False):
        """Return a DataFrame of raw candles for (symbol, interval, days).

        Behaviour matrix:
          USE_CACHE=False  OR  end_offset_days>0  → fetch live (bypass cache)
          force_refresh=True                      → fetch live, overwrite cache
          cache file fresh                        → read parquet, return
          cache file missing/stale               → fetch live, save parquet, return
          no exchange on any live-fetch path      → raises CacheMissError
        """
        ivl_label = _ivl_str(interval)

        # ── Hard bypass (disabled or historical offset) ───────────────────────
        if not self._use or end_offset_days > 0:
            if exchange is None:
                raise CacheMissError(
                    f"Cache bypassed for {symbol} {ivl_label} {days}d "
                    f"but no exchange supplied."
                )
            return self._do_fetch(exchange, symbol, interval, days, end_offset_days)

        path = self._path(symbol, interval, days)

        # ── Cache hit (exact match) ───────────────────────────────────────────
        if not force_refresh and self._is_fresh(path):
            try:
                df = pd.read_parquet(path)
                print(f"  [cache] {symbol} {ivl_label} {days}d — "
                      f"{len(df):,} bars from disk")
                return df
            except Exception as exc:
                print(f"  [cache] read error for {path.name}: {exc} — re-fetching")

        # ── Cache hit (larger window covers requested days) ───────────────────
        # e.g. 30d request satisfied by slicing a fresh 180d file
        if not force_refresh:
            sliced = self._try_slice_larger(symbol, interval, days)
            if sliced is not None:
                return sliced

        # ── Cache miss / force refresh ────────────────────────────────────────
        if exchange is None:
            raise CacheMissError(
                f"No valid cache for {symbol} {ivl_label} {days}d. "
                f"Run:  python cache_manager.py --refresh"
            )

        df = self._do_fetch(exchange, symbol, interval, days)
        try:
            df.to_parquet(path, index=False)
        except Exception as exc:
            print(f"  [cache] WARNING — could not save {path.name}: {exc}")
        return df

    # ── Bulk refresh ──────────────────────────────────────────────────────────

    def refresh_all(self, exchange, symbols=None, intervals=None, days=180):
        """Fetch and overwrite cached files for all (symbol, interval) combos.

        symbols   defaults to config.APPROVED_COINS
        intervals defaults to ["5", "60", "240", "15"] (plus "D" if REGIME_FILTER)
        """
        if symbols is None:
            symbols = list(config.APPROVED_COINS)
        if intervals is None:
            intervals = ["5", "60", "240", "15"]
            if getattr(config, "REGIME_FILTER", False):
                intervals.append("D")

        total = len(symbols) * len(intervals)
        done  = 0
        errors = 0

        print(f"\nRefreshing cache: {len(symbols)} coins × "
              f"{len(intervals)} timeframes = {total} files  (days={days})")
        print("─" * 60)

        for sym in symbols:
            for ivl in intervals:
                done += 1
                fetch_days = (days + 60) if str(ivl).upper() == "D" else days
                ivl_label = _ivl_str(ivl)
                print(f"  [{done:>{len(str(total))}}/{total}] "
                      f"{sym} {ivl_label} {fetch_days}d ...",
                      end="", flush=True)
                t0 = time.time()
                try:
                    df = self._do_fetch(exchange, sym, ivl, fetch_days)
                    path = self._path(sym, ivl, days)
                    df.to_parquet(path, index=False)
                    print(f" {len(df):,} bars  ({time.time() - t0:.1f}s)")
                except Exception as exc:
                    errors += 1
                    print(f" FAILED: {exc}")
                time.sleep(0.4)   # gentle rate limit between fetches

        print("─" * 60)
        ok = total - errors
        print(f"  Done: {ok}/{total} succeeded"
              + (f", {errors} failed" if errors else "") + ".")

    # ── Cache management ──────────────────────────────────────────────────────

    def clear(self, symbol=None, interval=None):
        """Delete cache files.

        clear()                → delete everything
        clear("ZECUSDT")       → delete all TFs for ZECUSDT
        clear("ZECUSDT", "5")  → delete ZECUSDT_5m_*.parquet only
        """
        deleted = 0
        for p in sorted(self._dir.glob("*.parquet")):
            name  = p.stem   # e.g. "ZECUSDT_5m_180d"
            parts = name.split("_")
            sym_match = (symbol   is None) or (len(parts) >= 1 and parts[0] == symbol)
            ivl_match = (interval is None) or (len(parts) >= 2 and parts[1] == _ivl_str(interval))
            if sym_match and ivl_match:
                p.unlink()
                deleted += 1
        noun = "file" if deleted == 1 else "files"
        print(f"  Deleted {deleted} cache {noun}.")

    def status(self):
        """Print an inventory table of all cached files."""
        files = sorted(self._dir.glob("*.parquet"))
        if not files:
            print("  Cache is empty.")
            return

        now = time.time()
        print(f"\n  {'Symbol':<16} {'TF':<6} {'Days':<6} {'Age':<12} "
              f"{'Size':<9} State")
        print(f"  {'-' * 58}")

        for p in files:
            name  = p.stem
            parts = name.split("_")
            sym      = parts[0]                      if len(parts) >= 1 else "?"
            ivl      = parts[1]                      if len(parts) >= 2 else "?"
            days_str = parts[2].rstrip("d")          if len(parts) >= 3 else "?"

            age_secs = now - p.stat().st_mtime
            if age_secs < 3600:
                age_str = f"{age_secs / 60:.0f}m ago"
            elif age_secs < 86_400:
                age_str = f"{age_secs / 3600:.1f}h ago"
            else:
                age_str = f"{age_secs / 86_400:.1f}d ago"

            size_b  = p.stat().st_size
            size_str = (f"{size_b / 1_048_576:.1f}MB" if size_b >= 1_048_576
                        else f"{size_b / 1024:.0f}KB")

            state = "valid" if self._is_fresh(p) else "STALE"
            print(f"  {sym:<16} {ivl:<6} {days_str:<6} {age_str:<12} "
                  f"{size_str:<9} {state}")

        print()
