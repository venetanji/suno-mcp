"""Bounded, shared admission for browser operations."""

import asyncio
from contextlib import asynccontextmanager


class OperationError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, *, started: bool):
        super().__init__(message)
        self.status = status
        self.code = code
        self.started = started


class BrowserOperations:
    def __init__(self, capacity: int, queue_timeout: float, operation_timeout: float):
        self.capacity = capacity
        self.queue_timeout = queue_timeout
        self.operation_timeout = operation_timeout
        self.waiting = 0
        self.active = None
        self._owner = None
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict:
        return {
            "active_operation": self.active, "queued": self.waiting,
            "queue_capacity": self.capacity, "queue_timeout_seconds": self.queue_timeout,
            "operation_timeout_seconds": self.operation_timeout,
        }

    @asynccontextmanager
    async def run(self, operation: str):
        if self._owner is asyncio.current_task():
            yield
            return
        if self._lock.locked() or self.waiting:
            if self.waiting >= self.capacity:
                raise OperationError(
                    429, "browser_queue_full", "The browser queue is full; this operation was not started.",
                    started=False,
                )
            self.waiting += 1
            try:
                async with asyncio.timeout(self.queue_timeout):
                    await self._lock.acquire()
            except TimeoutError as error:
                raise OperationError(
                    503, "browser_queue_timeout", "The browser queue wait expired; this operation was not started.",
                    started=False,
                ) from error
            finally:
                self.waiting -= 1
        else:
            await self._lock.acquire()
        self._owner = asyncio.current_task()
        self.active = operation
        deadline = asyncio.timeout(self.operation_timeout)
        try:
            try:
                async with deadline:
                    yield
            except TimeoutError as error:
                if not deadline.expired():
                    raise
                raise OperationError(
                    504, "browser_operation_timeout",
                    "The browser operation deadline expired. Check generation/upload status and your library before retrying; a submission may already have been accepted.",
                    started=True,
                ) from error
        finally:
            self.active = None
            self._owner = None
            self._lock.release()
