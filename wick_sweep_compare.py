"""
wick_sweep_compare.py — Test a "liquidity sweep / fake breakout" filter on top of
the existing WR + RSI hybrid signal.

The hypothesis:
  A valid WR/RSI reversal is more reliable when the candle BEFORE the signal bar
  spiked *beyond* the recent range (stop hunt / liquidity sweep) but CLOSED back
  inside it — confirming the breakout was fake and a reversal is forming.

Filter conditions tested:
  Variant A — WICK_SWEEP_LOOKBACK bars of range, wick exceeds range by ≥ 0 (any sweep)
  Variant B — wick must exceed range by ≥ WICK_SWEEP_MIN_PCT × ATR  (minimum spike size)
  Variant C — Variant B + close must be inside the range by ≥ WICK_SWEEP_CLOSE_PCT × ATR

For longs  : bar[i-1] wick HIGH > recent N-bar high  AND  close[i-1] <= recent N-bar high
For shorts : bar[i-1] wick LOW  < recent N-bar low   AND  close[i-1] >= recent N-bar low

Usage:
    python wick_sweep_compare.py                             # BTCUSDT, 180 days
    python wick_sweep_compare.py 180 BTCUSDT ZECUSDT SUIUSDT  # multiple coins
    python wick_sweep_compare.py 90 BTCUSDT                   # 90-day window
"""

import bisect
import contextlib
import io
import sys
import time

import pandas as pd

import config
import backtest_hybrid as bh
from data_cache import DataCache, CacheMissError

_cache = DataCache()

# ── Wick sweep variants to test ───────────────────────────────────────────────
# Each entry: (label, lookback_bars, min_spike_atr, min_close_inside_atr)
#   lookback_bars      — how many bars define the "range" high/low
#   min_spike_atr      — wick must exceed range by ≥ this × ATR  (0 = any wick)
#   min_close_inside   — close must be inside range by ≥ this × ATR  (0 = any close inside)
VARIANTS = [
    ("any wick  N=5",  5,  0.0, 0.0),
    ("any wick  N=10", 10, 0.0, 0.0),
    ("any wick  N=20", 20, 0.0, 0.0),
    ("0.3×ATR   N=10", 10, 0.3, 0.0),
    ("0.5×ATR   N=10", 10, 0.5, 0.0),
    ("1.0×ATR   N=10", 10, 1.0, 0.0),
    ("0.5×ATR+close", 10, 0.5, 0.3),  # spike ≥ 0.5 ATR AND close inside by ≥ 0.3 ATR
]


# ── Wick sweep check ─────────────────────────────────────────────────────────

def _wick_sweep_long(df5, i, lookback, min_spike_atr, min_close_inside_atr):
    """
    Return True if bar[i-1] spiked BELOW the recent range low and closed back above it.
    (Stop-hunt of support → rejection → long signal on bar[i].)
    bar[i] is the signal candle; bar[i-1] is the sweep candle.
    Range defined by bar[i-lookback-1 : i-1] (excludes the sweep candle itself).
    """
    if i < lookback + 2:
        return False
    atr = df5.iloc[i]["atr"]
    if pd.isna(atr) or atr <= 0:
        return False
    sweep_bar = df5.iloc[i - 1]
    range_low = float(df5["low"].iloc[i - lookback - 1 : i - 1].min())

    # Wick must have broken below the range low
    if sweep_bar["low"] >= range_low:
        return False
    spike = range_low - sweep_bar["low"]
    if spike < min_spike_atr * atr:
        return False

    # Close must be back inside (above) the range low
    if sweep_bar["close"] < range_low:
        return False
    if min_close_inside_atr > 0:
        close_inside = sweep_bar["close"] - range_low
        if close_inside < min_close_inside_atr * atr:
            return False
    return True


