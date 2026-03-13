"""
backtest_sma.py — Walk-forward backtest of the SMA Pullback in Trend strategy.

Signal logic (15m candles, closed-candle — no look-ahead bias):

  LONG:
    1. 20 SMA is above 200 SMA
    2. 20 SMA slope is rising: SMA[i] > SMA[i-3] by ≥ SMA_SLOPE_MIN pct
    3. One of the last 3 closed candles (excluding the signal candle) had its
       low touch within SMA_TOUCH_PCT of the 20 SMA from above (pullback happened)
    4. Signal candle (bar[i]) closed back above the 20 SMA
    5. All shared 5m filters pass (ADX, ATR, time, 5m EMA200, 1H EMA200, 4H EMA200)

  SHORT:
    Mirror of LONG — 20 SMA below 200 SMA, slope falling, pullback from below,
    signal candle closes back below 20 SMA.

SL/TP:
  SL = swing low/high of last SWING_LOOKBACK 5m candles ± SL_BUFFER_PCT
  TP = fixed RR (config.RR) — same as WR/RSI strategies

Acceptance criteria (before adding to live scanner):
  PF ≥ 1.20  |  MaxDD ≤ $500  |  ≥ 40 trades over 180 days

Usage:
    python backtest_sma.py                         # BTCUSDT, 180 days
    python backtest_sma.py ZECUSDT 180
    python backtest_sma.py PAXGUSDT 180 --quiet
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

# ── Config defaults (can be overridden in config.py) ─────────────────────────
_SMA_FAST       = getattr(config, "SMA_FAST",      20)
_SMA_SLOW       = getattr(config, "SMA_SLOW",      200)
_SMA_SLOPE_MIN  = getattr(config, "SMA_SLOPE_MIN", 0.001)   # 0.1 % over 3 bars
_SMA_TOUCH_PCT  = getattr(config, "SMA_TOUCH_PCT", 0.003)   # within 0.3 % of SMA


# ── Historical data fetching (mirrors backtest_hybrid.py) ────────────────────

def _fetch_chunk(session, symbol, interval, end_ms, limit=1000):
    for attempt in range(6):
        try:
            resp = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
                end=end_ms,
            )
            if resp.get("retCode", 0) == 10006:
                wait = 20 * (attempt + 1)
                print(f"\n  [rate-limit] sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
                continue
            return resp["result"]["list"]
        except Exception as exc:
            msg = str(exc).lower()
            is_rate = "10006" in msg or "429" in msg or "rate limit" in msg or "too many" in msg
            is_conn = "connection" in msg or "reset" in msg or "ssl" in msg or "timeout" in msg
            if is_rate:
                wait = 20 * (attempt + 1)      # 20s, 40s, 60s …
                print(f"\n  [rate-limit] sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
            elif is_conn:
                wait = 60 * (attempt + 1)      # 60s, 120s, 180s … IP-block needs longer
                print(f"\n  [conn-reset] sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
            elif attempt == 5:
                raise
            else:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError("_fetch_chunk: failed after 6 attempts")


def fetch_all_candles(session, symbol, interval, days):
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    end_ms   = now_ms
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
        time.sleep(0.5)  # 2 req/s sustained

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
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["atr"]    = ta.volatility.average_true_range(
                       df["high"], df["low"], df["close"],
                       window=config.ATR_PERIOD)
    df["atr_ma"] = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
    return df


def _build_1h_indicators(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


def _build_4h_indicators(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


def _build_15m_indicators(df):
    df = df.copy()
    df["sma20"]  = df["close"].rolling(_SMA_FAST).mean()
    df["sma200"] = df["close"].rolling(_SMA_SLOW).mean()
    return df


# ── Signal detection ──────────────────────────────────────────────────────────

def _check_signal(df5, i5, df15, i15, df1h, h_idx, df4h, h4_idx, symbol):
    """
    Evaluate SMA pullback signal at 5m bar i5 (corresponds to 15m bar i15).

    Closed-candle convention:
        signal candle   = df15.iloc[i15]        (the bar that just closed)
        previous candle = df15.iloc[i15 - 1]    etc.
        5m signal bar   = df5.iloc[i5]          (same convention)

    Returns (signal, entry, sl, tp) or (None, ...).
    """
    sw    = config.SWING_LOOKBACK
    min5  = max(200, sw)
    min15 = _SMA_SLOW + 5

    if i5 < min5 or i15 < min15 or h_idx < 200:
        return None, None, None, None

    # ── 5m shared filters ────────────────────────────────────────────────────
    row5  = df5.iloc[i5]
    price = row5["close"]
    ema5  = row5["ema200"]
    adx   = row5["adx"]
    atr   = row5["atr"]
    atr_ma = row5["atr_ma"]

    if any(pd.isna(v) for v in [price, ema5, adx, atr, atr_ma]):
        return None, None, None, None

    if atr <= atr_ma or adx <= config.ADX_THRESHOLD:
        return None, None, None, None

    if config.ADX_RISING_FILTER:
        adx_prev = df5.iloc[i5 - 1]["adx"]
        if not pd.isna(adx_prev) and adx <= adx_prev:
            return None, None, None, None

    if config.TIME_FILTER:
        bar_hour = datetime.fromtimestamp(
            int(df5["timestamp"].iloc[i5]) / 1000, tz=timezone.utc
        ).hour
        if bar_hour < config.TIME_FILTER_START or bar_hour >= config.TIME_FILTER_END:
            return None, None, None, None

    # ── 1H filter ────────────────────────────────────────────────────────────
    row_1h   = df1h.iloc[h_idx]
    price_1h = row_1h["close"]
    ema_1h   = row_1h["ema200"]
    htf_long_ok  = True
    htf_short_ok = True
    if config.HTF_FILTER and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    # ── 4H filter ────────────────────────────────────────────────────────────
    excl_4h = getattr(config, "HTF_4H_FILTER_EXCLUDED", [])
    if (getattr(config, "HTF_4H_FILTER", False)
            and symbol not in excl_4h
            and df4h is not None and h4_idx >= 200):
        row_4h = df4h.iloc[h4_idx]
        p4h    = float(row_4h["close"])
        ema_4h = row_4h["ema200"]
        if not pd.isna(ema_4h):
            if p4h <= ema_4h:
                htf_long_ok  = False
            if p4h >= ema_4h:
                htf_short_ok = False

    # ── 15m SMA signal logic ─────────────────────────────────────────────────
    row15      = df15.iloc[i15]
    close15    = row15["close"]
    sma20_now  = row15["sma20"]
    sma200_now = row15["sma200"]

    if pd.isna(sma20_now) or pd.isna(sma200_now):
        return None, None, None, None

    sma20_prev3 = df15.iloc[i15 - 3]["sma20"]
    if pd.isna(sma20_prev3) or sma20_prev3 == 0:
        return None, None, None, None

    slope_rise = (sma20_now - sma20_prev3) / sma20_prev3 >= _SMA_SLOPE_MIN
    slope_fall = (sma20_prev3 - sma20_now) / sma20_prev3 >= _SMA_SLOPE_MIN

    # Touch check: any of the 3 bars before the signal candle had a low/high
    # within SMA_TOUCH_PCT of the 20 SMA at that bar (closed bars only).
    def _touched_from_above(look):
        """Any bar in look had low within touch_pct of sma20 from above."""
        for k in range(1, look + 1):
            idx = i15 - k
            if idx < 0:
                return False
            lo   = df15.iloc[idx]["low"]
            sma  = df15.iloc[idx]["sma20"]
            if pd.isna(sma) or sma == 0:
                continue
            if abs(lo - sma) / sma <= _SMA_TOUCH_PCT:
                return True
        return False

    def _touched_from_below(look):
        """Any bar in look had high within touch_pct of sma20 from below."""
        for k in range(1, look + 1):
            idx = i15 - k
            if idx < 0:
                return False
            hi  = df15.iloc[idx]["high"]
            sma = df15.iloc[idx]["sma20"]
            if pd.isna(sma) or sma == 0:
                continue
            if abs(hi - sma) / sma <= _SMA_TOUCH_PCT:
                return True
        return False

    # ── LONG ─────────────────────────────────────────────────────────────────
    if (htf_long_ok
            and price > ema5               # 5m uptrend
            and sma20_now > sma200_now     # 20 SMA above 200 SMA
            and slope_rise                 # 20 SMA rising
            and close15 > sma20_now        # signal candle closed above 20 SMA
            and _touched_from_above(3)):   # prior pullback to 20 SMA happened
        swing_low = float(df5["low"].iloc[i5 - sw + 1 : i5 + 1].min())
        sl = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist = price - sl
        if sl_dist > 0:
            tp = price + sl_dist * config.RR
            return "long", price, sl, tp

    # ── SHORT ─────────────────────────────────────────────────────────────────
    long_only = symbol in getattr(config, "SMA_LONG_ONLY_COINS", [])
    if (not long_only
            and htf_short_ok
            and price < ema5               # 5m downtrend
            and sma20_now < sma200_now     # 20 SMA below 200 SMA
            and slope_fall                 # 20 SMA falling
            and close15 < sma20_now        # signal candle closed below 20 SMA
            and _touched_from_below(3)):   # prior bounce to 20 SMA happened
        swing_high = float(df5["high"].iloc[i5 - sw + 1 : i5 + 1].max())
        sl = swing_high * (1 + config.SL_BUFFER_PCT)
        sl_dist = sl - price
        if sl_dist > 0:
            tp = price - sl_dist * config.RR
            return "short", price, sl, tp

    return None, None, None, None


# ── Walk-forward simulation ───────────────────────────────────────────────────

def run_backtest(symbol, days, quiet=False):
    import exchange

    print(f"\n{'=' * 62}")
    print(f"  SMA Backtest  |  {symbol}  |  {days} days  |  RR={config.RR}"
          f"  |  risk=${config.RISK_PER_TRADE}/trade")
    print(f"  SMA{_SMA_FAST}/{_SMA_SLOW} on 15m  |  slope≥{_SMA_SLOPE_MIN*100:.2f}%"
          f"  |  touch≤{_SMA_TOUCH_PCT*100:.2f}%")
    print(f"{'=' * 62}")

    df5  = fetch_all_candles(exchange.session, symbol,   5, days)
    time.sleep(5)
    df15 = fetch_all_candles(exchange.session, symbol,  15, days)
    time.sleep(5)
    df1h = fetch_all_candles(exchange.session, symbol,  60, days)
    df4h = None
    if getattr(config, "HTF_4H_FILTER", False):
        excl_4h = getattr(config, "HTF_4H_FILTER_EXCLUDED", [])
        if symbol not in excl_4h:
            time.sleep(3)
            df4h = fetch_all_candles(exchange.session, symbol, 240, days)

    print("  Computing indicators ...", end="", flush=True)
    df5  = _build_5m_indicators(df5)
    df15 = _build_15m_indicators(df15)
    df1h = _build_1h_indicators(df1h)
    if df4h is not None:
        df4h = _build_4h_indicators(df4h)
    print(" done\n")

    ts5  = df5["timestamp"].values
    ts15 = df15["timestamp"].values
    ts1h = df1h["timestamp"].values
    ts4h = df4h["timestamp"].values if df4h is not None else None

    # The 15m bar containing 5m bar i closed at ts15[i15]:
    # we need the 15m bar whose open <= ts5[i] < open + 15min,
    # i.e. the last 15m bar that started at or before ts5[i].
    min_start = max(200, config.SWING_LOOKBACK, _SMA_SLOW + 5)
    trades    = []
    skip_until_5m = -1

    for i5 in range(min_start, len(df5) - 1):
        if i5 < skip_until_5m:
            continue

        ts_now = ts5[i5]

        # Corresponding closed 15m bar index
        i15  = bisect.bisect_right(ts15, ts_now) - 1
        h_idx = bisect.bisect_right(ts1h, ts_now) - 1
        h4_idx = (bisect.bisect_right(ts4h, ts_now) - 1) if ts4h is not None else -1

        if i15 < _SMA_SLOW + 5 or h_idx < 200:
            continue

        sig, entry, sl, tp = _check_signal(
            df5, i5, df15, i15, df1h, h_idx, df4h, h4_idx, symbol
        )
        if sig is None:
            continue

        # ── Trade simulation (5m forward bars — same as backtest_hybrid.py) ──
        result   = None
        exit_idx = None
        exit_px  = None

        for j in range(i5 + 1, min(i5 + 2000, len(df5))):
            hi  = df5.iloc[j]["high"]
            lo  = df5.iloc[j]["low"]

            if sig == "long":
                if hi >= tp:
                    result, exit_px, exit_idx = "win",  tp, j; break
                if lo <= sl:
                    result, exit_px, exit_idx = "loss", sl, j; break
            else:
                if lo <= tp:
                    result, exit_px, exit_idx = "win",  tp, j; break
                if hi >= sl:
                    result, exit_px, exit_idx = "loss", sl, j; break

        if result is None:
            continue

        sl_dist  = abs(entry - sl)
        pnl_pts  = (exit_px - entry) if sig == "long" else (entry - exit_px)
        rr_achvd = pnl_pts / sl_dist if sl_dist > 0 else 0
        pnl_usd  = config.RISK_PER_TRADE * rr_achvd

        trade = {
            "symbol":     symbol,
            "side":       sig,
            "entry_time": datetime.fromtimestamp(ts5[i5]        / 1000, tz=timezone.utc),
            "exit_time":  datetime.fromtimestamp(ts5[exit_idx]  / 1000, tz=timezone.utc),
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
            mark = "✓" if result == "win" else "✗"
            print(
                f"  {mark} {sig.upper():5s}  "
                f"{trade['entry_time'].strftime('%Y-%m-%d %H:%M')} → "
                f"{trade['exit_time'].strftime('%m-%d %H:%M')}  "
                f"entry={entry:.4f}  sl={sl:.4f}  tp={tp:.4f}  "
                f"exit={exit_px:.4f}  RR={rr_achvd:+.2f}  "
                f"PnL=${pnl_usd:+.2f}"
            )

        skip_until_5m = exit_idx + 1

    _print_stats(trades, days, symbol)
    return trades


# ── Statistics report (mirrors backtest_rsi.py) ───────────────────────────────

def _print_stats(trades, days, symbol):
    if not trades:
        print(f"\n  No completed trades found for {symbol} over {days} days.")
        print(f"  (SMA pullback needs sustained trends — try a trending period.)\n")
        return

    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    total  = len(trades)

    wr      = len(wins) / total * 100
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

    longs  = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]
    l_wr   = len([t for t in longs  if t["result"] == "win"]) / len(longs)  * 100 if longs  else 0
    s_wr   = len([t for t in shorts if t["result"] == "win"]) / len(shorts) * 100 if shorts else 0

    first_dt = trades[0]["entry_time"].strftime("%Y-%m-%d")
    last_dt  = trades[-1]["exit_time"].strftime("%Y-%m-%d")

    # Acceptance gate
    pass_pf    = "✅" if pf    >= 1.20  else "❌"
    pass_dd    = "✅" if max_dd <= 500   else "❌"
    pass_count = "✅" if total  >= 40    else "❌"

    print(f"\n{'─' * 62}")
    print(f"  RESULTS (SMA) — {symbol}  ({days} days: {first_dt} → {last_dt})")
    print(f"{'─' * 62}")
    print(f"  Total trades       : {total}  {pass_count}  (need ≥ 40)")
    print(f"  Wins / Losses      : {len(wins)} / {len(losses)}")
    print(f"  Win rate           : {wr:.1f}%"
          f"  (L {l_wr:.0f}% / S {s_wr:.0f}%)")
    print(f"  Avg trades / day   : {tpd:.2f}")
    print(f"  Net PnL            : ${net_pnl:+.2f}  (at ${config.RISK_PER_TRADE} risk/trade)")
    print(f"  Avg RR achieved    : {avg_rr:.2f}R")
    print(f"  Profit factor      : {pf:.2f}  {pass_pf}  (need ≥ 1.20)")
    print(f"  Avg win            : ${avg_win:+.2f}")
    print(f"  Avg loss           : ${avg_loss:+.2f}")
    print(f"  Max drawdown       : ${max_dd:.2f}  {pass_dd}  (need ≤ $500)")
    print(f"  Max consec. losses : {max_streak}")
    print(f"{'─' * 62}")
    all_pass = (pf >= 1.20 and max_dd <= 500 and total >= 40)
    print(f"  Acceptance gate    : {'PASS ✅ — ready for portfolio test' if all_pass else 'FAIL ❌ — do not add to live scanner'}")
    print(f"{'─' * 62}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    symbol = sys.argv[1]              if len(sys.argv) > 1 else "BTCUSDT"
    days   = int(sys.argv[2])         if len(sys.argv) > 2 else 180
    quiet  = "--quiet" in sys.argv

    run_backtest(symbol, days, quiet=quiet)
