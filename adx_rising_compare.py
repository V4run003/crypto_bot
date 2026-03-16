"""
adx_rising_compare.py — Compare baseline vs ADX_RISING vs ADX_RISING2 filters.

Runs the hybrid backtest 3 ways on each coin:
  1. Both filters OFF          (baseline)
  2. ADX_RISING_FILTER = True  (1 candle rising)
  3. ADX_RISING2_FILTER = True (2 consecutive candles rising)

Usage:
    python adx_rising_compare.py                    # BTCUSDT, 180 days
    python adx_rising_compare.py 180 BTCUSDT ZECUSDT
    python adx_rising_compare.py 90 BTCUSDT
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid


def _compute_stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0
    wins    = [t for t in trades if t["result"] == "win"]
    losses  = [t for t in trades if t["result"] == "loss"]
    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    wr      = len(wins) / len(trades) * 100
    equity  = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak = mx = 0.0
    for e in equity:
        if e > peak:
            peak = e
        d = peak - e
        if d > mx:
            mx = d
    return pf, wr, mx, len(trades)


def _run(symbol, days):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        trades = backtest_hybrid.run_backtest(symbol, days, quiet=True)
    return _compute_stats(trades)


def _test(symbol, days):
    print(f"\n{'=' * 68}")
    print(f"  {symbol}  |  {days} days  |  ADX rising filter comparison")
    print(f"{'=' * 68}")
    print(f"  {'Mode':<26}  {'PF':>5}  {'WR%':>6}  {'N':>5}  {'MaxDD':>8}  {'dPF':>6}  {'dWR':>7}")
    print(f"  {'-' * 63}")

    # 1. Baseline
    config.ADX_RISING_FILTER  = False
    config.ADX_RISING2_FILTER = False
    pf0, wr0, dd0, n0 = _run(symbol, days)
    print(f"  {'Baseline (both OFF)':<26}  {pf0:5.2f}  {wr0:5.1f}%  {n0:5d}  ${dd0:6.0f}  {'':>6}  {'':>7}")
    time.sleep(4)

    # 2. ADX_RISING (1 candle)
    config.ADX_RISING_FILTER  = True
    config.ADX_RISING2_FILTER = False
    pf1, wr1, dd1, n1 = _run(symbol, days)
    dpf1, dwr1 = pf1 - pf0, wr1 - wr0
    flag1 = "✅" if pf1 > pf0 and wr1 > wr0 and n1 >= n0 * 0.4 else ("⚠ " if n1 < n0 * 0.4 else "❌")
    print(f"  {'ADX rising 1 candle':<26}  {pf1:5.2f}  {wr1:5.1f}%  {n1:5d}  ${dd1:6.0f}  {dpf1:+6.2f}  {dwr1:+6.1f}pp  {flag1}")
    time.sleep(4)

    # 3. ADX_RISING2 (2 candles)
    config.ADX_RISING_FILTER  = False
    config.ADX_RISING2_FILTER = True
    pf2, wr2, dd2, n2 = _run(symbol, days)
    dpf2, dwr2 = pf2 - pf0, wr2 - wr0
    flag2 = "✅" if pf2 > pf0 and wr2 > wr0 and n2 >= n0 * 0.4 else ("⚠ " if n2 < n0 * 0.4 else "❌")
    print(f"  {'ADX rising 2 candles':<26}  {pf2:5.2f}  {wr2:5.1f}%  {n2:5d}  ${dd2:6.0f}  {dpf2:+6.2f}  {dwr2:+6.1f}pp  {flag2}")

    # Restore
    config.ADX_RISING_FILTER  = False
    config.ADX_RISING2_FILTER = False

    # Verdict
    print()
    if pf1 > pf0 and wr1 > wr0 and n1 >= n0 * 0.4:
        print(f"  >> 1-candle filter: BETTER  (PF {dpf1:+.2f}, WR {dwr1:+.1f}pp, {n1} trades)")
    else:
        print(f"  >> 1-candle filter: REJECTED  ({n1} trades = {n1/n0*100:.0f}% of baseline)")

    if pf2 > pf0 and wr2 > wr0 and n2 >= n0 * 0.4:
        print(f"  >> 2-candle filter: BETTER  (PF {dpf2:+.2f}, WR {dwr2:+.1f}pp, {n2} trades)")
    else:
        print(f"  >> 2-candle filter: REJECTED  ({n2} trades = {n2/n0*100:.0f}% of baseline)")


def main():
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    coins = args if args else ["BTCUSDT"]

    for idx, symbol in enumerate(coins):
        _test(symbol, days)
        if idx < len(coins) - 1:
            print("\n  Sleeping 8s ...")
            time.sleep(8)
    print()


if __name__ == "__main__":
    main()
