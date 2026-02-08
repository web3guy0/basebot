"""
Signal engine — core decision logic.
Evaluates TokenState against hard rules + anti-spam guards.
On trigger, enqueues the contract address for Telegram delivery.
"""
import asyncio
import logging
import time

import config

logger = logging.getLogger("signal")


class SignalEngine:
    """
    Evaluates tokens against signal rules.
    Called on every Swap event update AND every DexScreener poll.
    """

    def __init__(self, state_tracker=None):
        # Signal output queue — Telegram sender consumes from here
        self.signal_queue: asyncio.Queue[str] = asyncio.Queue()
        # State tracker reference (for deployer spam check)
        self.tracker = state_tracker
        # Anti-spam: track signals per hour
        self._signal_timestamps: list[float] = []
        # Stats
        self.total_evaluated: int = 0
        self.total_signaled: int = 0
        self.total_rejected: int = 0
        self._reject_reasons: dict[str, int] = {}
        # Time-to-signal tracking (seconds from pool creation → signal)
        self._signal_latencies: list[float] = []

    async def evaluate(self, state) -> bool:
        """
        Evaluate a TokenState against signal rules.
        Returns True if signal was fired, False otherwise.
        Thread-safe via asyncio (single-threaded event loop).
        """
        self.total_evaluated += 1

        # ── Already signaled — once signaled=True, this token is permanently ignored ──
        if state.signaled:
            return False

        token = state.token_address

        # ── HARD CONDITIONS (ALL REQUIRED) ──

        # 1. Token age ≤ 180 seconds
        age = state.age_seconds
        if age > config.MAX_TOKEN_AGE_SECONDS:
            self._reject(token, "too_old", f"age={age:.0f}s")
            return False

        # 2. Market cap ≤ 30,000 USD
        mcap = state.best_mcap
        if mcap > config.MAX_MCAP_USD and mcap > 0:
            self._reject(token, "mcap_high", f"mcap=${mcap:.0f}")
            return False

        # 3. Liquidity ≥ 3,000 USD
        liquidity = state.best_liquidity
        if liquidity < config.MIN_LIQUIDITY_USD:
            # Don't log this as rejection — it's the most common pre-condition
            return False

        # 4. Ignore liquidity below absolute minimum
        if liquidity < config.IGNORE_LIQUIDITY_BELOW_USD:
            return False

        # 5. Total buys ≥ 2
        buys = state.best_buys
        if buys < config.MIN_BUYS:
            return False

        # 6. Largest single buy ≥ 10% of liquidity
        if liquidity > 0:
            largest_buy_pct = (state.largest_buy_usd / liquidity) * 100
        else:
            largest_buy_pct = 0

        if largest_buy_pct < config.MIN_LARGEST_BUY_PCT:
            self._reject(token, "weak_buy", f"largest={largest_buy_pct:.1f}%")
            return False

        # ── ANTI-SPAM GUARDS ──

        # Max signals per hour
        now = time.time()
        self._signal_timestamps = [
            t for t in self._signal_timestamps if now - t < 3600
        ]
        if len(self._signal_timestamps) >= config.MAX_SIGNALS_PER_HOUR:
            self._reject(token, "rate_limited", "max signals/hour reached")
            return False

        # Deployer spam check — reject if deployer launched too many tokens in 24h
        if state.deployer_address and self.tracker:
            deployer_count = self.tracker.record_deployer(state.deployer_address)
            if deployer_count > config.MAX_DEPLOYER_TOKENS_24H:
                self._reject(token, "deployer_spam", f"deployer launched {deployer_count} tokens in 24h")
                return False

        # Bytecode safety (non-blocking — only blocks if result available)
        if state.bytecode_safe is False:
            self._reject(token, "unsafe_bytecode", "failed safety check")
            return False

        # DexScreener honeypot proxy: if sells exist, probably not a honeypot
        # If DexScreener shows 0 sells with >5 buys, suspicious
        if state.ds_sells_m5 is not None and state.ds_buys_m5 is not None:
            if state.ds_buys_m5 > 5 and state.ds_sells_m5 == 0:
                self._reject(token, "no_sells", "possible honeypot (0 sells)")
                return False

        # ── SIGNAL TRIGGERED ──

        # ── SIGNAL TRIGGERED — mark permanently, one signal per token ──
        state.signaled = True
        state.signal_time = now
        self._signal_timestamps.append(now)
        self.total_signaled += 1

        # Track time-to-signal (pool creation → signal fire)
        time_to_signal = now - state.first_seen
        self._signal_latencies.append(time_to_signal)

        momentum = state.has_momentum()

        logger.info(
            f"{'='*60}\n"
            f"  🎯 SIGNAL FIRED\n"
            f"  Token:     {state.token_address}\n"
            f"  Version:   {state.dex_version}\n"
            f"  Age:       {age:.0f}s\n"
            f"  Mcap:      ${mcap:,.0f}\n"
            f"  Liquidity: ${liquidity:,.0f}\n"
            f"  Buys:      {buys} (unique: {len(state.unique_buyers)})\n"
            f"  Largest:   ${state.largest_buy_usd:,.0f} ({largest_buy_pct:.1f}% of liq)\n"
            f"  Volume:    ${state.buy_volume_usd:,.0f}\n"
            f"  Momentum:  {'YES' if momentum else 'no'}\n"
            f"  Latency:   {time_to_signal:.1f}s (pool → signal)\n"
            f"  Hooks:     {state.hooks_address[:10] if state.hooks_address != '0x'+'0'*40 else 'none'}\n"
            f"{'='*60}"
        )

        # Enqueue for Telegram
        await self.signal_queue.put(state.token_address)
        return True

    def _reject(self, token: str, reason: str, detail: str = ""):
        """Track rejection reason for debugging."""
        self._reject_reasons[reason] = self._reject_reasons.get(reason, 0) + 1
        self.total_rejected += 1
        if detail:
            logger.debug(f"[skip] {token[:10]}... {reason}: {detail}")

    def get_stats(self) -> dict:
        stats = {
            "evaluated": self.total_evaluated,
            "signaled": self.total_signaled,
            "rejected": self.total_rejected,
            "reject_reasons": dict(self._reject_reasons),
            "signals_this_hour": len(self._signal_timestamps),
        }
        # Time-to-signal metrics
        if self._signal_latencies:
            stats["avg_latency_s"] = round(sum(self._signal_latencies) / len(self._signal_latencies), 1)
            stats["min_latency_s"] = round(min(self._signal_latencies), 1)
            stats["max_latency_s"] = round(max(self._signal_latencies), 1)
        return stats
