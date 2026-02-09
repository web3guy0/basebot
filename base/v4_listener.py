"""
Uniswap V4 PoolManager listener.
Subscribes to Initialize (new pool) + Swap events from the singleton contract.
All V4 events come from ONE address — no dynamic per-pool subscriptions needed.

Uses web3 v7 handler-based subscription pattern.
"""
import asyncio
import logging
from eth_abi import decode

import config
from base.constants import (
    V4_POOL_MANAGER,
    TOPIC_V4_INITIALIZE,
    TOPIC_V4_SWAP,
    ETH_ADDRESSES,
    BLOCKED_HOOKS,
)
from base.price_utils import estimate_mcap, estimate_liquidity_usd

logger = logging.getLogger("v4_listener")


class V4Listener:
    """Listens to Uniswap V4 PoolManager for Initialize + Swap events."""

    def __init__(self, w3, state_tracker, signal_engine, eth_price_fn, whale_queue=None, discovery_queue=None):
        self.w3 = w3
        self.tracker = state_tracker
        self.engine = signal_engine
        self.eth_price_fn = eth_price_fn
        self.whale_queue = whale_queue
        self.discovery_queue = discovery_queue
        self.pool_id_to_token: dict[str, tuple[str, bool]] = {}  # pool_id -> (token_addr, eth_is_token0)

    async def register_subscriptions(self):
        """Register V4 PoolManager subscriptions (Initialize + Swap).
        Does NOT call handle_subscriptions — caller must do that after all subs are registered.
        """
        logger.info(f"Registering V4 subscriptions on PoolManager {V4_POOL_MANAGER}")

        async def on_initialize(ctx):
            try:
                await self._handle_initialize(ctx.result)
            except Exception as e:
                logger.error(f"Error processing V4 Initialize: {e}")

        async def on_swap(ctx):
            try:
                await self._handle_swap(ctx.result)
            except Exception as e:
                logger.error(f"Error processing V4 Swap: {e}")

        await self.w3.eth.subscribe(
            "logs",
            {"address": V4_POOL_MANAGER, "topics": [TOPIC_V4_INITIALIZE]},
            handler=on_initialize,
            label="v4_initialize",
        )

        await self.w3.eth.subscribe(
            "logs",
            {"address": V4_POOL_MANAGER, "topics": [TOPIC_V4_SWAP]},
            handler=on_swap,
            label="v4_swap",
        )
        logger.info("V4 subscriptions registered (Initialize + Swap)")

    async def _handle_initialize(self, log):
        """New V4 pool created. Filter for ETH/WETH pairs, check hooks."""
        topics = log["topics"]
        data = bytes(log["data"])

        pool_id = topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
        currency0 = "0x" + (topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2]))[-40:]
        currency1 = "0x" + (topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3]))[-40:]

        # Decode non-indexed: fee, tickSpacing, hooks, sqrtPriceX96, tick
        decoded = decode(["uint24", "int24", "address", "uint160", "int24"], data)
        fee, tick_spacing, hooks_raw, sqrt_price_x96, tick = decoded

        c0 = currency0.lower()
        c1 = currency1.lower()

        # Must be an ETH/WETH pair
        if c0 not in ETH_ADDRESSES and c1 not in ETH_ADDRESSES:
            return

        if c0 in ETH_ADDRESSES:
            token_address = currency1
            eth_is_token0 = True
        else:
            token_address = currency0
            eth_is_token0 = False

        # Format hooks address
        if isinstance(hooks_raw, bytes):
            hooks_lower = "0x" + hooks_raw.hex()
        elif isinstance(hooks_raw, int):
            hooks_lower = f"0x{hooks_raw:040x}"
        else:
            hooks_lower = str(hooks_raw).lower()

        # Hooks safety check (blacklist: reject known-malicious, allow standard hooks)
        if hooks_lower in BLOCKED_HOOKS:
            logger.debug(f"[v4-skip] {pool_id[:16]}.. hooks={hooks_lower[:16]}..")
            return

        has_hooks = not hooks_lower.endswith('0' * 40)
        logger.debug(
            f"[v4-init] {token_address[:10]}.. fee={fee} tick={tick} "
            f"hooks={'none' if not has_hooks else hooks_lower[:16]}.."
        )

        state = self.tracker.create(
            token_address=token_address,
            pair_address=pool_id,
            dex_version="v4",
            hooks_address=hooks_lower,
            sqrt_price_x96=sqrt_price_x96,
            # NOTE: deployer not extracted — would need extra eth_getTransaction RPC.
            # EVM deployer spam is rare (gas cost), bytecode safety compensates.
        )
        self.pool_id_to_token[pool_id] = (token_address.lower(), eth_is_token0)

        # Push to discovery feed (personal bot — no auto-buy)
        if self.discovery_queue:
            self.discovery_queue.put_nowait({
                "token": token_address,
                "pool": pool_id,
                "dex": "v4",
                "hooks": hooks_lower if has_hooks else None,
            })

        if sqrt_price_x96 > 0:
            estimate_mcap(state, sqrt_price_x96, eth_is_token0, self.eth_price_fn())

    async def _handle_swap(self, log):
        """Swap on a tracked V4 pool. Update buy/sell stats."""
        topics = log["topics"]
        data = bytes(log["data"])

        pool_id = topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
        entry = self.pool_id_to_token.get(pool_id)
        if entry is None:
            return
        token_address, eth_is_token0 = entry

        state = self.tracker.get(token_address)
        if state is None or state.signaled:
            return

        decoded = decode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], data)
        amount0, amount1, sqrt_price_x96, liquidity, tick, fee = decoded

        sender = "0x" + (topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2]))[-40:]
        state.sqrt_price_x96 = sqrt_price_x96
        eth_price = self.eth_price_fn()

        # Precise ETH value using known token ordering
        if eth_is_token0:
            eth_value = abs(amount0) / 1e18
            is_buy = amount0 > 0  # ETH entering pool → user buying meme token
        else:
            eth_value = abs(amount1) / 1e18
            is_buy = amount1 > 0  # ETH entering pool → user buying meme token

        usd_value = eth_value * eth_price

        if is_buy:
            updated = self.tracker.record_buy(token_address, sender, usd_value)
            if updated:
                if liquidity > 0 and sqrt_price_x96 > 0:
                    estimate_liquidity_usd(updated, liquidity, sqrt_price_x96, eth_price)
                await self.engine.evaluate(updated)
        else:
            self.tracker.record_sell(token_address)

        # Whale alert: large swap on a tracked token
        if self.whale_queue and usd_value >= config.WHALE_ALERT_MIN_USD:
            self.whale_queue.put_nowait({
                "token": token_address,
                "chain": "base",
                "is_buy": is_buy,
                "usd": usd_value,
                "sender": sender,
                "symbol": state.token_symbol if state else "",
            })
