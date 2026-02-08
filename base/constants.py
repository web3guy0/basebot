"""
All contract addresses, event topics, ABIs, and dangerous selectors for Base Mainnet.
Covers Uniswap V3 + V4. Single source of truth.
"""
from web3 import Web3

# ═══════════════════════════════════════════════════════════════
#  BASE MAINNET ADDRESSES
# ═══════════════════════════════════════════════════════════════

# Native ETH representation in V4 (currency0 = address(0))
ETH_NATIVE = "0x0000000000000000000000000000000000000000"

# Wrapped ETH on Base
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")

# Stablecoins (for ETH/USD price reference)
USDC = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
USDbC = Web3.to_checksum_address("0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA")

# ── Uniswap V3 ─────────────────────────────────────────────────
V3_FACTORY = Web3.to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
V3_SWAP_ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
V3_QUOTER_V2 = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")

# ── Uniswap V4 ─────────────────────────────────────────────────
V4_POOL_MANAGER = Web3.to_checksum_address("0x498581fF718922c3f8e6A244956aF099B2652b2b")
V4_QUOTER = Web3.to_checksum_address("0x0d5e0f971ed27fbff6c2837bf31316121532048d")
V4_UNIVERSAL_ROUTER = Web3.to_checksum_address("0x6ff5693b99212da76ad316178a184ab56d299b43")

# ── Hooks Whitelist (Option C) ──────────────────────────────────
# Only pools whose hooks address is in this set are considered safe.
# address(0) = no hooks = safe by default.
# Add known-safe hook contracts here as the V4 ecosystem matures.
SAFE_HOOKS = {
    ETH_NATIVE,  # address(0) → no hooks
}

# Addresses that represent ETH (native or wrapped) for pair filtering
ETH_ADDRESSES = {
    ETH_NATIVE.lower(),
    WETH.lower(),
}

# ═══════════════════════════════════════════════════════════════
#  EVENT TOPIC HASHES
# ═══════════════════════════════════════════════════════════════

# ── V3 Events ───────────────────────────────────────────────────
# PoolCreated(address indexed token0, address indexed token1,
#             uint24 indexed fee, int24 tickSpacing, address pool)
TOPIC_V3_POOL_CREATED = "0x" + Web3.keccak(
    text="PoolCreated(address,address,uint24,int24,address)"
).hex()

# Swap(address indexed sender, address indexed recipient,
#      int256 amount0, int256 amount1, uint160 sqrtPriceX96,
#      uint128 liquidity, int24 tick)
TOPIC_V3_SWAP = "0x" + Web3.keccak(
    text="Swap(address,address,int256,int256,uint160,uint128,int24)"
).hex()

# ── V4 Events ───────────────────────────────────────────────────
# Initialize(bytes32 indexed id, address indexed currency0,
#            address indexed currency1, uint24 fee, int24 tickSpacing,
#            address hooks, uint160 sqrtPriceX96, int24 tick)
TOPIC_V4_INITIALIZE = "0x" + Web3.keccak(
    text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
).hex()

# Swap(bytes32 indexed id, address indexed sender,
#      int128 amount0, int128 amount1, uint160 sqrtPriceX96,
#      uint128 liquidity, int24 tick, uint24 fee)
TOPIC_V4_SWAP = "0x" + Web3.keccak(
    text="Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
).hex()

# ModifyLiquidity(bytes32 indexed id, address indexed sender,
#                 int24 tickLower, int24 tickUpper,
#                 int256 liquidityDelta, bytes32 salt)
TOPIC_V4_MODIFY_LIQUIDITY = "0x" + Web3.keccak(
    text="ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"
).hex()

# ═══════════════════════════════════════════════════════════════
#  DANGEROUS FUNCTION SELECTORS (for bytecode scanning)
# ═══════════════════════════════════════════════════════════════

DANGEROUS_SELECTORS = {
    "40c10f19": "mint(address,uint256)",
    "44df8e70": "blacklist(address)",
    "e47d6060": "isBlacklisted(address)",
    "3950935e": "setTax(uint256)",
    "0e83672a": "setMaxTxAmount(uint256)",
    "c9567bf9": "openTrading()",
    "1694505e": "uniswapV2Pair()",
    "49bd5a5e": "uniswapV2Router()",
}

# Selectors that are fine by themselves but context-dependent
CONTEXT_SELECTORS = {
    "8da5cb5b": "owner()",
    "715018a6": "renounceOwnership()",
    "f2fde38b": "transferOwnership(address)",
}

# Proxy bytecode patterns (instant reject)
PROXY_PATTERNS = [
    "363d3d373d3d3d363d",  # EIP-1167 minimal proxy
    "5f5f5f5f5f365f5f",    # UUPS proxy pattern
]

# ═══════════════════════════════════════════════════════════════
#  ABIs (minimal, only what we need)
# ═══════════════════════════════════════════════════════════════

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

V3_POOL_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidity",
        "outputs": [{"name": "", "type": "uint128"}],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    },
]

V3_FACTORY_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "token0", "type": "address"},
            {"indexed": True, "name": "token1", "type": "address"},
            {"indexed": True, "name": "fee", "type": "uint24"},
            {"indexed": False, "name": "tickSpacing", "type": "int24"},
            {"indexed": False, "name": "pool", "type": "address"},
        ],
        "name": "PoolCreated",
        "type": "event",
    },
]

# V4 PoolManager events ABI
V4_POOL_MANAGER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "id", "type": "bytes32"},
            {"indexed": True, "name": "currency0", "type": "address"},
            {"indexed": True, "name": "currency1", "type": "address"},
            {"indexed": False, "name": "fee", "type": "uint24"},
            {"indexed": False, "name": "tickSpacing", "type": "int24"},
            {"indexed": False, "name": "hooks", "type": "address"},
            {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "name": "tick", "type": "int24"},
        ],
        "name": "Initialize",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "id", "type": "bytes32"},
            {"indexed": True, "name": "sender", "type": "address"},
            {"indexed": False, "name": "amount0", "type": "int128"},
            {"indexed": False, "name": "amount1", "type": "int128"},
            {"indexed": False, "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "name": "liquidity", "type": "uint128"},
            {"indexed": False, "name": "tick", "type": "int24"},
            {"indexed": False, "name": "fee", "type": "uint24"},
        ],
        "name": "Swap",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "id", "type": "bytes32"},
            {"indexed": True, "name": "sender", "type": "address"},
            {"indexed": False, "name": "tickLower", "type": "int24"},
            {"indexed": False, "name": "tickUpper", "type": "int24"},
            {"indexed": False, "name": "liquidityDelta", "type": "int256"},
            {"indexed": False, "name": "salt", "type": "bytes32"},
        ],
        "name": "ModifyLiquidity",
        "type": "event",
    },
]
