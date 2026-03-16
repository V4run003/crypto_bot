"""
batch_backtest.py — Discover and batch-backtest candidate coins.

Steps:
  1. Queries Bybit for the top N USDT perpetuals sorted by 24h volume.
  2. Removes every coin that has already been approved or explicitly rejected.
  3. Runs backtest_hybrid.run_backtest(symbol, <days>, quiet=True) on each.
  4. Prints live progress line per coin, then a ranked summary table.

Usage:
    python batch_backtest.py           # top 50 symbols, 180-day backtest
    python batch_backtest.py 30 90     # top 30 symbols, 90-day backtest
    python batch_backtest.py 60 180    # top 60 symbols, 180-day backtest
"""

import contextlib
import io
import sys
import time

import backtest_hybrid
import exchange
import config

# ── All coins already tested (approved + previously rejected) ─────────────────
ALREADY_TESTED = {
    # Currently approved (batch run 1 + 2)
    "ZECUSDT", "WIFUSDT", "SUIUSDT", "BTCUSDT", "PAXGUSDT",
    "TIAUSDT", "ETHUSDT", "1000PEPEUSDT",
    "POWERUSDT", "BCHUSDT", "RIVERUSDT", "PIPPINUSDT", "XAUTUSDT", "SAHARAUSDT",
    # Rejected — batch run 1 (90d/180d)
    "ARBUSDT", "GRTUSDT", "INJUSDT", "BNBUSDT", "DOTUSDT",
    "CRVUSDT", "SOLUSDT", "XRPUSDT", "TONUSDT", "DOGEUSDT",
    "NEARUSDT", "APTUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
    # Rejected — batch run 2 (180d)
    "HYPEUSDT", "FLOWUSDT", "FARTCOINUSDT", "JELLYJELLYUSDT", "KITEUSDT",
    "XAGUSDT", "TAOUSDT", "RESOLVUSDT", "SIGNUSDT", "AAVEUSDT",
    "ENAUSDT", "SAHARAUSDT", "VIRTUALUSDT", "ARIAUSDT", "PUMPFUNUSDT",
    "BARDUSDT", "XAUUSDT", "LTCUSDT", "PENGUUSDT", "ARCUSDT",
    "SHIB1000USDT", "TRUMPUSDT", "CYSUSDT", "HBARUSDT", "GALAUSDT",
    "XPLUSDT", "SEIUSDT", "WLDUSDT", "ZROUSDT", "XLMUSDT",
    "UNIUSDT", "HUMAUSDT", "ROBOUSDT", "VVVUSDT", "ASTERUSDT",
    "NAORISUSDT", "ONDOUSDT",
}

# Validation thresholds (must match config criteria)
PF_PASS = 1.25
DD_PASS = 300.0

# Pause between coins so the burst of candle-fetch requests doesn't trip the
# Bybit rate limiter.  180-day run fetches ~57 kline chunks per coin; at
# 0.12 s/chunk that's ≈7 s of API time — adding 5 s cooldown keeps the
# sustained rate comfortably below the 10 req/s public-endpoint limit.
INTER_COIN_DELAY_SECS = 5


# ── Candidate discovery ───────────────────────────────────────────────────────

def get_candidates(top_n: int) -> list[dict]:
    """Return untested USDT perps within the top_n ranked by 24h USD turnover."""
    resp    = exchange.session.get_tickers(category="linear")
    tickers = resp["result"]["list"]
    usdt    = [t for t in tickers if t["symbol"].endswith("USDT")]
    usdt.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)

    candidates = []
    for t in usdt[:top_n]:
        sym = t["symbol"]
        vol = float(t.get("turnover24h", 0))
        if sym not in ALREADY_TESTED and vol >= config.VOLUME_FILTER_USD:
            candidates.append({"symbol": sym, "volume": vol})
    return candidates


# ── Stats helper (re-computes from trade list, avoids re-printing) ────────────

