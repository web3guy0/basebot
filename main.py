"""
Early Token Signal Detector — Main Orchestrator.

Detects new tokens on Base (Uniswap V3 + V4) within 3 minutes,
evaluates early buy activity, enriches with DexScreener data,
and sends contract addresses to Based Bot via Telegram.

Usage:
    python main.py              # normal mode (reads .env)
    DRY_RUN=true python main.py # dry run (no Telegram sends)
"""
import asyncio
import logging
import signal as signal_module
import sys
import time

from web3 import AsyncWeb3
from web3.providers import WebSocketProvider

import config
from constants import WETH
from state import TokenStateTracker
from signal_engine import SignalEngine
from v4_listener import V4Listener
from v3_listener import V3Listener
from dexscreener import DexScreenerEnricher
from safety import SafetyChecker, run_safety_check
from telegram_sender import TelegramSender

# ── Logging ──
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")
logging.getLogger("web3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


class EthPriceOracle:
    """Fetches ETH/USD price via DexScreener. Refreshes every 60s."""

    def __init__(self):
        self.price: float = 2500.0  # default fallback

    async def update(self):
        try:
            import aiohttp

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                url = f"https://api.dexscreener.com/tokens/v1/base/{WETH}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            for pair in data:
                                qt = pair.get("quoteToken", {})
                                if qt.get("symbol") in ("USDC", "USDbC"):
                                    price_str = pair.get("priceUsd")
                                    if price_str:
                                        self.price = float(price_str)
                                        logger.debug(f"ETH price: ${self.price:,.0f}")
                                        return
        except Exception as e:
            logger.debug(f"ETH price fetch failed: {e}")

    async def run_refresh_loop(self):
        while True:
            await self.update()
            await asyncio.sleep(60)

    def get_price(self) -> float:
        return self.price


class SignalDetector:
    """Main application — wires up all components and runs them concurrently."""

    def __init__(self):
        self.state_tracker = TokenStateTracker(max_age=300)
        self.engine = SignalEngine(state_tracker=self.state_tracker)
        self.eth_oracle = EthPriceOracle()
        self.dex_enricher = DexScreenerEnricher(self.state_tracker, self.engine)
        self.telegram = TelegramSender(self.engine.signal_queue)
        self.w3 = None
        self._safety_checker = None

    async def start(self):
        logger.info("=" * 60)
        logger.info("  EARLY TOKEN SIGNAL DETECTOR")
        logger.info(f"  Chain:      Base (8453)")
        logger.info(f"  Mode:       {'DRY RUN' if config.DRY_RUN else 'LIVE'}")
        logger.info(f"  Max age:    {config.MAX_TOKEN_AGE_SECONDS}s")
        logger.info(f"  Max mcap:   ${config.MAX_MCAP_USD:,.0f}")
        logger.info(f"  Min liq:    ${config.MIN_LIQUIDITY_USD:,.0f}")
        logger.info(f"  Min buys:   {config.MIN_BUYS}")
        logger.info(f"  Signals/hr: {config.MAX_SIGNALS_PER_HOUR}")
        logger.info("=" * 60)

        logger.info(f"Connecting to {config.RPC_WSS[:50]}...")

        async with AsyncWeb3(WebSocketProvider(config.RPC_WSS)) as w3:
            self.w3 = w3

            chain_id = await w3.eth.chain_id
            if chain_id != config.CHAIN_ID:
                logger.error(f"Wrong chain! Expected {config.CHAIN_ID}, got {chain_id}")
                return

            block = await w3.eth.block_number
            logger.info(f"Connected to Base | Block: {block}")

            self._safety_checker = SafetyChecker(w3)

            # Fetch initial ETH price
            await self.eth_oracle.update()
            logger.info(f"ETH price: ${self.eth_oracle.get_price():,.0f}")

            # Build listeners (they share the same w3 + subscription manager)
            v4 = V4Listener(w3, self.state_tracker, self.engine, self.eth_oracle.get_price)
            v3 = V3Listener(w3, self.state_tracker, self.engine, self.eth_oracle.get_price)

            # Register all subscriptions (V4 + V3), then run handler once
            await v4.register_subscriptions()
            await v3.register_subscriptions()

            # Launch background tasks
            tasks = [
                asyncio.create_task(
                    w3.subscription_manager.handle_subscriptions(run_forever=True),
                    name="subscription_handler",
                ),
                asyncio.create_task(self.dex_enricher.start(), name="dex_enricher"),
                asyncio.create_task(self.telegram.start(), name="telegram"),
                asyncio.create_task(self.eth_oracle.run_refresh_loop(), name="eth_price"),
                asyncio.create_task(self._eviction_loop(), name="eviction"),
                asyncio.create_task(self._safety_loop(), name="safety"),
                asyncio.create_task(self._stats_loop(), name="stats"),
            ]

            logger.info("All systems running. Waiting for new tokens...")

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception():
                    logger.error(f"Task {task.get_name()} crashed: {task.exception()}")
            for task in pending:
                task.cancel()

    async def _eviction_loop(self):
        while True:
            self.state_tracker.evict_stale()
            await asyncio.sleep(30)

    async def _safety_loop(self):
        checked: set[str] = set()
        while True:
            for addr, state in list(self.state_tracker.states.items()):
                if addr in checked or state.bytecode_safe is not None:
                    checked.add(addr)
                    continue
                asyncio.create_task(run_safety_check(self._safety_checker, state))
                checked.add(addr)
            await asyncio.sleep(2)

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(300)
            stats = self.engine.get_stats()
            active = self.state_tracker.active_count
            latency_str = ""
            if "avg_latency_s" in stats:
                latency_str = (
                    f" latency_avg={stats['avg_latency_s']}s"
                    f" min={stats['min_latency_s']}s"
                    f" max={stats['max_latency_s']}s"
                )
            logger.info(
                f"[stats] active={active} evaluated={stats['evaluated']} "
                f"signaled={stats['signaled']} rejected={stats['rejected']} "
                f"signals_this_hr={stats['signals_this_hour']}{latency_str}"
            )
            if stats["reject_reasons"]:
                top = sorted(stats["reject_reasons"].items(), key=lambda x: -x[1])[:5]
                logger.info(f"[stats] top rejections: {dict(top)}")


async def main():
    detector = SignalDetector()
    loop = asyncio.get_event_loop()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown(detector)))
    await detector.start()


async def _shutdown(detector: SignalDetector):
    logger.info("Shutting down...")
    if detector.dex_enricher:
        await detector.dex_enricher.stop()
    if detector.telegram:
        await detector.telegram.stop()
    logger.info("Goodbye.")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
