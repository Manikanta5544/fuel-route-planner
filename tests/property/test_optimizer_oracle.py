"""The greedy must match an exact DP (fuel in 0.5 gal units, stations on 5-mile multiples)."""

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from planner.optimizer import effective_price, plan_stops

MPG, UNIT_MILES = 10.0, 5.0  # 5 miles = 0.5 gal at 10 MPG
INF = math.inf


def oracle_cost(pos_units, price, total_units, cap_units):
    """Exact minimum cost (price * gallons), or inf when infeasible."""
    if total_units <= cap_units:
        return 0.0
    cur = [INF] * (cap_units + 1)
    cur[cap_units] = 0.0
    here = 0
    for p, c in zip([*pos_units, total_units], [*price, 0.0], strict=True):
        gap = p - here
        cur = [*cur[gap:], *([INF] * gap)]  # drive: fuel f -> f - gap
        if p == total_units:
            return min(cur)
        for f in range(1, cap_units + 1):  # buy one 0.5-gal unit at a time
            cur[f] = min(cur[f], cur[f - 1] + 0.5 * c)
        here = p
    return min(cur)


def greedy_cost(pos_units, price, total_units, cap_units):
    buys = plan_stops(
        [u * UNIT_MILES for u in pos_units],
        price,
        total_units * UNIT_MILES,
        mpg=MPG,
        max_range=cap_units * UNIT_MILES,
    )
    return None if buys is None else sum(g * price[i] for i, g in buys)


@st.composite
def instances(draw):
    total = draw(st.integers(1, 480))  # up to 2,400 miles
    cap = draw(st.integers(10, 100))  # 50 .. 500 mile range
    n = draw(st.integers(0, 25))
    pos = sorted(draw(st.integers(0, total)) for _ in range(n))
    cents = draw(st.lists(st.integers(250, 420), min_size=n, max_size=n))
    off = draw(st.lists(st.floats(0, 60), min_size=n, max_size=n))
    return pos, [c / 100 for c in cents], off, total, cap


def check(pos, price, total, cap):
    expected, got = oracle_cost(pos, price, total, cap), greedy_cost(pos, price, total, cap)
    if expected == INF:
        assert got is None
    else:
        assert got is not None and math.isclose(got, expected, abs_tol=1e-6)


@settings(max_examples=1500, deadline=None)
@given(instances())
def test_greedy_matches_dp_on_plain_prices(inst):
    pos, price, _off, total, cap = inst
    check(pos, price, total, cap)


@settings(max_examples=1500, deadline=None)
@given(instances())
def test_greedy_matches_dp_on_effective_prices(inst):
    pos, price, off, total, cap = inst
    eff = [float(effective_price(p, o, MPG, 5.0, 25.0)) for p, o in zip(price, off, strict=True)]
    check(pos, eff, total, cap)


def test_both_feasible_and_infeasible_instances_are_exercised():
    assert oracle_cost([100], [3.0], 150, 100) < INF
    assert oracle_cost([], [], 150, 100) == INF