def _wick_sweep_short(df5, i, lookback, min_spike_atr, min_close_inside_atr):
    """
    Return True if bar[i-1] spiked ABOVE the recent range high and closed back below it.
    (Stop-hunt of resistance → rejection → short signal on bar[i].)
    """
    if i < lookback + 2:
        return False
    atr = df5.iloc[i]["atr"]
    if pd.isna(atr) or atr <= 0:
        return False
    sweep_bar  = df5.iloc[i - 1]
    range_high = float(df5["high"].iloc[i - lookback - 1 : i - 1].max())

    # Wick must have broken above the range high
    if sweep_bar["high"] <= range_high:
        return False
    spike = sweep_bar["high"] - range_high
    if spike < min_spike_atr * atr:
        return False

    # Close must be back inside (below) the range high
    if sweep_bar["close"] > range_high:
        return False
    if min_close_inside_atr > 0:
        close_inside = range_high - sweep_bar["close"]
        if close_inside < min_close_inside_atr * atr:
            return False
    return True


# ── Filtered signal check ─────────────────────────────────────────────────────

def _check_signal_with_sweep(df5, i, df1h, h_idx, symbol, df4h, h4_idx,
                             lookback, min_spike, min_close):
    """Run the existing hybrid signal check, then apply the wick sweep filter."""
    sig, entry, sl, tp, strat = bh._check_signal(
        df5, i, df1h, h_idx, symbol, df4h=df4h, h4_idx=h4_idx)
    if sig is None:
        return None, None, None, None
    if sig == "long" and not _wick_sweep_long(df5, i, lookback, min_spike, min_close):
        return None, None, None, None
    if sig == "short" and not _wick_sweep_short(df5, i, lookback, min_spike, min_close):
        return None, None, None, None
    return sig, entry, sl, tp


# ── Single-pass backtest with optional sweep filter ───────────────────────────

def _run_backtest(df5, df1h, df4h, ts4h, symbol, use_sweep,
                  lookback=10, min_spike=0.5, min_close=0.0):
    ts5  = df5["timestamp"].values
    ts1h = df1h["timestamp"].values

    min_start = max(250, lookback + 2,
                    config.WR_EXHAUSTION_LOOKBACK + 1,
                    config.RSI_PERIOD + 1,
                    config.SWING_LOOKBACK)
    trades     = []
    skip_until = -1

    for i in range(min_start, len(df5) - 1):
        if i < skip_until:
            continue
        h_idx  = bisect.bisect_right(ts1h, ts5[i]) - 1
        h4_idx = (bisect.bisect_right(ts4h, ts5[i]) - 1) if ts4h is not None else -1

        if use_sweep:
            sig, entry, sl, tp = _check_signal_with_sweep(
                df5, i, df1h, h_idx, symbol, df4h, h4_idx,
                lookback, min_spike, min_close)
        else:
            sig, entry, sl, tp, _ = bh._check_signal(
                df5, i, df1h, h_idx, symbol, df4h=df4h, h4_idx=h4_idx)

        if sig is None:
            continue

        sl_dist = abs(entry - sl)
        for j in range(i + 1, min(i + 2000, len(df5))):
            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]
            if sig == "long":
                if hi >= tp:
                    trades.append({"result": "win",  "pnl": config.RISK_PER_TRADE * config.RR})
                    skip_until = j + config.TRADE_COOLDOWN_SECS // 300
                    break
                if lo <= sl:
                    trades.append({"result": "loss", "pnl": -config.RISK_PER_TRADE})
                    skip_until = j + config.TRADE_COOLDOWN_SECS // 300
                    break
            else:
                if lo <= tp:
                    trades.append({"result": "win",  "pnl": config.RISK_PER_TRADE * config.RR})
                    skip_until = j + config.TRADE_COOLDOWN_SECS // 300
                    break
                if hi >= sl:
                    trades.append({"result": "loss", "pnl": -config.RISK_PER_TRADE})
                    skip_until = j + config.TRADE_COOLDOWN_SECS // 300
                    break

    return trades


