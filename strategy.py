import logging
from datetime import datetime, timezone

import pandas as pd
import ta

import config

logger = logging.getLogger(__name__)


def check_signal(candles_5m, candles_1h, candles_4h=None, symbol=None):
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
    if config.HTF_FILTER and price_1h is not None and ema_1h is not None and not pd.isna(ema_1h):
        htf_long_ok  = price_1h > ema_1h
        htf_short_ok = price_1h < ema_1h

    # ── 4H EMA200 filter ─────────────────────────────────────────────────────
    excl_4h = getattr(config, "HTF_4H_FILTER_EXCLUDED", [])
    if (getattr(config, "HTF_4H_FILTER", False)
            and (symbol is None or symbol not in excl_4h)
            and candles_4h is not None):
        price_4h, ema_4h = _get_htf_ema(candles_4h)
        if price_4h is not None and ema_4h is not None and not pd.isna(ema_4h):
            if price_4h <= ema_4h:
                htf_long_ok  = False   # 4H bearish — no longs
            if price_4h >= ema_4h:
                htf_short_ok = False   # 4H bullish — no shorts

    # ── Closed signal candle values (iloc[-2]) ────────────────────────────────
    row      = df.iloc[-2]
    row_prev = df.iloc[-3]   # for ADX-rising filter
    price    = row["close"]
    ema      = row["ema200"]
    ema50    = row["ema50"]
    adx      = row["adx"]
    adx_prev = row_prev["adx"]
    atr      = row["atr"]
    atr_ma   = row["atr_ma"]

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
    if adx <= config.ADX_THRESHOLD:
        return _no_signal(adx=adx)

    # ADX rising filter — momentum must be increasing
    if config.ADX_RISING_FILTER and not pd.isna(adx_prev) and adx <= adx_prev:
        return _no_signal(adx=adx)

    # ADX rising 2-candle filter — stricter momentum confirmation
    if config.ADX_RISING2_FILTER:
        adx_prev2 = df.iloc[-4]["adx"]
        if not (not pd.isna(adx_prev) and not pd.isna(adx_prev2)
                and adx > adx_prev and adx_prev > adx_prev2):
            return _no_signal(adx=adx)

    # Time-of-day filter — only trade during liquid hours
    if config.TIME_FILTER:
        bar_hour = datetime.fromtimestamp(
            float(row["timestamp"]) / 1000, tz=timezone.utc
        ).hour
        if bar_hour < config.TIME_FILTER_START or bar_hour >= config.TIME_FILTER_END:
            return _no_signal(adx=adx)

    # EMA50 pullback filter — price must be within N ATR of EMA50
    if config.EMA50_PULLBACK_FILTER and not pd.isna(ema50):
        if abs(price - ema50) > config.EMA50_PULLBACK_ATR_MULT * atr:
            return _no_signal(adx=adx)

    # Volume confirmation — hook candle must have above-average volume
    if config.VOLUME_CONFIRM_FILTER:
        vol_ma = row["vol_ma20"]
        if not pd.isna(vol_ma) and row["volume"] < vol_ma * config.VOLUME_CONFIRM_MULT:
            return _no_signal(adx=adx)

    prev_high = float(df["high"].iloc[-3])
    prev_low  = float(df["low"].iloc[-3])

    # ── Long: 5m price above EMA200, 1H aligned, selling-exhaustion hook ──────
    if price > ema and htf_long_ok and _wr_exhaustion_long(df):
        if config.CANDLE_CONFIRM_FILTER and price <= prev_high:
            return _no_signal(adx=adx)   # candle hasn't broken above prev high yet
        if config.RESISTANCE_FILTER:
            recent_high = float(df["high"].iloc[-22:-2].max())
            if price > recent_high - config.RESISTANCE_ATR_MULT * atr:
                return _no_signal(adx=adx)   # too close to resistance
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
        if config.CANDLE_CONFIRM_FILTER and price >= prev_low:
            return _no_signal(adx=adx)   # candle hasn't broken below prev low yet
        if config.RESISTANCE_FILTER:
            recent_low = float(df["low"].iloc[-22:-2].min())
            if price < recent_low + config.RESISTANCE_ATR_MULT * atr:
                return _no_signal(adx=adx)   # too close to support
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


def get_regime(candles_1d, symbol=None):
    """
    Classify the current daily trend as 'bull', 'bear', or 'neutral'.

      bull    = daily close > EMA × (1 + REGIME_NEUTRAL_PCT)  → take longs only
      bear    = daily close < EMA × (1 - REGIME_NEUTRAL_PCT)  → take shorts only
      neutral = price within ±REGIME_NEUTRAL_PCT band         → take both

    Uses iloc[-2] (last fully-closed daily bar) to avoid look-ahead on the
    still-forming today candle.  Fails open ('neutral') on any error so the
    bot keeps trading rather than going silent on a data hiccup.
    """
    if not config.REGIME_FILTER:
        return "neutral"
    excluded = getattr(config, "REGIME_FILTER_EXCLUDED", [])
    if symbol and symbol in excluded:
        return "neutral"
    try:
        df = pd.DataFrame(
            candles_1d,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.iloc[::-1].reset_index(drop=True)   # oldest → newest
        df["close"] = df["close"].astype(float)
        n = config.REGIME_EMA_PERIOD
        if len(df) < n + 2:          # need at least n+1 bars + 1 closed bar
            return "neutral"
        ema_series = ta.trend.ema_indicator(df["close"], window=n)
        ema_val = float(ema_series.iloc[-2])   # last CLOSED daily bar
        price   = float(df["close"].iloc[-2])
        if pd.isna(ema_val):
            return "neutral"
        band = config.REGIME_NEUTRAL_PCT
        if price > ema_val * (1 + band):
            return "bull"
        if price < ema_val * (1 - band):
            return "bear"
        return "neutral"
    except Exception as exc:
        logger.warning("get_regime error: %s", exc)
        return "neutral"


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
        df["wr"]       = ta.momentum.williams_r(df["high"], df["low"], df["close"], lbp=14)
        df["atr"]      = ta.volatility.average_true_range(
                             df["high"], df["low"], df["close"], window=config.ATR_PERIOD)
        df["atr_ma"]   = df["atr"].rolling(config.ATR_MA_PERIOD).mean()
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        return df
    except Exception as exc:
        logger.error("_prepare_df error: %s", exc)
        return None


def _get_htf_ema(candles):
    """Return (latest_close, ema200) from any HTF candle list, or (None, None)."""
    try:
        df = pd.DataFrame(
            candles,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"],
        )
        df = df.iloc[::-1].reset_index(drop=True)
        df["close"] = df["close"].astype(float)
        df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)
        return float(df["close"].iloc[-1]), float(df["ema200"].iloc[-1])
    except Exception as exc:
        logger.warning("_get_htf_ema error: %s", exc)
        return None, None


def _get_1h_ema(candles_1h):
    """Return (latest_close, ema200) from 1-hour candles, or (None, None). Delegates to _get_htf_ema."""
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