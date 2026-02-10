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

    def __init__(self, signal_queue: asyncio.Queue, state_tracker=None, sol_state_tracker=None, whale_queue=None, pump_queue=None, discovery_queue=None):
        self.signal_queue = signal_queue
        self.whale_queue = whale_queue
        self.pump_queue = pump_queue  # kept for interface compatibility (unused without volume scanner)
        self.discovery_queue = discovery_queue
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

        # Consume signals + whale alerts + pump alerts + discovery feed
        tasks = [asyncio.create_task(self._send_loop())]
        if self.whale_queue:
            tasks.append(asyncio.create_task(self._whale_loop()))
        if self.pump_queue:
            tasks.append(asyncio.create_task(self._pump_loop()))
        if self.discovery_queue:
            tasks.append(asyncio.create_task(self._discovery_loop()))
        await asyncio.gather(*tasks)

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
        """Build and send a rich signal message with inline keyboard buttons."""
        # Look up state from tracker for extra context
        state = None
        chain = "base"

        if self.tracker:
            state = self.tracker.get(contract_address)
        if state is None and self.sol_tracker:
            state = self.sol_tracker.get(contract_address)
            if state:
                chain = "solana"

        # Chain-specific links
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

        # Inline keyboard: one-tap DexScreener, Explorer, copy-ready CA
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 DexScreener", "url": ds_link},
                    {"text": "🔍 Explorer", "url": explorer_link},
                ],
            ]
        }

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

            # Header: show name + symbol if available
            if state.token_name and state.token_symbol:
                name_tag = f" {state.token_name} (${state.token_symbol})"
            elif state.token_symbol:
                name_tag = f" ${state.token_symbol}"
            elif state.token_name:
                name_tag = f" {state.token_name}"
            else:
                name_tag = ""
            social_tag = "" if state.has_socials else " ⚠️no-socials"

            message = (
                f"🎯 <b>SIGNAL</b> {chain_emoji} {chain_name}{name_tag}{social_tag}\n"
                f"{'━' * 28}\n\n"
                f"<code>{contract_address}</code>\n\n"
                f"├ Mcap: <b>${mcap:,.0f}</b>\n"
                f"├ Liq: <b>${liq:,.0f}</b>\n"
                f"├ Buys: <b>{buys}</b> ({unique} unique) · Sells: {sells}\n"
                f"├ Vol: ${volume:,.0f}\n"
                f"├ Top buy: ${largest:,.0f} ({largest_pct:.0f}%)\n"
                f"├ Momentum: {'✅' if momentum else '❌'}\n"
                f"└ {dex_ver} · {age:.0f}s · latency {latency:.0f}s"
            )
        else:
            # Minimal message if state was already evicted
            message = (
                f"🎯 <b>SIGNAL</b> {chain_emoji} {chain_name}\n\n"
                f"<code>{contract_address}</code>"
            )

        await self._send_message(message, reply_markup=keyboard)
        logger.info(f"[bot] Signal sent to chat {self._chat_id}: {contract_address[:16]}...")

    async def _whale_loop(self):
        """Consume whale alert events and send notifications."""
        # Debounce: max 1 whale alert per token per 30s
        _last_alert: dict[str, float] = {}
        while True:
            try:
                event = await self.whale_queue.get()
                token = event["token"]
                now = time.time()
                # Debounce
                if token in _last_alert and now - _last_alert[token] < 30:
                    self.whale_queue.task_done()
                    continue
                _last_alert[token] = now
                # Prune old entries
                _last_alert = {k: v for k, v in _last_alert.items() if now - v < 60}
                await self._send_whale_alert(event)
                self.whale_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Whale loop error: {e}")
                await asyncio.sleep(1)

    async def _send_whale_alert(self, event: dict):
        """Send a whale buy/sell alert."""
        token = event["token"]
        chain = event.get("chain", "base")
        is_buy = event["is_buy"]
        usd = event["usd"]
        sender = event.get("sender", "?")
        symbol = event.get("symbol", "")

        emoji = "🐋💚" if is_buy else "🐋🔴"
        action = "BUY" if is_buy else "SELL"
        name = f" ${symbol}" if symbol else ""

        if chain == "solana":
            ds_link = f"https://dexscreener.com/solana/{token}"
            explorer_link = f"https://solscan.io/token/{token}"
        else:
            ds_link = f"https://dexscreener.com/base/{token}"
            explorer_link = f"https://basescan.org/token/{token}"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 DexScreener", "url": ds_link},
                    {"text": "🔍 Explorer", "url": explorer_link},
                ],
            ]
        }

        message = (
            f"{emoji} <b>WHALE {action}</b>{name}\n"
            f"{'━' * 28}\n\n"
            f"<code>{token}</code>\n\n"
            f"├ Amount: <b>${usd:,.0f}</b>\n"
            f"└ Wallet: {sender[:8]}..{sender[-4:]}"
        )
        await self._send_message(message, reply_markup=keyboard)

    async def _send_message(
        self,
        text: str,
        disable_preview: bool = True,
        reply_markup: dict | None = None,
    ):
        """Send a message via Telegram Bot API."""
        await self._ensure_session()
        try:
            payload: dict = {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            async with self._session.post(
                f"{self._api_base}/sendMessage",
                json=payload,
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
        chain = record.get("chain", "base")
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

        if chain == "solana":
            ds_link = f"https://dexscreener.com/solana/{token}"
        else:
            ds_link = f"https://dexscreener.com/base/{token}"

        keyboard = {
            "inline_keyboard": [
                [{"text": "📊 DexScreener", "url": ds_link}],
            ]
        }

        message = (
            f"{emoji} <b>POST-MORTEM: {outcome}</b>\n\n"
            f"<code>{token[:20]}...</code>\n"
            f"├ Mcap at signal: ${mcap_signal:,.0f}\n"
            f"├ Mcap at 10m: ${mcap_10m:,.0f}\n"
            f"├ Change: <b>{change:+.1f}%</b>\n"
            f"└ Latency: {latency:.0f}s"
        )
        await self._send_message(message, reply_markup=keyboard)

    async def send_dump_alert(
        self,
        token_address: str,
        chain: str,
        sells_60s: int,
        total_sells: int,
        total_buys: int,
        mcap: float,
        liq: float,
    ):
        """Send urgent dump/mass-sell alert for a signaled token."""
        if not self._bot_token or not self._chat_id:
            return

        if chain == "solana":
            ds_link = f"https://dexscreener.com/solana/{token_address}"
            explorer_link = f"https://solscan.io/token/{token_address}"
            chain_emoji = "🟣"
        else:
            ds_link = f"https://dexscreener.com/base/{token_address}"
            explorer_link = f"https://basescan.org/token/{token_address}"
            chain_emoji = "🔵"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 DexScreener", "url": ds_link},
                    {"text": "🔍 Explorer", "url": explorer_link},
                ],
            ]
        }

        sell_buy_ratio = f"{total_sells}/{total_buys}"

        message = (
            f"🚨 <b>DUMP ALERT</b> {chain_emoji}\n"
            f"{'━' * 28}\n\n"
            f"<code>{token_address}</code>\n\n"
            f"├ Sells in 60s: <b>{sells_60s}</b>\n"
            f"├ Total S/B: <b>{sell_buy_ratio}</b>\n"
            f"├ Mcap: ${mcap:,.0f}\n"
            f"└ Liq: ${liq:,.0f}"
        )
        await self._send_message(message, reply_markup=keyboard)

    async def _pump_loop(self):
        """Consume volume spike alerts and send formatted notifications."""
        while True:
            try:
                alert = await self.pump_queue.get()
                await self._send_pump_alert(alert)
                self.pump_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Pump loop error: {e}")
                await asyncio.sleep(1)

    async def _send_pump_alert(self, alert: dict):
        """Send a volume spike / pump detection alert."""
        token = alert.get("token", "?")
        symbol = alert.get("symbol", "")
        name = alert.get("name", "")
        mcap = alert.get("mcap", 0)
        liq = alert.get("liq", 0)
        volume_h1 = alert.get("volume_h1", 0)
        price_change_m5 = alert.get("price_change_m5", 0)
        price_change_h1 = alert.get("price_change_h1", 0)
        swaps = alert.get("swaps_2m", 0)
        has_socials = alert.get("has_socials", False)
        age_hours = alert.get("age_hours", 0)
        chain = alert.get("chain", "base")
        signal_mcap = alert.get("signal_mcap")

        # Age formatting
        if age_hours < 1:
            age_str = f"{age_hours * 60:.0f}m"
        elif age_hours < 24:
            age_str = f"{age_hours:.0f}h"
        else:
            age_str = f"{age_hours / 24:.0f}d"

        # Price direction emoji
        if price_change_m5 > 10:
            trend = "📈🔥"
        elif price_change_m5 > 0:
            trend = "📈"
        elif price_change_m5 < -10:
            trend = "📉"
        else:
            trend = "➡️"

        # Header: show name + symbol
        if name and symbol:
            name_tag = f" {name} (${symbol})"
        elif symbol:
            name_tag = f" ${symbol}"
        elif name:
            name_tag = f" {name}"
        else:
            name_tag = ""
        social_tag = " ✅" if has_socials else " ⚠️no-socials"

        if chain == "solana":
            ds_link = f"https://dexscreener.com/solana/{token}"
            explorer_link = f"https://solscan.io/token/{token}"
        else:
            ds_link = f"https://dexscreener.com/base/{token}"
            explorer_link = f"https://basescan.org/token/{token}"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 DexScreener", "url": ds_link},
                    {"text": "🔍 Explorer", "url": explorer_link},
                ],
            ]
        }

        # Build signal history line if bot previously caught this token
        signal_line = ""
        if signal_mcap is not None:
            multiplier = mcap / signal_mcap if signal_mcap > 0 else 0
            signal_line = f"\n├ 📍 Bot signaled at ${signal_mcap:,.0f} (<b>{multiplier:.1f}x</b> since)"

        message = (
            f"{trend} <b>PUMP DETECTED</b>{name_tag}{social_tag}\n"
            f"{'━' * 28}\n\n"
            f"<code>{token}</code>\n\n"
            f"├ Mcap: <b>${mcap:,.0f}</b>\n"
            f"├ Liq: ${liq:,.0f}\n"
            f"├ Vol 1h: ${volume_h1:,.0f}\n"
            f"├ Δ5m: <b>{price_change_m5:+.1f}%</b> · Δ1h: {price_change_h1:+.1f}%\n"
            f"├ Swaps/2m: <b>{swaps}</b>\n"
            f"└ Age: {age_str}"
            f"{signal_line}"
        )
        await self._send_message(message, reply_markup=keyboard)

    # ── Discovery Feed ──────────────────────────────────────────

    async def _discovery_loop(self):
        """Consume new-pair discoveries, delay for enrichment, then send.
        Waits ~15s after pool creation for DexScreener to index the pair,
        giving us token name, symbol, mcap, liquidity, and socials.
        Rate limited: min 5s between messages, max DISCOVERY_MAX_PER_HOUR/hr."""
        _timestamps: list[float] = []
        _last_send: float = 0.0
        MIN_INTERVAL = 5  # seconds between discovery messages
        MAX_PER_HOUR = config.DISCOVERY_MAX_PER_HOUR
        ENRICHMENT_DELAY = 15  # seconds to wait for DexScreener data
        MIN_DISCOVERY_MCAP = 500  # skip dust/dead pairs (no real buys)

        # Buffer: collect events, process after delay
        pending: list[tuple[float, dict]] = []  # (arrival_time, event)

        while True:
            try:
                # Drain queue without blocking — collect new events
                while not self.discovery_queue.empty():
                    try:
                        event = self.discovery_queue.get_nowait()
                        pending.append((time.time(), event))
                        self.discovery_queue.task_done()
                    except Exception:
                        break

                # Process events that have aged past the enrichment delay
                now = time.time()
                ready = [p for p in pending if now - p[0] >= ENRICHMENT_DELAY]
                pending = [p for p in pending if now - p[0] < ENRICHMENT_DELAY]

                for arrival_time, event in ready:
                    # Rate limit: prune old timestamps, check hourly cap
                    _timestamps = [t for t in _timestamps if now - t < 3600]
                    if len(_timestamps) >= MAX_PER_HOUR:
                        continue

                    # Min interval between messages
                    wait = MIN_INTERVAL - (now - _last_send)
                    if wait > 0:
                        await asyncio.sleep(wait)

                    # Check if token has enough data now
                    token = event.get("token", "")
                    state = self.tracker.get(token) if self.tracker else None
                    mcap = state.best_mcap if state else 0

                    # Skip dust/dead pairs — if after 15s still no meaningful mcap, drop it
                    if mcap < MIN_DISCOVERY_MCAP:
                        continue

                    await self._send_discovery(event)
                    _last_send = time.time()
                    now = _last_send
                    _timestamps.append(_last_send)

                # Cap pending buffer to prevent memory growth
                if len(pending) > 200:
                    pending = pending[-200:]

                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Discovery loop error: {e}")
                await asyncio.sleep(1)

    async def _send_discovery(self, event: dict):
        """Send an enriched new-pair discovery notification."""
        token = event.get("token", "?")
        pool = event.get("pool", "")
        dex = event.get("dex", "?")
        fee = event.get("fee")
        hooks = event.get("hooks")

        # Look up enriched state
        state = self.tracker.get(token) if self.tracker else None
        mcap = state.best_mcap if state else 0
        liq = state.best_liquidity if state else 0
        name = state.token_name if state and state.token_name else ""
        symbol = state.token_symbol if state and state.token_symbol else ""
        buys = state.best_buys if state else 0
        sells = state.total_sells if state else 0
        has_socials = state.has_socials if state else False
        bytecode_safe = state.bytecode_safe if state else None
        unique_buyers = len(state.unique_buyers) if state else 0
        age_s = state.age_seconds if state else 0

        # Header with name
        if name and symbol:
            title = f" {name} (${symbol})"
        elif symbol:
            title = f" ${symbol}"
        elif name:
            title = f" {name}"
        else:
            title = ""

        # Safety indicator
        if bytecode_safe is True:
            safety_tag = "✅"
        elif bytecode_safe is False:
            safety_tag = "⛔"
        else:
            safety_tag = "❓"

        # Socials indicator
        social_tag = "🌐" if has_socials else ""

        ds_link = f"https://dexscreener.com/base/{token}"
        explorer_link = f"https://basescan.org/token/{token}"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 DexScreener", "url": ds_link},
                    {"text": "🔍 Explorer", "url": explorer_link},
                ],
            ]
        }

        # Build info lines
        info_parts = [dex.upper()]
        if fee:
            info_parts.append(f"fee={fee}")
        if hooks:
            info_parts.append(f"hooks={hooks[:14]}..")
        info_str = " · ".join(info_parts)

        # Metrics line
        metrics_parts = []
        if mcap > 0:
            metrics_parts.append(f"💰 ${mcap:,.0f}")
        if liq > 0:
            metrics_parts.append(f"💧 ${liq:,.0f}")
        if buys > 0:
            metrics_parts.append(f"🟢 {buys}B/{sells}S")
        if unique_buyers > 0:
            metrics_parts.append(f"👥 {unique_buyers}")
        metrics_str = "  ".join(metrics_parts)

        # Status line
        status_parts = [f"{safety_tag} safety"]
        if social_tag:
            status_parts.append(f"{social_tag} socials")
        if age_s > 0:
            status_parts.append(f"⏱ {age_s:.0f}s")
        status_str = "  ".join(status_parts)

        message = (
            f"📡 <b>NEW PAIR</b>{title}\n"
            f"<code>{token}</code>\n"
            f"{info_str}\n"
            f"{metrics_str}\n"
            f"{status_str}"
        )
        await self._send_message(message, reply_markup=keyboard)

    async def stop(self):
        """Send offline message and close session."""
        if self._bot_token and self._chat_id:
            try:
                await self._send_message("🔴 <b>Signal Detector Offline</b>")
            except Exception:
                pass
        if self._session and not self._session.closed:
            await self._session.close()
