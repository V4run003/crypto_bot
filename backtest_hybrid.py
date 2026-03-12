"""
backtest_hybrid.py — Walk-forward backtest of the Hybrid strategy.

Mirrors scanner.py exactly:
  • RSI_APPROVED_COINS → RSI pullback first; WR exhaustion fallback if RSI silent
  • All other coins    → WR exhaustion only

This lets you measure the true combined numbers and compare directly against
running backtest.py (pure WR) or backtest_rsi.py (pure RSI).

Usage:
    python backtest_hybrid.py                       # BTCUSDT, 90 days
    python backtest_hybrid.py GRTUSDT 180
    python backtest_hybrid.py BTCUSDT 90 --quiet
"""

import bisect
import sys
import time
import logging
from datetime import datetime, timezone

import pandas as pd
import ta

import config

logging.basicConfig(level=logging.WARNING)


# ── Historical data fetching ──────────────────────────────────────────────────

def _fetch_chunk(session, symbol, interval, end_ms, limit=1000):
    for attempt in range(5):
        try:
            resp = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
                end=end_ms,
            )
            # Bybit rate-limit response: retCode 10006 ("Too many visits")
            if resp.get("retCode", 0) == 10006:
                wait = 20 * (attempt + 1)
                print(f"\n  [rate-limit] 10006 — sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
                continue
            return resp["result"]["list"]
        except Exception as exc:
            msg = str(exc).lower()
            if "10006" in msg or "429" in msg or "rate limit" in msg or "too many" in msg:
                wait = 20 * (attempt + 1)
                print(f"\n  [rate-limit] {exc} — sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
            elif attempt == 4:
                raise
            else:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError("_fetch_chunk: still rate-limited after 5 attempts")


def fetch_all_candles(session, symbol, interval, days, end_offset_days=0):
    now_ms   = int(time.time() * 1000)
    end_ms   = now_ms - end_offset_days * 86_400_000
    start_ms = end_ms  - days * 86_400_000
    rows     = []

    label = f"{symbol} {interval}m"
    print(f"  Fetching {label} ({days} days) ...", end="", flush=True)

    while True:
        chunk = _fetch_chunk(session, symbol, interval, end_ms)
        if not chunk:
            break
        rows.extend(chunk)
        oldest_ts = int(chunk[-1][0])
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.12)  # ≈8 req/s — stays under Bybit's 10 req/s public limit

    print(f" {len(rows)} candles")

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = df["timestamp"].astype(int)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df = (
        df.sort_values("timestamp")
          .drop_duplicates("timestamp")
          .reset_index(drop=True)
    )
    df = df[df["timestamp"] >= start_ms].reset_index(drop=True)
    return df


def fetch_daily_candles(session, symbol, days, end_offset_days=0):
    """Fetch daily candles for regime detection (Bybit interval='D')."""
    now_ms   = int(time.time() * 1000)
    end_ms   = now_ms - end_offset_days * 86_400_000
    start_ms = end_ms - days * 86_400_000
    rows     = []

    print(f"  Fetching {symbol} daily ({days}d) ...", end="", flush=True)

    while True:
        chunk = _fetch_chunk(session, symbol, "D", end_ms)
        if not chunk:
            break
        rows.extend(chunk)
        oldest_ts = int(chunk[-1][0])
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.12)

    print(f" {len(rows)} candles")

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = df["timestamp"].astype(int)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = (
        df.sort_values("timestamp")
          .drop_duplicates("timestamp")
          .reset_index(drop=True)
    )
    df = df[df["timestamp"] >= start_ms].reset_index(drop=True)
    return df


# ── Indicator computation ─────────────────────────────────────────────────────

def _build_5m_indicators(df):
    """Build all indicators needed by both WR and RSI strategies."""
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["wr"]     = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
    df["rsi"]    = ta.momentum.rsi(df["close"], window=config.RSI_PERIOD)
    df["atr"]      = ta.volatility.average_true_range(
                         df["high"], df["low"], df["close"],
                         window=config.ATR_PERIOD)
    df["atr_ma"]   = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    return df


