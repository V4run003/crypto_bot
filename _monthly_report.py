import config
import backtest
from collections import defaultdict

COINS = [
    "GRTUSDT","PAXGUSDT","TIAUSDT","ONDOUSDT","FILUSDT",
    "BTCUSDT","SUIUSDT","ARBUSDT","TONUSDT",
    "CRVUSDT","BNBUSDT","LDOUSDT","ETHUSDT",
]

all_trades = []
per_coin = {}
for sym in COINS:
    t = backtest.run_backtest(sym, 90, quiet=True)
    per_coin[sym] = t
    all_trades.extend(t)

def apply_daily_cap(trades, cap):
    """Keep only the first `cap` trades per UTC calendar day."""
    if cap is None:
        return trades
    day_counts = defaultdict(int)
    kept = []
    for t in trades:
        day = t["entry_time"].date() if hasattr(t["entry_time"], "date") else str(t["entry_time"])[:10]
        if day_counts[day] < cap:
            day_counts[day] += 1
            kept.append(t)
    return kept


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
    avg_w = gross_w / len(wins)   if wins   else 0
    avg_l = gross_l / len(losses) if losses else 0
    return dict(n=n, net=net, pf=pf, wr=wr, mdd=mdd, ms=ms, avg_w=avg_w, avg_l=avg_l)

W = 80
print()
print("=" * W)
print("  PER-COIN  |  90 days  |  RR=1.5  |  $25/trade  |  UTC 08-20")
print("=" * W)
print(f"  {'Symbol':<12} {'Trades/mo':>9} {'WR':>6} {'PF':>5} {'Net/mo':>9} {'AvgWin':>8} {'MaxDD':>8} {'MaxStrk':>8}")
print("  " + "-" * 78)
for sym in COINS:
    s = stats(per_coin[sym])
    if not s:
        print(f"  {sym:<12}  no trades")
        continue
    t_mo   = s["n"] / 3
    net_mo = s["net"] / 3
    print(f"  {sym:<12} {t_mo:>9.1f} {s['wr']:>5.1f}% {s['pf']:>5.2f} {net_mo:>+9.2f} {s['avg_w']:>+8.2f} {s['mdd']:>8.2f} {s['ms']:>8}")

print()
print("=" * W)
print("  COMBINED  |  all 13 coins  |  90 days")
print("=" * W)
s = stats(all_trades)
mo_trades  = s["n"] / 3
mo_net     = s["net"] / 3
mo_fees_mk = mo_trades * 3.00   # $3.00 market round-trip (0.06% each way)
mo_fees_lm = mo_trades * 1.20   # $1.20 limit round-trip  (blended ~0.024%)
print(f"  Total trades 90d : {s['n']}  ({mo_trades:.0f}/month)")
print(f"  Win rate         : {s['wr']:.1f}%")
print(f"  Profit factor    : {s['pf']:.2f}")
print(f"  Net PnL 90d      : ${s['net']:+.2f}  (avg/month: ${mo_net:+.2f})")
print(f"  Avg win          : ${s['avg_w']:+.2f}   Avg loss: -${s['avg_l']:.2f}")
print(f"  Max drawdown     : ${s['mdd']:.2f}")
print(f"  Max loss streak  : {s['ms']} trades")
print()
print("  -- Monthly fee impact (backtest gross, fees not included) --")
print(f"  Market orders (0.06%): -${mo_fees_mk:.0f}/mo  =>  net ${mo_net - mo_fees_mk:+.2f}")
print(f"  Limit orders  (0.02%): -${mo_fees_lm:.0f}/mo  =>  net ${mo_net - mo_fees_lm:+.2f}  <-- active config")
print()
net_after_fees = mo_net - mo_fees_lm
days_to_pass   = (500 / net_after_fees * 30) if net_after_fees > 0 else 999
print("  -- CFT challenge projection --")
print(f"  Monthly net (after limit-order fees): ${net_after_fees:+.2f}")
print(f"  Profit target to pass               : $500")
print(f"  Est. days to pass                   : ~{days_to_pass:.0f} days")
print(f"  Daily avg PnL (net)                 : ${net_after_fees/30:+.2f}")
print("=" * W)

# ── 6 trades/day cap comparison ──────────────────────────────────────────────
capped = apply_daily_cap(sorted(all_trades, key=lambda t: t["entry_time"]), cap=6)
sc = stats(capped)
mo_t6     = sc["n"] / 3
mo_net6   = sc["net"] / 3
mo_fees6  = mo_t6 * 1.20
net_af6   = mo_net6 - mo_fees6
d2p6      = (500 / net_af6 * 30) if net_af6 > 0 else 999
print()
print("=" * W)
print("  SCENARIO: MAX_TRADES_PER_DAY = 6  vs  current 15")
print("=" * W)
print(f"  {'Metric':<35} {'Cap=15 (current)':>16} {'Cap=6':>10}")
print("  " + "-" * 64)
print(f"  {'Trades/month':<35} {mo_trades:>16.0f} {mo_t6:>10.0f}")
print(f"  {'Win rate':<35} {s['wr']:>15.1f}% {sc['wr']:>9.1f}%")
print(f"  {'Profit factor':<35} {s['pf']:>16.2f} {sc['pf']:>10.2f}")
print(f"  {'Gross PnL/month':<35} ${mo_net:>+15.2f} ${mo_net6:>+9.2f}")
print(f"  {'Fees/month (limit orders)':<35} ${mo_fees_lm:>-15.0f} ${mo_fees6:>-9.0f}")
print(f"  {'Net PnL/month (after fees)':<35} ${net_after_fees:>+15.2f} ${net_af6:>+9.2f}")
print(f"  {'Max drawdown':<35} ${s['mdd']:>15.2f} ${sc['mdd']:>9.2f}")
print(f"  {'Max consec. losses':<35} {s['ms']:>16} {sc['ms']:>10}")
print(f"  {'Est. days to pass CFT ($500)':<35} {'~'+str(int(days_to_pass))+' days':>16} {'~'+str(int(d2p6))+' days':>10}")
print("=" * W)
