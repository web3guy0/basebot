"""
Configuration loader — reads .env and exposes all settings.
"""
import os
from dotenv import load_dotenv

load_dotenv()


# ── Blockchain RPC ──────────────────────────────────────────────
RPC_WSS = os.getenv("RPC_WSS", "wss://base-mainnet.g.alchemy.com/v2/YOUR_KEY")
RPC_HTTP = os.getenv("RPC_HTTP", "https://mainnet.base.org")
CHAIN_ID = 8453  # Base Mainnet

# ── Telegram ────────────────────────────────────────────────────
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "signal_session")
BASED_BOT_USERNAME = os.getenv("BASED_BOT_USERNAME", "BasedBot")

# ── Signal Thresholds ──────────────────────────────────────────
MAX_TOKEN_AGE_SECONDS = int(os.getenv("MAX_TOKEN_AGE_SECONDS", "180"))
MAX_MCAP_USD = float(os.getenv("MAX_MCAP_USD", "30000"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "3000"))
MIN_BUYS = int(os.getenv("MIN_BUYS", "2"))
MIN_LARGEST_BUY_PCT = float(os.getenv("MIN_LARGEST_BUY_PCT", "10"))  # % of liquidity
MAX_SIGNALS_PER_HOUR = int(os.getenv("MAX_SIGNALS_PER_HOUR", "5"))

# ── Anti-Spam ──────────────────────────────────────────────────
IGNORE_LIQUIDITY_BELOW_USD = float(os.getenv("IGNORE_LIQUIDITY_BELOW_USD", "2000"))
MAX_DEPLOYER_TOKENS_24H = int(os.getenv("MAX_DEPLOYER_TOKENS_24H", "2"))
# ── Latency Cutoff ─────────────────────────────────────────────────
# If signal latency (pool creation → signal) exceeds this, skip it.
# Set to 0 to disable (allow any latency within MAX_TOKEN_AGE_SECONDS).
# Recommended: start at 0, then tighten to 90 after reviewing latency data.
MAX_SIGNAL_LATENCY_SECONDS = int(os.getenv("MAX_SIGNAL_LATENCY_SECONDS", "0"))
# ── Solana ─────────────────────────────────────────────────────
SOL_ENABLED = os.getenv("SOL_ENABLED", "false").lower() == "true"
# Helius recommended (free tier: 100k credits/day). Public endpoint is unreliable.
SOL_RPC_WSS = os.getenv("SOL_RPC_WSS", "wss://api.mainnet-beta.solana.com")
SOL_RPC_HTTP = os.getenv("SOL_RPC_HTTP", "https://api.mainnet-beta.solana.com")
# Solana-specific thresholds (faster chain = tighter windows)
SOL_MAX_TOKEN_AGE_SECONDS = int(os.getenv("SOL_MAX_TOKEN_AGE_SECONDS", "120"))
SOL_MIN_LIQUIDITY_SOL = float(os.getenv("SOL_MIN_LIQUIDITY_SOL", "10"))
# ── Mode ───────────────────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# ── Logging ────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
