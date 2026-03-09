import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import config, backtest

config.ADX_RISING_FILTER     = False
config.EMA50_PULLBACK_FILTER = False
config.TIME_FILTER           = True

COINS = [
    "GRTUSDT","PAXGUSDT","TIAUSDT","ONDOUSDT","FILUSDT",
    "BTCUSDT","SUIUSDT","ARBUSDT","TONUSDT",
    "CRVUSDT","BNBUSDT","LDOUSDT","ETHUSDT",
]

DAYS = 90
rows = []

for sym in COINS:
    try:
        trades = backtest.run_backtest(sym, DAYS, quiet=True)
        if not trades:
            rows.append((sym, 0, 0.0, 0.0, 0.0, 0.0, 0.0, "NO TRADES"))
            continue
        wins   = [t for t in trades if t["result"] == "win"]
        losses = [t for t in trades if t["result"] == "loss"]
        n   = len(trades)
        net = sum(t["pnl_usd"] for t in trades)
        pf  = sum(t["pnl_usd"] for t in wins) / abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.0
        wr  = len(wins) / n * 100
        equity = [0.0]
        for t in trades:
            equity.append(equity[-1] + t["pnl_usd"])
        peak = mdd = 0.0
        for e in equity:
            if e > peak: peak = e
            if peak - e > mdd: mdd = peak - e
        td  = (trades[-1]["exit_time"] - trades[0]["entry_time"]).days or 1
        tpm = n / td * 30
        verdict = "APPROVED" if pf >= 1.1 else ("BORDERLINE" if pf >= 1.0 else "REJECTED")
        rows.append((sym, n, round(tpm, 1), round(wr, 1), round(net, 2), round(pf, 2), round(mdd, 2), verdict))
    except Exception as ex:
        rows.append((sym, 0, 0.0, 0.0, 0.0, 0.0, 0.0, f"ERROR: {ex}"))

rows.sort(key=lambda x: x[5] if isinstance(x[5], float) else -1, reverse=True)

SEP = "=" * 84
sep = "-" * 84
print()
print(SEP)
print("  COIN SCREENING REPORT  |  90 days  |  RR=1.5  |  $35/trade  |  UTC 08:00-20:00")
print(SEP)
print(f"  {'Symbol':<12} {'Trades':>6} {'T/mo':>5} {'WR%':>6} {'NetPnL':>9} {'PF':>5} {'MaxDD':>8}  Verdict")
print(sep)
for sym, n, tpm, wr, net, pf, mdd, verdict in rows:
    marker = " <-- KEEP" if verdict == "APPROVED" else (" <-- WATCH" if verdict == "BORDERLINE" else "")
    print(f"  {sym:<12} {n:>6} {tpm:>5} {wr:>5}% {net:>+9.2f} {pf:>5.2f} {mdd:>8.2f}  {verdict}{marker}")
print(SEP)