def _compute_stats(trades: list) -> tuple:
    """Return (pf, net_pnl, max_dd, n_trades)."""
    if not trades:
        return 0.0, 0.0, 0.0, 0

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

    return pf, net_pnl, max_dd, len(trades)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    days  = int(sys.argv[2]) if len(sys.argv) > 2 else 180

    print(f"\nFetching top {top_n} symbols by 24h volume ...")
    try:
        candidates = get_candidates(top_n)
    except Exception as exc:
        print(f"ERROR: could not fetch tickers — {exc}")
        sys.exit(1)

    if not candidates:
        print("No untested candidates found — increase top_n or clear ALREADY_TESTED.")
        return

    print(f"Found {len(candidates)} untested candidates (volume ≥ ${config.VOLUME_FILTER_USD:,}/24h):\n")
    for i, c in enumerate(candidates, 1):
        print(f"  {i:2d}. {c['symbol']:<18s}  24h vol ${c['volume'] / 1_000_000:>7,.0f}M")

    print(f"\nRunning {days}-day hybrid backtest on each ...\n")

    results = []
    total   = len(candidates)

    for idx, c in enumerate(candidates, 1):
        sym = c["symbol"]
        print(f"  [{idx:2d}/{total}] {sym:<18s}", end="", flush=True)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                trades = backtest_hybrid.run_backtest(sym, days, quiet=True)

            pf, net_pnl, max_dd, n = _compute_stats(trades)
            flag = "✅" if pf >= PF_PASS and max_dd <= DD_PASS else ""
            print(f"  PF={pf:.2f}  NetPnL=${net_pnl:>+7.0f}  MaxDD=${max_dd:.0f}  Trades={n}  {flag}")

            results.append({
                "symbol":  sym,
                "pf":      pf,
                "net_pnl": net_pnl,
                "max_dd":  max_dd,
                "trades":  n,
                "volume":  c["volume"],
            })
        except Exception as exc:
            print(f"  SKIP — {exc}")
            time.sleep(3)

        if idx < total:
            time.sleep(INTER_COIN_DELAY_SECS)

    # ── Summary table ─────────────────────────────────────────────────────────
    results.sort(key=lambda x: x["pf"], reverse=True)

    print(f"\n{'═' * 78}")
    print(f"  BATCH BACKTEST SUMMARY  |  {days} days  |  RR={config.RR}  |  ${config.RISK_PER_TRADE} risk/trade")
    print(f"  Pass criteria:  PF ≥ {PF_PASS}  AND  MaxDD ≤ ${DD_PASS:.0f}")
    print(f"{'═' * 78}")
    print(f"  {'Symbol':<18s}  {'PF':>5}  {'Net PnL':>9}  {'MaxDD':>8}  {'Trades':>7}  {'24h Vol':>10}  Status")
    print(f"  {'-'*18}  {'-'*5}  {'-'*9}  {'-'*8}  {'-'*7}  {'-'*10}  ------")

    passed: list[str] = []
    for r in results:
        vol_m  = r["volume"] / 1_000_000
        status = "✅ PASS" if r["pf"] >= PF_PASS and r["max_dd"] <= DD_PASS else "❌"
        if r["pf"] >= PF_PASS and r["max_dd"] <= DD_PASS:
            passed.append(r["symbol"])
        print(
            f"  {r['symbol']:<18s}  {r['pf']:>5.2f}  "
            f"${r['net_pnl']:>+8.0f}  ${r['max_dd']:>7.0f}  "
            f"{r['trades']:>7d}  ${vol_m:>9,.0f}M  {status}"
        )

    print(f"{'═' * 78}")
    print(f"  Passed: {len(passed)} / {len(results)}")
    if passed:
        print(f"\n  ► Add these to APPROVED_COINS (run backtest_hybrid.py individually to confirm):")
        for sym in passed:
            print(f'      "{sym}",')
    print(f"{'═' * 78}\n")


if __name__ == "__main__":
    main()
