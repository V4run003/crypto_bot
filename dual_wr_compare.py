"""
dual_wr_compare.py — Test dual Williams %R confluence as an additional signal filter.

The original %R Trend Exhaustion indicator (upslidedown, TradingView) uses TWO
Williams %R periods simultaneously:
  • Short period (14)  — standard, already in our strategy
  • Long  period (112) — ~9.3 hours on 5m; captures the medium-term extreme

A signal only fires when BOTH are in the extreme zone AND both hook out together.
The theory: if price is at a 14-candle AND a 112-candle extreme simultaneously,
the reversal is higher quality — less noise, fewer false entries.

On 5m candles:
  • 14  periods = 70 minutes
  • 112 periods = 9.3 hours (roughly half a trading session)

This script compares three modes on BTC, ZEC, SUI:
  Baseline  — current strategy (WR-14 only for WR signal)
  Dual WR   — WR signal requires WR-14 AND WR-112 both in extreme zone + both hook out
  Dual WR+  — same but uses OR for the hookout (either breaks out, long-period stays)

Usage:
    python dual_wr_compare.py                              # BTC 180d
    python dual_wr_compare.py 180 BTCUSDT ZECUSDT SUIUSDT
    python dual_wr_compare.py 90 BTCUSDT
"""

import bisect
import contextlib
import io
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import ta

import config
import exchange

# ── Candle fetching (same as backtest_hybrid) ─────────────────────────────────

def _fetch_chunk(session, symbol, interval, end_ms):
    resp = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=str(interval),
        end=end_ms,
        limit=200,
    )
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {resp}")
    return resp["result"]["list"]


def fetch_all_candles(session, symbol, interval, days):
    import time as _time
    ms_per_candle = interval * 60 * 1000
    total_candles = days * 24 * 60 // interval
    end_ms        = int(_time.time() * 1000)
    all_candles   = []
    while len(all_candles) < total_candles:
        chunk = _fetch_chunk(session, symbol, interval, end_ms)
        if not chunk:
            break
        all_candles.extend(chunk)
        end_ms = int(chunk[-1][0]) - 1
        _time.sleep(0.15)
    df = pd.DataFrame(
        all_candles,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df = df.iloc[::-1].reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["timestamp"] = df["timestamp"].astype(int)
    return df


# ── Indicator builders ────────────────────────────────────────────────────────

def _build_5m(df):
    df = df.copy()
    df["ema200"]  = ta.trend.ema_indicator(df["close"], window=200)
    df["adx"]     = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["wr14"]    = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
    df["wr112"]   = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=112)
    df["rsi"]     = ta.momentum.rsi(df["close"], window=config.RSI_PERIOD)
    df["atr"]     = ta.volatility.average_true_range(
                        df["high"], df["low"], df["close"], window=config.ATR_PERIOD)
    df["atr_ma"]  = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
    return df


