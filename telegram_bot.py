"""
Personal Telegram Bot — sends rich signal notifications directly to you.

Uses the standard Telegram Bot API (HTTP), NOT Telethon MTProto.
Much simpler setup: just create a bot via @BotFather, get the token, and your chat_id.

Setup:
  1. Message @BotFather on Telegram → /newbot → get BOT_TOKEN
  2. Message @userinfobot → get your CHAT_ID
  3. Set BOT_TOKEN and BOT_CHAT_ID in .env

Can run alongside the Based Bot sender (dual output) or replace it entirely.
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

import config

logger = logging.getLogger("tg_bot")


class SignalBot:
    """
    Personal Telegram Bot that sends formatted signal alerts.
    Consumes from the shared signal_queue (same as TelegramSender).
    """

    def __init__(self, signal_queue: asyncio.Queue, state_tracker=None, sol_state_tracker=None):
        self.signal_queue = signal_queue
        self.tracker = state_tracker
        self.sol_tracker = sol_state_tracker
        self._session: Optional[aiohttp.ClientSession] = None
        self._bot_token = config.BOT_TOKEN
        self._chat_id = config.BOT_CHAT_ID
        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    async def start(self):
        """Verify bot token, then start consuming signals."""
        if not self._bot_token or not self._chat_id:
            logger.warning(
                "BOT_TOKEN or BOT_CHAT_ID not set — personal bot disabled. "
                "Create a bot via @BotFather and set BOT_TOKEN + BOT_CHAT_ID in .env"
            )
            return

        await self._ensure_session()

        # Verify bot token works
        try:
            async with self._session.get(f"{self._api_base}/getMe") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    bot_name = data.get("result", {}).get("username", "unknown")
                    logger.info(f"Personal TG bot connected: @{bot_name}")
                else:
                    logger.error(f"Bot token invalid (HTTP {resp.status}). Check BOT_TOKEN in .env")
                    return
        except Exception as e:
            logger.error(f"Failed to connect bot: {e}")
            return

        # Send startup message
        await self._send_message(
            "🟢 <b>Signal Detector Online</b>\n\n"
            f"Chain: Base (8453){' + Solana' if config.SOL_ENABLED else ''}\n"
            f"Mode: {'DRY RUN' if config.DRY_RUN else '🔴 LIVE'}\n"
            f"Max age: {config.MAX_TOKEN_AGE_SECONDS}s\n"
            f"Max mcap: ${config.MAX_MCAP_USD:,.0f}\n"
            f"Min liq: ${config.MIN_LIQUIDITY_USD:,.0f}"
        )

        # Consume signals
        await self._send_loop()

    async def _send_loop(self):
        """Consume from signal queue and send formatted alerts."""
        while True:
            try:
                contract_address = await self.signal_queue.get()
                await self._send_signal(contract_address)
                self.signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Bot send loop error: {e}")
                await asyncio.sleep(1)

    async def _send_signal(self, contract_address: str):
        """Build and send a rich signal message."""
        # Look up state from tracker for extra context
        state = None
        chain = "base"

        if self.tracker:
            state = self.tracker.get(contract_address)
        if state is None and self.sol_tracker:
            state = self.sol_tracker.get(contract_address)
            if state:
                chain = "solana"

        if state:
            age = state.age_seconds
            mcap = state.best_mcap
            liq = state.best_liquidity
            buys = state.best_buys
            sells = state.total_sells
            volume = state.buy_volume_usd
            largest = state.largest_buy_usd
            largest_pct = (largest / liq * 100) if liq > 0 else 0
            unique = len(state.unique_buyers)
            momentum = state.has_momentum()
            dex_ver = state.dex_version
            latency = time.time() - state.first_seen

            # Chain-specific explorer links
            if chain == "solana":
                explorer_link = f"https://solscan.io/token/{contract_address}"
                ds_link = f"https://dexscreener.com/solana/{contract_address}"
                chain_emoji = "🟣"
                chain_name = "Solana"
            else:
                explorer_link = f"https://basescan.org/token/{contract_address}"
                ds_link = f"https://dexscreener.com/base/{contract_address}"
                chain_emoji = "🔵"
                chain_name = "Base"

            message = (
                f"🎯 <b>SIGNAL DETECTED</b> {chain_emoji} {chain_name}\n"
                f"{'━' * 28}\n\n"
                f"📋 <b>CA:</b>\n<code>{contract_address}</code>\n\n"
                f"📊 <b>Metrics</b>\n"
                f"├ Mcap: <b>${mcap:,.0f}</b>\n"
                f"├ Liquidity: <b>${liq:,.0f}</b>\n"
                f"├ Buys: <b>{buys}</b> (unique: {unique}) | Sells: {sells}\n"
                f"├ Volume: ${volume:,.0f}\n"
                f"├ Largest buy: ${largest:,.0f} ({largest_pct:.0f}% of liq)\n"
                f"├ Momentum: {'✅ YES' if momentum else '❌ no'}\n"
                f"├ Age: {age:.0f}s | Latency: {latency:.0f}s\n"
                f"└ DEX: {dex_ver}\n\n"
                f"🔗 <a href=\"{ds_link}\">DexScreener</a> · "
                f"<a href=\"{explorer_link}\">Explorer</a>"
            )
        else:
            # Minimal message if state was already evicted
            message = (
                f"🎯 <b>SIGNAL DETECTED</b>\n\n"
                f"<code>{contract_address}</code>\n\n"
                f"<a href=\"https://dexscreener.com/base/{contract_address}\">DexScreener</a>"
            )

        await self._send_message(message)
        logger.info(f"[bot] Signal sent to chat {self._chat_id}: {contract_address[:16]}...")

    async def _send_message(self, text: str, disable_preview: bool = True):
        """Send a message via Telegram Bot API."""
        await self._ensure_session()
        try:
            async with self._session.post(
                f"{self._api_base}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": disable_preview,
                },
            ) as resp:
                if resp.status != 200:
                    data = await resp.json()
                    logger.error(f"Bot send failed: {data}")
        except Exception as e:
            logger.error(f"Bot send error: {e}")

    async def send_post_mortem(self, record: dict):
        """Send a post-mortem follow-up notification."""
        if not self._bot_token or not self._chat_id:
            return

        token = record["token"]
        outcome = record.get("outcome", "UNKNOWN")
        change = record.get("price_change_pct", 0)
        mcap_signal = record.get("mcap_at_signal", 0)
        mcap_10m = record.get("mcap_10m", 0)
        latency = record.get("latency_s", 0)

        emoji_map = {
            "TP_HIT": "🟢", "IMPULSE": "📈", "FLAT": "➖",
            "DUMP": "📉", "RUG": "🔴", "CHOP": "↔️",
        }
        emoji = emoji_map.get(outcome, "❓")

        message = (
            f"{emoji} <b>POST-MORTEM: {outcome}</b>\n\n"
            f"<code>{token[:20]}...</code>\n"
            f"├ Mcap at signal: ${mcap_signal:,.0f}\n"
            f"├ Mcap at 10m: ${mcap_10m:,.0f}\n"
            f"├ Change: <b>{change:+.1f}%</b>\n"
            f"└ Latency: {latency:.0f}s"
        )
        await self._send_message(message)

    async def stop(self):
        """Send offline message and close session."""
        if self._bot_token and self._chat_id:
            try:
                await self._send_message("🔴 <b>Signal Detector Offline</b>")
            except Exception:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