def _build_1h_indicators(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


def _build_4h_indicators(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


def _build_daily_indicators(df):
    """Compute the regime EMA on daily candles."""
    df = df.copy()
    df["ema_regime"] = ta.trend.ema_indicator(df["close"], window=config.REGIME_EMA_PERIOD)
    return df


# ── Shared pre-checks (same for both WR and RSI) ─────────────────────────────

def _passes_common_filters(df5, i, df1h, h_idx):
    """
    Returns (passes, price, ema5, atr, htf_long_ok, htf_short_ok) or
    (False, ...) if any shared filter rejects the bar.
    """
    sw    = config.SWING_LOOKBACK
    min_i = max(200, config.WR_EXHAUSTION_LOOKBACK + 1,
                config.RSI_PERIOD + 1, sw)

    if i < min_i or h_idx < 200:
        return False, None, None, None, None, None

    row      = df5.iloc[i]
    row_prev = df5.iloc[i - 1]
    price    = row["close"]
    ema5     = row["ema200"]
    ema50    = row["ema50"]
    adx      = row["adx"]
    adx_prev = row_prev["adx"]
    atr      = row["atr"]
    atr_ma   = row["atr_ma"]

    if any(pd.isna(v) for v in [price, ema5, adx, atr, atr_ma]):
        return False, None, None, None, None, None

    if atr <= atr_ma or adx <= config.ADX_THRESHOLD:
        return False, None, None, None, None, None

    if config.ADX_RISING_FILTER and not pd.isna(adx_prev) and adx <= adx_prev:
        return False, None, None, None, None, None

    if config.ADX_RISING2_FILTER:
        adx_prev2 = df5.iloc[i - 2]["adx"]
        if not (not pd.isna(adx_prev) and not pd.isna(adx_prev2)
                and adx > adx_prev and adx_prev > adx_prev2):
            return False, None, None, None, None, None

    if config.TIME_FILTER:
        bar_hour = datetime.fromtimestamp(
            int(df5["timestamp"].iloc[i]) / 1000, tz=timezone.utc
        ).hour
        if bar_hour < config.TIME_FILTER_START or bar_hour >= config.TIME_FILTER_END:
            return False, None, None, None, None, None

    if config.EMA50_PULLBACK_FILTER and not pd.isna(ema50):
        if abs(price - ema50) > config.EMA50_PULLBACK_ATR_MULT * atr:
            return False, None, None, None, None, None

    # Volume confirmation — hook candle must have above-average volume
    if config.VOLUME_CONFIRM_FILTER:
        vol    = df5.iloc[i]["volume"]
        vol_ma = df5.iloc[i]["vol_ma20"]
        if not pd.isna(vol_ma) and vol < vol_ma * config.VOLUME_CONFIRM_MULT:
            return False, None, None, None, None, None

    # 1H HTF filter
    row_1h   = df1h.iloc[h_idx]
    price_1h = row_1h["close"]
    ema_1h   = row_1h["ema200"]
    htf_long_ok  = True
    htf_short_ok = True
    if config.HTF_FILTER and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    return True, price, ema5, atr, htf_long_ok, htf_short_ok


# ── RSI signal ────────────────────────────────────────────────────────────────

def _check_rsi_signal(df5, i, price, ema5, htf_long_ok, htf_short_ok):
    sw       = config.SWING_LOOKBACK
    rsi      = df5.iloc[i]["rsi"]
    rsi_prev = df5.iloc[i - 1]["rsi"]

    if pd.isna(rsi) or pd.isna(rsi_prev):
        return None, None, None, None

    prev_high = df5.iloc[i - 1]["high"]
    prev_low  = df5.iloc[i - 1]["low"]

    # Long
    if (price > ema5 and htf_long_ok
            and config.RSI_LONG_ZONE_LOW <= rsi <= config.RSI_LONG_ZONE_HIGH
            and rsi > rsi_prev):
        if config.CANDLE_CONFIRM_FILTER and price <= prev_high:
            return None, None, None, None
        if config.RESISTANCE_FILTER:
            _atr = df5.iloc[i]["atr"]
            recent_high = float(df5["high"].iloc[max(0, i - 20) : i].max())
            if not pd.isna(_atr) and price > recent_high - config.RESISTANCE_ATR_MULT * _atr:
                return None, None, None, None  # too close to resistance
        swing_low = float(df5["low"].iloc[i - sw + 1 : i + 1].min())
        sl = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist = price - sl
        if sl_dist > 0:
            return "long", price, sl, price + sl_dist * config.RR

    # Short
    if (price < ema5 and htf_short_ok
            and config.RSI_SHORT_ZONE_LOW <= rsi <= config.RSI_SHORT_ZONE_HIGH
            and rsi < rsi_prev):
        if config.CANDLE_CONFIRM_FILTER and price >= prev_low:
            return None, None, None, None
        if config.RESISTANCE_FILTER:
            _atr = df5.iloc[i]["atr"]
            recent_low = float(df5["low"].iloc[max(0, i - 20) : i].min())
            if not pd.isna(_atr) and price < recent_low + config.RESISTANCE_ATR_MULT * _atr:
                return None, None, None, None  # too close to support
        swing_high = float(df5["high"].iloc[i - sw + 1 : i + 1].max())
        sl = swing_high * (1 + config.SL_BUFFER_PCT)
        sl_dist = sl - price
        if sl_dist > 0:
            return "short", price, sl, price - sl_dist * config.RR

    return None, None, None, None


# ── WR signal ─────────────────────────────────────────────────────────────────

def _check_wr_signal(df5, i, price, ema5, htf_long_ok, htf_short_ok):
    lb = config.WR_EXHAUSTION_LOOKBACK
    sw = config.SWING_LOOKBACK
    wr = df5.iloc[i]["wr"]

    if pd.isna(wr):
        return None, None, None, None

    prev_high = df5.iloc[i - 1]["high"]
    prev_low  = df5.iloc[i - 1]["low"]

    # Long
    if price > ema5 and htf_long_ok and wr > -80:
        prev_wr = df5["wr"].iloc[i - lb : i]
        if (prev_wr < -80).sum() >= lb:
            if config.CANDLE_CONFIRM_FILTER and price <= prev_high:
                return None, None, None, None
            if config.RESISTANCE_FILTER:
                _atr = df5.iloc[i]["atr"]
                recent_high = float(df5["high"].iloc[max(0, i - 20) : i].max())
                if not pd.isna(_atr) and price > recent_high - config.RESISTANCE_ATR_MULT * _atr:
                    return None, None, None, None  # too close to resistance
            swing_low = float(df5["low"].iloc[i - sw + 1 : i + 1].min())
            sl = swing_low * (1 - config.SL_BUFFER_PCT)
            sl_dist = price - sl
            if sl_dist > 0:
                return "long", price, sl, price + sl_dist * config.RR

    # Short
    if price < ema5 and htf_short_ok and wr < -20:
        prev_wr = df5["wr"].iloc[i - lb : i]
        if (prev_wr > -20).sum() >= lb:
            if config.CANDLE_CONFIRM_FILTER and price >= prev_low:
                return None, None, None, None
            if config.RESISTANCE_FILTER:
                _atr = df5.iloc[i]["atr"]
                recent_low = float(df5["low"].iloc[max(0, i - 20) : i].min())
                if not pd.isna(_atr) and price < recent_low + config.RESISTANCE_ATR_MULT * _atr:
                    return None, None, None, None  # too close to support
            swing_high = float(df5["high"].iloc[i - sw + 1 : i + 1].max())
            sl = swing_high * (1 + config.SL_BUFFER_PCT)
            sl_dist = sl - price
            if sl_dist > 0:
                return "short", price, sl, price - sl_dist * config.RR

    return None, None, None, None


# ── Hybrid signal (mirrors scanner.py) ───────────────────────────────────────

def _check_signal(df5, i, df1h, h_idx, symbol, df1d=None, d_idx=-1, df4h=None, h4_idx=-1):
    ok, price, ema5, atr, htf_long_ok, htf_short_ok = \
        _passes_common_filters(df5, i, df1h, h_idx)
    if not ok:
        return None, None, None, None, None

    # ── Regime filter (daily EMA) ─────────────────────────────────────────────
    excluded = getattr(config, "REGIME_FILTER_EXCLUDED", [])
    if config.REGIME_FILTER and symbol not in excluded and df1d is not None and d_idx >= 0:
        row_d   = df1d.iloc[d_idx]
        d_price = row_d["close"]
        d_ema   = row_d["ema_regime"]
        if not pd.isna(d_ema):
            band = config.REGIME_NEUTRAL_PCT
            if d_price < d_ema * (1 - band):    # bear regime → longs forbidden
                htf_long_ok  = False
            elif d_price > d_ema * (1 + band):  # bull regime → shorts forbidden
                htf_short_ok = False

    # ── 4H EMA200 filter ─────────────────────────────────────────────────────
    excl_4h = getattr(config, "HTF_4H_FILTER_EXCLUDED", [])
    if (getattr(config, "HTF_4H_FILTER", False)
            and symbol not in excl_4h
            and df4h is not None and h4_idx >= 200):
        row_4h = df4h.iloc[h4_idx]
        p4h    = float(row_4h["close"])
        ema_4h = row_4h["ema200"]
        if not pd.isna(ema_4h):
            if p4h <= ema_4h:
                htf_long_ok  = False   # 4H bearish — no longs
            if p4h >= ema_4h:
                htf_short_ok = False   # 4H bullish — no shorts

    sig = entry = sl = tp = None
    strategy_used = None

    # RSI first on approved coins
    if config.RSI_STRATEGY and symbol in config.RSI_APPROVED_COINS:
        sig, entry, sl, tp = _check_rsi_signal(
            df5, i, price, ema5, htf_long_ok, htf_short_ok)
        if sig:
            strategy_used = "RSI"

    # WR fallback (or primary for non-RSI coins)
    if sig is None:
        sig, entry, sl, tp = _check_wr_signal(
            df5, i, price, ema5, htf_long_ok, htf_short_ok)
        if sig:
            strategy_used = "WR"

    return sig, entry, sl, tp, strategy_used


# ── Walk-forward simulation ───────────────────────────────────────────────────

def run_backtest(symbol, days, quiet=False):
    import exchange

    mode = "HYBRID" if config.RSI_STRATEGY else "WR-only"
    print(f"\n{'=' * 62}")
    print(f"  {mode} Backtest  |  {symbol}  |  {days} days  |  RR={config.RR}"
          f"  |  risk=${config.RISK_PER_TRADE}/trade")
    if config.RSI_STRATEGY:
        rsi_flag = "RSI+WR fallback" if symbol in config.RSI_APPROVED_COINS else "WR only"
        print(f"  Signal mode: {rsi_flag}")
    print(f"{'=' * 62}")

    df5  = fetch_all_candles(exchange.session, symbol, 5,  days)
    df1h = fetch_all_candles(exchange.session, symbol, 60, days)
    # Daily candles for regime filter (+60d extra for EMA warmup)
    df1d = None
    ts1d = None
    if config.REGIME_FILTER:
        df1d = fetch_daily_candles(exchange.session, symbol, days + 60)
        df1d = _build_daily_indicators(df1d)
        ts1d = df1d["timestamp"].values
    # 4H candles for 4H EMA200 filter
    df4h = None
    ts4h = None
    if getattr(config, "HTF_4H_FILTER", False):
        df4h = fetch_all_candles(exchange.session, symbol, 240, days)
        df4h = _build_4h_indicators(df4h)
        ts4h = df4h["timestamp"].values

    print("  Computing indicators ...", end="", flush=True)
    df5  = _build_5m_indicators(df5)
    df1h = _build_1h_indicators(df1h)
    print(" done\n")

    ts5  = df5["timestamp"].values
    ts1h = df1h["timestamp"].values

    min_start  = max(250, config.WR_EXHAUSTION_LOOKBACK + 1,
                     config.RSI_PERIOD + 1, config.SWING_LOOKBACK)
    trades     = []
    skip_until = -1
    rsi_count  = 0
    wr_count   = 0

    for i in range(min_start, len(df5) - 1):
        if i < skip_until:
            continue

        h_idx  = bisect.bisect_right(ts1h, ts5[i]) - 1
        h4_idx = (bisect.bisect_right(ts4h, ts5[i]) - 1) if ts4h is not None else -1
        d_idx  = -1
        if config.REGIME_FILTER and ts1d is not None:
            d_idx = bisect.bisect_right(ts1d, ts5[i]) - 2   # previous fully-closed daily bar
        sig, entry, sl, tp, strat = _check_signal(
            df5, i, df1h, h_idx, symbol, df1d, d_idx, df4h, h4_idx)
        if sig is None:
            continue

        result   = None
        exit_idx = None
        exit_px  = None
        pnl_usd  = 0.0

        sl_dist = abs(entry - sl)

        if config.PARTIAL_TP_ENABLED and sl_dist > 0:
            # ── Phase 1: wait for partial TP1 or original SL ─────────────────
            partial_tp1 = (entry + sl_dist * config.PARTIAL_TP1_R) if sig == "long" \
                     else (entry - sl_dist * config.PARTIAL_TP1_R)
            partial_tp2 = (entry + sl_dist * config.PARTIAL_TP2_R) if sig == "long" \
                     else (entry - sl_dist * config.PARTIAL_TP2_R)
            be_sl       = entry   # break-even stop

            phase1_hit = False
            for j in range(i + 1, min(i + 2000, len(df5))):
                hi = df5.iloc[j]["high"]
                lo = df5.iloc[j]["low"]
                if sig == "long":
                    if lo <= sl:
                        result, exit_px, exit_idx = "loss", sl, j
                        pnl_usd = -config.RISK_PER_TRADE
                        break
                    if hi >= partial_tp1:
                        phase1_hit = True
                        phase1_idx = j
                        break
                else:
                    if hi >= sl:
                        result, exit_px, exit_idx = "loss", sl, j
                        pnl_usd = -config.RISK_PER_TRADE
                        break
                    if lo <= partial_tp1:
                        phase1_hit = True
                        phase1_idx = j
                        break

            if phase1_hit:
                # First half locked in at PARTIAL_TP1_R
                # Phase 2: wait for TP2 or break-even stop
                for j in range(phase1_idx, min(phase1_idx + 2000, len(df5))):
                    hi = df5.iloc[j]["high"]
                    lo = df5.iloc[j]["low"]
                    if sig == "long":
                        if lo <= be_sl:
                            # Second half stopped at BE — only first half profit
                            result  = "win"
                            exit_px = partial_tp1  # effective blended price
                            pnl_usd = config.RISK_PER_TRADE * config.PARTIAL_TP1_R * 0.5
                            exit_idx = j
                            break
                        if hi >= partial_tp2:
                            result  = "win"
                            exit_px = partial_tp2
                            pnl_usd = (config.RISK_PER_TRADE * config.PARTIAL_TP1_R * 0.5
                                       + config.RISK_PER_TRADE * config.PARTIAL_TP2_R * 0.5)
                            exit_idx = j
                            break
                    else:
                        if hi >= be_sl:
                            result  = "win"
                            exit_px = partial_tp1
                            pnl_usd = config.RISK_PER_TRADE * config.PARTIAL_TP1_R * 0.5
                            exit_idx = j
                            break
                        if lo <= partial_tp2:
                            result  = "win"
                            exit_px = partial_tp2
                            pnl_usd = (config.RISK_PER_TRADE * config.PARTIAL_TP1_R * 0.5
                                       + config.RISK_PER_TRADE * config.PARTIAL_TP2_R * 0.5)
                            exit_idx = j
                            break
                else:
                    result = None  # timed out in phase 2

        else:
            # ── Original fixed TP logic ──────────────────────────────────────
            halfway     = entry + (tp - entry) * 0.5 if sig == "long" else entry - (entry - tp) * 0.5
            be_triggered = False
            active_sl    = sl
            for j in range(i + 1, min(i + 2000, len(df5))):
                hi    = df5.iloc[j]["high"]
                lo    = df5.iloc[j]["low"]
                close = df5.iloc[j]["close"]

                # Breakeven: move SL to entry when close crosses 50% of TP distance
                # Uses close (not high/low) to match live bot which checks current_price at candle close
                if config.BE_ENABLED and not be_triggered:
                    if (sig == "long" and close >= halfway) or (sig == "short" and close <= halfway):
                        be_triggered = True
                        active_sl    = entry

                if sig == "long":
                    if hi >= tp:
                        result, exit_px, exit_idx = "win", tp, j
                        break
                    if lo <= active_sl:
                        result, exit_px, exit_idx = ("loss" if active_sl < entry else "breakeven"), active_sl, j
                        break
                else:
                    if lo <= tp:
                        result, exit_px, exit_idx = "win", tp, j
                        break
                    if hi >= active_sl:
                        result, exit_px, exit_idx = ("loss" if active_sl > entry else "breakeven"), active_sl, j
                        break
                # ADX fade: if trend collapses, exit at candle close
                if config.ADX_FADE_ENABLED:
                    adx_j = df5.iloc[j]["adx"]
                    if not pd.isna(adx_j) and adx_j < config.ADX_THRESHOLD:
                        result, exit_px, exit_idx = "fade", df5.iloc[j]["close"], j
                        break

        if result is None:
            continue

        if not config.PARTIAL_TP_ENABLED:
            # Standard mode: derive PnL from exit price
            pnl_pts  = (exit_px - entry) if sig == "long" else (entry - exit_px)
            rr_achvd = pnl_pts / sl_dist if sl_dist > 0 else 0
            pnl_usd  = config.RISK_PER_TRADE * rr_achvd
        else:
            # Partial TP mode: pnl_usd was computed directly in the loop above
            rr_achvd = pnl_usd / config.RISK_PER_TRADE if config.RISK_PER_TRADE > 0 else 0

        if strat == "RSI":
            rsi_count += 1
        else:
            wr_count += 1

        trade = {
            "symbol":     symbol,
            "side":       sig,
            "strategy":   strat,
            "entry_time": datetime.fromtimestamp(ts5[i]        / 1000, tz=timezone.utc),
            "exit_time":  datetime.fromtimestamp(ts5[exit_idx] / 1000, tz=timezone.utc),
            "entry":      entry,
            "sl":         sl,
            "tp":         tp,
            "exit_px":    exit_px,
            "result":     result,
            "rr":         rr_achvd,
            "pnl_usd":    pnl_usd,
        }
        trades.append(trade)

        if not quiet:
            mark = "✓" if result == "win" else ("~" if result in ("fade", "breakeven") else "✗")
            print(
                f"  {mark} [{strat:3s}] {sig.upper():5s}  "
                f"{trade['entry_time'].strftime('%Y-%m-%d %H:%M')} → "
                f"{trade['exit_time'].strftime('%m-%d %H:%M')}  "
                f"entry={entry:.4f}  sl={sl:.4f}  tp={tp:.4f}  "
                f"exit={exit_px:.4f}  RR={rr_achvd:+.2f}  "
                f"PnL=${pnl_usd:+.2f}"
            )

        skip_until = exit_idx + 1

    _print_stats(trades, days, symbol, rsi_count, wr_count)
    return trades


# ── Statistics report ─────────────────────────────────────────────────────────

def _print_stats(trades, days, symbol, rsi_count, wr_count):
    if not trades:
        print(f"\n  No completed trades found for {symbol} over {days} days.")
        return

    wins      = [t for t in trades if t["result"] == "win"]
    losses    = [t for t in trades if t["result"] == "loss"]
    fades     = [t for t in trades if t["result"] == "fade"]
    breakevens= [t for t in trades if t["result"] == "breakeven"]
    total  = len(trades)

    wr_pct  = len(wins) / total * 100
    net_pnl = sum(t["pnl_usd"] for t in trades)
    avg_rr  = sum(t["rr"]      for t in trades) / total
    tpd     = total / days

    avg_win  = sum(t["pnl_usd"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0.0
    gross_w  = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_l  = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf       = gross_w / gross_l if gross_l > 0 else float("inf")

    equity = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak   = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    max_streak = cur_streak = 0
    for t in trades:
        if t["result"] == "loss":
            cur_streak += 1
            max_streak  = max(max_streak, cur_streak)
        else:
            cur_streak  = 0

    first_dt = trades[0]["entry_time"].strftime("%Y-%m-%d")
    last_dt  = trades[-1]["exit_time"].strftime("%Y-%m-%d")

    print(f"\n{'─' * 62}")
    print(f"  RESULTS (HYBRID) — {symbol}  ({days} days: {first_dt} → {last_dt})")
    print(f"{'─' * 62}")
    print(f"  Total trades       : {total}  (RSI: {rsi_count}  WR: {wr_count})")
    print(f"  Wins / Losses / BE / Fades: {len(wins)} / {len(losses)} / {len(breakevens)} / {len(fades)}")
    print(f"  Win rate           : {wr_pct:.1f}%")
    print(f"  Avg trades / day   : {tpd:.2f}")
    print(f"  Net PnL            : ${net_pnl:+.2f}  (at ${config.RISK_PER_TRADE} risk/trade)")
    print(f"  Avg RR achieved    : {avg_rr:.2f}R")
    print(f"  Profit factor      : {pf:.2f}")
    print(f"  Avg win            : ${avg_win:+.2f}")
    print(f"  Avg loss           : ${avg_loss:+.2f}")
    print(f"  Max drawdown       : ${max_dd:.2f}")
    print(f"  Max consec. losses : {max_streak}")
    print(f"{'─' * 62}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _symbol = sys.argv[1]              if len(sys.argv) > 1 else "BTCUSDT"
    _days   = int(sys.argv[2])         if len(sys.argv) > 2 else 90
    _quiet  = "--quiet" in sys.argv or "-q" in sys.argv
    run_backtest(_symbol, _days, quiet=_quiet)
