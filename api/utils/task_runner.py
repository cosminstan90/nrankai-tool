"""
Tracked async task runner.

Replaces bare asyncio.create_task() calls with a version that:
- Stores task references to prevent premature GC
- Logs unhandled exceptions
- Supports optional timeouts
"""

import asyncio
import logging
from typing import Coroutine, Any

logger = logging.getLogger(__name__)

_ACTIVE_TASKS: set[asyncio.Task] = set()


def create_tracked_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    timeout: int | None = None,
) -> asyncio.Task:
    """
    Create an asyncio task with tracking and optional timeout.

    Keeps a reference in _ACTIVE_TASKS to prevent GC, logs any
    unhandled exceptions when the task finishes.

    Args:
        coro: The coroutine to run.
        name: Human-readable name for logging and task.get_name().
        timeout: Optional timeout in seconds. Task is cancelled after this.

    Returns:
        The created asyncio.Task.
    """
    if timeout is not None:
        coro = _with_timeout(coro, timeout, name)

    task = asyncio.create_task(coro, name=name)
    _ACTIVE_TASKS.add(task)
    task.add_done_callback(_on_task_done)
    return task


async def _with_timeout(coro: Coroutine, timeout: int, name: str) -> None:
    try:
        await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"[task_runner] Task '{name}' timed out after {timeout}s")
        raise


def _on_task_done(task: asyncio.Task) -> None:
    _ACTIVE_TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            f"[task_runner] Unhandled exception in task '{task.get_name()}'",
            exc_info=task.exception(),
        )
