"""
stochrsi_compare.py — Test StochRSI as an additional signal-quality filter.

The original %R Trend Exhaustion author (upslidedown) stated in his TradingView
comments that his personal stack is:

    "%RTE + StochRSI + WaveTrend crosses for harmonic pattern confirmations"
    "I use it along with srsi and wavetrend crosses"

Idea: after a WR or RSI pullback signal fires, only take it when the StochRSI
confirms that momentum is turning from an extreme.

Three modes are tested:

  Baseline     — current strategy (no StochRSI gate)
  Zone only    — signal is taken only when StochRSI-K is inside the extreme zone
                   Long:  stochrsi_k < 0.20  (oversold zone)
                   Short: stochrsi_k > 0.80  (overbought zone)
  Crossing     — stricter: stochrsi_k must be in zone AND turning back
                   Long:  stochrsi_k < 0.20 and stochrsi_k > stochrsi_k_prev
                   Short: stochrsi_k > 0.80 and stochrsi_k < stochrsi_k_prev

StochRSI settings: window=14, smooth1=3, smooth2=3 (standard "14,3,3" settings).
Applied to both the WR and RSI signal layers.

Usage:
    python stochrsi_compare.py                              # BTC 180d
    python stochrsi_compare.py 180 BTCUSDT ZECUSDT SUIUSDT
    python stochrsi_compare.py 90 BTCUSDT
"""

import bisect
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import ta

import config
import exchange

# ── Candle fetching ───────────────────────────────────────────────────────────

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
    ms_per_candle = interval * 60 * 1000
    total_candles = days * 24 * 60 // interval
    end_ms        = int(time.time() * 1000)
    all_candles   = []
    while len(all_candles) < total_candles:
        chunk = _fetch_chunk(session, symbol, interval, end_ms)
        if not chunk:
            break
        all_candles.extend(chunk)
        end_ms = int(chunk[-1][0]) - 1
        time.sleep(0.15)
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
    df["ema200"]      = ta.trend.ema_indicator(df["close"], window=200)
    df["adx"]         = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["wr14"]        = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
    df["rsi"]         = ta.momentum.rsi(df["close"], window=config.RSI_PERIOD)
    df["atr"]         = ta.volatility.average_true_range(
                            df["high"], df["low"], df["close"], window=config.ATR_PERIOD)
    df["atr_ma"]      = df["atr"].rolling(config.ATR_MA_PERIOD).mean()

    stoch = ta.momentum.StochRSIIndicator(
        df["close"], window=14, smooth1=3, smooth2=3
    )
    df["stochrsi_k"] = stoch.stochrsi_k()   # smoothed %K (0–1 scale)
    df["stochrsi_d"] = stoch.stochrsi_d()   # smoothed %D
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


# ── StochRSI gate ────────────────────────────────────────────────────────────

def _stochrsi_gate(df5, i, direction, mode):
    """
    mode: "zone" → k inside extreme zone; "crossing" → zone AND k turning back.
    direction: "long" or "short"
    Returns True if the gate passes (signal allowed through).
    """
    k      = df5["stochrsi_k"].iloc[i]
    k_prev = df5["stochrsi_k"].iloc[i - 1] if i > 0 else float("nan")
    if pd.isna(k) or pd.isna(k_prev):
        return False

    if direction == "long":
        in_zone   = k < 0.20
        turning   = k > k_prev         # K curling upward (momentum returning)
    else:
        in_zone   = k > 0.80
        turning   = k < k_prev         # K curling downward

    if mode == "zone":
        return in_zone
    else:  # crossing
        return in_zone and turning


# ── Signal generators (identical logic to backtest_hybrid) ───────────────────

