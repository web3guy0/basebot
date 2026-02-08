"""
DexScreener REST API enrichment layer.
Polls token data after on-chain detection to get clean mcap, liquidity, buy/sell counts.
Secondary data source — on-chain is primary for speed.
"""
import asyncio
import logging
import time
import aiohttp

logger = logging.getLogger("dexscreener")

# DexScreener API base
BASE_URL = "https://api.dexscreener.com"

# Rate limit: 300 req/min for token/pair endpoints
# We self-limit to ~200/min to stay safe
MIN_REQUEST_INTERVAL = 0.3  # seconds between requests


class DexScreenerClient:
    """Async DexScreener API client for token enrichment."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._last_request: float = 0.0
        self._request_lock = asyncio.Lock()

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
                headers={"Accept": "application/json"},
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _rate_limited_get(self, url: str) -> dict | None:
        """GET with rate limiting."""
        async with self._request_lock:
            now = time.time()
            wait = MIN_REQUEST_INTERVAL - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)

            await self._ensure_session()
            try:
                async with self._session.get(url) as resp:
                    self._last_request = time.time()
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        logger.warning("DexScreener rate limited, backing off 5s")
                        await asyncio.sleep(5)
                        return None
                    else:
                        logger.debug(f"DexScreener {resp.status} for {url}")
                        return None
            except Exception as e:
                logger.debug(f"DexScreener request failed: {e}")
                return None

    async def get_token_pairs(self, token_address: str, chain: str = "base") -> list[dict]:
        """
        Fetch all pairs for a token on a given chain.
        Returns list of pair objects with mcap, liquidity, txns, etc.
        """
        url = f"{BASE_URL}/tokens/v1/{chain}/{token_address}"
        data = await self._rate_limited_get(url)
        if data is None:
            return []
        # Response is a list of pair objects
        if isinstance(data, list):
            return data
        return []

    async def get_pair(self, pair_address: str) -> dict | None:
        """Fetch a specific pair by address on Base."""
        url = f"{BASE_URL}/latest/dex/pairs/base/{pair_address}"
        data = await self._rate_limited_get(url)
        if data and "pairs" in data and data["pairs"]:
            return data["pairs"][0]
        return None

    async def get_latest_boosts(self) -> list[dict]:
        """Fetch latest boosted tokens (paid promotion signal)."""
        url = f"{BASE_URL}/token-boosts/latest/v1"
        data = await self._rate_limited_get(url)
        if isinstance(data, list):
            return data
        return []


class DexScreenerEnricher:
    """
    Background enrichment loop for tracked tokens.
    After on-chain detection, polls DexScreener every few seconds
    to get mcap/liquidity/volume data until the token ages out.
    """

    def __init__(self, state_tracker, signal_engine, poll_interval: float = 8.0, client: DexScreenerClient | None = None):
        self.tracker = state_tracker
        self.engine = signal_engine
        self.client = client or DexScreenerClient()
        self._owns_client = client is None  # only close if we created it
        self.poll_interval = poll_interval
        self._running = False

    async def start(self):
        """Run enrichment loop."""
        self._running = True
        logger.info(f"DexScreener enricher started (poll every {self.poll_interval}s)")

        while self._running:
            try:
                await self._enrich_cycle()
            except Exception as e:
                logger.error(f"DexScreener enrichment error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self._running = False
        if self._owns_client:
            await self.client.close()

    async def _enrich_cycle(self):
        """Enrich all active (non-signaled, non-stale) tokens."""
        now = time.time()
        tokens_to_enrich = []

        for addr, state in list(self.tracker.states.items()):
            if state.signaled:
                continue
            if state.age_seconds > 200:  # Past signal window + buffer
                continue
            # Don't re-fetch too often
            if now - state.ds_last_fetch < self.poll_interval:
                continue
            tokens_to_enrich.append(addr)

        if not tokens_to_enrich:
            return

        logger.debug(f"Enriching {len(tokens_to_enrich)} tokens via DexScreener")

        for addr in tokens_to_enrich:
            state = self.tracker.get(addr)
            if state is None or state.signaled:
                continue

            pairs = await self.client.get_token_pairs(addr)
            if not pairs:
                continue

            # Use the pair with highest liquidity
            best_pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))

            # Extract data
            liquidity = best_pair.get("liquidity", {})
            state.ds_liquidity_usd = liquidity.get("usd")
            state.ds_mcap = best_pair.get("marketCap") or best_pair.get("fdv")

            txns = best_pair.get("txns", {})
            m5 = txns.get("m5", {})
            state.ds_buys_m5 = m5.get("buys")
            state.ds_sells_m5 = m5.get("sells")

            volume = best_pair.get("volume", {})
            state.ds_volume_m5 = volume.get("m5")

            state.ds_last_fetch = time.time()

            logger.debug(
                f"[ds] {addr[:10]}... mcap=${state.ds_mcap} liq=${state.ds_liquidity_usd} "
                f"buys={state.ds_buys_m5} sells={state.ds_sells_m5}"
            )

            # Re-evaluate signal with enriched data
            await self.engine.evaluate(state)


class SolDexScreenerEnricher:
    """
    DexScreener enrichment for Solana tokens.

    Same pattern as the EVM enricher: polls active non-signaled tokens
    on an interval, fills ds_mcap / ds_liquidity_usd / ds_buys_m5, then
    re-evaluates through the shared signal engine.
    """

    def __init__(self, state_tracker, signal_engine, poll_interval: float = 8.0, client: DexScreenerClient | None = None):
        self.tracker = state_tracker
        self.engine = signal_engine
        self.client = client or DexScreenerClient()
        self._owns_client = client is None  # only close if we created it
        self.poll_interval = poll_interval
        self._running = False

    async def start(self):
        self._running = True
        logger.info(
            f"Solana DexScreener enricher started (poll every {self.poll_interval}s)"
        )
        while self._running:
            try:
                await self._enrich_cycle()
            except Exception as e:
                logger.error(f"Solana DexScreener enrichment error: {e}")
            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self._running = False
        if self._owns_client:
            await self.client.close()

    async def _enrich_cycle(self):
        now = time.time()
        tokens_to_enrich = []

        for addr, state in list(self.tracker.states.items()):
            if state.signaled:
                continue
            if state.age_seconds > 160:
                continue
            if now - state.ds_last_fetch < self.poll_interval:
                continue
            tokens_to_enrich.append(addr)

        if not tokens_to_enrich:
            return

        logger.debug(
            f"Enriching {len(tokens_to_enrich)} Solana tokens via DexScreener"
        )

        for addr in tokens_to_enrich:
            state = self.tracker.get(addr)
            if state is None or state.signaled:
                continue

            pairs = await self.client.get_token_pairs(addr, chain="solana")
            if not pairs:
                continue

            best_pair = max(
                pairs,
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
            )

            liquidity = best_pair.get("liquidity", {})
            state.ds_liquidity_usd = liquidity.get("usd")
            state.ds_mcap = best_pair.get("marketCap") or best_pair.get("fdv")

            txns = best_pair.get("txns", {})
            m5 = txns.get("m5", {})
            state.ds_buys_m5 = m5.get("buys")
            state.ds_sells_m5 = m5.get("sells")

            volume = best_pair.get("volume", {})
            state.ds_volume_m5 = volume.get("m5")

            state.ds_last_fetch = time.time()

            logger.debug(
                f"[sol-ds] {addr[:8]}... mcap=${state.ds_mcap} "
                f"liq=${state.ds_liquidity_usd} "
                f"buys={state.ds_buys_m5} sells={state.ds_sells_m5}"
            )

            await self.engine.evaluate(state)
