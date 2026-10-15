"""Helpers for ``discord.ext.tasks`` loops."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Awaitable[None]])


def resilient_iteration(name: str) -> Callable[[F], F]:
    """Log unexpected errors of a single loop iteration instead of letting them end the loop.

    ``tasks.loop`` only survives network errors – any other exception (e.g. a database hiccup) would stop
    reminders, giveaways or ticket timers until the next restart.
    """
    log = logging.getLogger(f"xumari.tasks.{name}")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> None:
            try:
                await func(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 – keep the loop alive, details go to the log
                log.exception("Loop iteration failed")

        return wrapper  # type: ignore[return-value]

    return decorator
