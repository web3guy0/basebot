"""
Solana program IDs and addresses for new token detection.

Primary: Raydium AMM V4 (most new Solana token launches).
Stubs:   Raydium CLMM / CP, Orca Whirlpool, Meteora DLMM, Pump.fun.
"""

# ═══════════════════════════════════════════════════════════════
#  SOLANA TOKEN ADDRESSES
# ═══════════════════════════════════════════════════════════════

# Wrapped SOL (SPL token)
WSOL = "So11111111111111111111111111111111111111112"

# SPL Token Programs
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# ═══════════════════════════════════════════════════════════════
#  DEX PROGRAM IDS
# ═══════════════════════════════════════════════════════════════

# Raydium AMM V4 — primary listener (most new token launches)
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Raydium CLMM (concentrated liquidity) — future stub
RAYDIUM_CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"

# Raydium Constant Product (newer) — future stub
RAYDIUM_CP = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"

# Pump.fun program — future stub (token graduation → Raydium pool)
PUMP_FUN = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Orca Whirlpool — future stub
ORCA_WHIRLPOOL = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"

# Meteora DLMM — future stub
METEORA_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"

# ═══════════════════════════════════════════════════════════════
#  RAY_LOG TYPES (Raydium AMM V4 on-chain event discriminators)
#
#  When Raydium logs "ray_log: <base64>", the first byte = type.
#  We decode base64 → check byte[0] to identify the event.
# ═══════════════════════════════════════════════════════════════

RAY_LOG_INIT = 0            # Pool initialization (new pool)
RAY_LOG_DEPOSIT = 1         # Add liquidity
RAY_LOG_WITHDRAW = 2        # Remove liquidity
RAY_LOG_SWAP_BASE_IN = 3    # Swap (exact input)
RAY_LOG_SWAP_BASE_OUT = 4   # Swap (exact output)

# ═══════════════════════════════════════════════════════════════
#  RAYDIUM INITIALIZE2 — RAY_LOG DATA LAYOUT
#
#  Byte offsets (little-endian):
#    0        : log_type (u8)   = 0
#    1-8      : open_time (u64)
#    9        : pc_decimals (u8)
#    10       : coin_decimals (u8)
#    11-18    : pc_lot_size (u64)
#    19-26    : coin_lot_size (u64)
#    27-34    : pc_amount (u64)  — initial SOL/WSOL liquidity (lamports)
#    35-42    : coin_amount (u64) — initial token liquidity
#    43-74    : market pubkey (32 bytes)
# ═══════════════════════════════════════════════════════════════

RAY_LOG_INIT_PC_AMOUNT_OFFSET = 27     # Start byte for pc_amount (SOL)
RAY_LOG_INIT_COIN_AMOUNT_OFFSET = 35   # Start byte for coin_amount (token)
RAY_LOG_INIT_MIN_LENGTH = 43           # Minimum bytes for a valid init log

# ═══════════════════════════════════════════════════════════════
#  RAYDIUM INITIALIZE2 — INSTRUCTION ACCOUNT INDICES
#
#  When the Raydium AMM V4 instruction is found in a parsed tx,
#  these indices map the instruction's accounts array.
# ═══════════════════════════════════════════════════════════════

RAYDIUM_IX_AMM = 4          # Pool / AMM address
RAYDIUM_IX_COIN_MINT = 8    # Token mint (the new token)
RAYDIUM_IX_PC_MINT = 9      # Quote mint (usually WSOL)
RAYDIUM_IX_USER_WALLET = 17 # Deployer wallet (last account)
