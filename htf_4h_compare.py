"""
htf_4h_compare.py — Test a 4H EMA200 as a third higher-timeframe trend filter.

Current V2 stack: 5m EMA200  +  1H EMA200  (HTF_FILTER).
Hypothesis: adding a 4H EMA200 layer catches bear-market regimes that the 1H
misses — specifically, situations where the 1H is still bullish but the 4H has
already rolled over, causing longs to fight the intermediate trend.

Three modes tested per coin:

  Baseline    — unchanged V2: 5m EMA200 + 1H EMA200
  +4H         — triple stack: 5m EMA200 + 1H EMA200 + 4H EMA200
  4H-only     — replace 1H with 4H: 5m EMA200 + 4H EMA200 (diagnostic)

A long is allowed only when price is above all active EMAs.
A short is allowed only when price is below all active EMAs.

Applies to both WR and RSI signal layers equally.

Usage:
    python htf_4h_compare.py                                 # 4 coins, 180d
    python htf_4h_compare.py 180 BTCUSDT ZECUSDT SUIUSDT BCHUSDT
    python htf_4h_compare.py 90 BTCUSDT
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
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["wr"]     = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
    df["rsi"]    = ta.momentum.rsi(df["close"], window=config.RSI_PERIOD)
    df["atr"]    = ta.volatility.average_true_range(
                       df["high"], df["low"], df["close"], window=config.ATR_PERIOD)
    df["atr_ma"] = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
    return df


def _build_htf(df):
    """EMA200 for any higher timeframe (1H or 4H)."""
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


# ── Common pre-checks (shared by all modes) ───────────────────────────────────

def _common_ok(df5, i):
    """Check 5m-level filters: EMA200 warmup, ADX, ATR, time window."""
    min_i = max(250, config.SWING_LOOKBACK, config.WR_EXHAUSTION_LOOKBACK + 1,
                config.RSI_PERIOD + 1)
    if i < min_i:
        return False, None, None, None, None

    row    = df5.iloc[i]
    price  = row["close"]
    ema5   = row["ema200"]
    adx    = row["adx"]
    atr    = row["atr"]
    atr_ma = row["atr_ma"]

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

    return True, price, ema5, atr, adx


def _htf_alignment(df_htf, h_idx):
    """Return (long_ok, short_ok) for one HTF dataframe."""
    if h_idx < 200:
        return True, True   # not enough warmup — treat as neutral
    row   = df_htf.iloc[h_idx]
    p     = float(row["close"])
    ema   = row["ema200"]
    if pd.isna(ema):
        return True, True
    return p > ema, p < ema


# ── Signal generators (identical to backtest_hybrid logic) ───────────────────

def _wr_signal(df5, i, price, ema5, htf_lo, htf_so):
    lb = config.WR_EXHAUSTION_LOOKBACK
    sw = config.SWING_LOOKBACK
    wr = df5["wr"].iloc[i]
    if pd.isna(wr):
        return None, None, None

    if price > ema5 and htf_lo and wr > -80:
        prev = df5["wr"].iloc[i - lb : i]
        if (prev < -80).sum() >= lb:
            sl = float(df5["low"].iloc[i - sw + 1 : i + 1].min()) * (1 - config.SL_BUFFER_PCT)
            d  = price - sl
            if d > 0:
                return "long", sl, price + d * config.RR

    if price < ema5 and htf_so and wr < -20:
        prev = df5["wr"].iloc[i - lb : i]
        if (prev > -20).sum() >= lb:
            sl = float(df5["high"].iloc[i - sw + 1 : i + 1].max()) * (1 + config.SL_BUFFER_PCT)
            d  = sl - price
            if d > 0:
                return "short", sl, price - d * config.RR

    return None, None, None


def _rsi_signal(df5, i, price, ema5, htf_lo, htf_so):
    sw       = config.SWING_LOOKBACK
    rsi      = df5.iloc[i]["rsi"]
    rsi_prev = df5.iloc[i - 1]["rsi"]
    if pd.isna(rsi) or pd.isna(rsi_prev):
        return None, None, None

    if (price > ema5 and htf_lo
            and config.RSI_LONG_ZONE_LOW <= rsi <= config.RSI_LONG_ZONE_HIGH
            and rsi > rsi_prev):
        sl = float(df5["low"].iloc[i - sw + 1 : i + 1].min()) * (1 - config.SL_BUFFER_PCT)
        d  = price - sl
        if d > 0:
            return "long", sl, price + d * config.RR

    if (price < ema5 and htf_so
            and config.RSI_SHORT_ZONE_LOW <= rsi <= config.RSI_SHORT_ZONE_HIGH
            and rsi < rsi_prev):
        sl = float(df5["high"].iloc[i - sw + 1 : i + 1].max()) * (1 + config.SL_BUFFER_PCT)
        d  = sl - price
        if d > 0:
            return "short", sl, price - d * config.RR

    return None, None, None


# ── Walk-forward simulation ───────────────────────────────────────────────────

def _simulate(df5, df1h, df4h, symbol, mode):
    """
    mode: "baseline" | "plus_4h" | "only_4h"
      baseline  — 1H EMA200 only (current V2)
      plus_4h   — 1H EMA200 AND 4H EMA200 must agree
      only_4h   — 4H EMA200 only (replaces 1H)
    """
    ts5  = df5["timestamp"].values
    ts1h = df1h["timestamp"].values
    ts4h = df4h["timestamp"].values

    cooldown_bars = config.TRADE_COOLDOWN_SECS // (5 * 60)
    min_start     = max(250, config.WR_EXHAUSTION_LOOKBACK + 1,
                        config.RSI_PERIOD + 1, config.SWING_LOOKBACK)

    trades     = []
    skip_until = -1

    for i in range(min_start, len(df5) - 1):
        if i < skip_until:
            continue

        ok, price, ema5, atr, adx = _common_ok(df5, i)
        if not ok:
            continue

        # ── HTF alignment per mode ────────────────────────────────────────────
        h1_idx = bisect.bisect_right(ts1h, ts5[i]) - 1
        h4_idx = bisect.bisect_right(ts4h, ts5[i]) - 1

        if mode == "baseline":
            htf_lo, htf_so = _htf_alignment(df1h, h1_idx)

        elif mode == "plus_4h":
            lo1h, so1h = _htf_alignment(df1h, h1_idx)
            lo4h, so4h = _htf_alignment(df4h, h4_idx)
            # Both timeframes must agree
            htf_lo = lo1h and lo4h
            htf_so = so1h and so4h

        else:  # only_4h
            htf_lo, htf_so = _htf_alignment(df4h, h4_idx)

        # ── Signal: RSI first on approved coins, WR fallback ──────────────────
        sig = sl = tp = None

        if config.RSI_STRATEGY and symbol in config.RSI_APPROVED_COINS:
            sig, sl, tp = _rsi_signal(df5, i, price, ema5, htf_lo, htf_so)

        if sig is None:
            sig, sl, tp = _wr_signal(df5, i, price, ema5, htf_lo, htf_so)

        if sig is None:
            continue

        # ── Trade outcome ─────────────────────────────────────────────────────
        result = exit_idx = None
        pnl_usd = 0.0
        for j in range(i + 1, min(i + 2000, len(df5))):
            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]
            if sig == "long":
                if lo <= sl:
                    result, exit_idx = "loss", j
                    pnl_usd = -config.RISK_PER_TRADE
                    break
                if hi >= tp:
                    result, exit_idx = "win", j
                    pnl_usd = config.RISK_PER_TRADE * config.RR
                    break
            else:
                if hi >= sl:
                    result, exit_idx = "loss", j
                    pnl_usd = -config.RISK_PER_TRADE
                    break
                if lo <= tp:
                    result, exit_idx = "win", j
                    pnl_usd = config.RISK_PER_TRADE * config.RR
                    break

        if result is None:
            continue

        trades.append({"result": result, "pnl_usd": pnl_usd, "side": sig})
        skip_until = exit_idx + cooldown_bars

    return trades


# ── Statistics ────────────────────────────────────────────────────────────────

def _stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0, 0, 0
    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    longs  = [t for t in trades if t["side"] == "long"]
    gw     = sum(t["pnl_usd"] for t in wins)
    gl     = abs(sum(t["pnl_usd"] for t in losses))
    pf     = gw / gl if gl > 0 else float("inf")
    wr     = len(wins) / len(trades) * 100
    net    = sum(t["pnl_usd"] for t in trades)
    n_long = len(longs)
    n_short= len(trades) - n_long
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
    return pf, wr, net, mx, len(trades), n_long, n_short


MODES = [
    ("Baseline   (5m + 1H EMA200)",       "baseline"),
    ("+4H filter (5m + 1H + 4H EMA200)",  "plus_4h"),
    ("4H only    (5m + 4H EMA200)",       "only_4h"),
]


def _run_coin(symbol, days):
    print(f"\n{'=' * 82}")
    print(f"  {symbol}  |  {days} days  |  4H EMA200 filter test")
    print(f"{'=' * 82}")

    print("  Fetching candles ...", end="", flush=True)
    df5  = fetch_all_candles(exchange.session, symbol,   5, days)
    df1h = fetch_all_candles(exchange.session, symbol,  60, days)
    df4h = fetch_all_candles(exchange.session, symbol, 240, days)
    print(" done")

    print("  Computing indicators ...", end="", flush=True)
    df5  = _build_5m(df5)
    df1h = _build_htf(df1h)
    df4h = _build_htf(df4h)
    print(" done\n")

    print(f"  {'Mode':<40}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  "
          f"{'MaxDD':>8}  {'N':>5}  {'L/S':>7}  {'dPF':>6}  {'dNet':>8}")
    print(f"  {'-' * 80}")

    base_pf = base_net = None

    for label, mode in MODES:
        trades = _simulate(df5, df1h, df4h, symbol, mode)
        pf, wr, net, dd, n, n_long, n_short = _stats(trades)
        dpf  = f"{pf  - base_pf :.2f}" if base_pf  is not None else "  —  "
        dnet = f"{net - base_net:+.0f}" if base_net is not None else "     —  "
        print(f"  {label:<40}  {pf:5.2f}  {wr:6.1f}  "
              f"${net:+8.0f}  ${dd:7.0f}  {n:5d}  "
              f"{n_long:3d}L/{n_short:<3d}S  {dpf:>6}  {dnet:>8}")
        if base_pf is None:
            base_pf, base_net = pf, net


# ── Entry point ───────────────────────────────────────────────────────────────

DEFAULT_COINS = ["BTCUSDT", "ZECUSDT", "SUIUSDT", "BCHUSDT"]
DEFAULT_DAYS  = 180

if __name__ == "__main__":
    args  = sys.argv[1:]
    # First numeric arg = days; remaining positional args = symbols
    days    = DEFAULT_DAYS
    symbols = []
    for a in args:
        if a.isdigit():
            days = int(a)
        else:
            symbols.append(a.upper())
    if not symbols:
        symbols = DEFAULT_COINS

    print(f"\n4H EMA200 Filter Comparison  |  {days}d  |  coins: {', '.join(symbols)}")
    print(f"RR={config.RR}  risk=${config.RISK_PER_TRADE}/trade  "
          f"ADX≥{config.ADX_THRESHOLD}  ATR>ATR_MA  "
          f"TIME={'on' if config.TIME_FILTER else 'off'}")

    for sym in symbols:
        _run_coin(sym, days)

    print(f"\n{'=' * 82}")
    print("  Legend:")
    print("  Baseline   = current V2: 5m EMA200 + 1H EMA200")
    print("  +4H filter = triple stack: 5m + 1H + 4H EMA200 (all must agree)")
    print("  4H only    = diagnostic: 5m EMA200 + 4H EMA200 (replaces 1H)")
    print("  dPF / dNet = delta vs Baseline")
    print("  L/S        = long/short trade split")
    print(f"{'=' * 82}\n")
