"""
rsi2_compare.py — Compare RSI-14 zone strategy vs RSI-2 extreme strategy.

Fetches candles ONCE per symbol, then replays both parameter sets in-memory
without touching config.py.

RSI-14 (current): period=14, long zone 30-45, short zone 55-70
RSI-2  (test):    period=2,  long zone  0-10, short zone 90-100

Usage:
    python rsi2_compare.py                              # BTCUSDT, 180 days
    python rsi2_compare.py 180 BTCUSDT ZECUSDT SUIUSDT
    python rsi2_compare.py 90 BTCUSDT
"""

import bisect
import contextlib
import io
import sys
import time
import logging
from datetime import datetime, timezone
from typing import NamedTuple

import pandas as pd
import ta

import config
import exchange

logging.basicConfig(level=logging.WARNING)

# ── Parameter sets to compare ────────────────────────────────────────────────

class RSIParams(NamedTuple):
    label:         str
    period:        int
    long_low:      float
    long_high:     float
    short_low:     float
    short_high:    float

PRESETS = [
    RSIParams("RSI-14 zone (current)", 14, 30, 45, 55, 70),
    RSIParams("RSI-2  extreme",         2,  0, 10, 90, 100),
    RSIParams("RSI-3  extreme",         3,  0, 15, 85, 100),
]


# ── Candle fetch (rate-limit aware) ──────────────────────────────────────────

