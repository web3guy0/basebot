"""
Lightweight safety checks — bytecode scanning + hooks check.
Non-blocking: runs as a background task per token, results stored in TokenState.
"""
import asyncio
import logging

from base.constants import DANGEROUS_SELECTORS, CONTEXT_SELECTORS, PROXY_PATTERNS

logger = logging.getLogger("safety")


class SafetyChecker:
    """Bytecode analysis ported from the Go bytecode analyzer."""

    def __init__(self, w3):
        self.w3 = w3

    async def check_token(self, token_address: str) -> dict:
        """
        Analyze token contract bytecode for dangerous patterns.
        Returns dict with findings.
        Non-blocking — run as asyncio.create_task().
        """
        result = {
            "safe": True,
            "has_mint": False,
            "has_blacklist": False,
            "has_tax": False,
            "is_proxy": False,
            "bytecode_size": 0,
            "reasons": [],
        }

        try:
            code = await self.w3.eth.get_code(
                self.w3.to_checksum_address(token_address)
            )
        except Exception as e:
            result["safe"] = False
            result["reasons"].append(f"Failed to fetch bytecode: {e}")
            return result

        if len(code) == 0:
            result["safe"] = False
            result["reasons"].append("No bytecode — not a contract")
            return result

        result["bytecode_size"] = len(code)
        code_hex = code.hex()

        # Check dangerous selectors
        critical_count = 0
        warning_count = 0

        for selector, func_name in DANGEROUS_SELECTORS.items():
            if selector in code_hex:
                if selector == "40c10f19":  # mint
                    result["has_mint"] = True
                    result["reasons"].append("Has mint() function")
                    critical_count += 1
                elif selector in ("44df8e70", "e47d6060"):  # blacklist
                    result["has_blacklist"] = True
                    result["reasons"].append("Has blacklist functionality")
                    critical_count += 1
                elif selector == "3950935e":  # setTax
                    result["has_tax"] = True
                    result["reasons"].append("Has setTax() — owner can change fees")
                    warning_count += 1
                elif selector == "0e83672a":  # setMaxTxAmount
                    result["reasons"].append("Has setMaxTxAmount() — trading limits")
                    warning_count += 1
                elif selector == "c9567bf9":  # openTrading
                    result["reasons"].append("Has openTrading() — launch control")
                    warning_count += 1

        # Check context selectors (owner functions)
        for selector in CONTEXT_SELECTORS:
            if selector in code_hex:
                warning_count += 1

        # Check proxy patterns
        for pattern in PROXY_PATTERNS:
            if pattern in code_hex:
                result["is_proxy"] = True
                result["reasons"].append("Proxy contract — implementation can change")
                warning_count += 1
                break

        # Very small bytecode is suspicious
        if result["bytecode_size"] < 500:
            result["reasons"].append("Very small bytecode — possibly proxy or minimal")
            warning_count += 1

        # Classify risk
        if critical_count >= 2:
            result["safe"] = False
        elif critical_count == 1 and warning_count >= 2:
            result["safe"] = False
        # Single critical or many warnings → still flag but don't hard-reject
        # (DexScreener buy/sell ratio is a better honeypot indicator)

        return result


async def run_safety_check(checker: SafetyChecker, state):
    """
    Background safety check for a token state.
    Updates state.bytecode_safe in-place.
    Pass a reusable SafetyChecker instance.
    """
    try:
        result = await asyncio.wait_for(
            checker.check_token(state.token_address),
            timeout=10.0,
        )
        state.bytecode_safe = result["safe"]
        if not result["safe"]:
            logger.info(
                f"[unsafe] {state.token_address[:10]}... — "
                f"{', '.join(result['reasons'][:3])}"
            )
    except asyncio.TimeoutError:
        logger.debug(f"Safety check timed out for {state.token_address[:10]}...")
        # Don't block signal on timeout — speed > precision
        state.bytecode_safe = None
    except Exception as e:
        logger.debug(f"Safety check failed for {state.token_address[:10]}...: {e}")
        state.bytecode_safe = None
