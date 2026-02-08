"""
Telegram sender via Telethon (MTProto, user account).
Sends raw contract addresses to Based Bot.
NOT Telegram Bot API — this uses your personal account, looks like manual usage.
"""
import asyncio
import logging
import random

import config

logger = logging.getLogger("telegram")

# Telethon is optional — allow dry-run without it installed
try:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError

    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    logger.warning("telethon not installed — Telegram sends disabled (dry-run only)")


class TelegramSender:
    """
    Consumes contract addresses from signal_queue and sends to Based Bot.
    Uses Telethon MTProto client (user account, NOT bot token).
    """

    def __init__(self, signal_queue: asyncio.Queue):
        self.signal_queue = signal_queue
        self._client = None
        self._connected = False

    async def start(self):
        """Initialize Telethon client and start consuming signals."""
        if config.DRY_RUN:
            logger.info("DRY RUN mode — signals will be logged, not sent to Telegram")
            await self._dry_run_loop()
            return

        if not TELETHON_AVAILABLE:
            logger.error("telethon not installed. Install with: pip install telethon")
            await self._dry_run_loop()
            return

        if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
            logger.error("TELEGRAM_API_ID and TELEGRAM_API_HASH required. Get from https://my.telegram.org")
            await self._dry_run_loop()
            return

        # Connect Telethon client
        self._client = TelegramClient(
            config.TELEGRAM_SESSION_NAME,
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )

        await self._client.start()
        self._connected = True
        me = await self._client.get_me()
        logger.info(f"Telegram connected as: {me.first_name} (@{me.username})")

        # Start send loop
        await self._send_loop()

    async def _send_loop(self):
        """Consume from signal queue and send to Based Bot."""
        while True:
            try:
                contract_address = await self.signal_queue.get()
                await self._send_to_based_bot(contract_address)
                self.signal_queue.task_done()
            except Exception as e:
                logger.error(f"Telegram send loop error: {e}")
                await asyncio.sleep(1)

    async def _send_to_based_bot(self, contract_address: str):
        """Send the raw contract address to Based Bot."""
        if not self._connected or not self._client:
            logger.warning(f"[telegram-offline] Would send: {contract_address}")
            return

        try:
            # Random delay 500-800ms between sends (anti-spam)
            delay = random.uniform(0.5, 0.8)
            await asyncio.sleep(delay)

            # Send just the contract address — no /buy, no commands
            await self._client.send_message(
                config.BASED_BOT_USERNAME,
                contract_address,
            )
            logger.info(f"[sent] {contract_address} → @{config.BASED_BOT_USERNAME}")

        except FloodWaitError as e:
            logger.warning(f"Telegram flood wait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            logger.error(f"Failed to send to Telegram: {e}")

    async def _dry_run_loop(self):
        """Consume signals and just log them (no Telegram)."""
        while True:
            try:
                contract_address = await self.signal_queue.get()
                logger.info(
                    f"[DRY RUN] Signal: {contract_address} "
                    f"(would send to @{config.BASED_BOT_USERNAME})"
                )
                print(f"\n{'*'*50}")
                print(f"  DRY RUN SIGNAL: {contract_address}")
                print(f"{'*'*50}\n")
                self.signal_queue.task_done()
            except Exception as e:
                logger.error(f"Dry run loop error: {e}")
                await asyncio.sleep(1)

    async def stop(self):
        if self._client:
            await self._client.disconnect()
            logger.info("Telegram client disconnected")
