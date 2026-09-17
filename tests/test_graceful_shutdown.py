"""Ctrl+C shutdown on Windows (regression for the log lines
"Polling stopped" -> "ERROR Failed to fetch updates - TelegramNetworkError:
... ServerDisconnectedError" -> "Sleep for 1.000000 seconds and try again").

aiogram cannot install loop signal handlers on Windows, so Ctrl+C reaches
asyncio's default handler, which cancels the main task. aiogram then skips its
graceful stop path and closes the HTTP session while its polling task is still
inside getUpdates. The fake session below behaves like aiohttp does: an
in-flight long poll fails with "Server disconnected" when the session closes.
No network access.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, GetUpdates, TelegramMethod
from aiogram.types import User

from app import main as main_module


class LongPollingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.polling = asyncio.Event()
        self.closed = asyncio.Event()

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Test", username="test_bot")
        if isinstance(method, GetUpdates):
            self.polling.set()
            await self.closed.wait()
            raise TelegramNetworkError(method, "HTTP Client says - ServerDisconnectedError: Server disconnected")
        return True

    async def close(self) -> None:
        self.closed.set()

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


async def _cancel_leftovers() -> None:
    current = asyncio.current_task()
    leftovers = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    for task in leftovers:
        task.cancel()
    for task in leftovers:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _polling_tasks() -> list[asyncio.Task[Any]]:
    return [
        t for t in asyncio.all_tasks()
        if not t.done() and getattr(t.get_coro(), "__qualname__", "") == "Dispatcher._polling"
    ]


async def test_reproduce_cancellation_leaves_long_poll_running(caplog: pytest.LogCaptureFixture) -> None:
    """What asyncio's default Ctrl+C handling does to aiogram on Windows."""
    session = LongPollingSession()
    bot = Bot("123456:TEST", session=session)
    dispatcher = Dispatcher()
    main_task = asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False))
    await session.polling.wait()

    with caplog.at_level(logging.INFO, logger="aiogram"):
        main_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await main_task
        await asyncio.sleep(0.05)
        orphaned = _polling_tasks()
    await _cancel_leftovers()

    assert "Failed to fetch updates - TelegramNetworkError" in caplog.text
    assert orphaned  # the polling task outlived the dispatcher


async def test_ctrl_c_handler_stops_polling_gracefully(caplog: pytest.LogCaptureFixture) -> None:
    session = LongPollingSession()
    bot = Bot("123456:TEST", session=session)
    dispatcher = Dispatcher()
    main_task = asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False))
    await session.polling.wait()

    original = signal.getsignal(signal.SIGINT)
    restore = main_module.install_graceful_shutdown(dispatcher)
    try:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler) and handler is not original
        with caplog.at_level(logging.INFO, logger="aiogram"):
            handler(signal.SIGINT, None)  # what Python calls on Ctrl+C
            await asyncio.wait_for(main_task, timeout=5)
            await asyncio.sleep(0.05)
            leftovers = _polling_tasks()
        # A second Ctrl+C falls back to the previous (hard-interrupt) handler.
        assert signal.getsignal(signal.SIGINT) is original
    finally:
        restore()
        await _cancel_leftovers()

    assert "Failed to fetch updates" not in caplog.text
    assert "Polling stopped for bot" in caplog.text
    assert session.closed.is_set()
    assert leftovers == []
    assert signal.getsignal(signal.SIGINT) is original


async def test_handler_is_harmless_when_polling_is_not_running() -> None:
    dispatcher = Dispatcher()
    original = signal.getsignal(signal.SIGINT)
    restore = main_module.install_graceful_shutdown(dispatcher)
    try:
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)  # type: ignore[operator]
        await asyncio.sleep(0.05)  # the scheduled stop must not raise
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is original
