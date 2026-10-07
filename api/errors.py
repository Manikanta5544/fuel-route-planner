import functools
import logging

import orjson
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse
from pydantic import ValidationError

from planner.corridor import Infeasible
from routing.budget import BudgetExceeded
from routing.providers import NotRoutable, RoutingUnavailable

log = logging.getLogger("api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str, request_id: str) -> HttpResponse:
    body = {"error": {"code": code, "message": message, "request_id": request_id}}
    return HttpResponse(orjson.dumps(body), status=status, content_type="application/json")


def _describe(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])


def api_view(view):
    """Map domain exceptions to the specified error format; never leak internals."""

    @functools.wraps(view)
    async def wrapper(request, *args, **kwargs):
        rid = request.request_id
        try:
            return await view(request, *args, **kwargs)
        except ApiError as exc:
            return error_response(exc.status, exc.code, exc.message, rid)
        except ValidationError as exc:
            return error_response(400, "INVALID_REQUEST", _describe(exc), rid)
        except RequestDataTooBig:
            return error_response(400, "INVALID_REQUEST", "Request body too large", rid)
        except NotRoutable:
            msg = "No drivable road found near one of the locations, or no route exists"
            return error_response(422, "LOCATION_NOT_ROUTABLE", msg, rid)
        except Infeasible:
            msg = "No fuel plan exists: a stretch of the route has no reachable station"
            return error_response(422, "NO_FEASIBLE_FUEL_PLAN", msg, rid)
        except (RoutingUnavailable, BudgetExceeded):
            return error_response(
                503, "ROUTING_UNAVAILABLE", "Routing is temporarily unavailable", rid
            )
        except Exception:
            log.exception("unhandled error request_id=%s", rid)
            return error_response(500, "INTERNAL_ERROR", "Internal server error", rid)

    return wrapper
