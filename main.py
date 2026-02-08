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
from base.constants import WETH
from base.state import TokenStateTracker
from base.v4_listener import V4Listener
from base.v3_listener import V3Listener
from base.safety import SafetyChecker, run_safety_check
from signal_engine import SignalEngine
from dexscreener import DexScreenerClient, DexScreenerEnricher, SolDexScreenerEnricher
from telegram_sender import TelegramSender
from telegram_bot import SignalBot
from post_mortem import PostMortemTracker

# Solana imports (conditional on SOL_ENABLED)
if config.SOL_ENABLED:
    from solana.state import SolTokenStateTracker
    from solana.listener import SolanaListener
    from solana.safety import SolSafetyChecker, run_sol_safety_check

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


class SolPriceOracle:
    """Fetches SOL/USD price via DexScreener. Refreshes every 60s."""

    def __init__(self):
        self.price: float = 150.0  # default fallback

    async def update(self):
        try:
            import aiohttp

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            ) as session:
                url = "https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            for pair in data:
                                # Only use pairs where WSOL is the base token
                                bt = pair.get("baseToken", {})
                                if bt.get("address") != "So11111111111111111111111111111111111111112":
                                    continue
                                # Prefer stablecoin-quoted pairs for accuracy
                                qt = pair.get("quoteToken", {})
                                if qt.get("symbol") in ("USDC", "USDT"):
                                    price_str = pair.get("priceUsd")
                                    if price_str:
                                        self.price = float(price_str)
                                        logger.debug(f"SOL price: ${self.price:,.2f}")
                                        return
                            # Fallback: any pair where WSOL is base
                            for pair in data:
                                bt = pair.get("baseToken", {})
                                if bt.get("address") == "So11111111111111111111111111111111111111112":
                                    price_str = pair.get("priceUsd")
                                    if price_str:
                                        self.price = float(price_str)
                                        logger.debug(f"SOL price (fallback): ${self.price:,.2f}")
                                        return
        except Exception as e:
            logger.debug(f"SOL price fetch failed: {e}")

    async def run_refresh_loop(self):
        while True:
            await self.update()
            await asyncio.sleep(60)

    def get_price(self) -> float:
        return self.price


