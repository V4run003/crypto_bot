import time
import logging
import ctypes
from datetime import datetime, timezone

import ntplib

import config
import exchange
import risk
import scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot")


def sync_clock():
    """
    Query pool.ntp.org and, if running with admin rights, correct the local
    system clock. Either way, log the offset so issues are visible.
    """
    try:
        c      = ntplib.NTPClient()
        resp   = c.request("pool.ntp.org", version=3)
        offset = resp.offset          # seconds local is ahead (+) or behind (-)
        logger.info("NTP clock offset: %.3f s", offset)
        if abs(offset) < 1:
            logger.info("Clock is in sync — no correction needed.")
            return
        # Try to correct the system time (requires admin; silently skipped if not)
        if ctypes.windll.shell32.IsUserAnAdmin():
            import subprocess
            subprocess.run(["w32tm", "/resync", "/force"],
                           capture_output=True, check=False)
            logger.info("System clock synced via w32tm.")
        else:
            logger.warning(
                "Clock is %.1f s off. Run PowerShell as Admin and execute: "
                "w32tm /resync /force", offset
            )
    except Exception as exc:
        logger.warning("NTP sync skipped: %s", exc)


def main():
    mode = "TESTNET" if config.TESTNET else ("DEMO" if config.DEMO else "LIVE")
    logger.info("=" * 60)
    logger.info("Bybit Crypto Trading Bot  |  %s", mode)
    logger.info("=" * 60)
    sync_clock()

    try:
        balance = exchange.get_wallet_balance()
        logger.info("Wallet balance: $%.2f USDT", balance)
    except Exception as exc:
        logger.error("Failed to connect to exchange: %s", exc)
        return

    # Restore today's realized PnL so daily limits survive restarts
    today_pnl = exchange.get_today_pnl()
    if today_pnl != 0:
        risk.update_pnl(today_pnl)
        logger.info("Loaded today's realized PnL: $%.2f", today_pnl)

    # Restore any position that was open before this restart
    scanner.init_from_exchange()

    logger.info(
        "Bot running — scanning every %ds  |  risk $%s/trade  |  max loss $%s/day",
        config.SCAN_INTERVAL, config.RISK_PER_TRADE, config.MAX_DAILY_LOSS,
    )

    while True:
        try:
            utc_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            s = risk.get_stats()
            logger.info(
                "─── [%s]  trades=%d/%d  daily_pnl=$%.2f ───",
                utc_time, s["trade_count"], config.MAX_TRADES_PER_DAY, s["daily_loss"],
            )
            scanner.scan()

        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as exc:
            logger.error("Unhandled error: %s", exc, exc_info=True)

        time.sleep(config.SCAN_INTERVAL)


if __name__ == "__main__":
    main()