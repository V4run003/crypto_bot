"""
backtest_portfolio.py — Multi-coin concurrent backtest.

Simulates the live scanner's exact behaviour:
  • All APPROVED_COINS run on the same clock
  • One open trade at a time across all coins (scanner stops scanning if a
    position is open — mirrored here via a shared in-trade lock)
  • Shared 10-min cooldown after every close
  • Shared daily trade counter (MAX_TRADES_PER_DAY)
  • Shared daily loss cap (MAX_DAILY_LOSS)

Coins are iterated in APPROVED_COINS order on every bar — same as scanner.py.
RSI_APPROVED_COINS get RSI-first treatment; all others get WR-only.

Outputs:
  • Per-coin trade breakdown
  • Combined portfolio stats
  • Daily trade distribution (answers "does the 8→10 cap matter?")

Usage:
    python backtest_portfolio.py              # all APPROVED_COINS, 180 days
    python backtest_portfolio.py 90           # 90 days
    python backtest_portfolio.py 180 274      # 180d window ending 274 days ago
"""

import bisect
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import pandas as pd
import ta

import config
import exchange
import backtest_hybrid as bh   # reuse fetch, indicator builders and signal checkers

# ── Data loading ──────────────────────────────────────────────────────────────

def _load_coin(symbol, days, end_offset_days=0):
    df5  = bh.fetch_all_candles(exchange.session, symbol, 5,  days, end_offset_days)
    df1h = bh.fetch_all_candles(exchange.session, symbol, 60, days, end_offset_days)
    df5  = bh._build_5m_indicators(df5)
    df1h = bh._build_1h_indicators(df1h)
    return df5, df1h


# ── Portfolio simulation ───────────────────────────────────────────────────────

