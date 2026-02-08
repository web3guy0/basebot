# Early Token Signal Detector

Real-time detector for new tokens on **Base** (Uniswap V3 + V4). Catches tokens within 3 minutes of pool creation at <$30k market cap, evaluates early buy activity and contract safety, then sends the contract address to [Based Bot](https://t.me/BasedBot) on Telegram for execution.

**Signal only** — this bot does NOT trade. It detects and forwards.

---

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                    WebSocket (Base RPC)                      │
│                                                             │
│  V4 PoolManager ──► Initialize (new pool)                   │
│                 ──► Swap (buy/sell tracking)                 │
│                                                             │
│  V3 Factory    ──► PoolCreated (new pool)                   │
│  Global Swaps  ──► Swap (filtered for tracked pools)        │
└────────────────────────┬────────────────────────────────────┘
                         │
                    ┌────▼────┐
                    │  State  │  In-memory per-token tracking
                    │ Tracker │  (buys, volume, liquidity, age)
                    └────┬────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
        ┌─────▼───┐ ┌───▼────┐ ┌──▼──────────┐
        │ Safety  │ │ Signal │ │ DexScreener  │
        │ Checker │ │ Engine │ │  Enricher    │
        │bytecode │ │ rules  │ │ mcap/liq/txns│
        └─────────┘ └───┬────┘ └─────────────┘
                         │
                    ┌────▼─────┐
                    │ Telegram │  Sends contract address
                    │  Sender  │  to Based Bot via MTProto
                    └──────────┘
```

### Signal Rules (ALL must pass)

| Condition | Threshold | Source |
|---|---|---|
| Token age | ≤ 180 seconds | On-chain (block timestamp) |
| Market cap | ≤ $30,000 | DexScreener or on-chain estimate |
| Liquidity | ≥ $3,000 | DexScreener or on-chain estimate |
| Buy count | ≥ 2 | On-chain swap events |
| Largest buy | ≥ 10% of liquidity | On-chain swap events |
| Bytecode | No critical dangerous patterns | eth_getCode analysis |
| Honeypot proxy | Not 0 sells with >5 buys | DexScreener txn data |
| Deployer | ≤ 2 tokens in 24h | On-chain tracking |
| Rate limit | ≤ 5 signals/hour | Internal counter |

### Anti-Spam

- Deployer history tracking (rejects serial launchers)
- Hourly signal rate limiting
- Bytecode scanning for mint(), blacklist(), setTax(), proxy patterns
- DexScreener sell-count honeypot detection
- V4 hooks whitelist (only `address(0)` = hookless pools by default)

---

## Architecture

| File | Purpose |
|---|---|
| `main.py` | Orchestrator — connects WebSocket, launches all tasks |
| `config.py` | Loads `.env`, exposes all thresholds |
| `constants.py` | Base addresses, event topics, ABIs, dangerous selectors |
| `state.py` | `TokenState` dataclass + `TokenStateTracker` dict with TTL |
| `signal_engine.py` | Core decision logic — evaluates rules, enqueues signals |
| `v4_listener.py` | V4 PoolManager Initialize + Swap handler |
| `v3_listener.py` | V3 Factory PoolCreated + global Swap handler |
| `dexscreener.py` | REST enrichment (mcap, liquidity, txns) every 8s |
| `safety.py` | Bytecode scanning for dangerous function selectors |
| `price_utils.py` | Shared mcap/liquidity estimation from sqrtPriceX96 |
| `telegram_sender.py` | Telethon MTProto sender to Based Bot |

### Key Design Decisions

- **On-chain primary, DexScreener secondary**: On-chain events arrive in ~2s (Base block time). DexScreener data has 30-60s indexing lag but provides cleaner mcap/liquidity numbers.
- **Single WebSocket connection**: All 4 subscriptions (V4 Init, V4 Swap, V3 PoolCreated, V3 Swap) share one WebSocket via web3's subscription manager.
- **Telethon MTProto** (user account): Sends messages that look like manual chat. NOT the Telegram Bot API.
- **V4 hooks whitelist**: Only `address(0)` is whitelisted. Custom hooks are skipped since malicious hooks could manipulate pricing. Expand the whitelist in `constants.py` → `SAFE_HOOKS` as trusted hooks emerge.
- **In-memory only**: No database. Tokens are evicted after 300s. The bot is stateless across restarts.

---

## Setup

### Prerequisites

- Python 3.11+
- Base Mainnet WebSocket RPC (Alchemy or QuickNode recommended)
- Telegram account + API credentials (for live mode)

### Install

```bash
git clone <repo-url> && cd basebot
pip install -r requirements.txt
cp .env.example .env
```

### Configure `.env`

**Required for any mode:**
```
RPC_WSS=wss://base-mainnet.g.alchemy.com/v2/YOUR_KEY
```

**Required for live mode (Telegram sends):**
```
TELEGRAM_API_ID=12345678          # from https://my.telegram.org
TELEGRAM_API_HASH=abcdef123456    # from https://my.telegram.org
BASED_BOT_USERNAME=BasedBot       # exact Telegram username
DRY_RUN=false
```

### Get Telegram Credentials

1. Go to [https://my.telegram.org](https://my.telegram.org)
2. Log in with your phone number
3. Click **API development tools**
4. Create an application → note the `api_id` and `api_hash`
5. On first run with `DRY_RUN=false`, Telethon will prompt for your phone number and OTP code to create a session file

---

## Running

### Dry Run (recommended first)

```bash
python main.py
```

With `DRY_RUN=true` (default), signals are logged to console but NOT sent to Telegram. Use this to verify detection is working.

### Live Mode

```bash
DRY_RUN=false python main.py
```

Signals will be sent as raw contract addresses to Based Bot on Telegram.

### What You'll See

```
12:00:01 | main           | INFO  | ============================================================
12:00:01 | main           | INFO  |   EARLY TOKEN SIGNAL DETECTOR
12:00:01 | main           | INFO  |   Chain:      Base (8453)
12:00:01 | main           | INFO  |   Mode:       DRY RUN
12:00:01 | main           | INFO  | ============================================================
12:00:02 | main           | INFO  | Connected to Base | Block: 12345678
12:00:02 | main           | INFO  | ETH price: $2,500
12:00:02 | v4_listener    | INFO  | V4 subscriptions registered (Initialize + Swap)
12:00:02 | v3_listener    | INFO  | V3 subscriptions registered (PoolCreated + Swap)
12:00:02 | main           | INFO  | All systems running. Waiting for new tokens...
12:00:15 | state          | INFO  | [new-token] v4 | 0xabcdef12... | pair=0x789abc...
12:00:45 | signal         | INFO  | ============================================================
12:00:45 | signal         | INFO  |   🎯 SIGNAL FIRED
12:00:45 | signal         | INFO  |   Token:     0xabcdef1234567890abcdef1234567890abcdef12
12:00:45 | signal         | INFO  |   Age:       30s
12:00:45 | signal         | INFO  |   Mcap:      $12,000
12:00:45 | signal         | INFO  |   Liquidity: $5,000
12:00:45 | signal         | INFO  |   Buys:      3 (unique: 2)
12:00:45 | signal         | INFO  | ============================================================
```

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `RPC_WSS` | — | WebSocket RPC endpoint (required) |
| `RPC_HTTP` | `https://mainnet.base.org` | HTTP RPC for bytecode reads |
| `TELEGRAM_API_ID` | `0` | Telegram API ID from my.telegram.org |
| `TELEGRAM_API_HASH` | — | Telegram API hash |
| `BASED_BOT_USERNAME` | `BasedBot` | Telegram bot to send signals to |
| `MAX_TOKEN_AGE_SECONDS` | `180` | Max token age for signal (seconds) |
| `MAX_MCAP_USD` | `30000` | Max market cap for signal ($) |
| `MIN_LIQUIDITY_USD` | `3000` | Min pool liquidity ($) |
| `MIN_BUYS` | `2` | Min buy transactions |
| `MIN_LARGEST_BUY_PCT` | `10` | Min largest buy as % of liquidity |
| `MAX_SIGNALS_PER_HOUR` | `5` | Signal rate limit |
| `IGNORE_LIQUIDITY_BELOW_USD` | `2000` | Skip tokens below this liquidity |
| `MAX_DEPLOYER_TOKENS_24H` | `2` | Max tokens per deployer in 24h |
| `DRY_RUN` | `true` | Log-only mode (no Telegram sends) |
| `LOG_LEVEL` | `INFO` | Logging verbosity (DEBUG/INFO/WARNING) |

---

## Uniswap Contracts (Base Mainnet)

| Contract | Address |
|---|---|
| V4 PoolManager | `0x498581fF718922c3f8e6A244956aF099B2652b2b` |
| V3 Factory | `0x33128a8fC17869897dcE68Ed026d694621f6FDfD` |
| WETH | `0x4200000000000000000000000000000000000006` |

---

## Testing

### Dry Run Validation

1. Set `DRY_RUN=true`, `LOG_LEVEL=DEBUG`
2. Run `python main.py`
3. Watch for `[new-token]` logs (confirms pool detection works)
4. Watch for signal evaluations in debug logs
5. If you see `[DRY RUN] Signal:` — detection pipeline is working end-to-end

### Signal Engine Unit Test

```bash
python -c "
import asyncio
from signal_engine import SignalEngine
from state import TokenState
import time

engine = SignalEngine()
state = TokenState(
    token_address='0x' + 'a'*40,
    pair_address='0x' + 'b'*40,
    first_seen=time.time() - 60,
    dex_version='v4',
    liquidity_usd=5000,
    estimated_mcap=15000,
    total_buys=3,
    largest_buy_usd=600,
    bytecode_safe=True,
)
state.unique_buyers.add('0x1')

result = asyncio.run(engine.evaluate(state))
print(f'Signal fired: {result}')
print(f'Queue size: {engine.signal_queue.qsize()}')
"
```

---

## Limitations

- **No trading**: This bot only detects and forwards. Based Bot handles execution.
- **Mcap estimates are approximate**: On-chain estimates assume 1B token supply. DexScreener provides more accurate data when available.
- **ETH amount heuristic**: Swap direction uses `min(abs(amount0), abs(amount1))` as ETH amount — works for typical meme token pairs but is an approximation.
- **V4 hooks**: Only hookless pools are tracked by default. Custom hooks are skipped.
- **Stateless**: No persistence across restarts. All tracked tokens are lost on shutdown.
- **DexScreener lag**: DexScreener indexes 30-60s behind on-chain. Initial signals may fire on on-chain data alone before DexScreener enrichment arrives.

---

## License

Private — not for distribution.