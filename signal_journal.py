"""
Signal Journal — persistent append-only log of every signal decision.

Writes one JSON line per event to `signal_journal.jsonl`.
Survives restarts, easy to grep/analyze, no dependencies.

Events logged:
  - SIGNAL: token passed all rules → sent to Telegram
  - REJECT: token failed a rule (sampled — 1 in 20 to avoid log bloat)

Usage:
    journal = SignalJournal()
    journal.log_signal(state, metrics)
    journal.log_reject(token, reason, detail, state)
"""
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("journal")

JOURNAL_FILE = Path(os.getenv("SIGNAL_JOURNAL_PATH", "signal_journal.jsonl"))

# Only log 1 in N rejections to keep file size reasonable.
# Signals are always logged (they're rare and valuable).
REJECT_SAMPLE_RATE = 20


class SignalJournal:
    """Append-only JSONL logger for signal decisions."""

    def __init__(self, path: Path | None = None):
        self._path = path or JOURNAL_FILE
        self._reject_counter = 0
        # Ensure parent dir exists
        self._path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Signal journal: {self._path}")

    def log_signal(self, state, extra: dict | None = None):
        """Log a fired signal with full metrics snapshot."""
        record = {
            "ts": time.time(),
            "event": "SIGNAL",
            "token": state.token_address,
            "symbol": state.token_symbol or "",
            "name": state.token_name or "",
            "chain": "solana" if state.dex_version.startswith("solana") else "base",
            "dex": state.dex_version,
            "pair": state.pair_address,
            "age_s": round(state.age_seconds, 1),
            "latency_s": round(time.time() - state.first_seen, 1),
            "mcap": round(state.best_mcap, 0),
            "mcap_onchain": round(state.estimated_mcap, 0),
            "mcap_ds": round(state.ds_mcap, 0) if state.ds_mcap else None,
            "liq": round(state.best_liquidity, 0),
            "buys": state.best_buys,
            "sells": state.total_sells,
            "unique_buyers": len(state.unique_buyers),
            "buy_vol_usd": round(state.buy_volume_usd, 0),
            "largest_buy_usd": round(state.largest_buy_usd, 0),
            "largest_buy_pct": round(
                (state.largest_buy_usd / state.best_liquidity * 100)
                if state.best_liquidity > 0 else 0, 1
            ),
            "momentum": state.has_momentum(),
            "bytecode_safe": state.bytecode_safe,
            "has_socials": state.has_socials,
            "is_copycat": state.is_copycat,
            "hooks": state.hooks_address if state.hooks_address and not state.hooks_address.endswith("0" * 40) else None,
            "deployer": state.deployer_address or None,
        }
        if extra:
            record.update(extra)
        self._write(record)

    def log_reject(self, token: str, reason: str, detail: str = "", state=None):
        """Log a rejection (sampled). Always logs rate_limited and unusual reasons."""
        self._reject_counter += 1

        # Always log interesting rejections; sample the noisy common ones
        always_log = {"rate_limited", "deployer_spam", "copycat", "dup_symbol", "no_sells", "unsafe_bytecode"}
        if reason not in always_log and self._reject_counter % REJECT_SAMPLE_RATE != 0:
            return

        record = {
            "ts": time.time(),
            "event": "REJECT",
            "token": token,
            "reason": reason,
            "detail": detail,
        }
        if state:
            record.update({
                "symbol": getattr(state, "token_symbol", ""),
                "chain": "solana" if getattr(state, "dex_version", "").startswith("solana") else "base",
                "age_s": round(state.age_seconds, 1) if hasattr(state, "age_seconds") else None,
                "mcap": round(state.best_mcap, 0) if hasattr(state, "best_mcap") else None,
                "liq": round(state.best_liquidity, 0) if hasattr(state, "best_liquidity") else None,
                "buys": state.best_buys if hasattr(state, "best_buys") else None,
            })
        self._write(record)

    def _write(self, record: dict):
        """Append one JSON line to the journal file."""
        try:
            with open(self._path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            logger.debug(f"Journal write failed: {e}")
