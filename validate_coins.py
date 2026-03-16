"""
validate_coins.py — Re-validate all APPROVED_COINS over a longer window.

Runs the full hybrid backtest on every coin in config.APPROVED_COINS with
conservative rate-limiting scaled to the fetch size:

    180d → ~57 chunks/coin  →  0.12 s/chunk + 5 s between coins
    365d → ~105 chunks/coin →  0.15 s/chunk + 10 s between coins

Usage:
    python validate_coins.py          # 365-day validation (default)
    python validate_coins.py 180      # re-run 180-day baseline
    python validate_coins.py 365 --quiet
"""

import contextlib
import io
import sys
import time

import config
import backtest_hybrid

# ── Pass criteria (same as batch_backtest.py) ─────────────────────────────────
PF_PASS = 1.25
DD_PASS = 300.0


def _compute_stats(trades):
    if not trades:
        return 0.0, 0.0, 0.0, 0, 0, 0
    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    net     = sum(t["pnl_usd"] for t in trades)
    equity  = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak = max_dd = 0.0
    for e in equity:
        if e > peak: peak = e
        max_dd = max(max_dd, peak - e)
    rsi_n = sum(1 for t in trades if t.get("strategy") == "RSI")
    wr_n  = len(trades) - rsi_n
    return pf, net, max_dd, len(trades), rsi_n, wr_n


def main():
    args  = sys.argv[1:]
    days  = 365
    quiet = False
    if args and args[0].isdigit():
        days = int(args[0]); args = args[1:]
    if "--quiet" in args or "-q" in args:
        quiet = True

    # Scale rate limiting to fetch size:
    # 365d needs ~105 chunks/coin → increase inter-chunk sleep
    # (patched on the backtest_hybrid module in-memory, not in the file)
    if days >= 300:
        backtest_hybrid.INTER_CHUNK_SLEEP = 0.15   # ~6.7 req/s — extra headroom
        inter_coin_delay = 10
    else:
        backtest_hybrid.INTER_CHUNK_SLEEP = 0.12   # ~8 req/s
        inter_coin_delay = 5

    # Monkey-patch fetch_all_candles to respect INTER_CHUNK_SLEEP
    import time as _time
    _orig_fetch = backtest_hybrid.fetch_all_candles

    def _patched_fetch(session, symbol, interval, _days):
        import backtest_hybrid as _bh
        sleep_t = getattr(_bh, "INTER_CHUNK_SLEEP", 0.12)
        now_ms   = int(_time.time() * 1000)
        start_ms = now_ms - _days * 86_400_000
        end_ms   = now_ms
        rows     = []
        label    = f"{symbol} {interval}m"
        print(f"  Fetching {label} ({_days} days) ...", end="", flush=True)
        while True:
            chunk = _bh._fetch_chunk(session, symbol, interval, end_ms)
            if not chunk:
                break
            rows.extend(chunk)
            oldest = int(chunk[-1][0])
            if oldest <= start_ms:
                break
            end_ms = oldest - 1
            _time.sleep(sleep_t)
        print(f" {len(rows)} candles")
        import pandas as pd
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume","turnover"])
        df["timestamp"] = df["timestamp"].astype(int)
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df = df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
        return df[df["timestamp"] >= start_ms].reset_index(drop=True)

    backtest_hybrid.fetch_all_candles = _patched_fetch

    coins  = list(config.APPROVED_COINS)
    total  = len(coins)
    results = []

    print(f"\nValidating {total} APPROVED_COINS over {days} days")
    print(f"Rate limiting: {getattr(backtest_hybrid, 'INTER_CHUNK_SLEEP', 0.12):.2f}s/chunk"
          f"  {inter_coin_delay}s between coins")
    print(f"Pass criteria: PF >= {PF_PASS}  AND  MaxDD <= ${DD_PASS:.0f}\n")

    for idx, sym in enumerate(coins, 1):
        print(f"[{idx:2d}/{total}] {sym}", flush=True)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                trades = backtest_hybrid.run_backtest(sym, days, quiet=True)
            pf, net, dd, n, rsi_n, wr_n = _compute_stats(trades)
            strat = f"RSI:{rsi_n}/WR:{wr_n}"
            flag  = "✅" if pf >= PF_PASS and dd <= DD_PASS else "❌"
            print(f"       PF={pf:.2f}  Net=${net:>+7.0f}  DD=${dd:.0f}"
                  f"  Trades={n} ({strat})  {flag}")
            results.append(dict(symbol=sym, pf=pf, net=net, dd=dd, n=n,
                                rsi_n=rsi_n, wr_n=wr_n))
        except Exception as exc:
            print(f"       ERROR: {exc}")
            results.append(dict(symbol=sym, pf=0, net=0, dd=0, n=0,
                                rsi_n=0, wr_n=0))

        if idx < total:
            print(f"       (waiting {inter_coin_delay}s ...)\n")
            time.sleep(inter_coin_delay)

    # ── Summary ───────────────────────────────────────────────────────────────
    results.sort(key=lambda x: x["pf"], reverse=True)
    passed = [r for r in results if r["pf"] >= PF_PASS and r["dd"] <= DD_PASS]

    print(f"\n{'═'*78}")
    print(f"  VALIDATE RESULTS  |  {days} days  |  RR={config.RR}"
          f"  |  ${config.RISK_PER_TRADE} risk/trade")
    print(f"{'═'*78}")
    print(f"  {'Symbol':<18}  {'PF':>5}  {'Net PnL':>9}  {'MaxDD':>7}"
          f"  {'Trades':>7}  {'RSI/WR':>10}  Status")
    print(f"  {'-'*18}  {'-'*5}  {'-'*9}  {'-'*7}"
          f"  {'-'*7}  {'-'*10}  ------")
    for r in results:
        status = "✅ PASS" if r["pf"] >= PF_PASS and r["dd"] <= DD_PASS else "❌"
        strat  = f"{r['rsi_n']}r/{r['wr_n']}w"
        print(f"  {r['symbol']:<18}  {r['pf']:>5.2f}  ${r['net']:>+8.0f}"
              f"  ${r['dd']:>6.0f}  {r['n']:>7d}  {strat:>10}  {status}")
    print(f"{'═'*78}")
    print(f"  Passed: {len(passed)} / {len(results)}  "
          f"({'  '.join(r['symbol'] for r in passed) or 'none'})")

    if len(passed) < len(results):
        failed = [r["symbol"] for r in results if r not in passed]
        print(f"\n  ⚠  Consider removing from APPROVED_COINS if consistently failing:")
        for sym in [r["symbol"] for r in results if r["pf"] < PF_PASS or r["dd"] > DD_PASS]:
            r = next(x for x in results if x["symbol"] == sym)
            print(f'      # {sym:<18} PF {r["pf"]:.2f}  DD ${r["dd"]:.0f}')
    print(f"{'═'*78}\n")


if __name__ == "__main__":
    main()
