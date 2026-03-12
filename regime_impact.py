"""regime_impact.py — Show per-coin PnL impact of regime filter on vs off."""
from collections import defaultdict
import config
import backtest_portfolio as bp

def run_both():
    config.REGIME_FILTER = False
    t_off, _, _ = bp.run_portfolio(180, 0)

    config.REGIME_FILTER = True
    t_on, _, _ = bp.run_portfolio(180, 0)

    pc_off = defaultdict(list)
    pc_on  = defaultdict(list)
    for t in t_off: pc_off[t["symbol"]].append(t)
    for t in t_on:  pc_on[t["symbol"]].append(t)

    print()
    print(f"  {'Symbol':<18}  {'N_off':>5}  {'PF_off':>5}  {'Net_off':>8}  "
          f"{'N_on':>5}  {'PF_on':>5}  {'Net_on':>8}  {'Delta':>8}")
    print(f"  {'-'*78}")

    total_off = sum(t["pnl"] for t in t_off)
    total_on  = sum(t["pnl"] for t in t_on)

    for sym in config.APPROVED_COINS:
        s_off = bp._stats(pc_off.get(sym, []))
        s_on  = bp._stats(pc_on.get(sym, []))
        if not s_off or not s_on:
            continue
        delta = s_on["net"] - s_off["net"]
        flag = "<-- HURT" if delta < -50 else ("<-- HELPED" if delta > 50 else "")
        print(f"  {sym:<18}  {s_off['n']:>5}  {s_off['pf']:>5.2f}  ${s_off['net']:>7.0f}  "
              f"{s_on['n']:>5}  {s_on['pf']:>5.2f}  ${s_on['net']:>7.0f}  ${delta:>+7.0f}  {flag}")

    print(f"  {'-'*78}")
    print(f"  {'TOTAL':<18}  {len(t_off):>5}  {'':>5}  ${total_off:>7.0f}  "
          f"{len(t_on):>5}  {'':>5}  ${total_on:>7.0f}  ${total_on-total_off:>+7.0f}")

if __name__ == "__main__":
    run_both()
