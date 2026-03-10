"""
rr_sweep.py — Find the optimal RR for the hybrid strategy.

Sweeps RR from 1.0 to 3.0 and measures PF, WR%, net PnL and MaxDD at each value.
The current default is RR=1.5.

Usage:
    python rr_sweep.py                    # BTCUSDT, 180 days
    python rr_sweep.py 180 BTCUSDT ZECUSDT
    python rr_sweep.py 90 BTCUSDT
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid

RR_VALUES = [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]


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
    print(f"  {symbol}  |  {days} days  |  RR sweep  (risk=${config.RISK_PER_TRADE}/trade)")
    print(f"{'=' * 72}")
    print(f"  {'RR':<6}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  {'MaxDD':>8}  {'N':>5}  {'dPF':>6}  {'dNet':>8}")
    print(f"  {'-' * 67}")

    base_pf = base_net = None
    best_net = -float("inf")
    best_rr  = None

    for rr in RR_VALUES:
        config.RR = rr
        pf, wr, net, dd, n = _run(symbol, days)

        if base_pf is None:
            base_pf, base_net = pf, net
            dpf_str  = ""
            dnet_str = ""
            marker   = " ← base"
        else:
            dpf  = pf  - base_pf
            dnet = net - base_net
            dpf_str  = f"{dpf:+6.2f}"
            dnet_str = f"{dnet:+8.0f}"
            marker   = ""

        curr_rr = config.RR  # capture before any change
        if net > best_net:
            best_net = net
            best_rr  = rr

        # highlight current default
        flag = " ◄ current" if abs(rr - 1.5) < 0.01 else marker

        print(
            f"  {rr:<6.2f}  {pf:5.2f}  {wr:5.1f}%  ${net:8.0f}  ${dd:6.0f}  {n:5d}  {dpf_str:>6}  {dnet_str:>8}{flag}"
        )
        time.sleep(4)

    config.RR = 1.5  # restore default
    print(f"\n  >> Best net PnL at RR={best_rr}  (${best_net:.0f})")


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
