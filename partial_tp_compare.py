"""
partial_tp_compare.py — Compare fixed TP vs two partial-TP configurations.

Runs the hybrid backtest 3 ways on each coin:
  1. Fixed TP at 1.5R  (baseline, PARTIAL_TP_ENABLED=False)
  2. Partial: 50% at 1R → BE stop, 50% at 1.5R  (conservative split)
  3. Partial: 50% at 1R → BE stop, 50% at 2.0R  (stretch second target)

Key metrics: PF, WR%, Net PnL, MaxDD, trade count.

Usage:
    python partial_tp_compare.py                    # BTCUSDT 180 days
    python partial_tp_compare.py 180 BTCUSDT ZECUSDT
    python partial_tp_compare.py 90 BTCUSDT
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid


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


MODES = [
    # (label, partial_enabled, tp1, tp2)
    ("Fixed 1.5R (baseline)", False, 1.0, 1.5),
    ("Partial: 1R + 1.5R",    True,  1.0, 1.5),
    ("Partial: 1R + 2.0R",    True,  1.0, 2.0),
]


def _test(symbol, days):
    print(f"\n{'=' * 78}")
    print(f"  {symbol}  |  {days} days  |  Partial TP comparison")
    print(f"{'=' * 78}")
    print(f"  {'Mode':<26}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  {'MaxDD':>8}  {'N':>5}  Note")
    print(f"  {'-' * 73}")

    base_pf = base_wr = base_net = None

    for label, enabled, tp1, tp2 in MODES:
        config.PARTIAL_TP_ENABLED = enabled
        config.PARTIAL_TP1_R      = tp1
        config.PARTIAL_TP2_R      = tp2

        pf, wr, net, dd, n = _run(symbol, days)

        if base_pf is None:
            base_pf, base_wr, base_net = pf, wr, net
            note = "baseline"
            icon = ""
        else:
            dpf  = pf  - base_pf
            dwr  = wr  - base_wr
            dnet = net - base_net
            better = pf > base_pf and net > base_net
            icon   = "✅" if better else ("⚠ " if net > base_net or pf > base_pf else "❌")
            note   = f"dPF={dpf:+.2f}  dWR={dwr:+.1f}pp  dNet={dnet:+.0f}"

        print(f"  {label:<26}  {pf:5.2f}  {wr:5.1f}%  ${net:8.0f}  ${dd:6.0f}  {n:5d}  {icon} {note}")
        time.sleep(4)

    # Restore defaults
    config.PARTIAL_TP_ENABLED = False
    config.PARTIAL_TP1_R      = 1.0
    config.PARTIAL_TP2_R      = 2.0


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
