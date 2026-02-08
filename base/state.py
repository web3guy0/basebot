"""
In-memory token state tracker.
Each discovered token gets a TokenState object tracking buys, volume, age, etc.
Evicted after MAX_TOKEN_AGE_SECONDS to keep memory bounded.
"""
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("state")


@dataclass
class TokenState:
    token_address: str
    pair_address: str  # V3 pool address or V4 pool_id hex
    first_seen: float  # unix timestamp
    dex_version: str  # "v3" or "v4"

    # On-chain tracked
    liquidity_usd: float = 0.0
    estimated_mcap: float = 0.0
    total_buys: int = 0
    total_sells: int = 0
    buy_volume_usd: float = 0.0
    largest_buy_usd: float = 0.0
    unique_buyers: set = field(default_factory=set)
    deployer_address: str = ""

    # V4-specific
    hooks_address: str = "0x0000000000000000000000000000000000000000"
    sqrt_price_x96: int = 0

    # DexScreener enrichment (filled async)
    ds_mcap: float | None = None
    ds_liquidity_usd: float | None = None
    ds_buys_m5: int | None = None
    ds_sells_m5: int | None = None
    ds_volume_m5: float | None = None
    ds_last_fetch: float = 0.0

    # Safety
    bytecode_safe: bool | None = None  # None = not checked yet
    is_honeypot: bool | None = None

    # Signal state
    signaled: bool = False
    signal_time: float = 0.0

    # Swap timestamps for momentum detection
    recent_buy_times: list = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.first_seen

    @property
    def best_mcap(self) -> float:
        """Use DexScreener mcap if available, else on-chain estimate."""
        if self.ds_mcap is not None and self.ds_mcap > 0:
            return self.ds_mcap
        return self.estimated_mcap

    @property
    def best_liquidity(self) -> float:
        """Use DexScreener liquidity if available, else on-chain estimate."""
        if self.ds_liquidity_usd is not None and self.ds_liquidity_usd > 0:
            return self.ds_liquidity_usd
        return self.liquidity_usd

    @property
    def best_buys(self) -> int:
        """Use on-chain buy count (more real-time) or DexScreener if higher."""
        if self.ds_buys_m5 is not None:
            return max(self.total_buys, self.ds_buys_m5)
        return self.total_buys

    def has_momentum(self) -> bool:
        """Check optional momentum conditions (any one = True)."""
        now = time.time()
        # ≥ 2 buys within last 30 seconds
        recent = [t for t in self.recent_buy_times if now - t <= 30]
        if len(recent) >= 2:
            return True
        # Buy volume ≥ 20% of liquidity
        liq = self.best_liquidity
        if liq > 0 and self.buy_volume_usd >= liq * 0.20:
            return True
        # Same wallet bought twice
        if self.total_buys > len(self.unique_buyers) and self.total_buys >= 2:
            return True
        return False


class TokenStateTracker:
    """In-memory dictionary of token states with TTL eviction."""

    def __init__(self, max_age: int = 300):
        self.states: dict[str, TokenState] = {}
        self.max_age = max_age  # eviction TTL (seconds), slightly > signal window
        self._deployer_history: dict[str, dict[str, float]] = {}  # deployer -> {token -> timestamp}

    def get(self, token_address: str) -> TokenState | None:
        """Get token state by address. Returns None if not found or expired (TTL enforced)."""
        addr = token_address.lower()
        state = self.states.get(addr)
        if state is None:
            return None
        # Hard TTL: if age > max_age, drop immediately and return None
        if time.time() - state.first_seen > self.max_age:
            del self.states[addr]
            return None
        return state

    def create(
        self,
        token_address: str,
        pair_address: str,
        dex_version: str,
        hooks_address: str = "0x0000000000000000000000000000000000000000",
        sqrt_price_x96: int = 0,
        deployer: str = "",
    ) -> TokenState:
        addr = token_address.lower()
        if addr in self.states:
            return self.states[addr]

        state = TokenState(
            token_address=addr,
            pair_address=pair_address,
            first_seen=time.time(),
            dex_version=dex_version,
            hooks_address=hooks_address,
            sqrt_price_x96=sqrt_price_x96,
            deployer_address=deployer.lower() if deployer else "",
        )
        self.states[addr] = state
        logger.info(
            f"[new-token] {dex_version} | {addr[:10]}... | pair={pair_address[:10]}..."
        )
        return state

    def record_buy(
        self,
        token_address: str,
        buyer: str,
        amount_usd: float,
    ) -> TokenState | None:
        state = self.get(token_address)
        if state is None:
            return None

        state.total_buys += 1
        state.buy_volume_usd += amount_usd
        state.largest_buy_usd = max(state.largest_buy_usd, amount_usd)
        state.unique_buyers.add(buyer.lower())
        state.recent_buy_times.append(time.time())

        # Trim old timestamps (keep last 60s)
        cutoff = time.time() - 60
        state.recent_buy_times = [t for t in state.recent_buy_times if t > cutoff]

        return state

    def record_sell(self, token_address: str) -> TokenState | None:
        state = self.get(token_address)
        if state is None:
            return None
        state.total_sells += 1
        return state

    def record_deployer(self, deployer: str, token_address: str) -> int:
        """Track deployer activity. Idempotent per (deployer, token) pair.
        Returns number of unique tokens by this deployer in last 24h."""
        addr = deployer.lower()
        now = time.time()
        if addr not in self._deployer_history:
            self._deployer_history[addr] = {}
        # Only record once per token (idempotent across multiple evaluate() calls)
        if token_address not in self._deployer_history[addr]:
            self._deployer_history[addr][token_address] = now
        # Count unique tokens in last 24h
        cutoff = now - 86400
        self._deployer_history[addr] = {
            tok: ts for tok, ts in self._deployer_history[addr].items() if ts > cutoff
        }
        return len(self._deployer_history[addr])

    def evict_stale(self):
        """Remove tokens older than max_age. Call periodically."""
        stale = [
            addr
            for addr, state in self.states.items()
            if state.age_seconds > self.max_age
        ]
        for addr in stale:
            del self.states[addr]
        if stale:
            logger.debug(f"Evicted {len(stale)} stale tokens")

    @property
    def active_count(self) -> int:
        return len(self.states)
