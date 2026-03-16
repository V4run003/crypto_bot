"""
adx_sweep.py — Sweep ADX_THRESHOLD values to find the optimal trend-strength floor.

Lower threshold → more trades but some in choppy markets.
Higher threshold → fewer trades but cleaner trends.
Current default: ADX_THRESHOLD = 18

Usage:
    python adx_sweep.py                    # BTCUSDT, 180 days
    python adx_sweep.py 180 BTCUSDT ZECUSDT
    python adx_sweep.py 90 BTCUSDT
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid

ADX_VALUES = [14, 16, 18, 20, 22, 24, 26]


def _compute_stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0
    wins    = [t for t in trades if t["result"] == "win"]
    losses  = [t for t in trades if t["result"] == "loss"]
    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    wr      = len(wins) / len(trades) * 100
    net     = sum(t["pnl_usd"] for t in trades)
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
    return pf, wr, net, mx, len(trades)


def _run(symbol, days):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        trades = backtest_hybrid.run_backtest(symbol, days, quiet=True)
    return _compute_stats(trades)


def _sweep(symbol, days):
    print(f"\n{'=' * 72}")
    print(f"  {symbol}  |  {days} days  |  ADX_THRESHOLD sweep")
    print(f"{'=' * 72}")
    print(f"  {'ADX':>5}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  {'MaxDD':>8}  {'N':>5}  {'dPF':>6}  {'dNet':>8}")
    print(f"  {'-' * 67}")

    base_pf = base_net = None
    best_net = -float("inf")
    best_adx = None

    for adx in ADX_VALUES:
        config.ADX_THRESHOLD = adx
        pf, wr, net, dd, n = _run(symbol, days)

        if base_pf is None:
            base_pf, base_net = pf, net
            dpf_s = dnet_s = ""
        else:
            dpf_s  = f"{pf  - base_pf:+6.2f}"
            dnet_s = f"{net - base_net:+8.0f}"

        flag = " ◄ current" if adx == 18 else ""
        if net > best_net:
            best_net = net
            best_adx = adx

        print(f"  {adx:>5}  {pf:5.2f}  {wr:5.1f}%  ${net:8.0f}  ${dd:6.0f}  {n:5d}  {dpf_s:>6}  {dnet_s:>8}{flag}")
        time.sleep(4)

    config.ADX_THRESHOLD = 18  # restore
    print(f"\n  >> Best net PnL at ADX_THRESHOLD={best_adx}  (${best_net:.0f})")


def main():
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    coins = args if args else ["BTCUSDT"]

    for idx, symbol in enumerate(coins):
        _sweep(symbol, days)
        if idx < len(coins) - 1:
            print("\n  Sleeping 8s ...")
            time.sleep(8)
    print()


if __name__ == "__main__":
    main()
