"""Checkpointer factory — provides persistence for LangGraph state."""

from contextlib import AbstractAsyncContextManager
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from config.settings import get_settings


def get_checkpointer(*, in_memory: bool = False) -> Any:
    """Return a checkpointer or async context manager.

    Args:
        in_memory: If True, use MemorySaver (for tests). Otherwise use SqliteSaver.
    """
    if in_memory:
        return MemorySaver()

    settings = get_settings()
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    return AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path)


def get_async_checkpointer() -> AbstractAsyncContextManager:
    """Open the production SQLite checkpointer for one graph execution."""
    return get_checkpointer()
