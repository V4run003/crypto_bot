"""
backtest_rsi.py — Walk-forward backtest of the RSI Pullback in Trend strategy.

Replays the EXACT same signal logic as strategy_rsi.py candle-by-candle on real
historical data fetched from Bybit.  No look-ahead bias: the signal is evaluated
on bar[i] (closed candle), and the trade is tracked from bar[i+1] onwards.

Standalone usage:
    python backtest_rsi.py                       # BTCUSDT, last 90 days
    python backtest_rsi.py ETHUSDT 180           # ETHUSDT, last 180 days
    python backtest_rsi.py BTCUSDT 365 --quiet   # suppress per-trade lines
"""

import bisect
import sys
import time
import logging
from datetime import datetime, timezone

import pandas as pd
import ta

import config

logging.basicConfig(level=logging.WARNING)


# ── Historical data fetching (identical to backtest.py) ───────────────────────

def _fetch_chunk(session, symbol, interval, end_ms, limit=1000):
    for attempt in range(5):
        try:
            resp = session.get_kline(
                category="linear",
                symbol=symbol,
                interval=interval,
                limit=limit,
                end=end_ms,
            )
            return resp["result"]["list"]
        except Exception:
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def fetch_all_candles(session, symbol, interval, days):
    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days * 86_400_000
    end_ms   = now_ms
    rows     = []

    label = f"{symbol} {interval}m"
    print(f"  Fetching {label} ({days} days) ...", end="", flush=True)

    while True:
        chunk = _fetch_chunk(session, symbol, interval, end_ms)
        if not chunk:
            break
        rows.extend(chunk)
        oldest_ts = int(chunk[-1][0])
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.05)

    print(f" {len(rows)} candles")

    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
    )
    df["timestamp"] = df["timestamp"].astype(int)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df = (
        df.sort_values("timestamp")
          .drop_duplicates("timestamp")
          .reset_index(drop=True)
    )
    df = df[df["timestamp"] >= start_ms].reset_index(drop=True)
    return df


# ── Indicator computation ─────────────────────────────────────────────────────

