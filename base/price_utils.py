"""
Shared price estimation utilities for Uniswap V3 + V4 listeners.
Converts sqrtPriceX96 / liquidity values to USD estimates.
"""
import logging

logger = logging.getLogger("price")


def estimate_mcap(state, sqrt_price_x96: int, eth_is_token0: bool, eth_price: float):
    """
    Estimate market cap from sqrtPriceX96 assuming 1B token supply (meme default).
    Updates state.estimated_mcap in-place.
    """
    try:
        if sqrt_price_x96 == 0 or eth_price == 0:
            return
        price_ratio = (sqrt_price_x96 / (2**96)) ** 2
        if eth_is_token0 and price_ratio > 0:
            token_price_eth = 1 / price_ratio
        else:
            token_price_eth = price_ratio
        state.estimated_mcap = token_price_eth * eth_price * 1_000_000_000
    except (ZeroDivisionError, OverflowError):
        pass


def estimate_liquidity_usd(state, liquidity: int, sqrt_price_x96: int, eth_price: float):
    """
    Estimate pool liquidity in USD from on-chain liquidity + sqrtPriceX96.
    Approximation: TVL ≈ 2 * (L / sqrtPrice) * ethPrice.
    Updates state.liquidity_usd in-place.
    """
    try:
        if sqrt_price_x96 > 0 and eth_price > 0:
            state.liquidity_usd = (liquidity / sqrt_price_x96) * eth_price * 2
    except (ZeroDivisionError, OverflowError):
        pass
