"""
Tests for signal engine — covers EVM TokenState + Solana SolTokenState.
Run: python3 test_signal_engine.py
"""
import asyncio
import sys
import time

# Ensure project root is on path
sys.path.insert(0, ".")

from base.state import TokenState, TokenStateTracker
from solana.state import SolTokenState, SolTokenStateTracker
from signal_engine import SignalEngine
import config


def make_evm_state(**overrides) -> TokenState:
    """Create a test EVM TokenState with sane defaults."""
    defaults = dict(
        token_address="0xabc123",
        pair_address="0xpair456",
        first_seen=time.time() - 60,  # 60s old
        dex_version="v4",
        liquidity_usd=5000.0,
        estimated_mcap=15000.0,
        total_buys=3,
        buy_volume_usd=1000.0,
        largest_buy_usd=600.0,
        bytecode_safe=True,
        deployer_address="0xdeployer1",
    )
    defaults.update(overrides)
    state = TokenState(
        token_address=defaults["token_address"],
        pair_address=defaults["pair_address"],
        first_seen=defaults["first_seen"],
        dex_version=defaults["dex_version"],
    )
    state.liquidity_usd = defaults["liquidity_usd"]
    state.estimated_mcap = defaults["estimated_mcap"]
    state.total_buys = defaults["total_buys"]
    state.buy_volume_usd = defaults["buy_volume_usd"]
    state.largest_buy_usd = defaults["largest_buy_usd"]
    state.bytecode_safe = defaults["bytecode_safe"]
    state.deployer_address = defaults["deployer_address"]
    state.unique_buyers = {"0xbuyer1", "0xbuyer2"}  # satisfy MIN_UNIQUE_BUYERS
    return state


def make_sol_state(**overrides) -> SolTokenState:
    """Create a test Solana SolTokenState with sane defaults."""
    defaults = dict(
        token_address="SoLtOkEnMiNt111111111111111111111111111111",
        pair_address="RaYpOoL222222222222222222222222222222222222",
        first_seen=time.time() - 50,  # 50s old
        liquidity_sol=20.0,
        liquidity_usd=6000.0,
        estimated_mcap=12000.0,
        total_buys=3,
        buy_volume_usd=800.0,
        largest_buy_usd=700.0,
        deployer_address="DeployerWallet33333333333333333333333333333",
    )
    defaults.update(overrides)
    state = SolTokenState(
        token_address=defaults["token_address"],
        pair_address=defaults["pair_address"],
        first_seen=defaults["first_seen"],
    )
    state.liquidity_sol = defaults["liquidity_sol"]
    state.liquidity_usd = defaults["liquidity_usd"]
    state.estimated_mcap = defaults["estimated_mcap"]
    state.total_buys = defaults["total_buys"]
    state.buy_volume_usd = defaults["buy_volume_usd"]
    state.largest_buy_usd = defaults["largest_buy_usd"]
    state.deployer_address = defaults["deployer_address"]
    state.unique_buyers = {"buyer_wallet_1", "buyer_wallet_2"}  # satisfy MIN_UNIQUE_BUYERS
    # Both authorities revoked = safe
    state.mint_authority = None
    state.freeze_authority = None
    state.update_safety()
    return state


passed = 0
failed = 0


def run(coro):
    return asyncio.run(coro)


def run_test(name, func):
    global passed, failed
    try:
        func()
        print(f"  PASS  {name}")
        passed += 1
    except Exception as e:
        print(f"  FAIL  {name}: {e}")
        failed += 1


# ══════════════════════════════════════════════════════════════
#  EVM TESTS
# ══════════════════════════════════════════════════════════════


def test_evm_signal_fires():
    tracker = TokenStateTracker(max_age=300)
    engine = SignalEngine(state_tracker=tracker)
    state = make_evm_state()
    result = run(engine.evaluate(state))
    assert result is True, "EVM signal should fire"
    assert state.signaled is True


def test_evm_too_old():
    tracker = TokenStateTracker(max_age=300)
    engine = SignalEngine(state_tracker=tracker)
    state = make_evm_state(first_seen=time.time() - 200)  # 200s > 180s
    result = run(engine.evaluate(state))
    assert result is False, "Token too old should be rejected"


def test_evm_mcap_too_high():
    tracker = TokenStateTracker(max_age=300)
    engine = SignalEngine(state_tracker=tracker)
    state = make_evm_state(estimated_mcap=50000)  # > 30k
    result = run(engine.evaluate(state))
    assert result is False, "High mcap should be rejected"


def test_evm_unsafe_bytecode():
    tracker = TokenStateTracker(max_age=300)
    engine = SignalEngine(state_tracker=tracker)
    state = make_evm_state(bytecode_safe=False)
    result = run(engine.evaluate(state))
    assert result is False, "Unsafe bytecode should prevent signal"


def test_evm_one_signal_per_token():
    tracker = TokenStateTracker(max_age=300)
    engine = SignalEngine(state_tracker=tracker)
    state = make_evm_state()
    run(engine.evaluate(state))
    result2 = run(engine.evaluate(state))
    assert result2 is False, "Second eval on same token must not signal again"


# ══════════════════════════════════════════════════════════════
#  SOLANA TESTS
# ══════════════════════════════════════════════════════════════


