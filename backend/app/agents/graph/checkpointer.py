"""LangGraph checkpointer construction and process-wide lifecycle management.

The checkpointer persists graph state snapshots after every superstep, keyed by
``config.configurable.thread_id``. This is what enables multi-turn follow-up
questions: a new invocation resuming the same thread id starts from the state
left behind by the previous turn (most importantly the accumulated
``messages`` conversation shared by generation and repair loops).

Backends:
    - ``memory``: :class:`MemorySaver`, per-process, lost on restart. Default
      for development and tests.
    - ``redis``: :class:`AsyncRedisSaver`, survives restarts and is shareable
      by multiple backend replicas. Requires ``langgraph-checkpoint-redis``.
"""

from types import ModuleType
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver, SerializerProtocol
from langgraph.checkpoint.memory import MemorySaver

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

CheckpointerBackend = Literal["memory", "redis"]

_global_checkpointer: BaseCheckpointSaver | None = None

# Modules whose types may end up inside AnalyticsState and therefore inside a
# checkpoint. They must be registered explicitly on the msgpack serializer so
# checkpoint round-trips stay stable when LangGraph moves to its strict
# deserialization mode (LANGGRAPH_STRICT_MSGPACK).


def _import_app_state_modules() -> tuple[ModuleType, ...]:
    """Import the schema modules referenced by AnalyticsState."""
    import app.agents.schemas
    import app.repositories.schemas
    import app.schemas.analyze
    import app.security.schemas
    import app.semantic.retrieval.schemas

    return (
        app.agents.schemas,
        app.repositories.schemas,
        app.schemas.analyze,
        app.security.schemas,
        app.semantic.retrieval.schemas,
    )


_APP_STATE_MODULES: tuple[ModuleType, ...] = _import_app_state_modules()


def _app_state_serializer() -> SerializerProtocol:
    """Build a JsonPlusSerializer whose msgpack allowlist covers app state types."""
    import inspect

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    allowed_types: list[type] = [
        member
        for module in _APP_STATE_MODULES
        for member in vars(module).values()
        if inspect.isclass(member) and member.__module__ == module.__name__
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed_types)


async def create_checkpointer(
    backend: CheckpointerBackend | None = None,
) -> BaseCheckpointSaver:
    """Create and initialize a checkpointer for the configured backend."""
    settings = get_settings()
    selected: CheckpointerBackend = backend or settings.CHECKPOINTER_BACKEND

    if selected == "redis":
        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "CHECKPOINTER_BACKEND=redis requires the 'langgraph-checkpoint-redis' "
                "package. Install it with: pip install 'langgraph-checkpoint-redis>=0.6.0'"
            ) from exc

        redis_saver = AsyncRedisSaver(settings.redis_url)
        redis_saver.serde = _app_state_serializer()
        await redis_saver.asetup()  # creates the checkpoint index structures in Redis
        logger.info("Redis checkpointer initialized (%s).", settings.redis_url)
        return redis_saver

    memory_saver = MemorySaver()
    memory_saver.serde = _app_state_serializer()
    logger.info("In-memory checkpointer initialized (state is lost on restart).")
    return memory_saver


async def init_checkpointer() -> BaseCheckpointSaver:
    """Startup hook: build the process-wide checkpointer used by AnalyzeService.

    Must run before the first request so ``get_checkpointer()`` returns the
    instance the compiled graph was compiled with.
    """
    global _global_checkpointer
    _global_checkpointer = await create_checkpointer()
    return _global_checkpointer


def get_checkpointer() -> BaseCheckpointSaver | None:
    """Return the process-wide checkpointer, or ``None`` before startup.

    ``None`` keeps the graph stateless (each run independent) which is the
    behavior relied upon by unit tests and offline scripts.
    """
    return _global_checkpointer


async def shutdown_checkpointer() -> None:
    """Shutdown hook: release the checkpointer's backend connections."""
    global _global_checkpointer
    saver, _global_checkpointer = _global_checkpointer, None
    if saver is None:
        return

    aclose = getattr(saver, "aclose", None)
    if callable(aclose):
        await aclose()
        logger.info("Checkpointer closed.")
        return

    close = getattr(saver, "close", None)
    if callable(close):
        close()
    logger.info("Checkpointer released.")
