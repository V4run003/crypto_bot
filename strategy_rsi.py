"""
strategy_rsi.py  —  RSI Pullback in Trend strategy.

Signal logic (closed-candle, no look-ahead bias):
  LONG:  5m price > EMA200  AND  1H price > 1H EMA200  AND  ADX ≥ threshold
         AND  ATR > ATR_MA  AND  RSI_LONG_ZONE_LOW ≤ rsi ≤ RSI_LONG_ZONE_HIGH
         AND  rsi > rsi_prev  (RSI turning up after the pullback)

  SHORT: 5m price < EMA200  AND  1H price < 1H EMA200  AND  ADX ≥ threshold
         AND  ATR > ATR_MA  AND  RSI_SHORT_ZONE_LOW ≤ rsi ≤ RSI_SHORT_ZONE_HIGH
         AND  rsi < rsi_prev  (RSI turning down after the bounce)

SL / TP use the same swing-based calc as the WR strategy.
All shared filters (ADX rising, time, EMA50 pullback, HTF) are honoured.
"""

import logging
from datetime import datetime, timezone

import pandas as pd
import ta

import config

logger = logging.getLogger(__name__)


def check_signal(candles_5m, candles_1h):
    """
    Evaluate the most recently *closed* candle (iloc[-2]) against all
    RSI pullback strategy rules.

    Returns a dict:
        {signal: "long"|"short"|None, entry, sl, tp, adx, atr}
    """
    df = _prepare_df(candles_5m)
    if df is None:
        return _no_signal()

    # ── 1H higher-timeframe trend filter ─────────────────────────────────────
    price_1h, ema_1h = _get_1h_ema(candles_1h)
    htf_long_ok  = True
    htf_short_ok = True
    if config.HTF_FILTER and price_1h is not None and ema_1h is not None and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    # ── Closed signal candle values (iloc[-2]) ────────────────────────────────
    row      = df.iloc[-2]
    row_prev = df.iloc[-3]
    price    = row["close"]
    ema      = row["ema200"]
    ema50    = row["ema50"]
    adx      = row["adx"]
    adx_prev = row_prev["adx"]
    atr      = row["atr"]
    atr_ma   = row["atr_ma"]
    rsi      = row["rsi"]
    rsi_prev = row_prev["rsi"]

    if any(pd.isna(v) for v in [price, ema, adx, atr, atr_ma, rsi, rsi_prev]):
        return _no_signal()

    logger.debug(
        "[closed] price=%.4f  ema=%.4f  adx=%.2f  rsi=%.2f  rsi_prev=%.2f",
        price, ema, adx, rsi, rsi_prev,
    )

    # Volatility filter — skip flat markets
    if atr <= atr_ma:
        return _no_signal(adx=adx)

    # Trend-strength filter
    if adx <= config.ADX_THRESHOLD:
        return _no_signal(adx=adx)

    # ADX rising filter
    if config.ADX_RISING_FILTER and not pd.isna(adx_prev) and adx <= adx_prev:
        return _no_signal(adx=adx)

    # Time-of-day filter
    if config.TIME_FILTER:
        bar_hour = datetime.fromtimestamp(
            float(row["timestamp"]) / 1000, tz=timezone.utc
        ).hour
        if bar_hour < config.TIME_FILTER_START or bar_hour >= config.TIME_FILTER_END:
            return _no_signal(adx=adx)

    # EMA50 pullback filter
    if config.EMA50_PULLBACK_FILTER and not pd.isna(ema50):
        if abs(price - ema50) > config.EMA50_PULLBACK_ATR_MULT * atr:
            return _no_signal(adx=adx)

    # ── Long: uptrend + RSI pulled back into zone + RSI turning up ───────────
    if (price > ema and htf_long_ok
            and config.RSI_LONG_ZONE_LOW <= rsi <= config.RSI_LONG_ZONE_HIGH
            and rsi > rsi_prev):
        swing_low = float(df["low"].iloc[-(config.SWING_LOOKBACK + 1):-1].min())
        sl        = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist   = price - sl
        if sl_dist <= 0:
            return _no_signal(adx=adx)
        tp = price + sl_dist * config.RR
        return {"signal": "long",  "entry": price, "sl": sl, "tp": tp,
                "adx": adx, "atr": atr}

    # ── Short: downtrend + RSI bounced into zone + RSI turning down ──────────
    if (price < ema and htf_short_ok
            and config.RSI_SHORT_ZONE_LOW <= rsi <= config.RSI_SHORT_ZONE_HIGH
            and rsi < rsi_prev):
        swing_high = float(df["high"].iloc[-(config.SWING_LOOKBACK + 1):-1].max())
        sl         = swing_high * (1 + config.SL_BUFFER_PCT)
        sl_dist    = sl - price
        if sl_dist <= 0:
            return _no_signal(adx=adx)
        tp = price - sl_dist * config.RR
        return {"signal": "short", "entry": price, "sl": sl, "tp": tp,
                "adx": adx, "atr": atr}

    return _no_signal(adx=adx)


def check_adx(candles_5m):
    """Return the latest ADX value for live trade-management decisions."""
    df = _prepare_df(candles_5m)
    if df is None or pd.isna(df["adx"].iloc[-1]):
        return None
    return float(df["adx"].iloc[-1])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _prepare_df(candles):
    try:
        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.iloc[::-1].reset_index(drop=True)   # oldest → newest
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
        df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
        df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        df["rsi"]    = ta.momentum.rsi(df["close"], window=config.RSI_PERIOD)
        df["atr"]    = ta.volatility.average_true_range(
                           df["high"], df["low"], df["close"], window=config.ATR_PERIOD)
        df["atr_ma"] = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
        return df
    except Exception as exc:
        logger.error("_prepare_df error: %s", exc)
        return None


def _get_1h_ema(candles_1h):
    """Return (latest_close, ema200) from 1-hour candles, or (None, None)."""
    try:
        df = pd.DataFrame(
            candles_1h,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.iloc[::-1].reset_index(drop=True)
        df["close"] = df["close"].astype(float)
        df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
        return float(df["close"].iloc[-1]), float(df["ema200"].iloc[-1])
    except Exception as exc:
        logger.warning("_get_1h_ema error: %s", exc)
        return None, None


def _no_signal(adx=None):
    return {"signal": None, "entry": None, "sl": None, "tp": None,
            "adx": adx, "atr": None}
