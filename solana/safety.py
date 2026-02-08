"""
Solana token safety checks.

Primary check: mint authority and freeze authority must be revoked (None).
If mint authority exists → deployer can mint infinite tokens → dump.
If freeze authority exists → deployer can freeze your account → you can't sell.
Both must be None for the token to pass safety.

Uses raw Solana JSON-RPC (getAccountInfo with jsonParsed encoding)
via aiohttp. No solana-py dependency.
"""
import asyncio
import logging

import aiohttp

logger = logging.getLogger("sol_safety")


class SolSafetyChecker:
    """Check SPL token mint/freeze authorities via Solana RPC."""

    def __init__(self, rpc_http: str):
        self.rpc_http = rpc_http
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def check_mint(self, mint_address: str) -> dict:
        """
        Fetch SPL token mint info via getAccountInfo(jsonParsed).

        Returns dict with:
            safe: bool | None — True if both authorities revoked
            mint_authority: str | None
            freeze_authority: str | None
            supply: int
            decimals: int
            reasons: list[str] — why it's unsafe (if any)
        """
        await self._ensure_session()

        # Rate limit: min 100ms between RPC calls
        import time
        async with self._lock:
            now = time.time()
            wait = 0.1 - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                async with self._session.post(
                    self.rpc_http,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getAccountInfo",
                        "params": [
                            mint_address,
                            {"encoding": "jsonParsed"},
                        ],
                    },
                ) as resp:
                    self._last_call = time.time()
                    data = await resp.json()

                result = data.get("result", {})
                value = result.get("value")

                if value is None:
                    return {"safe": False, "reason": "Account not found"}

                parsed = value.get("data", {}).get("parsed", {})
                info = parsed.get("info", {})

                mint_authority = info.get("mintAuthority")
                freeze_authority = info.get("freezeAuthority")
                supply = info.get("supply", "0")
                decimals = info.get("decimals", 0)

                return {
                    "safe": mint_authority is None and freeze_authority is None,
                    "mint_authority": mint_authority,
                    "freeze_authority": freeze_authority,
                    "supply": int(supply),
                    "decimals": decimals,
                    "reasons": self._build_reasons(mint_authority, freeze_authority),
                }

            except Exception as e:
                logger.debug(
                    f"Solana safety check failed for {mint_address[:8]}...: {e}"
                )
                return {"safe": None, "reason": f"RPC error: {e}"}

    @staticmethod
    def _build_reasons(mint_auth, freeze_auth) -> list[str]:
        reasons = []
        if mint_auth is not None:
            reasons.append(f"Mint authority active: {mint_auth[:8]}...")
        if freeze_auth is not None:
            reasons.append(f"Freeze authority active: {freeze_auth[:8]}...")
        return reasons


async def run_sol_safety_check(checker: SolSafetyChecker, state) -> None:
    """
    Background safety check for a Solana token.
    Updates state.bytecode_safe, state.mint_authority, state.freeze_authority.
    """
    try:
        result = await asyncio.wait_for(
            checker.check_mint(state.token_address),
            timeout=10.0,
        )

        state.mint_authority = result.get("mint_authority")
        state.freeze_authority = result.get("freeze_authority")
        state.update_safety()

        if state.bytecode_safe is False:
            reasons = result.get("reasons", [])
            logger.info(
                f"[sol-unsafe] {state.token_address[:8]}... — "
                f"{', '.join(reasons[:3])}"
            )
        elif state.bytecode_safe is True:
            logger.debug(
                f"[sol-safe] {state.token_address[:8]}... authorities revoked"
            )

    except asyncio.TimeoutError:
        logger.debug(
            f"Solana safety check timed out for {state.token_address[:8]}..."
        )
        state.bytecode_safe = None
    except Exception as e:
        logger.debug(f"Solana safety check failed: {e}")
        state.bytecode_safe = None