def _build_1h(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


# ── Common filters ────────────────────────────────────────────────────────────

def _common_ok(df5, i, df1h, h_idx):
    min_i = max(250, config.SWING_LOOKBACK)
    if i < min_i or h_idx < 200:
        return False, None, None, None, None

    row   = df5.iloc[i]
    price = row["close"]
    ema5  = row["ema200"]
    adx   = row["adx"]
    atr   = row["atr"]
    atr_ma= row["atr_ma"]

    if any(pd.isna(v) for v in [price, ema5, adx, atr, atr_ma]):
        return False, None, None, None, None

    if atr <= atr_ma or adx <= config.ADX_THRESHOLD:
        return False, None, None, None, None

    if config.TIME_FILTER:
        bar_hour = datetime.fromtimestamp(
            int(df5["timestamp"].iloc[i]) / 1000, tz=timezone.utc
        ).hour
        if bar_hour < config.TIME_FILTER_START or bar_hour >= config.TIME_FILTER_END:
            return False, None, None, None, None

    row_1h = df1h.iloc[h_idx]
    ema_1h = row_1h["ema200"]
    htf_lo = htf_so = True
    if config.HTF_FILTER and not pd.isna(ema_1h):
        p1h    = float(row_1h["close"])
        htf_lo = p1h > ema_1h
        htf_so = p1h < ema_1h

    return True, price, ema5, htf_lo, htf_so


# ── Signal generators ─────────────────────────────────────────────────────────

def _wr_signal_baseline(df5, i, price, ema5, htf_lo, htf_so):
    """Current strategy: WR-14 only."""
    lb = config.WR_EXHAUSTION_LOOKBACK
    sw = config.SWING_LOOKBACK
    wr = df5["wr14"].iloc[i]
    if pd.isna(wr):
        return None, None, None, None

    # Long
    if price > ema5 and htf_lo and wr > -80:
        prev = df5["wr14"].iloc[i - lb : i]
        if (prev < -80).sum() >= lb:
            sl = float(df5["low"].iloc[i - sw + 1 : i + 1].min()) * (1 - config.SL_BUFFER_PCT)
            d  = price - sl
            if d > 0:
                return "long", price, sl, price + d * config.RR

    # Short
    if price < ema5 and htf_so and wr < -20:
        prev = df5["wr14"].iloc[i - lb : i]
        if (prev > -20).sum() >= lb:
            sl = float(df5["high"].iloc[i - sw + 1 : i + 1].max()) * (1 + config.SL_BUFFER_PCT)
            d  = sl - price
            if d > 0:
                return "short", price, sl, price - d * config.RR

    return None, None, None, None


def _wr_signal_dual(df5, i, price, ema5, htf_lo, htf_so, require_both_hook=True):
    """
    Dual WR: both WR-14 and WR-112 must be in the extreme zone before the hook.
    require_both_hook=True  → both must exit the zone on the signal candle (strict)
    require_both_hook=False → WR-14 hooks out; WR-112 just needs to have been in zone (relaxed)
    """
    lb   = config.WR_EXHAUSTION_LOOKBACK
    sw   = config.SWING_LOOKBACK
    wr14 = df5["wr14"].iloc[i]
    wr112= df5["wr112"].iloc[i]
    if pd.isna(wr14) or pd.isna(wr112):
        return None, None, None, None

    # Long
    if price > ema5 and htf_lo and wr14 > -80:
        wr14_hooked = True  # already confirmed above
        wr112_in_zone = (df5["wr112"].iloc[i - lb : i] < -80).sum() >= lb
        if require_both_hook:
            wr112_hooked = wr112 > -80
            zone_ok = wr112_in_zone and wr112_hooked
        else:
            zone_ok = wr112_in_zone  # just needs to have been oversold

        prev14 = df5["wr14"].iloc[i - lb : i]
        wr14_zone = (prev14 < -80).sum() >= lb
        if wr14_zone and zone_ok:
            sl = float(df5["low"].iloc[i - sw + 1 : i + 1].min()) * (1 - config.SL_BUFFER_PCT)
            d  = price - sl
            if d > 0:
                return "long", price, sl, price + d * config.RR

    # Short
    if price < ema5 and htf_so and wr14 < -20:
        wr112_in_zone = (df5["wr112"].iloc[i - lb : i] > -20).sum() >= lb
        if require_both_hook:
            wr112_hooked = wr112 < -20
            zone_ok = wr112_in_zone and wr112_hooked
        else:
            zone_ok = wr112_in_zone

        prev14 = df5["wr14"].iloc[i - lb : i]
        wr14_zone = (prev14 > -20).sum() >= lb
        if wr14_zone and zone_ok:
            sl = float(df5["high"].iloc[i - sw + 1 : i + 1].max()) * (1 + config.SL_BUFFER_PCT)
            d  = sl - price
            if d > 0:
                return "short", price, sl, price - d * config.RR

    return None, None, None, None


def _rsi_signal(df5, i, price, ema5, htf_lo, htf_so):
    """RSI pullback signal (unchanged — used for RSI-approved coins)."""
    sw       = config.SWING_LOOKBACK
    rsi      = df5.iloc[i]["rsi"]
    rsi_prev = df5.iloc[i - 1]["rsi"]
    if pd.isna(rsi) or pd.isna(rsi_prev):
        return None, None, None, None

    if (price > ema5 and htf_lo
            and config.RSI_LONG_ZONE_LOW <= rsi <= config.RSI_LONG_ZONE_HIGH
            and rsi > rsi_prev):
        sl = float(df5["low"].iloc[i - sw + 1 : i + 1].min()) * (1 - config.SL_BUFFER_PCT)
        d  = price - sl
        if d > 0:
            return "long", price, sl, price + d * config.RR

    if (price < ema5 and htf_so
            and config.RSI_SHORT_ZONE_LOW <= rsi <= config.RSI_SHORT_ZONE_HIGH
            and rsi < rsi_prev):
        sl = float(df5["high"].iloc[i - sw + 1 : i + 1].max()) * (1 + config.SL_BUFFER_PCT)
        d  = sl - price
        if d > 0:
            return "short", price, sl, price - d * config.RR

    return None, None, None, None


# ── Walk-forward simulation ───────────────────────────────────────────────────

def _simulate(df5, df1h, symbol, wr_mode):
    """
    wr_mode: "baseline" | "dual_strict" | "dual_relaxed"
    Always uses RSI-first on RSI_APPROVED_COINS (RSI logic is unchanged).
    """
    ts5  = df5["timestamp"].values
    ts1h = df1h["timestamp"].values
    min_start = max(250, config.WR_EXHAUSTION_LOOKBACK + 1,
                    config.RSI_PERIOD + 1, config.SWING_LOOKBACK)

    trades     = []
    skip_until = -1

    for i in range(min_start, len(df5) - 1):
        if i < skip_until:
            continue

        h_idx = bisect.bisect_right(ts1h, ts5[i]) - 1
        ok, price, ema5, htf_lo, htf_so = _common_ok(df5, i, df1h, h_idx)
        if not ok:
            continue

        sig = entry = sl = tp = None

        # RSI layer first (only on RSI-approved coins, unchanged)
        if config.RSI_STRATEGY and symbol in config.RSI_APPROVED_COINS:
            sig, entry, sl, tp = _rsi_signal(df5, i, price, ema5, htf_lo, htf_so)

        # WR layer (mode-dependent)
        if sig is None:
            if wr_mode == "baseline":
                sig, entry, sl, tp = _wr_signal_baseline(df5, i, price, ema5, htf_lo, htf_so)
            elif wr_mode == "dual_strict":
                sig, entry, sl, tp = _wr_signal_dual(df5, i, price, ema5, htf_lo, htf_so,
                                                      require_both_hook=True)
            else:  # dual_relaxed
                sig, entry, sl, tp = _wr_signal_dual(df5, i, price, ema5, htf_lo, htf_so,
                                                      require_both_hook=False)

        if sig is None:
            continue

        # Simulate trade
        result = exit_px = exit_idx = None
        pnl_usd = 0.0
        for j in range(i + 1, min(i + 2000, len(df5))):
            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]
            if sig == "long":
                if lo <= sl:
                    result, exit_px, exit_idx = "loss", sl, j
                    pnl_usd = -config.RISK_PER_TRADE
                    break
                if hi >= tp:
                    result, exit_px, exit_idx = "win", tp, j
                    pnl_usd = config.RISK_PER_TRADE * config.RR
                    break
            else:
                if hi >= sl:
                    result, exit_px, exit_idx = "loss", sl, j
                    pnl_usd = -config.RISK_PER_TRADE
                    break
                if lo <= tp:
                    result, exit_px, exit_idx = "win", tp, j
                    pnl_usd = config.RISK_PER_TRADE * config.RR
                    break

        if result is None:
            continue

        trades.append({"result": result, "pnl_usd": pnl_usd})
        cooldown = config.TRADE_COOLDOWN_SECS // (5 * 60)
        skip_until = exit_idx + cooldown

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def _stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0
    wins    = [t for t in trades if t["result"] == "win"]
    losses  = [t for t in trades if t["result"] == "loss"]
    gw      = sum(t["pnl_usd"] for t in wins)
    gl      = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gw / gl if gl > 0 else float("inf")
    wr      = len(wins) / len(trades) * 100
    net     = sum(t["pnl_usd"] for t in trades)
    eq      = [0.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl_usd"])
    peak = mx = 0.0
    for e in eq:
        if e > peak:
            peak = e
        d = peak - e
        if d > mx:
            mx = d
    return pf, wr, net, mx, len(trades)


# ── Main ──────────────────────────────────────────────────────────────────────

MODES = [
    ("Baseline  (WR-14 only)",      "baseline"),
    ("Dual WR strict (both hook)",  "dual_strict"),
    ("Dual WR relaxed (14 hooks)",  "dual_relaxed"),
]


def _run_coin(symbol, days):
    print(f"\n{'=' * 76}")
    print(f"  {symbol}  |  {days} days  |  Dual WR confluence test")
    print(f"{'=' * 76}")
    print(f"  {'Mode':<32}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  {'MaxDD':>8}  {'N':>5}"
          f"  {'dPF':>6}  {'dNet':>8}")
    print(f"  {'-' * 73}")

    print("  Fetching candles ...", end="", flush=True)
    df5  = fetch_all_candles(exchange.session, symbol, 5,  days)
    df1h = fetch_all_candles(exchange.session, symbol, 60, days)
    print(" building indicators ...", end="", flush=True)
    df5  = _build_5m(df5)
    df1h = _build_1h(df1h)
    print(" done")

    base_pf = base_net = None
    for label, mode in MODES:
        trades = _simulate(df5, df1h, symbol, mode)
        pf, wr, net, dd, n = _stats(trades)

        if base_pf is None:
            base_pf, base_net = pf, net
            dpf_s = dnet_s = ""
            flag  = " ◄ baseline"
        else:
            dpf_s  = f"{pf  - base_pf:+6.2f}"
            dnet_s = f"{net - base_net:+8.0f}"
            if pf > base_pf and net > base_net:
                flag = "  ✅ BETTER"
            elif pf > base_pf or net > base_net:
                flag = "  ~ mixed"
            else:
                flag = "  ❌"

        print(f"  {label:<32}  {pf:5.2f}  {wr:5.1f}%  ${net:8.0f}  ${dd:6.0f}  {n:5d}"
              f"  {dpf_s:>6}  {dnet_s:>8}{flag}")


def main():
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    coins = args if args else ["BTCUSDT", "ZECUSDT", "SUIUSDT"]

    for idx, symbol in enumerate(coins):
        _run_coin(symbol, days)
        if idx < len(coins) - 1:
            print("\n  Sleeping 8s ...")
            time.sleep(8)
    print()


if __name__ == "__main__":
    main()
