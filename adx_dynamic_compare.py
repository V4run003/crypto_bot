"""
adx_dynamic_compare.py — Test dynamic ADX floor: higher threshold in bear regime.

Hypothesis (Task 4): bear-regime bars have lower signal quality — raise ADX floor
from 18 → 20/22/24 only during bear periods, leave bull/neutral at 18.

Method:
  1. Run standard backtest at ADX=18 to get baseline trades.
  2. Post-hoc annotate each trade with its entry ADX value and daily regime
     (bull / bear / neutral) using the same logic as _check_signal.
  3. Show per-regime PnL breakdown — diagnose whether bear trades are
     structurally weaker.
  4. Simulate bear-floor = [18, 20, 22, 24] by dropping bear-regime trades
     with ADX < floor, re-compute PF/Net.

Note: post-hoc filtering slightly overstates improvement (dropped bear trades
would have shifted cooldown timing for subsequent entries). Treat as directional
signal only — if improvement is marginal, don't bother implementing.

Usage:
    python adx_dynamic_compare.py                          # all APPROVED_COINS, 180d
    python adx_dynamic_compare.py 180 BTCUSDT ZECUSDT
    python adx_dynamic_compare.py 180                      # all coins
"""

import bisect
import contextlib
import io
import sys
import time
from datetime import datetime, timezone

import pandas as pd

import config
import exchange
import backtest_hybrid as bh

BEAR_FLOORS  = [18, 20, 22, 24]   # 18 = baseline (no change)
DAYS_DEFAULT = 180


# ── helpers ───────────────────────────────────────────────────────────────────

def _regime_at(ts_ms, ts1d_arr, df1d):
    """Return 'bull', 'bear', or 'neutral' for the daily bar preceding ts_ms."""
    if df1d is None or len(ts1d_arr) == 0:
        return "unknown"
    # Use the closed daily bar before this timestamp (same as _check_signal d_idx)
    d_idx = bisect.bisect_right(ts1d_arr, ts_ms) - 2
    if d_idx < 0:
        return "unknown"
    row   = df1d.iloc[d_idx]
    price = row["close"]
    ema   = row["ema_regime"]
    if pd.isna(ema):
        return "unknown"
    band = config.REGIME_NEUTRAL_PCT
    if price < ema * (1 - band):
        return "bear"
    elif price > ema * (1 + band):
        return "bull"
    return "neutral"


def _adx_at(entry_time, df5):
    """Return ADX value of the 5m bar closest to entry_time."""
    ts_ms  = int(entry_time.timestamp() * 1000)
    ts_arr = df5["timestamp"].values
    idx    = bisect.bisect_right(ts_arr, ts_ms) - 1
    if idx < 0:
        return float("nan")
    return df5.iloc[idx]["adx"]


