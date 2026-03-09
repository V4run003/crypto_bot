import logging

import pandas as pd
import ta

import config

logger = logging.getLogger(__name__)


def check_signal(candles_5m, candles_1h):
    """
    Evaluate the most recently *closed* candle (iloc[-2]) against all
    strategy rules.  Using [-2] instead of [-1] prevents look-ahead bias
    and signal repainting on the forming candle.

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
    if price_1h is not None and ema_1h is not None and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    # ── Closed signal candle values (iloc[-2]) ────────────────────────────────
    row    = df.iloc[-2]
    price  = row["close"]
    ema    = row["ema200"]
    adx    = row["adx"]
    atr    = row["atr"]
    atr_ma = row["atr_ma"]

    if any(pd.isna(v) for v in [price, ema, adx, atr, atr_ma]):
        return _no_signal()

    logger.debug(
        "[closed] price=%.4f  ema=%.4f  adx=%.2f  atr=%.6f  atr_ma=%.6f",
        price, ema, adx, atr, atr_ma,
    )

    # Volatility filter — skip flat markets
    if atr <= atr_ma:
        return _no_signal(adx=adx)

    # Trend-strength filter
    if adx <= 20:
        return _no_signal(adx=adx)

    # ── Long: 5m price above EMA200, 1H aligned, selling-exhaustion hook ──────
    if price > ema and htf_long_ok and _wr_exhaustion_long(df):
        swing_low = float(df["low"].iloc[-(config.SWING_LOOKBACK + 1):-1].min())
        sl        = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist   = price - sl
        if sl_dist <= 0:
            return _no_signal(adx=adx)
        tp = price + sl_dist * config.RR
        return {"signal": "long",  "entry": price, "sl": sl, "tp": tp,
                "adx": adx, "atr": atr}

    # ── Short: 5m price below EMA200, 1H aligned, buying-exhaustion hook ──────
    if price < ema and htf_short_ok and _wr_exhaustion_short(df):
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
        df["adx"]    = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
        df["wr"]     = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
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


def _wr_exhaustion_long(df):
    """
    Selling exhaustion — long hook (closed-candle version):
    - The WR_EXHAUSTION_LOOKBACK candles *before* the signal candle (iloc[-2])
      were ALL below -80 (deep oversold).
    - The signal candle (iloc[-2]) has WR crossing back *above* -80.
    """
    wr       = df["wr"]
    lookback = config.WR_EXHAUSTION_LOOKBACK
    if wr.iloc[-2] <= -80:               # signal candle still in zone — no hook
        return False
    prev = wr.iloc[-(lookback + 2):-2]   # candles before the signal candle
    return (prev < -80).sum() >= lookback


def _wr_exhaustion_short(df):
    """
    Buying exhaustion — short hook (closed-candle version):
    - The WR_EXHAUSTION_LOOKBACK candles before the signal candle were ALL
      above -20 (deep overbought).
    - The signal candle (iloc[-2]) has WR crossing back *below* -20.
    """
    wr       = df["wr"]
    lookback = config.WR_EXHAUSTION_LOOKBACK
    if wr.iloc[-2] >= -20:               # still in zone — no hook
        return False
    prev = wr.iloc[-(lookback + 2):-2]
    return (prev > -20).sum() >= lookback