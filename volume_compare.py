"""
volume_compare.py — Test whether VOLUME_CONFIRM_FILTER improves win rate and PF.

For each candidate coin it runs the hybrid backtest twice:
  1. Filter OFF  (VOLUME_CONFIRM_FILTER = False)
  2. Filter ON   (VOLUME_CONFIRM_FILTER = True, mult = VOLUME_CONFIRM_MULT)

Reports PF, win rate %, trade count, and MaxDD for both runs.
The goal is to see if filtering weak-volume hooks pushes WR% toward 50%+.

Usage:
    python volume_compare.py                          # BTCUSDT 180 days
    python volume_compare.py 180                      # test all APPROVED_COINS, 180d
    python volume_compare.py 180 BTCUSDT ZECUSDT      # specific coins, 180d
    python volume_compare.py 90 BTCUSDT               # 90-day window
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_stats(trades: list) -> tuple:
    """Return (pf, wr_pct, max_dd, net_pnl, n_trades)."""
    if not trades:
        return 0.0, 0.0, 0.0, 0.0, 0

    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]

    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    wr_pct  = len(wins) / len(trades) * 100
    net_pnl = sum(t["pnl_usd"] for t in trades)

    equity = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak = max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    return pf, wr_pct, max_dd, net_pnl, len(trades)


def _run_quiet(symbol: str, days: int) -> tuple:
    """Run hybrid backtest silently, return stats tuple."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        trades = backtest_hybrid.run_backtest(symbol, days, quiet=True)
    return _compute_stats(trades)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    candidates = args if args else ["BTCUSDT"]

    mult = config.VOLUME_CONFIRM_MULT

    print(f"\nVolume filter comparison  |  {days} days  |  {len(candidates)} coin(s)")
    print(f"VOLUME_CONFIRM_MULT = {mult}x  (hook candle volume must exceed {mult}x 20-bar avg)")
    print(f"{'=' * 96}")
    print(
        f"  {'Symbol':<18}  "
        f"{'--- FILTER OFF ---':^36}  "
        f"{'--- FILTER ON ---':^36}  "
        f"Verdict"
    )
    print(
        f"  {'':18}  "
        f"{'PF':>5} {'WR%':>6} {'N':>5} {'MaxDD$':>7}  "
        f"{'PF':>5} {'WR%':>6} {'N':>5} {'MaxDD$':>7}"
    )
    print(f"{'─' * 96}")

    summary = []

    for idx, symbol in enumerate(candidates, 1):
        sys.stdout.write(f"  [{idx:2d}/{len(candidates)}] {symbol:<18s}  fetching ...\r")
        sys.stdout.flush()

        # Pass 1: filter OFF
        config.VOLUME_CONFIRM_FILTER = False
        pf_off, wr_off, dd_off, pnl_off, n_off = _run_quiet(symbol, days)
        time.sleep(3)

        # Pass 2: filter ON
        config.VOLUME_CONFIRM_FILTER = True
        pf_on, wr_on, dd_on, pnl_on, n_on = _run_quiet(symbol, days)

        config.VOLUME_CONFIRM_FILTER = False  # always restore

        # Verdict
        pf_better = pf_on > pf_off
        wr_better = wr_on > wr_off
        dd_ok     = dd_on <= dd_off * 1.15     # max 15% DD increase tolerated
        n_ok      = n_on >= n_off * 0.40       # must keep at least 40% of trades

        if pf_better and wr_better and dd_ok and n_ok:
            verdict = "BETTER"
            icon    = "✅"
        elif not n_ok:
            verdict = "TOO FEW TRADES"
            icon    = "⚠ "
        elif not dd_ok:
            verdict = "DD WORSE"
            icon    = "❌"
        else:
            verdict = "WR/PF NO GAIN"
            icon    = "—"

        pf_delta = pf_on - pf_off
        wr_delta = wr_on - wr_off

        print(
            f"  {symbol:<18}  "
            f"{pf_off:5.2f} {wr_off:5.1f}% {n_off:5d} ${dd_off:6.0f}  "
            f"{pf_on:5.2f} {wr_on:5.1f}% {n_on:5d} ${dd_on:6.0f}  "
            f"{icon} {verdict}  (PF {pf_delta:+.2f}  WR {wr_delta:+.1f}pp)"
        )

        summary.append({
            "symbol":  symbol,
            "pf_off":  pf_off, "wr_off": wr_off, "n_off": n_off, "dd_off": dd_off,
            "pf_on":   pf_on,  "wr_on":  wr_on,  "n_on":  n_on,  "dd_on":  dd_on,
            "verdict": verdict,
        })

        if idx < len(candidates):
            print(f"  Sleeping 5s before next coin ...")
            time.sleep(5)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"{'─' * 96}")
    better  = [r for r in summary if r["verdict"] == "BETTER"]
    worse   = [r for r in summary if r["verdict"] in ("DD WORSE", "WR/PF NO GAIN")]
    caution = [r for r in summary if r["verdict"] == "TOO FEW TRADES"]

    print(f"\n  Coins improved : {len(better)}")
    print(f"  Coins degraded : {len(worse)}")
    print(f"  Too few trades : {len(caution)}")

    if better:
        avg_wr_gain = sum(r["wr_on"] - r["wr_off"] for r in better) / len(better)
        avg_pf_gain = sum(r["pf_on"] - r["pf_off"] for r in better) / len(better)
        print(f"\n  On improved coins:")
        print(f"    Avg WR gain : +{avg_wr_gain:.1f} percentage points")
        print(f"    Avg PF gain : +{avg_pf_gain:.2f}")

    if len(summary) > 1:
        avg_wr_all = sum(r["wr_on"] - r["wr_off"] for r in summary) / len(summary)
        print(f"\n  Across all coins — avg WR change: {avg_wr_all:+.1f}pp")

    print()


if __name__ == "__main__":
    main()
