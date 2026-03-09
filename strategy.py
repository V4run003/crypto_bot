import logging

import pandas as pd
import ta

import config

logger = logging.getLogger(__name__)


def check_signal(candles):
    """
    Evaluate the latest candles against all strategy rules.

    Returns a dict:
        {signal: "long"|"short"|None, entry, sl, tp, adx, atr}
    """
    df = _prepare_df(candles)
    if df is None:
        return _no_signal()

    row    = df.iloc[-1]
    price  = row["close"]
    ema    = row["ema200"]
    adx    = row["adx"]
    wr     = row["wr"]
    atr    = row["atr"]
    atr_ma = row["atr_ma"]

    if any(pd.isna(v) for v in [price, ema, adx, wr, atr, atr_ma]):
        return _no_signal()

    logger.debug(
        "price=%.4f  ema=%.4f  adx=%.2f  wr=%.2f  atr=%.6f  atr_ma=%.6f",
        price, ema, adx, wr, atr, atr_ma,
    )

    # Volatility filter — skip flat / low-volatility markets
    if atr <= atr_ma:
        return _no_signal(adx=adx)

    # Trend-strength filter
    if adx <= 20:
        return _no_signal(adx=adx)

    # Long: price above EMA 200, Williams %R selling-exhaustion hook
    if price > ema and _wr_exhaustion_long(df):
        swing_low = float(df["low"].iloc[-config.SWING_LOOKBACK:].min())
        sl        = swing_low * (1 - config.SL_BUFFER_PCT)
        sl_dist   = price - sl
        if sl_dist <= 0:
            return _no_signal(adx=adx)
        tp = price + sl_dist * config.RR
        return {"signal": "long",  "entry": price, "sl": sl, "tp": tp,
                "adx": adx, "atr": atr}

    # Short: price below EMA 200, Williams %R buying-exhaustion hook
    if price < ema and _wr_exhaustion_short(df):
        swing_high = float(df["high"].iloc[-config.SWING_LOOKBACK:].max())
        sl         = swing_high * (1 + config.SL_BUFFER_PCT)
        sl_dist    = sl - price
        if sl_dist <= 0:
            return _no_signal(adx=adx)
        tp = price - sl_dist * config.RR
        return {"signal": "short", "entry": price, "sl": sl, "tp": tp,
                "adx": adx, "atr": atr}

    return _no_signal(adx=adx)


def check_adx(candles):
    """Return the latest ADX value for active trade-management checks."""
    df = _prepare_df(candles)
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


def _no_signal(adx=None):
    return {"signal": None, "entry": None, "sl": None, "tp": None,
            "adx": adx, "atr": None}


def _wr_exhaustion_long(df):
    """
    Selling exhaustion (long hook):
    - The previous WR_EXHAUSTION_LOOKBACK candles were ALL below -80 (deep oversold).
    - The current candle's WR has crossed back *above* -80 (bears exhausted).

    This fires only ONCE per exhaustion episode — on the first bar that exits
    the oversold zone — giving a precise, low-noise entry signal.
    """
    wr       = df["wr"]
    lookback = config.WR_EXHAUSTION_LOOKBACK
    # Current bar must have already exited the oversold zone
    if wr.iloc[-1] <= -80:
        return False
    # All preceding N bars must have been inside the oversold zone
    prev = wr.iloc[-(lookback + 1):-1]
    return (prev < -80).sum() >= lookback


def _wr_exhaustion_short(df):
    """
    Buying exhaustion (short hook):
    - The previous WR_EXHAUSTION_LOOKBACK candles were ALL above -20 (deep overbought).
    - The current candle's WR has crossed back *below* -20 (bulls exhausted).
    """
    wr       = df["wr"]
    lookback = config.WR_EXHAUSTION_LOOKBACK
    # Current bar must have already exited the overbought zone
    if wr.iloc[-1] >= -20:
        return False
    # All preceding N bars must have been inside the overbought zone
    prev = wr.iloc[-(lookback + 1):-1]
    return (prev > -20).sum() >= lookback