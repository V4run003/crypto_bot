import time
import logging
import ctypes
import traceback
from datetime import datetime, timezone

import ntplib

import logger as log_module
log_module.setup()   # configure formatters before any other module logs

import config
import exchange
import risk
import strategy
import position_manager
import scanner
import telegram_bot

_logger = logging.getLogger("bot")

_btc_4h_regime = None   # last known BTC 4H regime — used to detect crossovers


def sync_clock():
    """
    Query pool.ntp.org and, if running with admin rights, correct the local
    system clock. Either way, log the offset so issues are visible.
    """
    try:
        c      = ntplib.NTPClient()
        resp   = c.request("pool.ntp.org", version=3)
        offset = resp.offset
        _logger.info("NTP clock offset: %.3f s", offset)
        if abs(offset) < 1:
            _logger.info("Clock is in sync — no correction needed.")
            return
        if ctypes.windll.shell32.IsUserAnAdmin():
            import subprocess
            subprocess.run(["w32tm", "/resync", "/force"],
                           capture_output=True, check=False)
            _logger.info("System clock synced via w32tm.")
        else:
            _logger.warning(
                "Clock is %.1f s off. Run PowerShell as Admin: "
                "w32tm /resync /force", offset
            )
    except Exception as exc:
        _logger.warning("NTP sync skipped: %s", exc)


def seconds_to_next_candle() -> float:
    """
    Seconds until the next 5-minute candle close + CANDLE_BUFFER_SECS.
    E.g. at 12:03:30 UTC → 90 s until 12:05:00 + 3 s buffer = 93 s.
    """
    now     = datetime.now(timezone.utc)
    elapsed = (now.minute % 5) * 60 + now.second + now.microsecond / 1_000_000
    return max((5 * 60) - elapsed + config.CANDLE_BUFFER_SECS, 1.0)