def run_portfolio(days, end_offset_days=0):
    symbols = list(config.APPROVED_COINS)

    print(f"\n{'=' * 70}")
    print(f"  Portfolio Backtest  |  {len(symbols)} coins  |  {days} days")
    print(f"  MAX_TRADES_PER_DAY={config.MAX_TRADES_PER_DAY}  "
          f"MAX_DAILY_LOSS=${config.MAX_DAILY_LOSS}  "
          f"COOLDOWN={config.TRADE_COOLDOWN_SECS//60}m")
    print(f"{'=' * 70}\n")

    # ── Fetch all candles ─────────────────────────────────────────────────────
    print("Fetching candle data:")
    coin_data = {}
    for sym in symbols:
        print(f"  {sym} ...", end="", flush=True)
        try:
            df5, df1h = _load_coin(sym, days, end_offset_days)
            coin_data[sym] = (df5, df1h)
            print(f" {len(df5)} bars")
        except Exception as exc:
            print(f" FAILED ({exc}) — skipping")

    active_symbols = [s for s in symbols if s in coin_data]

    # ── Build a unified sorted list of all 5m timestamps ─────────────────────
    all_ts = set()
    for sym in active_symbols:
        all_ts.update(coin_data[sym][0]["timestamp"].tolist())
    all_ts = sorted(all_ts)

    # Pre-build {symbol: {timestamp: row_index}} lookup for fast access
    ts_to_idx = {}
    for sym in active_symbols:
        df5 = coin_data[sym][0]
        ts_to_idx[sym] = {int(ts): i for i, ts in enumerate(df5["timestamp"])}

    # Pre-build 1h timestamp arrays
    ts1h_arrays = {sym: coin_data[sym][1]["timestamp"].values for sym in active_symbols}

    # ── Shared state ──────────────────────────────────────────────────────────
    all_trades       = []
    in_trade         = False   # only one trade open at a time
    active_trade     = None    # dict with trade details
    cooldown_until   = -1      # timestamp (ms) until which no new trade allowed
    daily_loss       = 0.0
    daily_trade_cnt  = 0
    daily_trades_capped = 0    # days where cap was hit
    current_day      = None

    # For daily distribution
    trades_per_day   = defaultdict(int)
    cap_blocked_days = set()   # days where the trade cap blocked a valid signal

    cooldown_candles = config.TRADE_COOLDOWN_SECS // (5 * 60)  # in bar units

    # We'll iterate by 5m bar index in a global time-aligned way
    # For per-coin we need the index of each coin at each timestamp
    print("\nRunning simulation ...")

    global_bar_idx = 0
    skip_global_until_ts = -1   # used to advance past open trade duration

    for ts in all_ts:
        bar_dt  = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        bar_day = bar_dt.date()

        # ── Daily reset ───────────────────────────────────────────────────────
        if bar_day != current_day:
            current_day    = bar_day
            daily_loss     = 0.0
            daily_trade_cnt = 0

        # ── If in a trade, check if it resolved on this bar ───────────────────
        if in_trade:
            sym    = active_trade["symbol"]
            sig    = active_trade["sig"]
            sl     = active_trade["sl"]  # may be updated to entry after BE
            tp     = active_trade["tp"]
            i_open = active_trade["i_open"]

            if sym not in ts_to_idx or ts not in ts_to_idx[sym]:
                continue

            j = ts_to_idx[sym][ts]
            df5 = coin_data[sym][0]

            if j <= i_open:
                continue

            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]
            closed = False
            result = None
            pnl    = 0.0

            # Breakeven: move SL to entry when close crosses 50% of TP distance
            # Uses close (not high/low) to match live bot which checks current_price at candle close
            entry_px = active_trade["entry"]
            halfway  = (entry_px + (tp - entry_px) * 0.5) if sig == "long" \
                       else (entry_px - (entry_px - tp) * 0.5)
            close_j  = df5.iloc[j]["close"]
            if config.BE_ENABLED and not active_trade.get("be_triggered", False):
                if (sig == "long" and close_j >= halfway) or (sig == "short" and close_j <= halfway):
                    active_trade["be_triggered"] = True
                    active_trade["sl"]           = entry_px
                    sl = entry_px

            if sig == "long":
                if hi >= tp:
                    result, pnl, closed = "win",   config.RISK_PER_TRADE * config.RR, True
                elif lo <= sl:
                    be_hit = active_trade.get("be_triggered", False)
                    result, pnl, closed = ("breakeven", 0.0, True) if be_hit \
                                          else ("loss", -config.RISK_PER_TRADE, True)
            else:
                if lo <= tp:
                    result, pnl, closed = "win",   config.RISK_PER_TRADE * config.RR, True
                elif hi >= sl:
                    be_hit = active_trade.get("be_triggered", False)
                    result, pnl, closed = ("breakeven", 0.0, True) if be_hit \
                                          else ("loss", -config.RISK_PER_TRADE, True)

            # ADX fade: if trend collapses, exit at candle close
            if not closed and config.ADX_FADE_ENABLED:
                adx_j = df5.iloc[j]["adx"]
                if not pd.isna(adx_j) and adx_j < config.ADX_THRESHOLD:
                    _entry   = active_trade["entry"]
                    _sl_dist = abs(_entry - active_trade["sl"])
                    _close   = df5.iloc[j]["close"]
                    _pts     = (_close - _entry) if sig == "long" else (_entry - _close)
                    fade_pnl = config.RISK_PER_TRADE * (_pts / _sl_dist) if _sl_dist > 0 else 0.0
                    result, pnl, closed = "fade", fade_pnl, True

            if closed:
                active_trade["result"]   = result
                active_trade["pnl"]      = pnl
                active_trade["exit_ts"]  = ts
                active_trade["exit_idx"] = j
                all_trades.append(dict(active_trade))
                daily_loss      += pnl
                cooldown_until   = ts + config.TRADE_COOLDOWN_SECS * 1000
                in_trade         = False
                active_trade     = None
                trades_per_day[bar_day] += 1

            continue  # while in a trade, don't scan for new entries

        # ── Cooldown check ────────────────────────────────────────────────────
        if ts < cooldown_until:
            continue

        # ── Daily limits ──────────────────────────────────────────────────────
        if daily_loss <= -config.MAX_DAILY_LOSS:
            continue
        if daily_trade_cnt >= config.MAX_TRADES_PER_DAY:
            cap_blocked_days.add(bar_day)
            continue

        # ── Scan coins in order ───────────────────────────────────────────────
        for sym in active_symbols:
            if sym not in ts_to_idx or ts not in ts_to_idx[sym]:
                continue

            i   = ts_to_idx[sym][ts]
            df5, df1h = coin_data[sym]
            ts1h = ts1h_arrays[sym]
            h_idx = bisect.bisect_right(ts1h, ts) - 1

            sig, entry, sl, tp, strat = bh._check_signal(df5, i, df1h, h_idx, sym)
            if sig is None:
                continue

            # ── Signal found — open trade ─────────────────────────────────────
            open_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            active_trade = {
                "symbol":    sym,
                "sig":       sig,
                "strategy":  strat,
                "entry":     entry,
                "sl":        sl,
                "tp":        tp,
                "open_ts":   ts,
                "open_dt":   open_dt,
                "i_open":    i,
                "day":       bar_day,
            }
            in_trade          = True
            daily_trade_cnt  += 1
            break  # only one trade per bar across all coins

    # ── Handle trade still open at end of data ────────────────────────────────
    # (Mark as unresolved — excluded from stats)

    return all_trades, trades_per_day, cap_blocked_days


# ── Stats and reporting ───────────────────────────────────────────────────────

def _stats(trades):
    if not trades:
        return {}
    wins      = [t for t in trades if t["result"] == "win"]
    losses    = [t for t in trades if t["result"] == "loss"]
    fades     = [t for t in trades if t["result"] == "fade"]
    breakevens= [t for t in trades if t["result"] == "breakeven"]
    gw     = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl     = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf     = gw / gl if gl > 0 else float("inf")
    wr     = len(wins) / len(trades) * 100
    net    = sum(t["pnl"] for t in trades)
    eq     = [0.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl"])
    peak = mx = 0.0
    for e in eq:
        if e > peak:
            peak = e
        d = peak - e
        if d > mx:
            mx = d
    streak = max_streak = 0
    for t in trades:
        if t["result"] == "loss":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "fades": len(fades), "breakevens": len(breakevens),
        "wr": wr, "pf": pf, "net": net, "max_dd": mx,
        "max_streak": max_streak,
    }


