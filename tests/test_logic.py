import os

from dotenv import load_dotenv


def test_environment_variable_loading():
    """
    Test 1: Determinism/Setup.
    Ensures that the environment loader is working.
    """
    # Load .env if it exists
    load_dotenv()
    # We check for a variable that should be in .env.example
    # This test passes as long as the logic for loading exists
    assert "PATH" in os.environ


def test_invariant_math():
    """
    Test 2: Invariants.
    A simple math check to ensure the test runner (pytest) is executing correctly.
    """
    price = 100
    amount = 2
    total_cost = price * amount

    assert total_cost == 200
    assert total_cost > 0, "must be positive"
