"""In-process request coalescing. The leader's call is a detached, shielded task."""

import asyncio
from collections.abc import Awaitable, Callable


class SingleFlight:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    async def run[T](self, key: str, factory: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        """Return (result, is_leader). A cancelled waiter never cancels the shared call."""
        task = self._tasks.get(key)
        leader = task is None
        if task is None:
            task = asyncio.get_running_loop().create_task(factory())
            self._tasks[key] = task
            task.add_done_callback(lambda t: self._finish(key, t))
        return await asyncio.shield(task), leader

    def _finish(self, key: str, task: asyncio.Task) -> None:
        self._tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # mark retrieved when every waiter has gone away
