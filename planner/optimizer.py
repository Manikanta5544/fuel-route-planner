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
