"""Server-Sent Events (SSE) helpers for the streaming analyze endpoint.

Events travel in the standard SSE wire format::

    event: node
    data: {"node": "retrieve"}

While the analytics pipeline is busy (notably between LLM calls), no bytes
flow to the client and intermediate proxies may time out the connection.
:func:`heartbeat_stream` wraps any async event source and emits SSE comment
keep-alives (``: keep-alive``) when the source is idle, without disturbing
well-behaved SSE clients.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator

# An SSE item: (event name, JSON-serializable payload)
Event = tuple[str, dict]


def format_sse(event: str, data: dict | list | None = None) -> str:
    """Serialize one event into the SSE wire format."""
    return (
        f"event: {event}\n"
        f"data: {json.dumps(data if data is not None else {}, ensure_ascii=False, default=str)}\n\n"
    )


async def heartbeat_stream(
    source: AsyncIterator[Event],
    interval_seconds: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Forward ``source`` as SSE text, keeping the connection warm when idle.

    Works by pumping the source into a queue from a background task so that
    keep-alive comments can be interleaved while the pipeline is silent. Any
    exception raised by the source is re-raised on the consumer side (after
    the frames already emitted), so callers keep their own error semantics.

    Args:
        source: Async iterator of ``(event, payload)`` tuples.
        interval_seconds: Maximum idle time before a keep-alive is emitted.
    """
    queue: asyncio.Queue = asyncio.Queue()
    finished = object()  # sentinel: pump exhausted the source

    async def pump() -> None:
        try:
            try:
                async for item in source:
                    await queue.put(item)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - surfaced to the consumer
                await queue.put(exc)
        finally:
            await queue.put(finished)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval_seconds)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if isinstance(item, BaseException):
                raise item
            if item is finished:
                return
            event, payload = item
            yield format_sse(event, payload)
    finally:
        if not pump_task.done():
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
