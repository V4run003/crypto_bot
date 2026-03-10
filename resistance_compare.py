"""
resistance_compare.py — Test the RESISTANCE_FILTER across multiple ATR multipliers.

For each multiplier it runs the hybrid backtest with filter OFF vs ON and measures
PF, win rate, trade count, and MaxDD to find the sweet spot (or confirm no gain).

Usage:
    python resistance_compare.py                          # BTCUSDT, 180 days
    python resistance_compare.py 180 BTCUSDT ZECUSDT      # specific coins
    python resistance_compare.py 90 BTCUSDT               # 90-day window
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid

MULTIPLIERS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0]


def _compute_stats(trades: list) -> tuple:
    """Return (pf, wr_pct, max_dd, net_pnl, n_trades)."""
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0
    wins    = [t for t in trades if t["result"] == "win"]
    losses  = [t for t in trades if t["result"] == "loss"]
    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    wr_pct  = len(wins) / len(trades) * 100
    net_pnl = sum(t["pnl_usd"] for t in trades)
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
    return pf, wr_pct, mx, net_pnl, len(trades)


def _run_quiet(symbol: str, days: int) -> tuple:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        trades = backtest_hybrid.run_backtest(symbol, days, quiet=True)
    return _compute_stats(trades)


def _sweep(symbol: str, days: int) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {symbol}  |  {days} days  |  RESISTANCE_FILTER sweep")
    print(f"{'=' * 70}")
    print(f"  {'Mult':<8}  {'PF':>5}  {'WR%':>6}  {'N':>5}  {'MaxDD':>8}  {'dPF':>6}  {'dWR':>7}  Note")
    print(f"  {'-' * 65}")

    # Baseline: filter OFF
    config.RESISTANCE_FILTER = False
    pf0, wr0, dd0, pnl0, n0 = _run_quiet(symbol, days)
    print(f"  {'OFF':<8}  {pf0:5.2f}  {wr0:5.1f}%  {n0:5d}  ${dd0:6.0f}   {'':>6}  {'':>7}  baseline")
    time.sleep(4)

    best_wr = wr0
    best_mult = None

    for mult in MULTIPLIERS:
        config.RESISTANCE_FILTER    = True
        config.RESISTANCE_ATR_MULT  = mult
        pf, wr, dd, pnl, n = _run_quiet(symbol, days)

        dpf = pf - pf0
        dwr = wr - wr0
        dd_ok   = dd <= dd0 * 1.15
        n_ok    = n >= n0 * 0.40
        pf_up   = pf > pf0
        wr_up   = wr > wr0

        if not n_ok:
            flag = "too few trades"
            icon = "⚠ "
        elif pf_up and wr_up and dd_ok:
            flag = "BETTER"
            icon = "✅"
            if wr > best_wr:
                best_wr   = wr
                best_mult = mult
        elif not dd_ok:
            flag = "DD worse"
            icon = "❌"
        else:
            flag = "no gain"
            icon = "—"

        print(
            f"  {mult:<8.2f}  {pf:5.2f}  {wr:5.1f}%  {n:5d}  ${dd:6.0f}  "
            f"{dpf:+6.2f}  {dwr:+6.1f}pp  {icon} {flag}"
        )
        time.sleep(4)

    config.RESISTANCE_FILTER   = False
    config.RESISTANCE_ATR_MULT = 0.5  # restore default

    if best_mult is not None:
        print(f"\n  >> Best multiplier for {symbol}: {best_mult}x  (WR {wr0:.1f}% → {best_wr:.1f}%)")
    else:
        print(f"\n  >> No multiplier improved both PF and WR for {symbol}")


def main() -> None:
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    coins = args if args else ["BTCUSDT"]

    for idx, symbol in enumerate(coins):
        _sweep(symbol, days)
        if idx < len(coins) - 1:
            print("\n  Sleeping 8s before next coin ...")
            time.sleep(8)

    print()


if __name__ == "__main__":
    main()