def _fetch_chunk(symbol, interval, end_ms, limit=1000):
    for attempt in range(6):
        try:
            resp = exchange.session.get_kline(
                category="linear", symbol=symbol,
                interval=interval, limit=limit, end=end_ms,
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
                wait = 20 * (attempt + 1)
                print(f"\n  [rate-limit] sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
            elif is_conn:
                wait = 60 * (attempt + 1)
                print(f"\n  [conn-reset] sleeping {wait}s ...", end="", flush=True)
                time.sleep(wait)
            elif attempt == 5:
                raise
            else:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError("_fetch_chunk: failed after 6 attempts")


def fetch_candles(symbol, interval, days):
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    end_ms   = now_ms
    rows     = []
    print(f"  Fetching {symbol} {interval}m ({days}d) ...", end="", flush=True)
    while True:
        chunk = _fetch_chunk(symbol, interval, end_ms)
        if not chunk:
            break
        rows.extend(chunk)
        oldest = int(chunk[-1][0])
        if oldest <= start_ms:
            break
        end_ms = oldest - 1
        time.sleep(0.5)  # 2 req/s sustained
    print(f" {len(rows)} candles")
    df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume","turnover"])
    df["timestamp"] = df["timestamp"].astype(int)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    return df[df["timestamp"] >= start_ms].reset_index(drop=True)


# ── Indicator build (period is a parameter) ───────────────────────────────────

def build_5m(df_raw, rsi_period):
    df = df_raw.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["rsi"]    = ta.momentum.rsi(df["close"], window=rsi_period)
    df["atr"]    = ta.volatility.average_true_range(df["high"], df["low"], df["close"],
                                                     window=config.ATR_PERIOD)
    df["atr_ma"] = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
    return df


def build_1h(df_raw):
    df = df_raw.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


# ── Signal check (parameterised) ─────────────────────────────────────────────

def check_signal(df5, i, df1h, h_idx, p: RSIParams):
    sw    = config.SWING_LOOKBACK
    min_i = max(200, p.period + 1, sw)
    if i < min_i or h_idx < 200:
        return None, None, None, None

    row      = df5.iloc[i]
    row_prev = df5.iloc[i - 1]
    price    = row["close"]
    ema5     = row["ema200"]
    ema50    = row["ema50"]
    adx      = row["adx"]
    adx_prev = row_prev["adx"]
    atr      = row["atr"]
    atr_ma   = row["atr_ma"]
    rsi      = row["rsi"]
    rsi_prev = row_prev["rsi"]

    if any(pd.isna(v) for v in [price, ema5, adx, atr, atr_ma, rsi, rsi_prev]):
        return None, None, None, None

    if atr <= atr_ma or adx <= config.ADX_THRESHOLD:
        return None, None, None, None

    if config.ADX_RISING_FILTER and not pd.isna(adx_prev) and adx <= adx_prev:
        return None, None, None, None

    if config.TIME_FILTER:
        hour = datetime.fromtimestamp(int(df5["timestamp"].iloc[i]) / 1000, tz=timezone.utc).hour
        if hour < config.TIME_FILTER_START or hour >= config.TIME_FILTER_END:
            return None, None, None, None

    if config.EMA50_PULLBACK_FILTER and not pd.isna(ema50):
        if abs(price - ema50) > config.EMA50_PULLBACK_ATR_MULT * atr:
            return None, None, None, None

    row_1h = df1h.iloc[h_idx]
    ema_1h = row_1h["ema200"]
    price_1h = row_1h["close"]
    htf_long_ok = htf_short_ok = True
    if config.HTF_FILTER and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    # Long
    if (price > ema5 and htf_long_ok
            and p.long_low <= rsi <= p.long_high and rsi > rsi_prev):
        swing_low = float(df5["low"].iloc[i - sw + 1: i + 1].min())
        sl = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist = price - sl
        if sl_dist > 0:
            return "long", price, sl, price + sl_dist * config.RR

    # Short
    if (price < ema5 and htf_short_ok
            and p.short_low <= rsi <= p.short_high and rsi < rsi_prev):
        swing_high = float(df5["high"].iloc[i - sw + 1: i + 1].max())
        sl = swing_high * (1 + config.SL_BUFFER_PCT)
        sl_dist = sl - price
        if sl_dist > 0:
            return "short", price, sl, price - sl_dist * config.RR

    return None, None, None, None


# ── Walk-forward sim ─────────────────────────────────────────────────────────

def simulate(df5, df1h, p: RSIParams):
    ts5  = df5["timestamp"].values
    ts1h = df1h["timestamp"].values
    min_start = max(250, p.period + 1, config.SWING_LOOKBACK)
    trades = []
    skip_until = -1

    for i in range(min_start, len(df5) - 1):
        if i < skip_until:
            continue
        h_idx = bisect.bisect_right(ts1h, ts5[i]) - 1
        sig, entry, sl, tp = check_signal(df5, i, df1h, h_idx, p)
        if sig is None:
            continue

        result = exit_px = exit_idx = None
        for j in range(i + 1, min(i + 2000, len(df5))):
            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]
            if sig == "long":
                if lo <= sl:  result, exit_px, exit_idx = "loss", sl, j;  break
                if hi >= tp:  result, exit_px, exit_idx = "win",  tp, j;  break
            else:
                if hi >= sl:  result, exit_px, exit_idx = "loss", sl, j;  break
                if lo <= tp:  result, exit_px, exit_idx = "win",  tp, j;  break

        if result is None:
            continue

        sl_dist  = abs(entry - sl)
        pnl_pts  = (exit_px - entry) if sig == "long" else (entry - exit_px)
        rr_achvd = pnl_pts / sl_dist if sl_dist > 0 else 0
        trades.append({
            "result":  result,
            "pnl_usd": config.RISK_PER_TRADE * rr_achvd,
            "rr":      rr_achvd,
        })
        skip_until = exit_idx + 1

    return trades


def stats(trades):
    if not trades:
        return dict(n=0, wr=0, pf=0, net=0, max_dd=0, streak=0)
    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    net     = sum(t["pnl_usd"] for t in trades)
    equity  = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak = max_dd = 0.0
    for e in equity:
        if e > peak: peak = e
        max_dd = max(max_dd, peak - e)
    streak = cur = 0
    for t in trades:
        if t["result"] == "loss": cur += 1; streak = max(streak, cur)
        else: cur = 0
    return dict(n=len(trades), wr=len(wins)/len(trades)*100,
                pf=pf, net=net, max_dd=max_dd, streak=streak)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0]); args = args[1:]
    symbols = args if args else ["BTCUSDT"]

    all_results = []  # (symbol, preset_label, stats_dict)

    for sym in symbols:
        print(f"\n{'═'*62}\n  {sym}  |  {days} days\n{'═'*62}")
        raw5  = fetch_candles(sym, 5,  days)
        raw1h = fetch_candles(sym, 60, days)
        df1h  = build_1h(raw1h)

        sym_rows = []
        for p in PRESETS:
            print(f"  [{p.label}] computing ...", end="", flush=True)
            df5 = build_5m(raw5, p.period)
            trades = simulate(df5, df1h, p)
            s = stats(trades)
            print(f"  PF={s['pf']:.2f}  WR={s['wr']:.1f}%  Net=${s['net']:+.0f}"
                  f"  DD=${s['max_dd']:.0f}  N={s['n']}")
            sym_rows.append((p.label, s))
            all_results.append((sym, p.label, s))

        # Per-symbol mini table
        print(f"\n  {'Preset':<28}  {'PF':>5}  {'WR%':>5}  {'Net PnL':>9}"
              f"  {'MaxDD':>7}  {'Trades':>7}  {'Streak':>7}")
        print(f"  {'-'*28}  {'-'*5}  {'-'*5}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*7}")
        best_pf = max(r[1]["pf"] for r in sym_rows)
        for label, s in sym_rows:
            marker = " ◄" if s["pf"] == best_pf else ""
            print(f"  {label:<28}  {s['pf']:>5.2f}  {s['wr']:>5.1f}%"
                  f"  ${s['net']:>+8.0f}  ${s['max_dd']:>6.0f}"
                  f"  {s['n']:>7d}  {s['streak']:>7d}{marker}")

        time.sleep(5)  # rate-limit cooldown between symbols

    # Global summary if multiple symbols
    if len(symbols) > 1:
        print(f"\n{'═'*80}")
        print(f"  OVERALL SUMMARY  |  {days}d  |  {len(symbols)} symbols")
        print(f"{'═'*80}")
        for p in PRESETS:
            rows = [r for r in all_results if r[1] == p.label]
            total_net = sum(r[2]["net"] for r in rows)
            avg_pf    = sum(r[2]["pf"]  for r in rows) / len(rows)
            avg_dd    = sum(r[2]["max_dd"] for r in rows) / len(rows)
            total_n   = sum(r[2]["n"] for r in rows)
            print(f"  {p.label:<28}  avg PF={avg_pf:.2f}  "
                  f"total Net=${total_net:+.0f}  avg DD=${avg_dd:.0f}  "
                  f"total trades={total_n}")
        print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
