import re
import time
import uuid
from time import perf_counter

from asgiref.sync import markcoroutinefunction
from django.conf import settings

from api.apps import get_runtime
from api.errors import error_response

_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_windows: dict[str, tuple[int, int]] = {}  # ip -> (minute window, count); in-memory limiter


async def _allowed(ip: str) -> bool:
    limit = settings.RATE_LIMIT_PER_MIN
    if limit <= 0:
        return True
    window = int(time.time() // 60)
    factory = get_runtime().redis_factory
    if factory is not None:
        try:
            client = factory()
            key = f"rl:{ip}:{window}"
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, 90)
            return count <= limit
        except Exception:
            pass  # Redis trouble: fall through to the in-memory window
    if len(_windows) > 10_000:
        _windows.clear()
    seen_window, count = _windows.get(ip, (window, 0))
    count = count + 1 if seen_window == window else 1
    _windows[ip] = (window, count)
    return count <= limit


class RequestMiddleware:
    """Request id, per-IP rate limit and Server-Timing."""

    async_capable, sync_capable = True, False

    def __init__(self, get_response):
        self.get_response = get_response
        markcoroutinefunction(self)

    async def __call__(self, request):
        started = perf_counter()
        supplied = request.headers.get("X-Request-ID", "")
        request.request_id = supplied if _RID.match(supplied) else uuid.uuid4().hex
        request.timings = {}
        if request.path.startswith("/api/") and not await _allowed(
            request.META.get("REMOTE_ADDR", "")
        ):
            response = error_response(429, "RATE_LIMITED", "Too many requests", request.request_id)
            response["Retry-After"] = "60"
        else:
            response = await self.get_response(request)
        request.timings["total"] = (perf_counter() - started) * 1e3
        response["X-Request-ID"] = request.request_id
        response["Server-Timing"] = ", ".join(
            f"{k};dur={v:.1f}" for k, v in request.timings.items()
        )
        return response
