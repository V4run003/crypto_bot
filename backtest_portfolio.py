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
    python backtest_portfolio.py 180 0 2      # 180d, per-coin daily cap = 2
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
import backtest_sma as bs           # SMA signal checker and 15m indicator builder
from data_cache import DataCache, CacheMissError

_cache = DataCache()

# ── Data loading ──────────────────────────────────────────────────────────────

def _load_coin(symbol, days, end_offset_days=0):
    df5  = _cache.get(symbol, 5,  days, exchange=exchange.session,
                      end_offset_days=end_offset_days)
    if not getattr(config, "USE_CACHE", True):
        time.sleep(5)
    df1h = _cache.get(symbol, 60, days, exchange=exchange.session,
                      end_offset_days=end_offset_days)
    df5  = bh._build_5m_indicators(df5)
    df1h = bh._build_1h_indicators(df1h)
    df1d = None
    if config.REGIME_FILTER:
        if not getattr(config, "USE_CACHE", True):
            time.sleep(5)
        df1d = _cache.get(symbol, "D", days + 60, exchange=exchange.session,
                          end_offset_days=end_offset_days)
        df1d = bh._build_daily_indicators(df1d)
    df4h = None
    if getattr(config, "HTF_4H_FILTER", False):
        if not getattr(config, "USE_CACHE", True):
            time.sleep(5)
        df4h = _cache.get(symbol, 240, days, exchange=exchange.session,
                          end_offset_days=end_offset_days)
        df4h = bh._build_4h_indicators(df4h)
    df15 = None
    sma_coins = getattr(config, "SMA_APPROVED_COINS", [])
    if getattr(config, "SMA_STRATEGY", False) and symbol in sma_coins:
        if not getattr(config, "USE_CACHE", True):
            time.sleep(5)
        df15 = _cache.get(symbol, 15, days, exchange=exchange.session,
                          end_offset_days=end_offset_days)
        df15 = bs._build_15m_indicators(df15)
    return df5, df1h, df1d, df4h, df15


# ── Portfolio simulation ───────────────────────────────────────────────────────

