"""Fixed-route next-cheaper greedy. The destination is a virtual node with price 0."""

import numpy as np

TOL = 1e-6


def effective_price(price, off_miles, mpg: float, free_offset: float, g_ref: float):
    """Ranking proxy: real price inflated by estimated round-trip detour fuel."""
    return price * (1 + 2 * np.maximum(0, off_miles - free_offset) / (mpg * g_ref))


def plan_stops(pos, price, total: float, *, mpg: float, max_range: float):
    """Return [(index, gallons)] purchases, or None when some gap exceeds the range.

    `pos` is sorted miles along the route; start full at 0, destination at `total`.
    """
    pos, price = np.asarray(pos, dtype=float).tolist(), np.asarray(price, dtype=float).tolist()
    n = len(pos)
    marks = [0.0, *pos, total]
    if max(b - a for a, b in zip(marks, marks[1:], strict=False)) > max_range + TOL:
        return None
    prices = [*price, 0.0]
    nxt, stack = [0] * n, [n]
    for i in range(n - 1, -1, -1):
        while len(stack) > 1 and prices[stack[-1]] >= prices[i]:
            stack.pop()
        nxt[i] = stack[-1]
        stack.append(i)
    at_miles = [*pos, total]
    cap = max_range / mpg
    fuel, here, buys = cap, 0.0, []
    for i in range(n):
        fuel -= (pos[i] - here) / mpg
        here = pos[i]
        ahead = at_miles[nxt[i]] - here
        buy = max(0.0, ahead / mpg - fuel) if ahead <= max_range + TOL else cap - fuel
        if buy > TOL:
            buys.append((i, buy))
            fuel += buy
    return buys


def _modeled_cost(buys, price, off, mpg):
    """Cash at the stops plus the fuel burned on each round-trip detour (priced at that stop)."""
    return sum((g + 2 * off[i] / mpg) * price[i] for i, g in buys)


def _feasible(buys, pos, total, mpg, max_range):
    cap, fuel, here = max_range / mpg, max_range / mpg, 0.0
    for i, g in buys:
        fuel -= (pos[i] - here) / mpg
        here = pos[i]
        if fuel < -TOL:
            return False
        fuel += g
        if fuel > cap + TOL:
            return False
    return fuel - (total - here) / mpg >= -TOL


def prune_unprofitable(pos, price, off, total: float, buys, *, mpg: float, max_range: float):
    """Drop stops whose detour fuel costs more than they save; returns (buys, dropped).

    The greedy ranks stations with a smooth detour proxy that counts short detours as free, so it
    can add a stop 3 miles off the route for a 1.5-gallon top-up that saves 23 cents and burns
    about $1.90 of detour fuel. For each stop we try moving its gallons to the previous or the next
    stop and keep the change when the plan stays physically feasible
    (fuel never below 0 or above the tank) and the modeled cost (cash + real detour fuel) falls by
    at least a cent. Repeats until nothing improves, so the plan can only get cheaper in the model.
    """
    pos, price, off = (np.asarray(a, dtype=float).tolist() for a in (pos, price, off))
    current, dropped = [(int(i), float(g)) for i, g in buys], 0
    while len(current) > 1:
        best = _modeled_cost(current, price, off, mpg)
        winner = None
        for k, (_, g) in enumerate(current):
            for into in (k - 1, k + 1):
                if not 0 <= into < len(current):
                    continue
                trial = [
                    (i, h + g if j == into else h) for j, (i, h) in enumerate(current) if j != k
                ]
                if not _feasible(trial, pos, total, mpg, max_range):
                    continue
                cost = _modeled_cost(trial, price, off, mpg)
                if cost < best - 0.01 and (winner is None or cost < winner[0]):
                    winner = (cost, trial)
        if winner is None:
            break
        current, dropped = winner[1], dropped + 1
    return current, dropped