class SignalDetector:
    """Main application — wires up all components and runs them concurrently."""

    def __init__(self):
        # ── EVM ───────────────────────────────────────────────
        self.state_tracker = TokenStateTracker(max_age=300)

        # ── Solana (optional) ─────────────────────────────────
        self.sol_state_tracker = (
            SolTokenStateTracker(max_age=200) if config.SOL_ENABLED else None
        )

        # ── Shared engine (both chains push to same signal_queue) ──
        self.engine = SignalEngine(
            state_tracker=self.state_tracker,
            sol_state_tracker=self.sol_state_tracker,
        )
        self.eth_oracle = EthPriceOracle()
        self._shared_dex_client = DexScreenerClient()  # single client for all DexScreener calls
        self.dex_enricher = DexScreenerEnricher(self.state_tracker, self.engine, client=self._shared_dex_client)
        self.post_mortem = PostMortemTracker(
            dex_client=self._shared_dex_client,
            signal_engine=self.engine,
            on_complete=self._on_post_mortem,
        )

        # ── Telegram outputs (fanout: engine → both consumers) ──
        # Based Bot (Telethon userbot) — sends CA to Based Bot chat
        self._basedbot_queue: asyncio.Queue[str] = asyncio.Queue()
        self.telegram = TelegramSender(self._basedbot_queue)
        # Personal Bot (Bot API) — sends rich formatted signals to you
        self._personalbot_queue: asyncio.Queue[str] = asyncio.Queue()
        self.signal_bot = SignalBot(
            self._personalbot_queue,
            state_tracker=self.state_tracker,
            sol_state_tracker=self.sol_state_tracker,
        )

        self.w3 = None
        self._safety_checker = None

        # ── Solana components ─────────────────────────────────
        self.sol_enricher = None
        self.sol_listener = None
        self.sol_safety = None
        self.sol_price_oracle = None
        if config.SOL_ENABLED:
            self.sol_price_oracle = SolPriceOracle()
            self.sol_enricher = SolDexScreenerEnricher(
                self.sol_state_tracker, self.engine, client=self._shared_dex_client
            )

    async def start(self):
        logger.info("=" * 60)
        logger.info("  EARLY TOKEN SIGNAL DETECTOR")
        logger.info(f"  Chain:      Base (8453) + {'Solana' if config.SOL_ENABLED else 'Solana DISABLED'}")
        logger.info(f"  Mode:       {'DRY RUN' if config.DRY_RUN else 'LIVE'}")
        logger.info(f"  Max age:    {config.MAX_TOKEN_AGE_SECONDS}s (EVM) / {config.SOL_MAX_TOKEN_AGE_SECONDS}s (SOL)")
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
            self._v4 = V4Listener(w3, self.state_tracker, self.engine, self.eth_oracle.get_price)
            self._v3 = V3Listener(w3, self.state_tracker, self.engine, self.eth_oracle.get_price)

            # Register all subscriptions (V4 + V3), then run handler once
            await self._v4.register_subscriptions()
            await self._v3.register_subscriptions()

            # Launch background tasks
            tasks = [
                asyncio.create_task(
                    w3.subscription_manager.handle_subscriptions(run_forever=True),
                    name="subscription_handler",
                ),
                asyncio.create_task(self.dex_enricher.start(), name="dex_enricher"),
                asyncio.create_task(self._signal_fanout(), name="signal_fanout"),
                asyncio.create_task(self.telegram.start(), name="telegram"),
                asyncio.create_task(self.signal_bot.start(), name="signal_bot"),
                asyncio.create_task(self.eth_oracle.run_refresh_loop(), name="eth_price"),
                asyncio.create_task(self._eviction_loop(), name="eviction"),
                asyncio.create_task(self._safety_loop(), name="safety"),
                asyncio.create_task(self._stats_loop(), name="stats"),
                asyncio.create_task(self.post_mortem.start(), name="post_mortem"),
                asyncio.create_task(self._signal_hook_loop(), name="signal_hook"),
            ]

            logger.info("All systems running. Waiting for new tokens...")

            # ── Add Solana tasks if enabled ────────────────────
            if config.SOL_ENABLED:
                self.sol_safety = SolSafetyChecker(config.SOL_RPC_HTTP)
                self.sol_listener = SolanaListener(
                    wss_url=config.SOL_RPC_WSS,
                    http_url=config.SOL_RPC_HTTP,
                    state_tracker=self.sol_state_tracker,
                    signal_engine=self.engine,
                    sol_price_fn=self.sol_price_oracle.get_price,
                    min_liquidity_sol=config.SOL_MIN_LIQUIDITY_SOL,
                )
                await self.sol_price_oracle.update()
                logger.info(f"SOL price: ${self.sol_price_oracle.get_price():,.2f}")

                tasks.extend([
                    asyncio.create_task(
                        self.sol_listener.start(), name="sol_listener"
                    ),
                    asyncio.create_task(
                        self.sol_enricher.start(), name="sol_enricher"
                    ),
                    asyncio.create_task(
                        self.sol_price_oracle.run_refresh_loop(), name="sol_price"
                    ),
                    asyncio.create_task(
                        self._sol_eviction_loop(), name="sol_eviction"
                    ),
                    asyncio.create_task(
                        self._sol_safety_loop(), name="sol_safety"
                    ),
                ])
                logger.info(
                    f"Solana pipeline active: listener + enricher + safety "
                    f"(min liq {config.SOL_MIN_LIQUIDITY_SOL} SOL)"
                )

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception():
                    logger.error(f"Task {task.get_name()} crashed: {task.exception()}")
            for task in pending:
                task.cancel()

    async def _on_post_mortem(self, record: dict):
        """Forward post-mortem results to personal bot."""
        if self.signal_bot:
            await self.signal_bot.send_post_mortem(record)

    async def _signal_fanout(self):
        """Consume from engine's signal_queue and duplicate to all output queues."""
        while True:
            try:
                contract_address = await self.engine.signal_queue.get()
                # Fan out to both consumers
                await self._basedbot_queue.put(contract_address)
                await self._personalbot_queue.put(contract_address)
                self.engine.signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Signal fanout error: {e}")
                await asyncio.sleep(0.1)

    async def _signal_hook_loop(self):
        """Watch for new signals and schedule post-mortem follow-ups."""
        seen_signals: set[str] = set()
        while True:
            # EVM signals
            for addr, state in list(self.state_tracker.states.items()):
                if state.signaled and addr not in seen_signals:
                    seen_signals.add(addr)
                    latency = state.signal_time - state.first_seen
                    self.post_mortem.schedule(
                        token_address=addr,
                        mcap_at_signal=state.best_mcap,
                        latency=latency,
                        chain="base",
                    )
            # Solana signals
            if self.sol_state_tracker:
                for addr, state in list(self.sol_state_tracker.states.items()):
                    if state.signaled and addr not in seen_signals:
                        seen_signals.add(addr)
                        latency = state.signal_time - state.first_seen
                        self.post_mortem.schedule(
                            token_address=addr,
                            mcap_at_signal=state.best_mcap,
                            latency=latency,
                            chain="solana",
                        )
            # Prune: remove addresses evicted from both trackers
            active = set(self.state_tracker.states.keys())
            if self.sol_state_tracker:
                active |= set(self.sol_state_tracker.states.keys())
            seen_signals &= active
            await asyncio.sleep(2)

    async def _eviction_loop(self):
        while True:
            self.state_tracker.evict_stale()
            # Prune listener pool mappings for evicted tokens
            self._prune_listener_pools()
            await asyncio.sleep(30)

    def _prune_listener_pools(self):
        """Remove V3/V4 pool mappings for tokens no longer in state tracker."""
        active = set(self.state_tracker.states.keys())
        if hasattr(self, '_v3') and self._v3:
            stale_pools = [
                p for p, (tok, _) in list(self._v3.pool_to_token.items())
                if tok not in active
            ]
            for p in stale_pools:
                self._v3.pool_to_token.pop(p, None)
                self._v3._tracked_pools.discard(p)
        if hasattr(self, '_v4') and self._v4:
            stale_pools = [
                p for p, (tok, _) in list(self._v4.pool_id_to_token.items())
                if tok not in active
            ]
            for p in stale_pools:
                self._v4.pool_id_to_token.pop(p, None)

    async def _sol_eviction_loop(self):
        while True:
            if self.sol_state_tracker:
                self.sol_state_tracker.evict_stale()
            await asyncio.sleep(20)  # faster eviction for Solana

    async def _safety_loop(self):
        checked: set[str] = set()
        while True:
            for addr, state in list(self.state_tracker.states.items()):
                if addr in checked or state.bytecode_safe is not None:
                    checked.add(addr)
                    continue
                asyncio.create_task(run_safety_check(self._safety_checker, state))
                checked.add(addr)
            # Prune checked set: remove addresses no longer in tracker (evicted)
            checked &= set(self.state_tracker.states.keys())
            await asyncio.sleep(2)

    async def _sol_safety_loop(self):
        """Run mint/freeze authority checks for new Solana tokens."""
        checked: set[str] = set()
        while True:
            if self.sol_state_tracker and self.sol_safety:
                for addr, state in list(self.sol_state_tracker.states.items()):
                    if addr in checked or state.bytecode_safe is not None:
                        checked.add(addr)
                        continue
                    asyncio.create_task(
                        run_sol_safety_check(self.sol_safety, state)
                    )
                    checked.add(addr)
                # Prune checked set: remove addresses no longer in tracker (evicted)
                checked &= set(self.sol_state_tracker.states.keys())
            await asyncio.sleep(2)

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(300)
            stats = self.engine.get_stats()
            active_evm = self.state_tracker.active_count
            active_sol = (
                self.sol_state_tracker.active_count
                if self.sol_state_tracker
                else 0
            )
            latency_str = ""
            if "avg_latency_s" in stats:
                latency_str = (
                    f" latency_avg={stats['avg_latency_s']}s"
                    f" min={stats['min_latency_s']}s"
                    f" max={stats['max_latency_s']}s"
                )
            logger.info(
                f"[stats] evm={active_evm} sol={active_sol} "
                f"evaluated={stats['evaluated']} "
                f"signaled={stats['signaled']} rejected={stats['rejected']} "
                f"signals_this_hr={stats['signals_this_hour']}{latency_str}"
            )
            if stats.get("latency_distribution"):
                logger.info(f"[stats] latency buckets: {stats['latency_distribution']}")
            if stats.get("post_mortem_count"):
                logger.info(
                    f"[stats] post-mortems: {stats['post_mortem_count']} "
                    f"TP_hit={stats['tp_hit_rate']} rugs={stats['rug_rate']}"
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
    if detector.post_mortem:
        await detector.post_mortem.stop()
    if detector.telegram:
        await detector.telegram.stop()
    if detector.signal_bot:
        await detector.signal_bot.stop()
    # Solana cleanup
    if detector.sol_enricher:
        await detector.sol_enricher.stop()
    if detector.sol_listener:
        await detector.sol_listener.stop()
    if detector.sol_safety:
        await detector.sol_safety.close()
    # Close shared DexScreener client last
    if detector._shared_dex_client:
        await detector._shared_dex_client.close()
    logger.info("Goodbye.")
    # Cancel all running tasks for clean exit
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
