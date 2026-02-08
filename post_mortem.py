"""
Post-mortem tracker — async background task that checks token performance
10 minutes after a signal fires.

Records: token, latency, mcap_at_signal, mcap_10m, price_change_10m
This data is essential for false-positive auditing and threshold tuning.
"""
import asyncio
import logging
import time

logger = logging.getLogger("postmortem")


class PostMortemTracker:
    """
    Watches signaled tokens and records their 10-minute performance.
    Uses DexScreener to check mcap after the follow-up window.
    """

    def __init__(self, dex_client, signal_engine, follow_up_seconds: int = 600, on_complete=None):
        self.dex_client = dex_client
        self.engine = signal_engine
        self.follow_up_seconds = follow_up_seconds
        self._pending: list[dict] = []  # tokens awaiting follow-up
        self._running = False
        self._on_complete = on_complete  # async callback(record) for notifications

    def schedule(self, token_address: str, mcap_at_signal: float, latency: float, chain: str = "base"):
        """Schedule a post-mortem check for a token that just signaled."""
        self._pending.append({
            "token": token_address,
            "signal_time": time.time(),
            "mcap_at_signal": mcap_at_signal,
            "latency_s": latency,
            "chain": chain,
        })
        logger.debug(
            f"[pm-scheduled] {token_address[:10]}... "
            f"check in {self.follow_up_seconds}s"
        )

    async def start(self):
        """Run the post-mortem check loop."""
        self._running = True
        logger.info(
            f"Post-mortem tracker started "
            f"(follow-up window: {self.follow_up_seconds}s)"
        )

        while self._running:
            try:
                await self._check_cycle()
            except Exception as e:
                logger.error(f"Post-mortem check error: {e}")
            await asyncio.sleep(15)  # check every 15s for mature entries

    async def stop(self):
        self._running = False

    async def _check_cycle(self):
        """Check if any pending tokens are ready for follow-up."""
        now = time.time()
        still_pending = []

        for entry in self._pending:
            elapsed = now - entry["signal_time"]
            if elapsed < self.follow_up_seconds:
                still_pending.append(entry)
                continue

            # Time's up — query DexScreener for current state
            await self._do_follow_up(entry)

        self._pending = still_pending

    async def _do_follow_up(self, entry: dict):
        """Fetch current DexScreener data and record post-mortem."""
        token = entry["token"]
        mcap_at_signal = entry["mcap_at_signal"]
        chain = entry.get("chain", "base")

        pairs = await self.dex_client.get_token_pairs(token, chain=chain)

        mcap_now = 0.0
        liq_now = 0.0

        if pairs:
            best = max(
                pairs,
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
            )
            mcap_now = best.get("marketCap") or best.get("fdv") or 0
            liq_now = (best.get("liquidity") or {}).get("usd", 0)

        # Calculate price change
        if mcap_at_signal > 0 and mcap_now > 0:
            price_change_pct = ((mcap_now - mcap_at_signal) / mcap_at_signal) * 100
        elif mcap_at_signal > 0 and mcap_now == 0:
            price_change_pct = -100.0  # token disappeared = rug
        else:
            price_change_pct = 0.0

        record = {
            "token": token,
            "chain": chain,
            "latency_s": entry["latency_s"],
            "mcap_at_signal": mcap_at_signal,
            "mcap_10m": mcap_now,
            "liq_10m": liq_now,
            "price_change_pct": price_change_pct,
            "follow_up_time": time.time(),
        }

        # Classify outcome
        if price_change_pct >= 30:
            record["outcome"] = "TP_HIT"
        elif price_change_pct <= -50:
            record["outcome"] = "RUG"
        elif price_change_pct <= -20:
            record["outcome"] = "DUMP"
        elif abs(price_change_pct) <= 10:
            record["outcome"] = "FLAT"
        elif price_change_pct > 10:
            record["outcome"] = "IMPULSE"
        else:
            record["outcome"] = "CHOP"

        # Store in signal engine
        self.engine.record_post_mortem(record)

        # Fire notification callback (e.g., personal bot)
        if self._on_complete:
            try:
                await self._on_complete(record)
            except Exception as e:
                logger.debug(f"Post-mortem callback error: {e}")
