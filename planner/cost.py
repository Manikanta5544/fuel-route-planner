"""Cost model (ARCHITECTURE section 8): cash at stops is fact; the total is a labelled estimate."""


def summarize(purchases, route_miles: float, mpg: float, reference, billing: str) -> dict:
    """`purchases`: [(gallons, price)]; `reference`: (price, station_dict | None) or None."""
    consumed = route_miles / mpg
    bought = sum(g for g, _ in purchases)
    cash = sum(g * p for g, p in purchases)
    start_tank = max(0.0, consumed - bought)
    ref_price, ref_station = reference if reference else (None, None)
    included = billing == "reference" and (ref_price is not None or start_tank == 0)
    estimate = cash + start_tank * (ref_price or 0.0) if included else None
    if not included:
        valuation = "excluded"
    elif purchases:
        valuation = "first_purchase_price"
    else:
        valuation = "cheapest_corridor_station" if ref_price is not None else "not_applicable"
    return {
        "gallons_consumed": consumed,
        "gallons_purchased": bought,
        "cash_spent_at_stops_usd": cash,
        "average_purchase_price_per_gallon": cash / bought if bought else None,
        "initial_fuel": {
            "gallons": start_tank,
            "cost_included_in_estimate": included,
            "valuation": valuation,
            "reference_price_per_gallon": ref_price,
            "reference_station": ref_station,
        },
        "estimated_total_fuel_cost_usd": estimate,
    }