def _print_report(all_trades, trades_per_day, cap_blocked_days, days):
    if not all_trades:
        print("No completed trades.")
        return

    # ── Per-coin breakdown ────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  Per-Coin Breakdown")
    print(f"{'=' * 70}")
    print(f"  {'Symbol':<18}  {'N':>4}  {'WR%':>5}  {'PF':>5}  {'Net':>9}  "
          f"{'MaxDD':>7}  {'Strat':>9}")
    print(f"  {'-' * 65}")

    per_coin = defaultdict(list)
    for t in all_trades:
        per_coin[t["symbol"]].append(t)

    for sym in config.APPROVED_COINS:
        trades = per_coin.get(sym, [])
        if not trades:
            print(f"  {sym:<18}  {'—':>4}")
            continue
        s = _stats(trades)
        rsi_n = sum(1 for t in trades if t["strategy"] == "RSI")
        wr_n  = sum(1 for t in trades if t["strategy"] == "WR")
        strat_str = f"RSI={rsi_n}/WR={wr_n}" if rsi_n else f"WR={wr_n}"
        print(f"  {sym:<18}  {s['n']:>4}  {s['wr']:>4.1f}%  {s['pf']:>5.2f}  "
              f"${s['net']:>8.0f}  ${s['max_dd']:>6.0f}  {strat_str:>9}")

    # ── Combined portfolio stats ───────────────────────────────────────────────
    s   = _stats(all_trades)
    tpd = s["n"] / days
    first_dt = min(t["open_dt"] for t in all_trades).strftime("%Y-%m-%d")
    last_dt  = max(t["open_dt"] for t in all_trades).strftime("%Y-%m-%d")

    print(f"\n{'=' * 70}")
    print(f"  COMBINED PORTFOLIO  ({days} days: {first_dt} → {last_dt})")
    print(f"{'=' * 70}")
    print(f"  Total trades       : {s['n']}")
    print(f"  Wins / Losses / BE / Fades: {s['wins']} / {s['losses']} / {s['breakevens']} / {s['fades']}")
    print(f"  Win rate           : {s['wr']:.1f}%")
    print(f"  Profit factor      : {s['pf']:.2f}")
    print(f"  Net PnL            : ${s['net']:+.2f}")
    print(f"  Max drawdown       : ${s['max_dd']:.2f}")
    print(f"  Max consec. losses : {s['max_streak']}")
    print(f"  Avg trades / day   : {tpd:.2f}")

    # ── Daily trade distribution ──────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print(f"  Daily Trade Distribution  (answers: does the cap matter?)")
    print(f"{'─' * 70}")

    dist = defaultdict(int)
    for cnt in trades_per_day.values():
        dist[cnt] += 1

    max_cnt = max(dist.keys()) if dist else 0
    for k in range(1, max_cnt + 1):
        bar = "█" * min(dist[k], 40)
        print(f"  {k:>2} trades/day : {dist[k]:>4} days  {bar}")

    days_with_any = len(trades_per_day)
    days_total    = days
    print(f"\n  Days with ≥1 trade : {days_with_any} / {days_total}"
          f"  ({days_with_any/days_total*100:.0f}%)")
    print(f"  Days cap was hit   : {len(cap_blocked_days)}"
          f"  ({len(cap_blocked_days)/days_total*100:.1f}%)")

    if cap_blocked_days:
        print(f"\n  ⚠ Cap blocked signals on {len(cap_blocked_days)} days —"
              f" raising to 10 may capture those trades")
    else:
        print(f"\n  ✅ Cap was never hit — 8→10 change has zero impact")

    # ── Worst single day ──────────────────────────────────────────────────────
    daily_pnl = defaultdict(float)
    for t in all_trades:
        daily_pnl[t["day"]] += t["pnl"]
    if daily_pnl:
        worst_day = min(daily_pnl, key=daily_pnl.get)
        best_day  = max(daily_pnl, key=daily_pnl.get)
        print(f"\n  Best  single day   : {best_day}  ${daily_pnl[best_day]:+.2f}")
        print(f"  Worst single day   : {worst_day}  ${daily_pnl[worst_day]:+.2f}")
        days_under_limit = sum(1 for p in daily_pnl.values() if p < -config.MAX_DAILY_LOSS)
        print(f"  Days bot would stop: {days_under_limit}"
              f"  (daily loss >${ config.MAX_DAILY_LOSS})")

    print(f"\n{'=' * 70}\n")


def main():
    days            = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    end_offset_days = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    all_trades, trades_per_day, cap_blocked_days = run_portfolio(days, end_offset_days)
    _print_report(all_trades, trades_per_day, cap_blocked_days, days)


if __name__ == "__main__":
    main()