def test_sol_signal_fires():
    tracker = TokenStateTracker(max_age=300)
    sol_tracker = SolTokenStateTracker(max_age=200)
    engine = SignalEngine(state_tracker=tracker, sol_state_tracker=sol_tracker)
    state = make_sol_state()
    result = run(engine.evaluate(state))
    assert result is True, "Solana signal should fire"
    assert state.signaled is True
    assert state.dex_version == "solana-raydium"


def test_sol_too_old():
    tracker = TokenStateTracker(max_age=300)
    sol_tracker = SolTokenStateTracker(max_age=200)
    engine = SignalEngine(state_tracker=tracker, sol_state_tracker=sol_tracker)
    # 130s > SOL threshold of 120s
    state = make_sol_state(first_seen=time.time() - 130)
    result = run(engine.evaluate(state))
    assert result is False, "Solana token >120s should be rejected"


def test_sol_evm_age_threshold_difference():
    """EVM allows 180s, Solana only 120s. A 150s-old EVM token should signal,
    but a 150s-old Solana token should not."""
    tracker = TokenStateTracker(max_age=300)
    sol_tracker = SolTokenStateTracker(max_age=200)
    engine = SignalEngine(state_tracker=tracker, sol_state_tracker=sol_tracker)

    evm_state = make_evm_state(first_seen=time.time() - 150)
    sol_state = make_sol_state(first_seen=time.time() - 150)

    evm_result = run(engine.evaluate(evm_state))
    sol_result = run(engine.evaluate(sol_state))

    assert evm_result is True, "150s EVM token should still signal (< 180s)"
    assert sol_result is False, "150s Solana token should not signal (> 120s)"


def test_sol_mint_authority_unsafe():
    tracker = TokenStateTracker(max_age=300)
    sol_tracker = SolTokenStateTracker(max_age=200)
    engine = SignalEngine(state_tracker=tracker, sol_state_tracker=sol_tracker)
    state = make_sol_state()
    state.mint_authority = "SomeActiveAuthority1111111111111111111111111"
    state.update_safety()
    assert state.bytecode_safe is False
    result = run(engine.evaluate(state))
    assert result is False, "Active mint authority should fail safety"


def test_sol_freeze_authority_unsafe():
    tracker = TokenStateTracker(max_age=300)
    sol_tracker = SolTokenStateTracker(max_age=200)
    engine = SignalEngine(state_tracker=tracker, sol_state_tracker=sol_tracker)
    state = make_sol_state()
    state.freeze_authority = "SomeFreezeAuthority1111111111111111111111111"
    state.update_safety()
    assert state.bytecode_safe is False
    result = run(engine.evaluate(state))
    assert result is False, "Active freeze authority should fail safety"


def test_sol_deployer_spam():
    tracker = TokenStateTracker(max_age=300)
    sol_tracker = SolTokenStateTracker(max_age=200)
    engine = SignalEngine(state_tracker=tracker, sol_state_tracker=sol_tracker)

    deployer = "SpamDeployer11111111111111111111111111111111"
    # Pre-record deployer activity to exceed threshold (one unique token per call)
    for i in range(config.MAX_DEPLOYER_TOKENS_24H + 1):
        sol_tracker.record_deployer(deployer, f"FakeToken{i}")

    state = make_sol_state(deployer_address=deployer)
    result = run(engine.evaluate(state))
    assert result is False, "Deployer spam should prevent Solana signal"


def test_sol_state_tracker_ttl():
    tracker = SolTokenStateTracker(max_age=200)
    state = tracker.create(
        token_address="ExPirEdToKeN111111111111111111111111111111111",
        pair_address="pool",
        deployer="deployer",
        liquidity_sol=15.0,
        liquidity_usd=4500.0,
    )
    # Backdate first_seen to beyond TTL
    state.first_seen = time.time() - 210
    result = tracker.get("ExPirEdToKeN111111111111111111111111111111111")
    assert result is None, "Expired Solana token should return None"


def test_sol_state_properties():
    state = make_sol_state()
    assert state.best_mcap == 12000.0
    assert state.best_liquidity == 6000.0
    assert state.best_buys == 3
    # DexScreener override
    state.ds_mcap = 20000.0
    state.ds_liquidity_usd = 8000.0
    state.ds_buys_m5 = 5
    assert state.best_mcap == 20000.0
    assert state.best_liquidity == 8000.0
    assert state.best_buys == 5


# ══════════════════════════════════════════════════════════════
#  RUN ALL TESTS
# ══════════════════════════════════════════════════════════════

print("\n── EVM Signal Engine Tests ──")
run_test("evm_signal_fires", test_evm_signal_fires)
run_test("evm_too_old", test_evm_too_old)
run_test("evm_mcap_too_high", test_evm_mcap_too_high)
run_test("evm_unsafe_bytecode", test_evm_unsafe_bytecode)
run_test("evm_one_signal_per_token", test_evm_one_signal_per_token)

print("\n── Solana Signal Engine Tests ──")
run_test("sol_signal_fires", test_sol_signal_fires)
run_test("sol_too_old", test_sol_too_old)
run_test("sol_evm_age_threshold_difference", test_sol_evm_age_threshold_difference)
run_test("sol_mint_authority_unsafe", test_sol_mint_authority_unsafe)
run_test("sol_freeze_authority_unsafe", test_sol_freeze_authority_unsafe)
run_test("sol_deployer_spam", test_sol_deployer_spam)
run_test("sol_state_tracker_ttl", test_sol_state_tracker_ttl)
run_test("sol_state_properties", test_sol_state_properties)

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if failed:
    sys.exit(1)
else:
    print("All tests passed!")
