"""Independent textbook payoff fixtures; amounts include the stated entry fees."""
from datetime import datetime, timezone

import pytest

from src.services.trade_desk.analytics import analyze_payoff, payoff_at_price
from src.services.trade_desk.models import OptionLeg


def leg(right, side, strike, premium, quantity=1, multiplier=100, existing=False):
    return OptionLeg(contract_id=f'{right}-{strike}-{side}', right=right, side=side,
        quantity=quantity, multiplier=multiplier, strike=strike, entry_price=premium,
        existing=existing, expiry=None if right == 'stock' else datetime(2030, 1, 18, 21, tzinfo=timezone.utc))


# Each fixture lists independently worked-out debit, profit/loss bounds and roots.
CASES = [
    ('long_call', [leg('call', 'buy', 100, 5)], 1, 500, None, 501, [105.01]),
    ('long_put', [leg('put', 'buy', 100, 5)], 1, 500, 9499, 501, [94.99]),
    ('bull_call_debit', [leg('call', 'buy', 100, 5), leg('call', 'sell', 110, 2)], 2, 300, 698, 302, [103.02]),
    ('bear_put_debit', [leg('put', 'buy', 110, 12), leg('put', 'sell', 100, 5)], 2, 700, 298, 702, [102.98]),
    ('bull_put_credit', [leg('put', 'sell', 100, 5), leg('put', 'buy', 90, 2)], 2, -300, 298, 702, [97.02]),
    ('bear_call_credit', [leg('call', 'sell', 100, 5), leg('call', 'buy', 110, 2)], 2, -300, 298, 702, [102.98]),
    ('long_straddle', [leg('call', 'buy', 100, 5), leg('put', 'buy', 100, 5)], 2, 1000, None, 1002, [89.98, 110.02]),
    ('short_straddle', [leg('call', 'sell', 100, 5), leg('put', 'sell', 100, 5)], 2, -1000, 998, None, [90.02, 109.98]),
    ('long_strangle', [leg('put', 'buy', 95, 3), leg('call', 'buy', 105, 3)], 2, 600, None, 602, [88.98, 111.02]),
    ('short_strangle', [leg('put', 'sell', 95, 3), leg('call', 'sell', 105, 3)], 2, -600, 598, None, [89.02, 110.98]),
    ('call_butterfly', [leg('call', 'buy', 90, 12), leg('call', 'sell', 100, 5, 2), leg('call', 'buy', 110, 2)], 4, 400, 596, 404, [94.04, 105.96]),
    ('put_butterfly', [leg('put', 'buy', 90, 2), leg('put', 'sell', 100, 5, 2), leg('put', 'buy', 110, 12)], 4, 400, 596, 404, [94.04, 105.96]),
    ('iron_condor', [leg('put', 'buy', 90, 1), leg('put', 'sell', 95, 2), leg('call', 'sell', 105, 2), leg('call', 'buy', 110, 1)], 4, -200, 196, 304, [93.04, 106.96]),
    ('covered_call', [leg('stock', 'buy', None, 100, 100, 1), leg('call', 'sell', 105, 5)], 1, 9500, 999, 9501, [95.01]),
    ('owned_covered_call', [leg('stock', 'buy', None, 100, 100, 1, True), leg('call', 'sell', 105, 5)], 1, -500, 999, 9501, [95.01]),
    ('cash_secured_put', [leg('put', 'sell', 100, 5)], 1, -500, 499, 9501, [95.01]),
    ('uncovered_put', [leg('put', 'sell', 100, 5)], 1, -500, 499, 9501, [95.01]),
    ('uncovered_call', [leg('call', 'sell', 100, 5)], 1, -500, 499, None, [104.99]),
    ('nondefault_multiplier', [leg('call', 'buy', 100, 5, multiplier=10)], 1, 50, None, 51, [105.1]),
]


@pytest.mark.parametrize('name,legs,fees,debit,gain,loss,roots', CASES, ids=[case[0] for case in CASES])
def test_reference_payoff_bounds_and_breakevens(name, legs, fees, debit, gain, loss, roots):
    result = analyze_payoff(legs, fees=fees)
    assert result.entry_debit == pytest.approx(debit)
    assert result.max_gain == (None if gain is None else pytest.approx(gain))
    assert result.max_loss == (None if loss is None else pytest.approx(loss))
    assert result.gain_bound == ('unbounded' if gain is None else 'bounded')
    assert result.loss_bound == ('unbounded' if loss is None else 'bounded')
    assert result.breakevens == pytest.approx(roots)
    for price in roots:
        assert payoff_at_price(legs, price, fees) == pytest.approx(0, abs=1e-8)
    assert 'Infinity' not in result.model_dump_json()
