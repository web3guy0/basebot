"""
Solana token state tracker.

Mirrors the EVM TokenState interface so the shared SignalEngine
works identically for both chains without branching.
"""
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("sol_state")


@dataclass
class SolTokenState:
    """
    Token state for Solana tokens.

    Interface-compatible with EVM TokenState — exposes the same properties
    (best_mcap, best_liquidity, best_buys, bytecode_safe, has_momentum, etc.)
    so the shared SignalEngine can evaluate both chains uniformly.
    """

    token_address: str       # SPL token mint address
    pair_address: str        # Raydium AMM pool address
    first_seen: float        # unix timestamp
    dex_version: str = "solana-raydium"  # identifies chain + dex

    # ── On-chain tracked ────────────────────────────────────
    liquidity_sol: float = 0.0
    liquidity_usd: float = 0.0
    estimated_mcap: float = 0.0
    total_buys: int = 0
    total_sells: int = 0
    buy_volume_usd: float = 0.0
    largest_buy_usd: float = 0.0
    unique_buyers: set = field(default_factory=set)
    deployer_address: str = ""

    # ── Solana-specific safety ──────────────────────────────
    # None = revoked (safe), "unchecked" = not yet checked, str = address (unsafe)
    mint_authority: str | None = "unchecked"
    freeze_authority: str | None = "unchecked"

    # ── Signal engine compatibility (mapped from Solana safety) ──
    bytecode_safe: bool | None = None   # True = both authorities revoked
    is_honeypot: bool | None = None

    # ── EVM compat fields (unused, but required by signal engine) ──
    hooks_address: str = "0x" + "0" * 40
    sqrt_price_x96: int = 0

    # ── DexScreener enrichment (filled async) ───────────────
    ds_mcap: float | None = None
    ds_liquidity_usd: float | None = None
    ds_buys_m5: int | None = None
    ds_sells_m5: int | None = None
    ds_volume_m5: float | None = None
    ds_last_fetch: float = 0.0

    # ── Signal state ────────────────────────────────────────
    signaled: bool = False
    signal_time: float = 0.0

    # ── Swap timestamps for momentum detection ──────────────
    recent_buy_times: list = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.first_seen

    @property
    def best_mcap(self) -> float:
        if self.ds_mcap is not None and self.ds_mcap > 0:
            return self.ds_mcap
        return self.estimated_mcap

    @property
    def best_liquidity(self) -> float:
        if self.ds_liquidity_usd is not None and self.ds_liquidity_usd > 0:
            return self.ds_liquidity_usd
        return self.liquidity_usd

    @property
    def best_buys(self) -> int:
        if self.ds_buys_m5 is not None:
            return max(self.total_buys, self.ds_buys_m5)
        return self.total_buys

    def has_momentum(self) -> bool:
        """Check optional momentum conditions (any one = True)."""
        now = time.time()
        recent = [t for t in self.recent_buy_times if now - t <= 30]
        if len(recent) >= 2:
            return True
        liq = self.best_liquidity
        if liq > 0 and self.buy_volume_usd >= liq * 0.20:
            return True
        if self.total_buys > len(self.unique_buyers) and self.total_buys >= 2:
            return True
        return False

    def update_safety(self):
        """Map Solana mint/freeze authority status → bytecode_safe."""
        if self.mint_authority == "unchecked" or self.freeze_authority == "unchecked":
            self.bytecode_safe = None  # not checked yet
        elif self.mint_authority is None and self.freeze_authority is None:
            self.bytecode_safe = True  # both revoked = safe
        else:
            self.bytecode_safe = False  # authority still active = unsafe


class SolTokenStateTracker:
    """
    In-memory Solana token state tracker with TTL eviction.
    Shorter TTL than EVM (200s vs 300s) due to faster Solana block times.
    """

    def __init__(self, max_age: int = 200):
        self.states: dict[str, SolTokenState] = {}
        self.max_age = max_age
        self._deployer_history: dict[str, dict[str, float]] = {}  # deployer -> {token -> timestamp}

    def get(self, token_address: str) -> SolTokenState | None:
        state = self.states.get(token_address)
        if state is None:
            return None
        if time.time() - state.first_seen > self.max_age:
            del self.states[token_address]
            return None
        return state

    def create(
        self,
        token_address: str,
        pair_address: str,
        deployer: str = "",
        liquidity_sol: float = 0.0,
        liquidity_usd: float = 0.0,
    ) -> SolTokenState:
        if token_address in self.states:
            return self.states[token_address]

        state = SolTokenState(
            token_address=token_address,
            pair_address=pair_address,
            first_seen=time.time(),
            deployer_address=deployer,
            liquidity_sol=liquidity_sol,
            liquidity_usd=liquidity_usd,
        )
        self.states[token_address] = state
        logger.info(
            f"[sol-new] {token_address[:8]}... | pool={pair_address[:8]}... | "
            f"liq={liquidity_sol:.2f} SOL (${liquidity_usd:,.0f})"
        )
        return state

    def record_buy(
        self, token_address: str, buyer: str, amount_usd: float
    ) -> SolTokenState | None:
        state = self.get(token_address)
        if state is None:
            return None
        state.total_buys += 1
        state.buy_volume_usd += amount_usd
        state.largest_buy_usd = max(state.largest_buy_usd, amount_usd)
        state.unique_buyers.add(buyer)
        state.recent_buy_times.append(time.time())
        cutoff = time.time() - 60
        state.recent_buy_times = [t for t in state.recent_buy_times if t > cutoff]
        return state

    def record_sell(self, token_address: str) -> SolTokenState | None:
        state = self.get(token_address)
        if state is None:
            return None
        state.total_sells += 1
        return state

    def record_deployer(self, deployer: str, token_address: str) -> int:
        """Track deployer activity. Idempotent per (deployer, token) pair.
        Returns number of unique tokens by this deployer in last 24h."""
        now = time.time()
        if deployer not in self._deployer_history:
            self._deployer_history[deployer] = {}
        # Only record once per token (idempotent across multiple evaluate() calls)
        if token_address not in self._deployer_history[deployer]:
            self._deployer_history[deployer][token_address] = now
        # Count unique tokens in last 24h
        cutoff = now - 86400
        self._deployer_history[deployer] = {
            tok: ts for tok, ts in self._deployer_history[deployer].items() if ts > cutoff
        }
        return len(self._deployer_history[deployer])

    def evict_stale(self):
        """Remove tokens older than max_age."""
        stale = [
            addr for addr, state in self.states.items()
            if time.time() - state.first_seen > self.max_age
        ]
        for addr in stale:
            del self.states[addr]
        if stale:
            logger.debug(f"Evicted {len(stale)} stale Solana tokens")

    @property
    def active_count(self) -> int:
        return len(self.states)
