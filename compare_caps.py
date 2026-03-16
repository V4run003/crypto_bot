"""
compare_caps.py - Run per-coin daily cap comparison backtest.
Fetches data once, simulates portfolio with cap = none / 3 / 2 / 1.
"""
import sys
import time
from collections import defaultdict

import config
import exchange
import backtest_hybrid as bh
import backtest_portfolio as bp

DAYS = 180
END_OFFSET = 0

def fetch_all():
    symbols = list(config.APPROVED_COINS)
    coin_data = {}
    print("Fetching candle data (once) ...")
    for sym in symbols:
        print(f"  {sym} ...", end="", flush=True)
        try:
            df5, df1h, df1d = bp._load_coin(sym, DAYS, END_OFFSET)
            coin_data[sym] = (df5, df1h, df1d)
            print(f" {len(df5)} bars")
        except Exception as exc:
            print(f" FAILED ({exc}) — skipping")
        time.sleep(0.3)
    return coin_data


def run_with_cap(coin_data, cap):
    """Run portfolio simulation reusing pre-fetched data."""
    import bisect
    import pandas as pd
    from datetime import datetime, timezone
    from collections import defaultdict

    symbols = [s for s in config.APPROVED_COINS if s in coin_data]

    all_ts = set()
    for sym in symbols:
        all_ts.update(coin_data[sym][0]["timestamp"].tolist())
    all_ts = sorted(all_ts)

    ts_to_idx = {}
    for sym in symbols:
        df5 = coin_data[sym][0]
        ts_to_idx[sym] = {int(ts): i for i, ts in enumerate(df5["timestamp"])}
    ts1h_arrays = {sym: coin_data[sym][1]["timestamp"].values for sym in symbols}
    ts1d_arrays = {}
    if config.REGIME_FILTER:
        for sym in symbols:
            if coin_data[sym][2] is not None:
                ts1d_arrays[sym] = coin_data[sym][2]["timestamp"].values

    all_trades = []
    in_trade = False
    active_trade = None
    cooldown_until = -1
    daily_loss = 0.0
    daily_trade_cnt = 0
    daily_coin_cnt = defaultdict(int)
    current_day = None
    trades_per_day = defaultdict(int)
    cap_blocked_days = set()

    for ts in all_ts:
        bar_dt  = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        bar_day = bar_dt.date()

        if bar_day != current_day:
            current_day     = bar_day
            daily_loss      = 0.0
            daily_trade_cnt = 0
            daily_coin_cnt.clear()

        if in_trade:
            sym    = active_trade["symbol"]
            sl     = active_trade["sl"]
            tp     = active_trade["tp"]
            i_open = active_trade["i_open"]
            sig    = active_trade["sig"]

            if sym not in ts_to_idx or ts not in ts_to_idx[sym]:
                continue

            j   = ts_to_idx[sym][ts]
            df5 = coin_data[sym][0]
            if j <= i_open:
                continue

            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]
            closed = False
            result = None
            pnl    = 0.0

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
                    result, pnl, closed = "win",  config.RISK_PER_TRADE * config.RR, True
                elif lo <= sl:
                    be_hit = active_trade.get("be_triggered", False)
                    result, pnl, closed = ("breakeven", 0.0, True) if be_hit \
                                          else ("loss", -config.RISK_PER_TRADE, True)
            else:
                if lo <= tp:
                    result, pnl, closed = "win",  config.RISK_PER_TRADE * config.RR, True
                elif hi >= sl:
                    be_hit = active_trade.get("be_triggered", False)
                    result, pnl, closed = ("breakeven", 0.0, True) if be_hit \
                                          else ("loss", -config.RISK_PER_TRADE, True)

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
                active_trade["result"]  = result
                active_trade["pnl"]     = pnl
                active_trade["exit_ts"] = ts
                all_trades.append(dict(active_trade))
                daily_loss     += pnl
                cooldown_until  = ts + config.TRADE_COOLDOWN_SECS * 1000
                in_trade        = False
                active_trade    = None
                trades_per_day[bar_day] += 1
            continue

        if ts < cooldown_until:
            continue
        if daily_loss <= -config.MAX_DAILY_LOSS:
            continue
        if daily_trade_cnt >= config.MAX_TRADES_PER_DAY:
            cap_blocked_days.add(bar_day)
            continue

        for sym in symbols:
            if sym not in ts_to_idx or ts not in ts_to_idx[sym]:
                continue
            i = ts_to_idx[sym][ts]
            df5, df1h, df1d = coin_data[sym]
            ts1h = ts1h_arrays[sym]
            h_idx = bisect.bisect_right(ts1h, ts) - 1
            d_idx = -1
            if config.REGIME_FILTER and sym in ts1d_arrays:
                d_idx = bisect.bisect_right(ts1d_arrays[sym], ts) - 2

            sig_val, entry, sl, tp, strat = bh._check_signal(df5, i, df1h, h_idx, sym, df1d, d_idx)
            if sig_val is None:
                continue

            if cap > 0 and daily_coin_cnt[sym] >= cap:
                continue

            open_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            active_trade = {
                "symbol": sym, "sig": sig_val, "strategy": strat,
                "entry": entry, "sl": sl, "tp": tp,
                "open_ts": ts, "open_dt": open_dt, "i_open": i,
                "day": bar_day,
            }
            in_trade         = True
            daily_trade_cnt += 1
            daily_coin_cnt[sym] += 1
            break

    return all_trades, trades_per_day, cap_blocked_days


def summarize(label, trades):
    if not trades:
        print(f"{label}: no trades")
        return
    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gw / gl if gl > 0 else float("inf")
    net = sum(t["pnl"] for t in trades)
    eq = [0.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl"])
    peak = mx = 0.0
    for e in eq:
        if e > peak: peak = e
        d = peak - e
        if d > mx: mx = d
    streak = max_streak = 0
    for t in trades:
        if t["result"] == "loss":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    wr = len(wins) / len(trades) * 100
    print(f"{label:<12}  N={len(trades):>3}  WR={wr:>4.1f}%  PF={pf:>5.2f}  "
          f"Net=${net:>+7.0f}  MaxDD=${mx:>5.0f}  ConsecLoss={max_streak}")


if __name__ == "__main__":
    coin_data = fetch_all()
    print(f"\n{'='*70}")
    print(f"  Cap Comparison  (180d, no offset)")
    print(f"{'='*70}")

    for cap in [0, 3, 2, 1]:
        label = f"cap={'none':>4}" if cap == 0 else f"cap={cap:>4}/day"
        sys.stdout.write(f"  Running {label} ... "); sys.stdout.flush()
        t, tpd, cbd = run_with_cap(coin_data, cap)
        sys.stdout.write(f"done ({len(t)} trades)\n"); sys.stdout.flush()
        summarize(f"  {label}", t)

    print(f"{'='*70}")
