"""
Uniswap V3 Factory + Pool listener.
Subscribe to PoolCreated from Factory, then track Swap events on new WETH pools.

Uses web3 v7 handler-based subscription pattern.
V3 Swaps: we subscribe globally to the Swap topic and filter client-side
for pools we're tracking (since V3 emits swaps from individual pool contracts).
"""
import asyncio
import logging
from eth_abi import decode

from base.constants import (
    V3_FACTORY,
    TOPIC_V3_POOL_CREATED,
    TOPIC_V3_SWAP,
    ETH_ADDRESSES,
    V3_POOL_ABI,
)
from base.price_utils import estimate_mcap, estimate_liquidity_usd

logger = logging.getLogger("v3_listener")

ALLOWED_FEE_TIERS = {3000, 10000}


class V3Listener:
    """Listens to V3 Factory for PoolCreated, then Swaps on new pools."""

    def __init__(self, w3, state_tracker, signal_engine, eth_price_fn):
        self.w3 = w3
        self.tracker = state_tracker
        self.engine = signal_engine
        self.eth_price_fn = eth_price_fn
        self.pool_to_token: dict[str, tuple[str, bool]] = {}  # pool_addr -> (token_addr, eth_is_token0)
        self._tracked_pools: set[str] = set()

    async def register_subscriptions(self):
        """Register V3 Factory + global Swap subscriptions.
        Does NOT call handle_subscriptions — caller must do that after all subs are registered.
        """
        logger.info(f"Registering V3 subscriptions on Factory {V3_FACTORY}")

        async def on_pool_created(ctx):
            try:
                await self._handle_pool_created(ctx.result)
            except Exception as e:
                logger.error(f"Error processing V3 PoolCreated: {e}")

        async def on_swap(ctx):
            try:
                log = ctx.result
                pool_addr = log["address"]
                if hasattr(pool_addr, "lower"):
                    pool_addr = pool_addr.lower()
                else:
                    pool_addr = str(pool_addr).lower()
                if pool_addr in self._tracked_pools:
                    await self._handle_swap(log, pool_addr)
            except Exception as e:
                logger.error(f"Error processing V3 Swap: {e}")

        await self.w3.eth.subscribe(
            "logs",
            {"address": V3_FACTORY, "topics": [TOPIC_V3_POOL_CREATED]},
            handler=on_pool_created,
            label="v3_pool_created",
        )

        await self.w3.eth.subscribe(
            "logs",
            {"topics": [TOPIC_V3_SWAP]},
            handler=on_swap,
            label="v3_swap",
        )
        logger.info("V3 subscriptions registered (PoolCreated + Swap)")

    async def _handle_pool_created(self, log):
        """New V3 pool. Filter for WETH pairs + allowed fee tiers."""
        topics = log["topics"]
        data = bytes(log["data"])

        token0 = "0x" + (topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1]))[-40:]
        token1 = "0x" + (topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2]))[-40:]
        fee = int(topics[3].hex() if hasattr(topics[3], "hex") else str(topics[3]), 16)

        # Non-indexed: tickSpacing (int24), pool (address)
        decoded = decode(["int24", "address"], data)
        tick_spacing = decoded[0]
        pool_address_raw = decoded[1]

        if isinstance(pool_address_raw, str):
            pool_addr = pool_address_raw.lower()
        else:
            pool_addr = f"0x{pool_address_raw:040x}".lower() if isinstance(pool_address_raw, int) else str(pool_address_raw).lower()

        t0 = token0.lower()
        t1 = token1.lower()

        if t0 not in ETH_ADDRESSES and t1 not in ETH_ADDRESSES:
            return
        if fee not in ALLOWED_FEE_TIERS:
            return

        if t0 in ETH_ADDRESSES:
            token_address = token1
            eth_is_token0 = True
        else:
            token_address = token0
            eth_is_token0 = False

        logger.info(
            f"[v3-pool] New pool | token={token_address[:10]}... | "
            f"pool={pool_addr[:10]}... | fee={fee}"
        )

        state = self.tracker.create(
            token_address=token_address,
            pair_address=pool_addr,
            dex_version="v3",
            # NOTE: deployer not extracted — would need extra eth_getTransaction RPC.
            # EVM deployer spam is rare (gas cost), bytecode safety compensates.
        )

        self.pool_to_token[pool_addr] = (token_address.lower(), eth_is_token0)
        self._tracked_pools.add(pool_addr)

        # Try to get initial price from slot0
        try:
            pool_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(pool_addr),
                abi=V3_POOL_ABI,
            )
            slot0 = await pool_contract.functions.slot0().call()
            sqrt_price_x96 = slot0[0]
            state.sqrt_price_x96 = sqrt_price_x96
            estimate_mcap(state, sqrt_price_x96, eth_is_token0, self.eth_price_fn())
        except Exception as e:
            logger.debug(f"Could not read slot0 for new V3 pool: {e}")

    async def _handle_swap(self, log, pool_addr: str):
        """Swap on a tracked V3 pool."""
        entry = self.pool_to_token.get(pool_addr)
        if not entry:
            return
        token_address, eth_is_token0 = entry

        state = self.tracker.get(token_address)
        if state is None or state.signaled:
            self._tracked_pools.discard(pool_addr)
            self.pool_to_token.pop(pool_addr, None)
            return

        topics = log["topics"]
        data = bytes(log["data"])
        sender = "0x" + (topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1]))[-40:]

        # Non-indexed: amount0 (int256), amount1 (int256), sqrtPriceX96 (uint160),
        #              liquidity (uint128), tick (int24)
        decoded = decode(["int256", "int256", "uint160", "uint128", "int24"], data)
        amount0, amount1, sqrt_price_x96, liquidity, tick = decoded

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
                if liquidity > 0:
                    estimate_liquidity_usd(updated, liquidity, sqrt_price_x96, eth_price)
                await self.engine.evaluate(updated)
        else:
            self.tracker.record_sell(token_address)