def _stats(trades):
    if not trades:
        return {"n": 0, "pf": float("nan"), "wr": float("nan"),
                "net": 0.0, "max_dd": 0.0}
    wins  = [t for t in trades if t["result"] == "win"]
    gw    = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gl    = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf    = gw / gl if gl > 0 else float("inf")
    wr    = len(wins) / len(trades) * 100
    net   = sum(t["pnl_usd"] for t in trades)
    eq    = [0.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl_usd"])
    peak = mx = 0.0
    for e in eq:
        if e > peak:
            peak = e
        if peak - e > mx:
            mx = peak - e
    return {"n": len(trades), "pf": pf, "wr": wr, "net": net, "max_dd": mx}


# ── per-coin analysis ─────────────────────────────────────────────────────────

def _analyse_coin(symbol, days):
    print(f"\n{'=' * 72}")
    print(f"  {symbol}  |  {days} days  |  ADX_THRESHOLD=18 baseline + dynamic bear floor")
    print(f"{'=' * 72}")

    # Fetch data
    print("  Fetching candles ...", end="", flush=True)
    df5  = bh.fetch_all_candles(exchange.session, symbol,   5, days)
    time.sleep(5)
    df1h = bh.fetch_all_candles(exchange.session, symbol,  60, days)
    time.sleep(5)
    df1d = bh.fetch_daily_candles(exchange.session, symbol, days + 60)
    print(" done")

    df5  = bh._build_5m_indicators(df5)
    df1h = bh._build_1h_indicators(df1h)
    df1d = bh._build_daily_indicators(df1d)
    ts1d_arr = df1d["timestamp"].values

    # Run baseline at ADX=18
    config.ADX_THRESHOLD = 18
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        baseline_trades = bh.run_backtest(symbol, days, quiet=True)

    if not baseline_trades:
        print(f"  No trades — skipping.")
        return

    # Annotate trades with ADX at entry + daily regime
    for t in baseline_trades:
        ts_ms        = int(t["entry_time"].timestamp() * 1000)
        t["adx_val"] = _adx_at(t["entry_time"], df5)
        t["regime"]  = _regime_at(ts_ms, ts1d_arr, df1d)

    # ── Regime breakdown ──────────────────────────────────────────────────────
    print(f"\n  Regime breakdown  (ADX_THRESHOLD=18 baseline, N={len(baseline_trades)})")
    print(f"  {'Regime':<10}  {'N':>4}  {'WR%':>5}  {'PF':>5}  {'Net':>9}  "
          f"{'MaxDD':>7}  {'AvgADX':>7}")
    print(f"  {'-' * 58}")

    for regime in ("bull", "neutral", "bear", "unknown"):
        subset = [t for t in baseline_trades if t["regime"] == regime]
        if not subset:
            continue
        s      = _stats(subset)
        avg_adx = sum(t["adx_val"] for t in subset if not pd.isna(t["adx_val"])) / len(subset)
        print(f"  {regime:<10}  {s['n']:>4}  {s['wr']:>4.1f}%  {s['pf']:>5.2f}  "
              f"${s['net']:>8.0f}  ${s['max_dd']:>6.0f}  {avg_adx:>6.1f}")

    # ── Dynamic bear floor simulation ─────────────────────────────────────────
    bear_trades   = [t for t in baseline_trades if t["regime"] == "bear"]
    nobear_trades = [t for t in baseline_trades if t["regime"] != "bear"]

    if not bear_trades:
        print(f"\n  No bear-regime trades found — dynamic floor has no effect.")
        return

    print(f"\n  Dynamic bear floor simulation  ({len(bear_trades)} bear trades total)")
    print(f"  {'BearFloor':>9}  {'N_dropped':>9}  {'N_kept':>6}  {'N_total':>7}  "
          f"{'PF':>5}  {'Net':>9}  {'MaxDD':>7}  {'dNet':>8}")
    print(f"  {'-' * 70}")

    base_s  = _stats(baseline_trades)
    base_net = base_s["net"]

    for floor in BEAR_FLOORS:
        # Drop bear trades that would have been blocked by the higher floor
        kept_bear = [t for t in bear_trades if t["adx_val"] >= floor]
        dropped   = len(bear_trades) - len(kept_bear)
        sim_trades = sorted(nobear_trades + kept_bear,
                            key=lambda t: t["entry_time"])
        s    = _stats(sim_trades)
        dnet = s["net"] - base_net
        flag = " ◄ baseline" if floor == 18 else (
               " ◄ BEST" if s["net"] == max(
                   _stats(sorted(nobear_trades + [t for t in bear_trades if t["adx_val"] >= f],
                                 key=lambda t: t["entry_time"]))["net"]
                   for f in BEAR_FLOORS
               ) else ""
        )
        print(f"  {floor:>9}  {dropped:>9}  {len(kept_bear):>6}  {s['n']:>7}  "
              f"{s['pf']:>5.2f}  ${s['net']:>8.0f}  ${s['max_dd']:>6.0f}  "
              f"${dnet:>+7.0f}{flag}")

    # ADX distribution in bear trades — shows how many would be cut at each floor
    print(f"\n  Bear ADX distribution  (how many trades per ADX bucket):")
    print(f"  {'ADX range':<14}  {'N':>4}  {'WR%':>5}  {'PF':>5}  {'Net':>9}")
    print(f"  {'-' * 44}")
    buckets = [(18, 20), (20, 22), (22, 24), (24, 26), (26, 9999)]
    for lo, hi in buckets:
        sub = [t for t in bear_trades if lo <= t["adx_val"] < hi]
        if not sub:
            continue
        s = _stats(sub)
        label = f"ADX {lo}-{hi}" if hi < 9999 else f"ADX {lo}+"
        print(f"  {label:<14}  {s['n']:>4}  {s['wr']:>4.1f}%  {s['pf']:>5.2f}  "
              f"${s['net']:>8.0f}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    days = DAYS_DEFAULT
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]

    symbols = args if args else list(config.APPROVED_COINS)

    orig_adx = config.ADX_THRESHOLD  # save to restore after each coin

    print(f"\n  Dynamic ADX Compare  |  bear floor test  |  {days} days")
    print(f"  Coins: {', '.join(symbols)}")
    print(f"  Baseline ADX={orig_adx}  |  Bear floors tested: {BEAR_FLOORS}")

    for idx, sym in enumerate(symbols):
        try:
            _analyse_coin(sym, days)
        except Exception as exc:
            print(f"\n  {sym}: FAILED ({exc})")
        finally:
            config.ADX_THRESHOLD = orig_adx  # always restore

        if idx < len(symbols) - 1:
            print("\n  Sleeping 8s between coins ...")
            time.sleep(8)

    print(f"\n{'=' * 72}")
    print(f"  Analysis complete.  ADX_THRESHOLD restored to {orig_adx}.")
    print(f"  Verdict guide:")
    print(f"    Bear PF < Bull PF    → bear trades are structurally weaker (expected)")
    print(f"    dNet at floor 22 > 0 → raising bear floor improves portfolio")
    print(f"    dNet at floor 22 < 0 → bear trades are fine; don't raise floor")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
