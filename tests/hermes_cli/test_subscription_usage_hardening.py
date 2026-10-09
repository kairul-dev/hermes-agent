import pytest
from hermes_cli import subscription_usage as usage

@pytest.mark.parametrize("amount", ["nan", "inf", "-inf", "1e999"])
def test_deepseek_omits_non_finite_balances(amount):
    assert usage._deepseek_balance({"balance_infos": [{"currency": "USD", "total_balance": amount}]}) is None

@pytest.mark.parametrize("amount", ["nan", "inf", "1e999"])
def test_codex_omits_invalid_credits_without_losing_windows(amount):
    assert usage._codex_balance({"credits": {"has_credits": True, "balance": amount}}) is None
