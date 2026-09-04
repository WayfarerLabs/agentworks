"""Small concurrency primitive for bounded read-only observation fan-out."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping


@contextmanager
def cancelling_futures[K, T](
    tasks: Mapping[K, Callable[[], T]],
    *,
    max_workers: int = 8,
) -> Iterator[dict[Future[T], K]]:
    """Submit bounded tasks and cancel queued work when their consumer exits exceptionally."""
    if not tasks:
        yield {}
        return
    executor = ThreadPoolExecutor(max_workers=min(max_workers, len(tasks)))
    futures = {executor.submit(copy_context().run, task): key for key, task in tasks.items()}
    try:
        yield futures
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