def _stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0
    wins    = [t for t in trades if t["result"] == "win"]
    losses  = [t for t in trades if t["result"] == "loss"]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    wr      = len(wins) / len(trades) * 100
    net     = sum(t["pnl"] for t in trades)
    equity  = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl"])
    peak = dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak - e > dd:
            dd = peak - e
    return pf, wr, dd, net, len(trades)


# ── Per-coin sweep ────────────────────────────────────────────────────────────

def _run_coin(symbol, days):
    import exchange
    print(f"\n  Loading {symbol} ...", end="", flush=True)
    df5  = _cache.get(symbol, 5,   days, exchange=exchange.session)
    df1h = _cache.get(symbol, 60,  days, exchange=exchange.session)
    df4h = None
    ts4h = None
    excl_4h = getattr(config, "HTF_4H_FILTER_EXCLUDED", [])
    if getattr(config, "HTF_4H_FILTER", False) and symbol not in excl_4h:
        df4h = _cache.get(symbol, 240, days, exchange=exchange.session)
        df4h = bh._build_4h_indicators(df4h)
        ts4h = df4h["timestamp"].values
    df5  = bh._build_5m_indicators(df5)
    df1h = bh._build_1h_indicators(df1h)
    print(" done")

    print(f"\n{'=' * 74}")
    print(f"  {symbol}  |  {days} days  |  Wick Sweep Filter comparison")
    print(f"{'=' * 74}")
    print(f"  {'Variant':<20}  {'PF':>5}  {'WR%':>6}  {'N':>5}  {'Net':>8}  "
          f"{'MaxDD':>7}  {'dPF':>6}  {'dN%':>6}  Result")
    print(f"  {'-' * 71}")

    # Baseline — no filter
    base_trades = _run_backtest(df5, df1h, df4h, ts4h, symbol, use_sweep=False)
    pf0, wr0, dd0, net0, n0 = _stats(base_trades)
    print(f"  {'Baseline':<20}  {pf0:5.2f}  {wr0:5.1f}%  {n0:5d}  "
          f"${net0:7.0f}  ${dd0:6.0f}  {'':>6}  {'':>6}  (no filter)")

    best = None

    for label, lookback, min_spike, min_close in VARIANTS:
        trades = _run_backtest(df5, df1h, df4h, ts4h, symbol, use_sweep=True,
                               lookback=lookback, min_spike=min_spike,
                               min_close=min_close)
        pf, wr, dd, net, n = _stats(trades)
        dpf  = pf - pf0
        dn_p = (n - n0) / n0 * 100 if n0 > 0 else 0.0

        n_ok   = n >= n0 * 0.30          # must keep ≥ 30% of trades
        pf_ok  = pf > pf0
        wr_ok  = wr > wr0
        net_ok = net > net0
        dd_ok  = dd <= dd0 * 1.15

        if not n_ok:
            icon, note = "⚠ ", "too few trades"
        elif pf_ok and net_ok and dd_ok:
            icon, note = "✅", "BETTER"
            if best is None or pf > best[0]:
                best = (pf, label)
        else:
            icon, note = "❌", "no gain"

        print(f"  {label:<20}  {pf:5.2f}  {wr:5.1f}%  {n:5d}  "
              f"${net:7.0f}  ${dd:6.0f}  {dpf:+6.2f}  {dn_p:+5.1f}%  {icon} {note}")

    print(f"  {'-' * 71}")
    if best:
        print(f"\n  >> Best variant: {best[1]}  (PF {pf0:.2f} → {best[0]:.2f})")
    else:
        print(f"\n  >> No variant improved PF + net PnL for {symbol}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    coins = args if args else ["BTCUSDT"]

    print(f"\nWick Sweep Filter  |  {days} days  |  coins: {', '.join(coins)}")
    print("Hypothesis: bar[i-1] spiked beyond N-bar range but closed back inside")
    print("           → confirms fake breakout before the WR/RSI reversal signal\n")

    for idx, symbol in enumerate(coins):
        _run_coin(symbol, days)
        if idx < len(coins) - 1:
            print("\n  Sleeping 5s ...")
            time.sleep(5)
    print()


if __name__ == "__main__":
    main()