def _wr_signal(df5, i, price, ema5, htf_lo, htf_so):
    lb = config.WR_EXHAUSTION_LOOKBACK
    sw = config.SWING_LOOKBACK
    wr = df5["wr14"].iloc[i]
    if pd.isna(wr):
        return None, None, None, None

    if price > ema5 and htf_lo and wr > -80:
        prev = df5["wr14"].iloc[i - lb : i]
        if (prev < -80).sum() >= lb:
            sl = float(df5["low"].iloc[i - sw + 1 : i + 1].min()) * (1 - config.SL_BUFFER_PCT)
            d  = price - sl
            if d > 0:
                return "long", price, sl, price + d * config.RR

    if price < ema5 and htf_so and wr < -20:
        prev = df5["wr14"].iloc[i - lb : i]
        if (prev > -20).sum() >= lb:
            sl = float(df5["high"].iloc[i - sw + 1 : i + 1].max()) * (1 + config.SL_BUFFER_PCT)
            d  = sl - price
            if d > 0:
                return "short", price, sl, price - d * config.RR

    return None, None, None, None


def _rsi_signal(df5, i, price, ema5, htf_lo, htf_so):
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

def _simulate(df5, df1h, symbol, stoch_mode):
    """
    stoch_mode: "baseline" | "zone" | "crossing"
    RSI layer is unchanged for RSI_APPROVED_COINS.
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

        # RSI layer first (RSI-approved coins only)
        if config.RSI_STRATEGY and symbol in config.RSI_APPROVED_COINS:
            sig, entry, sl, tp = _rsi_signal(df5, i, price, ema5, htf_lo, htf_so)

            # Apply StochRSI gate to RSI signals
            if sig is not None and stoch_mode != "baseline":
                if not _stochrsi_gate(df5, i, sig, stoch_mode):
                    sig = entry = sl = tp = None

        # WR layer fallback
        if sig is None:
            sig, entry, sl, tp = _wr_signal(df5, i, price, ema5, htf_lo, htf_so)

            # Apply StochRSI gate to WR signals
            if sig is not None and stoch_mode != "baseline":
                if not _stochrsi_gate(df5, i, sig, stoch_mode):
                    sig = entry = sl = tp = None

        if sig is None:
            continue

        # Simulate trade outcome
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
        cooldown    = config.TRADE_COOLDOWN_SECS // (5 * 60)
        skip_until  = exit_idx + cooldown

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def _stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0
    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    gw     = sum(t["pnl_usd"] for t in wins)
    gl     = abs(sum(t["pnl_usd"] for t in losses))
    pf     = gw / gl if gl > 0 else float("inf")
    wr     = len(wins) / len(trades) * 100
    net    = sum(t["pnl_usd"] for t in trades)
    eq     = [0.0]
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


# ── Per-coin runner ───────────────────────────────────────────────────────────

MODES = [
    ("Baseline  (no StochRSI)",          "baseline"),
    ("Zone only (k<20 / k>80)",          "zone"),
    ("Crossing  (zone + k turning)",     "crossing"),
]


def _run_coin(symbol, days):
    print(f"\n{'=' * 76}")
    print(f"  {symbol}  |  {days} days  |  StochRSI filter test")
    print(f"{'=' * 76}")
    print(f"  {'Mode':<35}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  {'MaxDD':>8}  {'N':>5}"
          f"  {'dPF':>6}  {'dNet':>8}")
    print(f"  {'-' * 76}")

    print("  Fetching candles ...", end="", flush=True)
    df5  = fetch_all_candles(exchange.session, symbol,  5, days)
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

        print(f"  {label:<35}  {pf:5.2f}  {wr:5.1f}%  ${net:8.0f}  ${dd:6.0f}  {n:5d}"
              f"  {dpf_s:>6}  {dnet_s:>8}{flag}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args    = sys.argv[1:]
    days    = int(args[0]) if args and args[0].isdigit() else 180
    symbols = [a.upper() for a in args[1:]] if len(args) > 1 else ["BTCUSDT"]

    print(f"\nStochRSI filter comparison — {days}d")
    print("StochRSI(14,3,3):  Long gate k<0.20  |  Short gate k>0.80")
    print("Modes: zone-only vs zone+turning (crossing)")

    for sym in symbols:
        _run_coin(sym, days)

    print(f"\n{'=' * 76}")
    print("Verdict guide:")
    print("  ✅ BETTER  →  both PF and net PnL improved  →  consider adopting")
    print("  ~ mixed    →  one improved, one didn't      →  marginal; skip")
    print("  ❌         →  both worse OR trade count cut with no PF gain  →  reject")
    print()


if __name__ == "__main__":
    main()