def _build_5m_indicators(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["rsi"]    = ta.momentum.rsi(df["close"], window=config.RSI_PERIOD)
    df["atr"]    = ta.volatility.average_true_range(
                       df["high"], df["low"], df["close"],
                       window=config.ATR_PERIOD)
    df["atr_ma"] = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
    return df


def _build_1h_indicators(df):
    df = df.copy()
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
    return df


# ── Signal detection (mirrors strategy_rsi.py exactly) ───────────────────────

def _check_signal(df5, i, df1h, h_idx):
    """
    Evaluate whether a valid RSI pullback signal exists at 5m bar index `i`.

    Mapping to strategy_rsi.py iloc[-2] convention:
        strategy iloc[-2]   → df5.iloc[i]       (signal candle)
        strategy iloc[-3]   → df5.iloc[i-1]     (previous candle for rsi_prev)
        strategy swing      → df5.iloc[i-sw+1:i+1]

    Returns (signal, entry, sl, tp) or (None, None, None, None).
    """
    sw     = config.SWING_LOOKBACK
    min_i  = max(200, config.RSI_PERIOD + 1, sw)

    if i < min_i or h_idx < 200:
        return None, None, None, None

    row      = df5.iloc[i]
    row_prev = df5.iloc[i - 1]
    price    = row["close"]
    ema5     = row["ema200"]
    ema50    = row["ema50"]
    adx      = row["adx"]
    adx_prev = row_prev["adx"]
    atr      = row["atr"]
    atr_ma   = row["atr_ma"]
    rsi      = row["rsi"]
    rsi_prev = row_prev["rsi"]

    if any(pd.isna(v) for v in [price, ema5, adx, atr, atr_ma, rsi, rsi_prev]):
        return None, None, None, None

    # Volatility and trend-strength filters
    if atr <= atr_ma or adx <= config.ADX_THRESHOLD:
        return None, None, None, None

    # ADX rising filter
    if config.ADX_RISING_FILTER and not pd.isna(adx_prev) and adx <= adx_prev:
        return None, None, None, None

    # Time-of-day filter
    if config.TIME_FILTER:
        bar_hour = datetime.fromtimestamp(int(df5["timestamp"].iloc[i]) / 1000, tz=timezone.utc).hour
        if bar_hour < config.TIME_FILTER_START or bar_hour >= config.TIME_FILTER_END:
            return None, None, None, None

    # EMA50 pullback filter
    if config.EMA50_PULLBACK_FILTER and not pd.isna(ema50):
        if abs(price - ema50) > config.EMA50_PULLBACK_ATR_MULT * atr:
            return None, None, None, None

    # 1H HTF filter
    row_1h   = df1h.iloc[h_idx]
    price_1h = row_1h["close"]
    ema_1h   = row_1h["ema200"]
    htf_long_ok  = True
    htf_short_ok = True
    if config.HTF_FILTER and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    # RSI pullback — LONG: uptrend, RSI pulled into oversold zone, now turning up
    if (price > ema5 and htf_long_ok
            and config.RSI_LONG_ZONE_LOW <= rsi <= config.RSI_LONG_ZONE_HIGH
            and rsi > rsi_prev):
        swing_low = float(df5["low"].iloc[i - sw + 1 : i + 1].min())
        sl = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist = price - sl
        if sl_dist > 0:
            tp = price + sl_dist * config.RR
            return "long", price, sl, tp

    # RSI pullback — SHORT: downtrend, RSI bounced into overbought zone, now turning down
    if (price < ema5 and htf_short_ok
            and config.RSI_SHORT_ZONE_LOW <= rsi <= config.RSI_SHORT_ZONE_HIGH
            and rsi < rsi_prev):
        swing_high = float(df5["high"].iloc[i - sw + 1 : i + 1].max())
        sl = swing_high * (1 + config.SL_BUFFER_PCT)
        sl_dist = sl - price
        if sl_dist > 0:
            tp = price - sl_dist * config.RR
            return "short", price, sl, tp

    return None, None, None, None


# ── Walk-forward simulation ───────────────────────────────────────────────────

def run_backtest(symbol, days, quiet=False):
    import exchange

    print(f"\n{'=' * 62}")
    print(f"  RSI Backtest  |  {symbol}  |  {days} days  |  RR={config.RR}"
          f"  |  risk=${config.RISK_PER_TRADE}/trade")
    print(f"  RSI zones: long {config.RSI_LONG_ZONE_LOW}-{config.RSI_LONG_ZONE_HIGH}"
          f"  |  short {config.RSI_SHORT_ZONE_LOW}-{config.RSI_SHORT_ZONE_HIGH}"
          f"  |  period={config.RSI_PERIOD}")
    print(f"{'=' * 62}")

    df5  = fetch_all_candles(exchange.session, symbol, 5,  days)
    df1h = fetch_all_candles(exchange.session, symbol, 60, days)

    print("  Computing indicators ...", end="", flush=True)
    df5  = _build_5m_indicators(df5)
    df1h = _build_1h_indicators(df1h)
    print(" done\n")

    ts5  = df5["timestamp"].values
    ts1h = df1h["timestamp"].values

    min_start = max(250, config.RSI_PERIOD + 1, config.SWING_LOOKBACK)
    trades    = []
    skip_until = -1

    for i in range(min_start, len(df5) - 1):
        if i < skip_until:
            continue

        h_idx = bisect.bisect_right(ts1h, ts5[i]) - 1

        sig, entry, sl, tp = _check_signal(df5, i, df1h, h_idx)
        if sig is None:
            continue

        result   = None
        exit_idx = None
        exit_px  = None

        for j in range(i + 1, min(i + 2000, len(df5))):
            hi = df5.iloc[j]["high"]
            lo = df5.iloc[j]["low"]

            if sig == "long":
                if lo <= sl:
                    result, exit_px, exit_idx = "loss", sl, j
                    break
                if hi >= tp:
                    result, exit_px, exit_idx = "win", tp, j
                    break
            else:
                if hi >= sl:
                    result, exit_px, exit_idx = "loss", sl, j
                    break
                if lo <= tp:
                    result, exit_px, exit_idx = "win", tp, j
                    break
            # ADX fade: if trend collapses, exit at candle close
            adx_j = df5.iloc[j]["adx"]
            if not pd.isna(adx_j) and adx_j < config.ADX_THRESHOLD:
                result, exit_px, exit_idx = "fade", df5.iloc[j]["close"], j
                break

        if result is None:
            continue

        sl_dist  = abs(entry - sl)
        pnl_pts  = (exit_px - entry) if sig == "long" else (entry - exit_px)
        rr_achvd = pnl_pts / sl_dist if sl_dist > 0 else 0
        pnl_usd  = config.RISK_PER_TRADE * rr_achvd

        trade = {
            "symbol":     symbol,
            "side":       sig,
            "entry_time": datetime.fromtimestamp(ts5[i]        / 1000, tz=timezone.utc),
            "exit_time":  datetime.fromtimestamp(ts5[exit_idx] / 1000, tz=timezone.utc),
            "entry":      entry,
            "sl":         sl,
            "tp":         tp,
            "exit_px":    exit_px,
            "result":     result,
            "rr":         rr_achvd,
            "pnl_usd":    pnl_usd,
        }
        trades.append(trade)

        if not quiet:
            mark = "✓" if result == "win" else ("~" if result == "fade" else "✗")
            print(
                f"  {mark} {sig.upper():5s}  "
                f"{trade['entry_time'].strftime('%Y-%m-%d %H:%M')} → "
                f"{trade['exit_time'].strftime('%m-%d %H:%M')}  "
                f"entry={entry:.4f}  sl={sl:.4f}  tp={tp:.4f}  "
                f"exit={exit_px:.4f}  RR={rr_achvd:+.2f}  "
                f"PnL=${pnl_usd:+.2f}"
            )

        skip_until = exit_idx + 1

    _print_stats(trades, days, symbol)
    return trades


# ── Statistics report ─────────────────────────────────────────────────────────

def _print_stats(trades, days, symbol):
    if not trades:
        print(f"\n  No completed trades found for {symbol} over {days} days.")
        return

    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    fades  = [t for t in trades if t["result"] == "fade"]
    total  = len(trades)

    wr      = len(wins) / total * 100
    net_pnl = sum(t["pnl_usd"] for t in trades)
    avg_rr  = sum(t["rr"]      for t in trades) / total
    tpd     = total / days

    avg_win  = sum(t["pnl_usd"] for t in wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0.0
    gross_w  = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    gross_l  = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0))
    pf       = gross_w / gross_l if gross_l > 0 else float("inf")

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

    max_streak = cur_streak = 0
    for t in trades:
        if t["result"] == "loss":
            cur_streak += 1
            max_streak  = max(max_streak, cur_streak)
        else:
            cur_streak  = 0

    first_dt = trades[0]["entry_time"].strftime("%Y-%m-%d")
    last_dt  = trades[-1]["exit_time"].strftime("%Y-%m-%d")

    print(f"\n{'─' * 62}")
    print(f"  RESULTS (RSI) — {symbol}  ({days} days: {first_dt} → {last_dt})")
    print(f"{'─' * 62}")
    print(f"  Total trades       : {total}")
    print(f"  Wins / Losses / Fades: {len(wins)} / {len(losses)} / {len(fades)}"
          f"  (fade net: ${sum(t['pnl_usd'] for t in fades):+.2f})")
    print(f"  Win rate           : {wr:.1f}%")
    print(f"  Avg trades / day   : {tpd:.2f}")
    print(f"  Net PnL            : ${net_pnl:+.2f}  (at ${config.RISK_PER_TRADE} risk/trade)")
    print(f"  Avg RR achieved    : {avg_rr:.2f}R")
    print(f"  Profit factor      : {pf:.2f}")
    print(f"  Avg win            : ${avg_win:+.2f}")
    print(f"  Avg loss           : ${avg_loss:+.2f}")
    print(f"  Max drawdown       : ${max_dd:.2f}")
    print(f"  Max consec. losses : {max_streak}")
    print(f"{'─' * 62}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _symbol = sys.argv[1]              if len(sys.argv) > 1 else "BTCUSDT"
    _days   = int(sys.argv[2])         if len(sys.argv) > 2 else 90
    _quiet  = "--quiet" in sys.argv or "-q" in sys.argv
    run_backtest(_symbol, _days, quiet=_quiet)
