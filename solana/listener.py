"""
Solana Raydium AMM V4 listener.

Detects new pool creation via WebSocket logsSubscribe on the Raydium program.
Flow:
  1. Subscribe to logs mentioning RAYDIUM_AMM_V4 program
  2. For each notification, decode ray_log base64 → check first byte
  3. If log_type == 0 (init): fetch full tx via getTransaction(jsonParsed)
  4. Extract token mint from postTokenBalances, pool address from instruction
     accounts, deployer from first signer
  5. Calculate initial liquidity from ray_log (pc_amount in lamports)
  6. Create SolTokenState → signal engine evaluates

Uses raw WebSocket via aiohttp (no solana-py dependency).
Auto-reconnects with exponential backoff on disconnect.
"""
import asyncio
import base64
import json
import logging
import struct
import time

import aiohttp

from solana.constants import (
    RAYDIUM_AMM_V4,
    WSOL,
    RAY_LOG_INIT,
    RAY_LOG_INIT_PC_AMOUNT_OFFSET,
    RAY_LOG_INIT_COIN_AMOUNT_OFFSET,
    RAY_LOG_INIT_MIN_LENGTH,
)

logger = logging.getLogger("sol_listener")


class SolanaListener:
    """
    Detects new Raydium AMM V4 pool creation on Solana mainnet.

    Only processes initialization events (ray_log type 0).
    Swap/buy tracking is delegated to DexScreener enrichment
    (same pattern as EVM — on-chain detection, off-chain enrichment).
    """

    def __init__(
        self,
        wss_url: str,
        http_url: str,
        state_tracker,
        signal_engine,
        sol_price_fn,
        min_liquidity_sol: float = 10.0,
    ):
        self.wss_url = wss_url
        self.http_url = http_url
        self.tracker = state_tracker
        self.engine = signal_engine
        self.sol_price_fn = sol_price_fn
        self.min_liquidity_sol = min_liquidity_sol
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._rpc_lock = asyncio.Lock()
        self._last_rpc: float = 0.0
        # Stats
        self.pools_detected: int = 0
        self.pools_skipped: int = 0

    async def start(self):
        """Connect to Solana WebSocket and listen for Raydium events."""
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("Starting Solana listener (Raydium AMM V4)")

        backoff = 1
        while self._running:
            try:
                await self._connect_and_listen()
                backoff = 1  # reset on clean exit
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Solana WebSocket error: {e}")
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)

        if self._session and not self._session.closed:
            await self._session.close()

    async def stop(self):
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    async def _connect_and_listen(self):
        """Single WebSocket connection lifecycle."""
        logger.info("Connecting to Solana WebSocket...")

        async with self._session.ws_connect(
            self.wss_url,
            heartbeat=30,
            max_msg_size=0,  # no limit
        ) as ws:
            # Subscribe to Raydium AMM V4 program logs
            await ws.send_json({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [RAYDIUM_AMM_V4]},
                    {"commitment": "confirmed"},
                ],
            })

            # Read subscription confirmation
            resp = await ws.receive_json(timeout=10)
            sub_id = resp.get("result")
            if sub_id is None:
                error = resp.get("error", {})
                logger.error(f"Solana subscription failed: {error}")
                return

            logger.info(f"Solana Raydium subscription active (id={sub_id})")

            # Process incoming messages
            async for msg in ws:
                if not self._running:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_notification(data)
                    except Exception as e:
                        logger.debug(f"Message parse error: {e}")
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    logger.warning("Solana WebSocket closed, reconnecting...")
                    break

    async def _handle_notification(self, data: dict):
        """Process a logsNotification from Solana."""
        if data.get("method") != "logsNotification":
            return

        value = (
            data.get("params", {}).get("result", {}).get("value", {})
        )
        signature = value.get("signature")
        err = value.get("err")
        logs = value.get("logs", [])

        # Skip failed transactions
        if err is not None:
            return

        # Search for ray_log entries in the log lines
        for line in logs:
            if "ray_log:" not in line:
                continue

            try:
                parts = line.split("ray_log: ", 1)
                if len(parts) < 2:
                    continue
                b64_str = parts[1].strip()
                raw = base64.b64decode(b64_str)
                if len(raw) < 1:
                    continue

                log_type = raw[0]

                if log_type == RAY_LOG_INIT:
                    # New pool initialization — process it
                    asyncio.create_task(
                        self._handle_pool_init(signature, raw)
                    )
                    return  # one init per tx

            except Exception as e:
                logger.debug(f"ray_log parse error: {e}")
                continue

    async def _handle_pool_init(self, signature: str, ray_log_data: bytes):
        """Handle a Raydium AMM V4 pool initialization."""

        # ── Parse initial liquidity from ray_log ──────────────
        init_sol_lamports = 0
        init_coin_amount = 0
        try:
            if len(ray_log_data) >= RAY_LOG_INIT_MIN_LENGTH:
                init_sol_lamports = struct.unpack(
                    "<Q",
                    ray_log_data[
                        RAY_LOG_INIT_PC_AMOUNT_OFFSET
                        : RAY_LOG_INIT_PC_AMOUNT_OFFSET + 8
                    ],
                )[0]
                init_coin_amount = struct.unpack(
                    "<Q",
                    ray_log_data[
                        RAY_LOG_INIT_COIN_AMOUNT_OFFSET
                        : RAY_LOG_INIT_COIN_AMOUNT_OFFSET + 8
                    ],
                )[0]
        except Exception:
            pass

        init_sol = init_sol_lamports / 1e9  # lamports → SOL

        # Quick filter: skip tiny pools
        if init_sol < self.min_liquidity_sol:
            self.pools_skipped += 1
            logger.debug(
                f"[sol-skip] {signature[:16]}... liq={init_sol:.2f} SOL "
                f"< min {self.min_liquidity_sol}"
            )
            return

        # ── Fetch full transaction for account details ────────
        tx = await self._rpc_get_transaction(signature)
        if tx is None:
            logger.debug(f"Failed to fetch tx {signature[:16]}...")
            return

        # ── Extract token mints from postTokenBalances ────────
        meta = tx.get("meta", {})
        if meta is None:
            return
        post_balances = meta.get("postTokenBalances", [])

        mints_in_tx = set()
        for bal in post_balances:
            mint = bal.get("mint", "")
            if mint:
                mints_in_tx.add(mint)

        # Must involve WSOL
        if WSOL not in mints_in_tx:
            return

        # Find the non-WSOL mint (the new token)
        token_mints = [m for m in mints_in_tx if m != WSOL]
        if not token_mints:
            return

        token_mint = token_mints[0]

        # Already tracked? Skip duplicate inits
        if self.tracker.get(token_mint) is not None:
            return

        # ── Extract pool address from Raydium instruction ─────
        pool_address = self._extract_pool_address(tx)

        # ── Extract deployer (first signer) ───────────────────
        account_keys = (
            tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        )
        deployer = ""
        if account_keys:
            first_key = account_keys[0]
            deployer = (
                first_key.get("pubkey", first_key)
                if isinstance(first_key, dict)
                else str(first_key)
            )

        # ── Calculate USD liquidity ───────────────────────────
        sol_price = self.sol_price_fn()
        liquidity_usd = init_sol * sol_price * 2  # both sides of pool

        self.pools_detected += 1

        logger.debug(
            f"[sol-pool] {token_mint[:8]}.. liq={init_sol:.1f}SOL(${liquidity_usd:,.0f}) "
            f"deployer={deployer[:8]}.. sig={signature[:12]}.."
        )

        # ── Create token state ────────────────────────────────
        self.tracker.create(
            token_address=token_mint,
            pair_address=pool_address or signature[:32],
            deployer=deployer,
            liquidity_sol=init_sol,
            liquidity_usd=liquidity_usd,
        )

    def _extract_pool_address(self, tx: dict) -> str:
        """Extract Raydium AMM pool address from transaction accounts."""
        try:
            instructions = (
                tx.get("transaction", {})
                .get("message", {})
                .get("instructions", [])
            )
            for ix in instructions:
                program_id = ix.get("programId", "")
                if program_id == RAYDIUM_AMM_V4:
                    accounts = ix.get("accounts", [])
                    if len(accounts) > 4:
                        return accounts[4]  # AMM address at index 4

            # Fallback: check inner instructions
            inner = tx.get("meta", {}).get("innerInstructions", [])
            for group in inner:
                for ix in group.get("instructions", []):
                    if ix.get("programId", "") == RAYDIUM_AMM_V4:
                        accounts = ix.get("accounts", [])
                        if len(accounts) > 4:
                            return accounts[4]
        except Exception:
            pass
        return ""

    async def _rpc_get_transaction(self, signature: str) -> dict | None:
        """Fetch a transaction with jsonParsed encoding. Rate-limited."""
        async with self._rpc_lock:
            # Rate limit: min 100ms between RPC calls
            now = time.time()
            wait = 0.1 - (now - self._last_rpc)
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                async with self._session.post(
                    self.http_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            signature,
                            {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0,
                                "commitment": "confirmed",
                            },
                        ],
                    },
                ) as resp:
                    self._last_rpc = time.time()
                    data = await resp.json()
                    return data.get("result")
            except Exception as e:
                logger.debug(f"RPC getTransaction failed: {e}")
                return None
