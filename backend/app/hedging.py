import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")


async def hedge(primary: Callable[[], Awaitable[T]], fallback: Callable[[], Awaitable[T]], delay: float) -> T:
    primary_task = asyncio.create_task(primary())
    fallback_task: asyncio.Task[T] | None = None

    try:
        done, _ = await asyncio.wait({primary_task}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        if done:
            return await primary_task

        fallback_task = asyncio.create_task(fallback())
        done, pending = await asyncio.wait(
            {primary_task, fallback_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        return await done.pop()
    finally:
        if fallback_task and not fallback_task.done():
            fallback_task.cancel()
