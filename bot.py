import time
import logging
import ctypes
from datetime import datetime, timezone

import ntplib

import logger as log_module
log_module.setup()   # configure formatters before any other module logs

import config
import exchange
import risk
import position_manager
import scanner

_logger = logging.getLogger("bot")


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

    sync_clock()

    try:
        balance = exchange.get_wallet_balance()
        _logger.info("Wallet balance: $%.2f USDT", balance)
    except Exception as exc:
        _logger.error("Failed to connect to exchange: %s", exc)
        return

    # Restore today's realized PnL so daily limits survive restarts
    today_pnl = exchange.get_today_pnl()
    if today_pnl != 0:
        risk.update_pnl(today_pnl)
        _logger.info("Loaded today's realized PnL: $%.2f", today_pnl)

    # Sync with any positions open before this restart
    position_manager.init_from_exchange()

    _logger.info(
        "Bot running — syncing to 5m candle closes | "
        "risk $%s/trade | max loss $%s/day | cooldown %dm",
        config.RISK_PER_TRADE, config.MAX_DAILY_LOSS,
        config.TRADE_COOLDOWN_SECS // 60,
    )

    while True:
        wait = seconds_to_next_candle()
        _logger.info("Next scan in %.1f s (waiting for candle close)", wait)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            _logger.info("Bot stopped by user.")
            break

        try:
            utc_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            s = risk.get_stats()
            _logger.info(
                "─── [%s]  trades=%d/%d  daily_pnl=$%.2f  cooldown=%.0fs ───",
                utc_time, s["trade_count"], config.MAX_TRADES_PER_DAY,
                s["daily_loss"], s["cooldown_secs"],
            )
            scanner.scan()

        except KeyboardInterrupt:
            _logger.info("Bot stopped by user.")
            break
        except Exception as exc:
            _logger.error("Unhandled error: %s", exc, exc_info=True)


if __name__ == "__main__":
    main()