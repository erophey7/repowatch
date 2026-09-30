"""Resizable scheduler slots shared by index checks and cache operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, AbstractAsyncContextManager
from collections.abc import AsyncGenerator


class RepoSlots:
    """One live pool for all tasks owned by a repository dispatcher.

    Warming cannot occupy the final quarter (rounded up, at least one)
    of a multi-slot pool. At size one, one check and one warm may overlap.
    Resizing never cancels holders: lower limits apply to new admissions as
    existing operations finish. Index checks can still wait for other checks.
    This pool does not limit manual API jobs or retention operations.
    """

    def __init__(self, size: int) -> None:
        """Start an empty pool bound to its owning event loop."""
        self.size = 0
        self._active = {'check': 0, 'warm': 0}
        self._changed = asyncio.Event()
        self.resize(size)

    def resize(self, size: int) -> None:
        """Change admission limits in place, including for tasks already waiting."""
        if type(size) is not int or size < 1:
            raise ValueError('size must be at least 1')
        if size != self.size:
            self.size = size
            self._changed.set()

    def _available(self, kind: str) -> bool:
        """Check both total capacity and the phase-specific reservation."""
        if sum(self._active.values()) >= max(2, self.size):
            return False
        limit = self.size if kind == 'check' else max(1, self.size - (self.size + 3) // 4)
        return self._active[kind] < limit

    @asynccontextmanager
    async def _hold(self, kind: str) -> AsyncGenerator[None]:
        """Wait without holding capacity; release it on success, error or cancellation."""
        while not self._available(kind):
            # No await between checking capacity and clearing the event: another
            # task cannot release a slot in that interval on this event loop.
            self._changed.clear()
            await self._changed.wait()
        self._active[kind] += 1
        try:
            yield
        finally:
            self._active[kind] -= 1
            self._changed.set()

    def check(self) -> AbstractAsyncContextManager[None]:
        """Acquire capacity for fetching and recording one repository's index."""
        return self._hold('check')

    def warm(self) -> AbstractAsyncContextManager[None]:
        """Acquire bounded cache-work capacity, preserving the check reservation."""
        return self._hold('warm')
