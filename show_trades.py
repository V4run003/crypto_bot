"""
Display recent trade history from trade_log.csv.

Usage:
    python show_trades.py          # last 20 trades
    python show_trades.py 50       # last 50 trades
    python show_trades.py all      # entire history
"""
import csv
import os
import sys

LOG = "trade_log.csv"


def _load(n):
    if not os.path.isfile(LOG):
        print("No trade log found. Trades are recorded once the bot closes a position.")
        return []
    with open(LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows if n == "all" else rows[-n:]


def _print_table(rows):
    if not rows:
        print("No trades to display.")
        return

    wins   = sum(1 for r in rows if float(r["pnl"]) >= 0)
    losses = len(rows) - wins
    total  = sum(float(r["pnl"]) for r in rows)
    wr     = wins / len(rows) * 100 if rows else 0

    header = f"{'#':>4}  {'Date/Time':^19}  {'Symbol':^10}  {'Side':^5}  {'Strat':^5}  {'Entry':^5}  {'Entry $':>10}  {'Exit $':>10}  {'PnL':>8}  {'Dur':>6}  Reason"
    sep    = "─" * len(header)
    print(sep)
    print(header)
    print(sep)

    for i, r in enumerate(rows, 1):
        pnl     = float(r["pnl"])
        flag    = "✓" if pnl >= 0 else "✗"
        dur_m   = float(r["duration_mins"])
        dur_str = f"{dur_m:.0f}m" if dur_m < 120 else f"{dur_m/60:.1f}h"
        print(
            f"{i:>4}  {r['datetime']:^19}  {r['symbol']:^10}  "
            f"{r['side']:^5}  {r['strategy']:^5}  {r['entry_type'][:5]:^5}  "
            f"{float(r['entry']):>10,.4f}  {float(r['exit']):>10,.4f}  "
            f"{pnl:>+8,.2f} {flag}  {dur_str:>6}  {r['close_reason']}"
        )

    print(sep)
    print(
        f"  Showing {len(rows)} trade(s)   |   "
        f"{wins}W / {losses}L   WR {wr:.0f}%   |   "
        f"Net PnL  ${total:+,.2f}"
    )
    print(sep)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "20"
    n   = "all" if arg.lower() == "all" else int(arg)
    _print_table(_load(n))
