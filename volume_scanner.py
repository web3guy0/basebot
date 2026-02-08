"""
Volume Spike Scanner — detects sudden activity surges on ANY Uniswap V3 pool.

Piggybacks on the existing V3 global Swap subscription: we already receive
every V3 swap event on Base but only process tracked (new-token) pools.
This module counts swap frequency for ALL pools and flags spikes.

When a spike is detected on a pool NOT already tracked as a new token,
we enrich via DexScreener (V3 pool_addr = DexScreener pair_addr) and alert.
This catches old tokens that suddenly start pumping.

V4 pools are excluded because pool_id (bytes32 hash) doesn't map directly
to a DexScreener pair address. Most tokens with V4 pools also have V3 pools,
so they'll be caught by the V3 scanner anyway.
"""
import asyncio
import logging
import time
from collections import defaultdict

import config

logger = logging.getLogger("scanner")

# ── Tunable thresholds ───────────────────────────────────────────
SPIKE_WINDOW_S = 120         # Rolling window (seconds) for counting swaps
MIN_SWAPS_FOR_SPIKE = 10     # Min swaps in window to trigger
COOLDOWN_S = 600             # 10 min between alerts per pool
MAX_TRACKED_POOLS = 10_000   # Memory cap: drop least-active pools beyond this
PAIR_MIN_AGE_MS = 300_000    # 5 min — skip very new pairs (already in new-token pipeline)
PAIR_MIN_LIQ_USD = 1_000     # Skip dust pools


class VolumeScanner:
    """Detects volume spikes across all V3 pools by swap frequency."""

    def __init__(self, alert_queue: asyncio.Queue, dex_client, new_token_pools: set | None = None):
        """
        Args:
            alert_queue: Queue to push spike alert dicts into (consumed by TG bot).
            dex_client: Shared DexScreenerClient for enrichment.
            new_token_pools: Live reference to the set of pool addrs already tracked
                             as new tokens — spikes on these are skipped.
        """
        self.alert_queue = alert_queue
        self.dex_client = dex_client
        self._new_token_pools = new_token_pools if new_token_pools is not None else set()

        # pool_addr -> list of swap timestamps (rolling window)
        self._swaps: dict[str, list[float]] = defaultdict(list)
        # pool_addr -> last spike alert time (cooldown)
        self._cooldowns: dict[str, float] = {}
        self._total_spikes = 0

    # ── Called from V3 listener for EVERY swap ──────────────────

    def record_swap(self, pool_addr: str):
        """Record that a swap happened. Ultra-lightweight: just a timestamp append."""
        self._swaps[pool_addr].append(time.time())

    # ── Background loop ─────────────────────────────────────────

    async def run(self):
        """Main loop: prune old data, check for spikes, enrich + alert."""
        logger.info("Volume spike scanner active")
        while True:
            try:
                now = time.time()
                self._prune(now)
                await self._check_spikes(now)
            except Exception as e:
                logger.error(f"Scanner error: {e}")
            await asyncio.sleep(10)

    def _prune(self, now: float):
        """Evict stale data to keep memory bounded."""
        cutoff = now - SPIKE_WINDOW_S
        stale = []
        for pool in list(self._swaps):
            self._swaps[pool] = [t for t in self._swaps[pool] if t > cutoff]
            if not self._swaps[pool]:
                stale.append(pool)
        for pool in stale:
            del self._swaps[pool]

        # Prune cooldowns
        self._cooldowns = {p: t for p, t in self._cooldowns.items() if now - t < COOLDOWN_S}

        # Memory cap: drop least-active pools
        if len(self._swaps) > MAX_TRACKED_POOLS:
            sorted_pools = sorted(self._swaps, key=lambda p: len(self._swaps[p]))
            excess = len(self._swaps) - MAX_TRACKED_POOLS
            for pool in sorted_pools[:excess]:
                del self._swaps[pool]

    async def _check_spikes(self, now: float):
        """Scan all pools for swap frequency spikes."""
        for pool, timestamps in list(self._swaps.items()):
            # Skip pools already tracked as new tokens
            if pool in self._new_token_pools:
                continue
            # Skip if in cooldown
            if pool in self._cooldowns:
                continue
            # Check swap count in window
            count = len(timestamps)
            if count < MIN_SWAPS_FOR_SPIKE:
                continue

            # Spike detected
            self._cooldowns[pool] = now
            self._total_spikes += 1
            logger.info(f"[spike] {pool[:12]}.. swaps={count}/{SPIKE_WINDOW_S}s")
            asyncio.create_task(self._enrich_and_alert(pool, count))

    async def _enrich_and_alert(self, pool_addr: str, swap_count: int):
        """Fetch token info from DexScreener and push alert to queue."""
        try:
            pair_data = await self.dex_client.get_pair(pool_addr)
            if not pair_data:
                return

            base_token = pair_data.get("baseToken", {})
            token_addr = base_token.get("address", "")
            symbol = base_token.get("symbol", "")
            name = base_token.get("name", "")
            mcap = pair_data.get("marketCap") or pair_data.get("fdv") or 0
            liq = (pair_data.get("liquidity") or {}).get("usd", 0)
            volume_h1 = (pair_data.get("volume") or {}).get("h1", 0)
            price_change_m5 = (pair_data.get("priceChange") or {}).get("m5", 0)
            price_change_h1 = (pair_data.get("priceChange") or {}).get("h1", 0)
            pair_created = pair_data.get("pairCreatedAt", 0)

            # Skip very new pairs — already handled by the new-token pipeline
            if pair_created and time.time() * 1000 - pair_created < PAIR_MIN_AGE_MS:
                return

            # Skip dust / dead pools
            if liq < PAIR_MIN_LIQ_USD:
                return

            info = pair_data.get("info", {})
            has_socials = bool(info.get("socials") or info.get("websites"))

            # Calculate pair age for display
            age_hours = 0
            if pair_created:
                age_hours = (time.time() * 1000 - pair_created) / 3_600_000

            alert = {
                "type": "volume_spike",
                "token": token_addr,
                "symbol": symbol,
                "name": name,
                "chain": "base",
                "mcap": mcap,
                "liq": liq,
                "volume_h1": volume_h1,
                "price_change_m5": price_change_m5,
                "price_change_h1": price_change_h1,
                "swaps_2m": swap_count,
                "has_socials": has_socials,
                "age_hours": age_hours,
            }

            await self.alert_queue.put(alert)
            logger.info(
                f"[spike] ${symbol} mcap=${mcap:,.0f} liq=${liq:,.0f} "
                f"vol_h1=${volume_h1:,.0f} Δ5m={price_change_m5:+.0f}% swaps={swap_count}"
            )

        except Exception as e:
            logger.debug(f"Spike enrichment failed for {pool_addr[:12]}.. : {e}")

    def get_stats(self) -> dict:
        """Return scanner stats for the stats loop."""
        return {
            "pools_observed": len(self._swaps),
            "total_spikes": self._total_spikes,
        }
