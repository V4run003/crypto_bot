"""
rsi_zone_sweep.py — Sweep RSI entry zone boundaries to optimise signal quality.

Tests 7 presets on each RSI-approved coin. Zones are defined as (long_low, long_high)
and the short zone mirrors them symmetrically (100-high, 100-low).

Current defaults:  RSI_LONG_ZONE_LOW=30  RSI_LONG_ZONE_HIGH=45
                   RSI_SHORT_ZONE_LOW=55  RSI_SHORT_ZONE_HIGH=70

Only meaningful on RSI_APPROVED_COINS (ZEC, SUI, BTC) — WR-only coins are unaffected.

Usage:
    python rsi_zone_sweep.py                          # ZEC, SUI, BTC — 180 days
    python rsi_zone_sweep.py 180 BTCUSDT ZECUSDT      # specific coins
    python rsi_zone_sweep.py 90 BTCUSDT
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid

# (label, long_low, long_high)  →  short = (100-high, 100-low)
PRESETS = [
    ("(28-45 / 55-72)  wider",    28, 45),
    ("(30-45 / 55-70)  current",  30, 45),
    ("(30-42 / 58-70)  tgt ceil", 30, 42),
    ("(32-45 / 55-68)  tgt floor",32, 45),
    ("(32-42 / 58-68)  tgt both", 32, 42),
    ("(35-45 / 55-65)  deep floor",35, 45),
    ("(35-42 / 58-65)  deep both", 35, 42),
]


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
    # Ensure coin is treated as RSI-approved for all runs
    original_rsi_coins = list(config.RSI_APPROVED_COINS)
    if symbol not in config.RSI_APPROVED_COINS:
        config.RSI_APPROVED_COINS = original_rsi_coins + [symbol]

    print(f"\n{'=' * 76}")
    print(f"  {symbol}  |  {days} days  |  RSI zone sweep")
    print(f"{'=' * 76}")
    print(f"  {'Preset':<28}  {'PF':>5}  {'WR%':>6}  {'NetPnL':>9}  {'MaxDD':>8}  {'N':>5}  {'dPF':>6}  {'dNet':>8}")
    print(f"  {'-' * 71}")

    base_pf = base_net = None
    best_net = -float("inf")
    best_label = None

    for label, ll, lh in PRESETS:
        config.RSI_LONG_ZONE_LOW   = ll
        config.RSI_LONG_ZONE_HIGH  = lh
        config.RSI_SHORT_ZONE_LOW  = 100 - lh
        config.RSI_SHORT_ZONE_HIGH = 100 - ll

        pf, wr, net, dd, n = _run(symbol, days)

        if base_pf is None:
            base_pf, base_net = pf, net
            dpf_s = dnet_s = ""
        else:
            dpf_s  = f"{pf  - base_pf:+6.2f}"
            dnet_s = f"{net - base_net:+8.0f}"

        flag = " ◄" if "current" in label else ""
        if net > best_net:
            best_net   = net
            best_label = label

        print(f"  {label:<28}  {pf:5.2f}  {wr:5.1f}%  ${net:8.0f}  ${dd:6.0f}  {n:5d}  {dpf_s:>6}  {dnet_s:>8}{flag}")
        time.sleep(4)

    # Restore
    config.RSI_LONG_ZONE_LOW   = 30
    config.RSI_LONG_ZONE_HIGH  = 45
    config.RSI_SHORT_ZONE_LOW  = 55
    config.RSI_SHORT_ZONE_HIGH = 70
    config.RSI_APPROVED_COINS  = original_rsi_coins

    print(f"\n  >> Best net PnL: {best_label}  (${best_net:.0f})")


def main():
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    # Default: test all 3 RSI-approved coins
    coins = args if args else list(config.RSI_APPROVED_COINS)

    for idx, symbol in enumerate(coins):
        _sweep(symbol, days)
        if idx < len(coins) - 1:
            print("\n  Sleeping 8s ...")
            time.sleep(8)
    print()


if __name__ == "__main__":
    main()
