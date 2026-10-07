import numpy as np
import pytest

from planner.corridor import Infeasible, build_bundle, plan_route, trivial_bundle
from tests.helpers import line_route, make_store

ROUTE = [(35.0, -100.0), (35.0, -90.0)]  # about 570 miles due east


def kw(**over):
    return {"mpg": 10, "max_range": 500, "free_offset": 5, "g_ref": 25, **over}


def test_bundle_positions_are_scaled_to_provider_distance():
    store = make_store([(35.0, -99.0, 3.0), (35.0, -95.0, 3.0), (35.0, -91.0, 3.0)])
    route = line_route(ROUTE, road_factor=1.1)
    bundle = build_bundle(route, store, None)
    assert list(bundle.idx) == [0, 1, 2]
    assert np.all(np.diff(bundle.pos) > 0) and bundle.pos[-1] < route.distance_miles
    frac = bundle.pos[1] / route.distance_miles
    assert frac == pytest.approx(0.5, abs=0.01)
    assert bundle.off.max() < 1.0


def test_corridor_excludes_far_stations_and_widens_only_when_needed():
    near = (35.0, -95.0, 3.0)
    off_20 = (35.0 + 20 / 69.0, -97.0, 2.0)  # ~20 miles north of the road
    store = make_store([near, off_20])
    bundle = build_bundle(line_route(ROUTE), store, None)
    result = plan_route(bundle, store, **kw())
    assert result.corridor_miles == 8.0 and result.candidates == 1
    # a 560-mile route with only one station 20 miles off: needs widening to 30
    store = make_store([off_20])
    bundle = build_bundle(line_route(ROUTE), store, None)
    result = plan_route(bundle, store, **kw())
    assert result.corridor_miles == 30.0 and len(result.stops) == 1


def test_infeasible_when_no_station_can_bridge_the_gap():
    store = make_store([(35.0, -99.5, 3.0)])
    bundle = build_bundle(line_route(ROUTE), store, None)
    with pytest.raises(Infeasible):
        plan_route(bundle, store, **kw())


def test_short_trip_has_no_stops_and_reference_is_cheapest_candidate():
    store = make_store([(35.0, -98.0, 3.4), (35.0, -97.0, 3.1)])
    bundle = build_bundle(line_route([(35.0, -100.0), (35.0, -96.0)]), store, None)
    result = plan_route(bundle, store, **kw())
    assert result.stops == [] and result.cheapest_idx == 1


def test_trivial_bundle_for_identical_endpoints():
    bundle = trivial_bundle(35.0, -100.0)
    store = make_store([(35.0, -100.0, 3.0)])
    result = plan_route(bundle, store, **kw())
    assert bundle.distance_miles == 0 and result.stops == [] and result.cheapest_idx is None
