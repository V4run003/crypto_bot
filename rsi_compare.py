"""
rsi_compare.py — Find which APPROVED_COINS benefit from the RSI strategy.

For each candidate coin it runs the backtest twice:
  1. Pure WR  (coin NOT in RSI_APPROVED_COINS)
  2. Hybrid   (coin added to RSI_APPROVED_COINS → RSI first, WR fallback)

A coin is recommended for RSI_APPROVED_COINS when:
  • Hybrid PF  > WR PF   (edge improves)
  AND
  • Hybrid MaxDD ≤ WR MaxDD * 1.10  (drawdown doesn't get noticeably worse)

Usage:
    python rsi_compare.py                  # test all APPROVED_COINS, 180 days
    python rsi_compare.py 90               # 90-day window
    python rsi_compare.py 180 ZECUSDT BTCUSDT   # specific coins, 180 days
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid

# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_stats(trades: list) -> tuple:
    """Return (pf, net_pnl, max_dd, n_trades, rsi_count, wr_count)."""
    if not trades:
        return 0.0, 0.0, 0.0, 0, 0, 0

    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]

    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    net_pnl = sum(t["pnl_usd"] for t in trades)

    equity = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak   = equity[0]
    max_dd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > max_dd:
            max_dd = dd

    rsi_count = sum(1 for t in trades if t.get("strategy") == "RSI")
    wr_count  = len(trades) - rsi_count
    return pf, net_pnl, max_dd, len(trades), rsi_count, wr_count


def _run_quiet(symbol: str, days: int) -> tuple:
    """Run backtest_hybrid silently, return stats tuple."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        trades = backtest_hybrid.run_backtest(symbol, days, quiet=True)
    return _compute_stats(trades)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Parse args: optional days (int) then optional symbol list
    args = sys.argv[1:]
    days = 180
    if args and args[0].isdigit():
        days = int(args[0])
        args = args[1:]
    candidates = args if args else list(config.APPROVED_COINS)

    # Exclude coins already determined to NOT benefit from RSI in previous runs
    # (coins already in RSI_APPROVED_COINS are re-verified, not skipped)
    print(f"\nRSI vs WR comparison  |  {days} days  |  {len(candidates)} coins")
    print(f"Recommendation threshold: hybrid PF > WR PF  AND  MaxDD ≤ WR MaxDD × 1.10\n")

    # Save original RSI_APPROVED_COINS so we can restore it after each test
    original_rsi_coins = list(config.RSI_APPROVED_COINS)

    results = []

    for idx, symbol in enumerate(candidates, 1):
        print(f"  [{idx:2d}/{len(candidates)}] {symbol:<18s}", end="", flush=True)

        # ── Pass 1: pure WR (remove coin from RSI list) ───────────────────────
        config.RSI_APPROVED_COINS = [c for c in original_rsi_coins if c != symbol]
        pf_wr, pnl_wr, dd_wr, n_wr, _, _ = _run_quiet(symbol, days)
        time.sleep(2)  # brief pause between the two fetches

        # ── Pass 2: hybrid RSI+WR (add coin to RSI list) ──────────────────────
        config.RSI_APPROVED_COINS = [c for c in original_rsi_coins if c != symbol] + [symbol]
        pf_h, pnl_h, dd_h, n_h, rsi_n, wr_n = _run_quiet(symbol, days)

        # ── Verdict ───────────────────────────────────────────────────────────
        pf_delta  = pf_h - pf_wr
        dd_ok     = dd_h <= dd_wr * 1.10
        recommend = pf_h > pf_wr and dd_ok
        flag      = "✅ ADD RSI" if recommend else (
                    "⚠  RSI worse DD" if pf_h > pf_wr and not dd_ok else
                    "—  WR better")

        print(
            f"  WR: PF={pf_wr:.2f} DD=${dd_wr:.0f} | "
            f"Hybrid: PF={pf_h:.2f} DD=${dd_h:.0f} (RSI:{rsi_n} WR:{wr_n}) | "
            f"ΔPF={pf_delta:+.2f}  {flag}"
        )

        results.append({
            "symbol":    symbol,
            "pf_wr":     pf_wr,
            "dd_wr":     dd_wr,
            "pf_h":      pf_h,
            "dd_h":      dd_h,
            "rsi_n":     rsi_n,
            "wr_n":      wr_n,
            "pf_delta":  pf_delta,
            "recommend": recommend,
        })

        # Restore between coins
        config.RSI_APPROVED_COINS = list(original_rsi_coins)
        time.sleep(5)  # rate-limit cooldown between coins

    # Restore unconditionally
    config.RSI_APPROVED_COINS = list(original_rsi_coins)

    # ── Summary ───────────────────────────────────────────────────────────────
    adds = [r for r in results if r["recommend"]]
    results.sort(key=lambda x: x["pf_delta"], reverse=True)

    print(f"\n{'═' * 90}")
    print(f"  RSI COMPARE SUMMARY  |  {days} days  |  RR={config.RR}  |  ${config.RISK_PER_TRADE} risk/trade")
    print(f"{'═' * 90}")
    print(f"  {'Symbol':<18s}  {'WR PF':>6}  {'WR DD':>7}  │  {'Hyb PF':>6}  {'Hyb DD':>7}  {'RSI/WR':>7}  {'ΔPF':>6}  Verdict")
    print(f"  {'-'*18}  {'-'*6}  {'-'*7}  {'─'*1}  {'-'*6}  {'-'*7}  {'-'*7}  {'-'*6}  -------")
    for r in results:
        verdict = "✅ ADD RSI" if r["recommend"] else ("⚠  DD↑"   if r["pf_h"] > r["pf_wr"] else "—  WR≥Hyb")
        print(
            f"  {r['symbol']:<18s}  {r['pf_wr']:>6.2f}  ${r['dd_wr']:>6.0f}  │  "
            f"{r['pf_h']:>6.2f}  ${r['dd_h']:>6.0f}  "
            f"{r['rsi_n']:>3}r/{r['wr_n']:<3}w  {r['pf_delta']:>+6.2f}  {verdict}"
        )
    print(f"{'═' * 90}")
    print(f"  Recommend RSI: {len(adds)} / {len(results)}")
    if adds:
        adds.sort(key=lambda x: x["pf_delta"], reverse=True)
        print(f"\n  ► Add/keep in RSI_APPROVED_COINS:")
        for r in adds:
            print(f'      "{r["symbol"]}",   # ΔPF {r["pf_delta"]:+.2f}  Hybrid PF {r["pf_h"]:.2f}  DD ${r["dd_h"]:.0f}')
    print(f"{'═' * 90}\n")


if __name__ == "__main__":
    main()
