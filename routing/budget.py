"""Per-request upstream call budget (ARCHITECTURE section 6.3)."""

from dataclasses import dataclass


class BudgetExceeded(Exception):
    pass


@dataclass(slots=True)
class CallBudget:
    max_routing: int = 3
    routing_calls: int = 0
    geocode_calls: int = 0  # always 0: there is no geocoder; kept so external_calls stays honest

    @property
    def external_calls(self) -> int:
        return self.routing_calls + self.geocode_calls

    def spend_routing(self) -> None:
        if self.routing_calls >= self.max_routing:
            raise BudgetExceeded("routing call budget exhausted")
        self.routing_calls += 1
