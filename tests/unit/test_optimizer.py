import pytest

from planner.cost import summarize
from planner.optimizer import effective_price, plan_stops


def plan(pos, price, total, **kw):
    return plan_stops(pos, price, total, mpg=10, max_range=500, **kw)


def test_trip_within_range_needs_no_stops():
    assert plan([100, 200], [3.0, 2.0], 300) == []


def test_exactly_500_miles_is_reachable():
    assert plan([], [], 500.0) == []
    assert plan([250.0], [3.0], 500.0) == []


def test_start_equals_finish():
    assert plan([], [], 0.0) == []


def test_gap_larger_than_range_is_infeasible():
    assert plan([100], [3.0], 700) is None
    assert plan([], [], 500.1) is None
    assert plan([600], [3.0], 700) is None  # origin -> first station too long


def test_equal_price_tie_fills_at_first_station():
    buys = plan([100, 200], [3.0, 3.0], 650)
    assert buys[0] == (0, pytest.approx(10.0)) and buys[1] == (1, pytest.approx(5.0))


def test_cheaper_station_just_before_finish():
    buys = plan([300, 595], [3.5, 2.0], 600)
    assert buys == [(0, pytest.approx(9.5)), (1, pytest.approx(0.5))]


def test_destination_is_a_price_zero_node():
    """Reaching the finish from A is possible, yet buying only enough for the cheaper B wins."""
    buys = plan([300, 590], [3.0, 2.0], 600)
    assert buys == [(0, pytest.approx(9.0)), (1, pytest.approx(1.0))]
    cost = sum(g * p for (_, g), p in zip(buys, [3.0, 2.0], strict=True))
    assert cost == pytest.approx(29.0)  # "buy just enough to finish at A" would cost 30


def test_detour_adjusted_price_example():
    far = effective_price(3.00, 40, 10, 5, 25)
    near = effective_price(3.10, 2, 10, 5, 25)
    assert far == pytest.approx(3.84) and far > near == pytest.approx(3.10)


def test_cost_model_facts_and_estimate():
    s = summarize([(30.0, 3.0), (20.0, 4.0)], 1000, 10, (3.0, None), "reference")
    assert s["gallons_consumed"] == 100 and s["gallons_purchased"] == 50
    assert s["cash_spent_at_stops_usd"] == 170
    assert s["initial_fuel"]["gallons"] == 50
    assert s["estimated_total_fuel_cost_usd"] == pytest.approx(170 + 50 * 3.0)
    short = summarize([], 300, 10, (3.2, {"station_id": "9"}), "reference")
    assert short["cash_spent_at_stops_usd"] == 0
    assert short["estimated_total_fuel_cost_usd"] == pytest.approx(30 * 3.2)
    assert short["initial_fuel"]["valuation"] == "cheapest_corridor_station"
    ex = summarize([], 300, 10, (3.2, None), "excluded")
    assert ex["estimated_total_fuel_cost_usd"] is None
    assert summarize([], 0, 10, None, "reference")["estimated_total_fuel_cost_usd"] == 0


# --- pruning of stops whose detour costs more than they save -------------------------------------
import numpy as np  # noqa: E402
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from planner.optimizer import _feasible, _modeled_cost, prune_unprofitable  # noqa: E402


def prune(pos, price, off, total):
    eff = effective_price(np.array(price), np.array(off), 10, 5.0, 25)
    buys = plan_stops(pos, eff, total, mpg=10, max_range=500)
    return buys, *prune_unprofitable(pos, price, off, total, buys, mpg=10, max_range=500)


def test_tiny_top_up_with_a_detour_is_merged_into_the_previous_stop():
    """LA -> Salt Lake City seen live: stop 2 (3 mi off route, 1.57 gal, 14 c/gal cheaper) burns
    about $1.9 of detour fuel to save 23 cents, so one stop is enough."""
    buys, kept, dropped = prune([300.5, 677.3], [3.282, 3.136], [0.2, 3.0], 693.0)
    assert [i for i, _ in buys] == [0, 1] and [i for i, _ in kept] == [0] and dropped == 1
    assert sum(g for _, g in kept) == pytest.approx(sum(g for _, g in buys), abs=1e-6)


def test_worthwhile_cheaper_stop_is_kept():
    buys, kept, dropped = prune([200.0, 400.0], [3.50, 2.50], [0.5, 0.5], 800.0)
    assert dropped == 0 and kept == buys


def test_merge_that_would_overfill_the_tank_is_refused():
    # the second stop is needed: a single fill at mile 100 cannot cover 650 miles
    buys, kept, dropped = prune([100.0, 600.0], [3.30, 3.20], [0.0, 4.0], 650.0)
    assert dropped == 0 and kept == buys


@settings(max_examples=400, deadline=None)
@given(
    st.lists(st.tuples(st.integers(1, 59), st.integers(250, 450), st.integers(0, 40)), max_size=10)
)
def test_pruning_never_raises_modeled_cost_and_stays_feasible(rows):
    total = 600.0
    rows = sorted({r[0]: r for r in rows}.values())
    pos, price, off = (
        [r[0] * 10.0 for r in rows],
        [r[1] / 100 for r in rows],
        [r[2] / 4 for r in rows],
    )
    eff = effective_price(np.array(price), np.array(off), 10, 5.0, 25)
    buys = plan_stops(pos, eff, total, mpg=10, max_range=500)
    if buys is None:
        return
    kept, dropped = prune_unprofitable(pos, price, off, total, buys, mpg=10, max_range=500)
    assert _feasible(kept, pos, total, 10, 500)
    assert _modeled_cost(kept, price, off, 10) <= _modeled_cost(buys, price, off, 10) + 1e-9
    assert len(kept) == len(buys) - dropped
    assert sum(g for _, g in kept) == pytest.approx(sum(g for _, g in buys), abs=1e-6)