def main():
    mode = "TESTNET" if config.TESTNET else ("DEMO" if config.DEMO else "LIVE")
    _logger.info("=" * 60)
    _logger.info("Bybit Crypto Trading Bot  |  %s", mode)
    _logger.info("=" * 60)

    # ── Backtest mode — runs and exits; no live trading ───────────────────────
    if config.BACKTEST:
        _logger.info("BACKTEST mode enabled — live trading disabled.")
        import backtest as bt
        for sym in config.BACKTEST_SYMBOLS:
            bt.run_backtest(sym, config.BACKTEST_DAYS, quiet=config.BACKTEST_QUIET)
        return

    telegram_bot.start()
    sync_clock()

    try:
        balance = exchange.get_wallet_balance()
        _logger.info("Wallet balance: $%.2f USDT", balance)
    except Exception as exc:
        _logger.error("Failed to connect to exchange: %s", exc)
        telegram_bot.notify_error("Connection Failed on Startup", str(exc))
        telegram_bot.stop()
        return

    # Restore today's realized PnL so daily limits survive restarts
    today_pnl = exchange.get_today_pnl()
    if today_pnl != 0:
        risk.update_pnl(today_pnl)
        _logger.info("Loaded today's realized PnL: $%.2f", today_pnl)

    # Sync with any positions open before this restart
    position_manager.init_from_exchange()
    risk.update_peak_balance()   # establish baseline peak for trailing DD guard

    s = risk.get_stats()
    target_balance = config.ACCOUNT_SIZE + config.PROFIT_TARGET
    profit_needed  = max(0.0, target_balance - balance)
    telegram_bot.notify_bot_started(
        balance=balance, peak=s["peak_balance"],
        dd_left=s["trailing_dd_left"], profit_needed=profit_needed,
    )

    _logger.info(
        "Bot running — syncing to 5m candle closes | "
        "risk $%s/trade | max loss $%s/day | cooldown %dm",
        config.RISK_PER_TRADE, config.MAX_DAILY_LOSS,
        config.TRADE_COOLDOWN_SECS // 60,
    )

    # Track UTC date so we can fire the daily report at midnight rollover.
    _last_report_day = datetime.now(timezone.utc).date()

    # ── Signal drought tracking ───────────────────────────────────────────────
    _last_signal_time  = None   # set whenever a trade is opened
    _drought_alerted   = False  # reset when a new trade fires

    while True:
        wait = seconds_to_next_candle()
        _logger.info("Next scan in %.1f s (waiting for candle close)", wait)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            _logger.info("Bot stopped by user.")
            telegram_bot.notify_bot_stopped("User stopped bot (Ctrl+C)")
            telegram_bot.stop()
            break

        try:
            now_utc = datetime.now(timezone.utc)

            # ── Midnight daily report ─────────────────────────────────────────
            today = now_utc.date()
            if today != _last_report_day:
                _send_daily_report(_last_report_day)
                _last_report_day = today

            utc_time = now_utc.strftime("%H:%M:%S UTC")
            s = risk.get_stats()
            _logger.info(
                "─── [%s]  trades=%d/%d  daily_pnl=$%.2f  trailing_dd_left=$%.2f  cooldown=%.0fs ───",
                utc_time, s["trade_count"], config.MAX_TRADES_PER_DAY,
                s["daily_loss"], s["trailing_dd_left"], s["cooldown_secs"],
            )
            trade_opened = scanner.scan()
            if trade_opened:
                _last_signal_time = now_utc
                _drought_alerted  = False
            _check_btc_4h_regime()

            # ── Signal drought alert ──────────────────────────────────────────
            drought_hours = getattr(config, "DROUGHT_ALERT_HOURS", 6)
            if drought_hours > 0:
                hour_utc = now_utc.hour
                in_window = config.TIME_FILTER_START <= hour_utc < config.TIME_FILTER_END
                if in_window and _last_signal_time is not None and not _drought_alerted:
                    silent_h = (now_utc - _last_signal_time).total_seconds() / 3600
                    if silent_h >= drought_hours:
                        telegram_bot.notify_signal_drought(silent_h)
                        _drought_alerted = True

        except KeyboardInterrupt:
            _logger.info("Bot stopped by user.")
            telegram_bot.notify_bot_stopped("User stopped bot (Ctrl+C)")
            telegram_bot.stop()
            break
        except Exception as exc:
            detail = traceback.format_exc()
            _logger.error("Unhandled error: %s", exc, exc_info=True)
            telegram_bot.notify_error("Unhandled Bot Error", detail)


def _check_btc_4h_regime():
    """Fetch BTC 4H candles and send a Telegram alert if the regime has flipped."""
    global _btc_4h_regime
    if not getattr(config, "REGIME_ALERT_ENABLED", False):
        return
    try:
        candles_4h = exchange.get_candles_4h("BTCUSDT")
        regime = strategy.get_btc_4h_regime(candles_4h)
        if regime is None:
            return
        if _btc_4h_regime is not None and regime != _btc_4h_regime:
            telegram_bot.notify_regime_change(regime)
            _logger.info("BTC 4H regime changed: %s → %s", _btc_4h_regime, regime)
        _btc_4h_regime = regime
    except Exception as exc:
        _logger.warning("BTC 4H regime check failed: %s", exc)


def _send_daily_report(report_day):
    """Send Telegram daily summary for `report_day` (a datetime.date)."""
    try:
        s = risk.get_stats()
        try:
            balance = exchange.get_wallet_balance()
        except Exception:
            balance = 0.0
        date_str       = report_day.strftime("%a %d %b %Y")
        target_balance = config.ACCOUNT_SIZE + config.PROFIT_TARGET
        profit_needed  = max(0.0, target_balance - balance)
        telegram_bot.notify_daily_report(
            date_str=date_str,
            trade_count=s["trade_count"],
            wins=s["daily_wins"],
            losses=s["daily_losses"],
            daily_pnl=s["daily_loss"],
            balance=balance,
            peak=s["peak_balance"],
            dd_left=s["trailing_dd_left"],
            profit_needed=profit_needed,
        )
    except Exception as exc:
        _logger.warning("Failed to send daily report notification: %s", exc)


if __name__ == "__main__":
    main()