import config
import backtest

CANDIDATES = ["LINKUSDT", "DOTUSDT", "ATOMUSDT", "AAVEUSDT", "NEARUSDT", "INJUSDT"]
DAYS = 180


def stats(trades):
    if not trades:
        return None
    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    n       = len(trades)
    net     = sum(t["pnl_usd"] for t in trades)
    gross_w = sum(t["pnl_usd"] for t in wins)
    gross_l = abs(sum(t["pnl_usd"] for t in losses))
    pf      = gross_w / gross_l if gross_l else 0
    wr      = len(wins) / n * 100
    equity  = [0.0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak = mdd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        if peak - e > mdd:
            mdd = peak - e
    streak = ms = 0
    for t in trades:
        if t["result"] == "loss":
            streak += 1
            ms = max(ms, streak)
        else:
            streak = 0
    return dict(n=n, net=net, pf=pf, wr=wr, mdd=mdd, ms=ms,
                mo_trades=n / (DAYS / 30), mo_net=net / (DAYS / 30))


print()
print("=" * 72)
print(f"  SCREEN  |  {len(CANDIDATES)} candidates  |  {DAYS} days  |  RR=1.5  |  $25/trade")
print("=" * 72)
print(f"  {'Symbol':<12} {'T/mo':>6} {'WR':>6} {'PF':>5} {'Net/mo':>9} {'MaxDD':>8} {'MaxStrk':>8}  Verdict")
print("  " + "-" * 70)

for sym in CANDIDATES:
    t = backtest.run_backtest(sym, DAYS, quiet=True)
    s = stats(t)
    if not s:
        print(f"  {sym:<12}  no trades")
        continue
    if s["pf"] >= 1.30 and s["mdd"] <= 300:
        verdict = "APPROVED"
    elif s["pf"] >= 1.15:
        verdict = "BORDERLINE"
    else:
        verdict = "REJECTED"
    print(f"  {sym:<12} {s['mo_trades']:>6.1f} {s['wr']:>5.1f}% {s['pf']:>5.2f} "
          f"{s['mo_net']:>+9.2f} {s['mdd']:>8.2f} {s['ms']:>8}  {verdict}")

print("=" * 72)