def run_portfolio(days, end_offset_days=0, coin_daily_cap=0, sma_only=False, debug_sma=False):
    symbols = list(config.APPROVED_COINS)

    print(f"\n{'=' * 70}")
    cap_str = f"  coin cap={coin_daily_cap}/day" if coin_daily_cap > 0 else ""
    print(f"  Portfolio Backtest  |  {len(symbols)} coins  |  {days} days{cap_str}")
    print(f"  MAX_TRADES_PER_DAY={config.MAX_TRADES_PER_DAY}  "
          f"MAX_DAILY_LOSS=${config.MAX_DAILY_LOSS}  "
          f"COOLDOWN={config.TRADE_COOLDOWN_SECS//60}m")
    print(f"{'=' * 70}\n")

    # ── Fetch all candles ─────────────────────────────────────────────────────
    print("Fetching candle data:")
    coin_data = {}
    for idx, sym in enumerate(symbols):
        if idx > 0 and not getattr(config, "USE_CACHE", True):
            time.sleep(8)  # cool-down between coins when fetching live
        print(f"  {sym} ...", end="", flush=True)
        try:
            df5, df1h, df1d, df4h, df15 = _load_coin(sym, days, end_offset_days)
            coin_data[sym] = (df5, df1h, df1d, df4h, df15)
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
    # Pre-build 4H timestamp arrays for 4H EMA filter
    ts4h_arrays = {}
    if getattr(config, "HTF_4H_FILTER", False):
        for sym in active_symbols:
            if coin_data[sym][3] is not None:
                ts4h_arrays[sym] = coin_data[sym][3]["timestamp"].values
    # Pre-build 15m timestamp arrays for SMA signal
    ts15_arrays = {}
    if getattr(config, "SMA_STRATEGY", False):
        for sym in active_symbols:
            if coin_data[sym][4] is not None:
                ts15_arrays[sym] = coin_data[sym][4]["timestamp"].values

    if debug_sma:
        print("\n  [debug-sma] 15m candle windows:")
        for sym in active_symbols:
            if sym in ts15_arrays:
                df15 = coin_data[sym][4]
                dt0 = datetime.fromtimestamp(int(df15["timestamp"].iloc[0])  / 1000, tz=timezone.utc)
                dt1 = datetime.fromtimestamp(int(df15["timestamp"].iloc[-1]) / 1000, tz=timezone.utc)
                print(f"    {sym:<18}  {len(df15):>6} 15m bars  {dt0.date()} → {dt1.date()}")
            else:
                print(f"    {sym:<18}  NOT LOADED (SMA_STRATEGY=False or not in SMA_APPROVED_COINS)")
        print()
    # Pre-build daily timestamp arrays for regime filter
    ts1d_arrays = {}
    if config.REGIME_FILTER:
        for sym in active_symbols:
            if coin_data[sym][2] is not None:
                ts1d_arrays[sym] = coin_data[sym][2]["timestamp"].values

    # ── Shared state ──────────────────────────────────────────────────────────
    all_trades       = []
    in_trade         = False   # only one trade open at a time
    active_trade     = None    # dict with trade details
    cooldown_until   = -1      # timestamp (ms) until which no new trade allowed
    daily_loss       = 0.0
    daily_trade_cnt  = 0
    daily_coin_cnt   = defaultdict(int)  # per-coin trade count for current day
    daily_trades_capped = 0    # days where cap was hit
    current_day      = None

    # For daily distribution
    trades_per_day   = defaultdict(int)
    cap_blocked_days = set()   # days where the trade cap blocked a valid signal

    cooldown_candles = config.TRADE_COOLDOWN_SECS // (5 * 60)  # in bar units

    # ── Debug counters (populated only when debug_sma=True) ──────────────────
    dbg_slot_skip      = 0                    # 5m bars skipped: in_trade was True
    dbg_wr_fired       = defaultdict(int)     # per coin: WR/RSI signal found
    dbg_sma_attempted  = defaultdict(int)     # per coin: SMA scan reached (WR/RSI silent)
    dbg_sma_fired      = defaultdict(int)     # per coin: SMA signal found

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
            daily_coin_cnt.clear()

        # ── If in a trade, check if it resolved on this bar ───────────────────
        if in_trade:
            if debug_sma:
                dbg_slot_skip += 1
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
            df5, df1h, df1d, df4h, df15 = coin_data[sym]
            ts1h  = ts1h_arrays[sym]
            h_idx = bisect.bisect_right(ts1h, ts) - 1
            h4_idx = (bisect.bisect_right(ts4h_arrays[sym], ts) - 1) \
                     if sym in ts4h_arrays else -1
            d_idx = -1
            if config.REGIME_FILTER and sym in ts1d_arrays:
                d_idx = bisect.bisect_right(ts1d_arrays[sym], ts) - 2   # previous closed daily bar

            sig, entry, sl, tp, strat = (None, None, None, None, None) \
                if sma_only else bh._check_signal(
                df5, i, df1h, h_idx, sym, df1d, d_idx, df4h, h4_idx)
            if sig is not None and debug_sma:
                dbg_wr_fired[sym] += 1
            # SMA fallback (or primary when --sma-only): try SMA pullback
            if sig is None and sym in ts15_arrays:
                if debug_sma:
                    dbg_sma_attempted[sym] += 1
                i15 = bisect.bisect_right(ts15_arrays[sym], ts) - 1
                sig_s, entry_s, sl_s, tp_s = bs._check_signal(
                    df5, i, df15, i15, df1h, h_idx, df4h, h4_idx, sym)
                if sig_s is not None:
                    sig, entry, sl, tp, strat = sig_s, entry_s, sl_s, tp_s, "SMA"
                    if debug_sma:
                        dbg_sma_fired[sym] += 1
            if sig is None:
                continue

            # ── Per-coin daily cap ────────────────────────────────────────────
            if coin_daily_cap > 0 and daily_coin_cnt[sym] >= coin_daily_cap:
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
            daily_coin_cnt[sym] += 1
            break  # only one trade per bar across all coins

    # ── Handle trade still open at end of data ────────────────────────────────
    # (Mark as unresolved — excluded from stats)

    if debug_sma:
        total_bars = len(all_ts)
        sma_trades = [t for t in all_trades if t.get("strategy") == "SMA"]
        print(f"\n{'=' * 70}")
        print(f"  [debug-sma]  SMA signal diagnostics")
        print(f"{'=' * 70}")
        print(f"  Total 5m bars scanned : {total_bars:,}")
        print(f"  Bars skipped (in_trade): {dbg_slot_skip:,}  "
              f"({dbg_slot_skip / total_bars * 100:.1f}% of all bars)")
        free_bars = total_bars - dbg_slot_skip
        print(f"  Free bars (slot open) : {free_bars:,}  ({free_bars / total_bars * 100:.1f}%)")
        print()
        print(f"  {'Coin':<18}  {'WR/RSI fired':>12}  {'SMA attempted':>13}  "
              f"{'SMA fired':>9}  {'SMA trades':>10}  Note")
        print(f"  {'-' * 68}")
        for sym in active_symbols:
            wr   = dbg_wr_fired[sym]
            att  = dbg_sma_attempted[sym]
            frd  = dbg_sma_fired[sym]
            trds = sum(1 for t in sma_trades if t["symbol"] == sym)
            note = ""
            if sym not in ts15_arrays:
                note = "← no 15m data loaded"
            elif att == 0:
                note = "← never reached (WR/RSI always fired first, or earlier coin took slot)"
            print(f"  {sym:<18}  {wr:>12,}  {att:>13,}  {frd:>9,}  {trds:>10,}  {note}")

        # SMA trades by month
        if sma_trades:
            from collections import Counter
            months = Counter(t["open_dt"].strftime("%Y-%m") for t in sma_trades)
            print(f"\n  SMA trades by month ({len(sma_trades)} total):")
            for mo in sorted(months):
                bar = "█" * months[mo]
                print(f"    {mo}  {months[mo]:>3}  {bar}")
        else:
            print(f"\n  No SMA trades in this run.")
        print(f"\n{'=' * 70}\n")

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
        sma_n = sum(1 for t in trades if t["strategy"] == "SMA")
        parts = []
        if rsi_n: parts.append(f"RSI={rsi_n}")
        if wr_n:  parts.append(f"WR={wr_n}")
        if sma_n: parts.append(f"SMA={sma_n}")
        strat_str = "/".join(parts) if parts else "—"
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
    args            = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_sma          = "--no-sma"   in sys.argv
    sma_only        = "--sma-only" in sys.argv
    debug_sma       = "--debug-sma" in sys.argv
    days            = int(args[0]) if len(args) > 0 else 180
    end_offset_days = int(args[1]) if len(args) > 1 else 0
    coin_daily_cap  = int(args[2]) if len(args) > 2 else 0
    if no_sma:
        config.SMA_STRATEGY = False
        print("  [--no-sma] SMA fallback disabled — WR+RSI only\n")
    if sma_only:
        config.SMA_STRATEGY = True
        config.SMA_APPROVED_COINS = list(config.APPROVED_COINS)  # enable SMA for all coins
        print("  [--sma-only] SMA strategy only — WR+RSI disabled\n")
    if debug_sma and not sma_only and not getattr(config, "SMA_STRATEGY", False):
        config.SMA_STRATEGY = True
        print("  [--debug-sma] SMA_STRATEGY forced True for diagnostics\n")
    all_trades, trades_per_day, cap_blocked_days = run_portfolio(
        days, end_offset_days, coin_daily_cap, sma_only=sma_only, debug_sma=debug_sma)
    _print_report(all_trades, trades_per_day, cap_blocked_days, days)


if __name__ == "__main__":
    main()
