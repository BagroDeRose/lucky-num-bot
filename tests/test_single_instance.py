"""Single-instance protection: one bot process per machine.

Two processes polling with the same token compete for getUpdates and invite
flood control. The guard is an exclusive OS file lock (msvcrt on Windows,
flock elsewhere), so the kernel — not a PID file — decides whether an
instance is live, and the lock disappears when the holder dies however it
dies.

Every test locks a file under tmp_path, never the real runtime location.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import main as main_module
from app.single_instance import (
    ACQUIRE_TIMEOUT,
    AlreadyRunningError,
    SingleInstance,
    lock_path,
    runtime_dir,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_first_instance_acquires_and_stores_no_data(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    lock = SingleInstance(path)
    lock.acquire()
    try:
        assert lock.held
        assert path.exists()
    finally:
        lock.release()
    assert not lock.held
    assert path.read_bytes() == b""  # a lock target, never storage: no secrets in it


def test_second_acquisition_fails_while_the_first_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    first = SingleInstance(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match="already running"):
            SingleInstance(path).acquire(timeout=0)
    finally:
        first.release()


def test_releasing_allows_the_next_instance(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    first = SingleInstance(path)
    first.acquire()
    first.release()

    second = SingleInstance(path)
    second.acquire()  # must not raise: this is the restart-after-shutdown path
    try:
        assert second.held
    finally:
        second.release()


def test_context_manager_releases_even_when_the_body_raises(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    with pytest.raises(ValueError, match="boom"), SingleInstance(path):
        raise ValueError("boom")

    with SingleInstance(path):  # released despite the exception
        pass


def test_acquire_is_reentrant_for_the_same_object(tmp_path: Path) -> None:
    lock = SingleInstance(tmp_path / "bot.lock")
    lock.acquire()
    lock.acquire()  # no double-open, no self-deadlock
    lock.release()
    assert not lock.held


def test_lock_location_is_deterministic_and_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from_root = lock_path()
    monkeypatch.chdir(tmp_path)
    assert lock_path() == from_root
    assert from_root.is_absolute()
    assert runtime_dir() in from_root.parents
    assert tmp_path not in from_root.parents  # not derived from the working directory


def test_lock_is_created_under_the_user_runtime_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
        assert lock_path() == Path(r"C:\Users\someone\AppData\Local\LuckyNum\bot.lock")
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        assert lock_path() == Path("/run/user/1000/LuckyNum/bot.lock")


# --- cross-process: the guarantee that matters --------------------------------

_HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from app.single_instance import SingleInstance

with SingleInstance({path!r}):
    print("locked", flush=True)
    time.sleep(60)
"""


def _spawn_holder(path: Path) -> subprocess.Popen[str]:
    script = _HOLDER.format(root=str(PROJECT_ROOT), path=str(path))
    process = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.stdout.readline().startswith("locked"):
            return process
        if process.poll() is not None:
            raise AssertionError(f"holder exited early: {process.stderr.read() if process.stderr else ''}")
    process.kill()
    raise AssertionError("holder did not acquire the lock in time")


def test_another_process_holding_the_lock_blocks_this_one(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    holder = _spawn_holder(path)
    try:
        with pytest.raises(AlreadyRunningError):
            SingleInstance(path).acquire(timeout=0)
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_lock_is_free_again_after_the_holder_is_killed(tmp_path: Path) -> None:
    """Crash recovery: the OS drops the lock, so no stale-lock cleanup is needed."""
    path = tmp_path / "bot.lock"
    holder = _spawn_holder(path)
    holder.kill()
    holder.wait(timeout=30)

    lock = SingleInstance(path)
    # No explicit sleep: acquire() waits out the OS releasing the dead
    # process's lock, which is exactly what a supervisor restart relies on.
    lock.acquire()
    try:
        assert lock.held
    finally:
        lock.release()


# --- startup integration ---------------------------------------------------------


async def test_second_instance_does_not_start_the_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected instance must touch neither the database nor Telegram."""
    monkeypatch.setattr(main_module.settings, "bot_token", "123456:TEST")
    monkeypatch.setattr(main_module, "lock_path_for_tests", tmp_path / "bot.lock", raising=False)
    monkeypatch.setattr(
        main_module, "SingleInstance", lambda: SingleInstance(tmp_path / "bot.lock")
    )

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("a second instance must not reach this")

    monkeypatch.setattr(main_module, "init_db", AsyncMock(side_effect=must_not_run))
    monkeypatch.setattr(main_module, "Bot", must_not_run)
    monkeypatch.setattr(main_module, "WeeklyScheduler", must_not_run)

    holder = SingleInstance(tmp_path / "bot.lock")
    holder.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            await main_module.run()
    finally:
        holder.release()


def test_main_exits_with_code_1_when_another_instance_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(main_module.settings, "bot_token", "123456:TEST")
    monkeypatch.setattr(
        main_module, "SingleInstance", lambda: SingleInstance(tmp_path / "bot.lock")
    )
    monkeypatch.setattr(main_module, "Bot", lambda *a, **k: pytest.fail("must not start"))

    holder = SingleInstance(tmp_path / "bot.lock")
    holder.acquire()
    try:
        with pytest.raises(SystemExit) as exit_info:
            main_module.main()
    finally:
        holder.release()

    assert exit_info.value.code == 1
    assert "already running" in caplog.text


async def test_normal_shutdown_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After run() returns normally the lock is free for the next start."""
    path = tmp_path / "bot.lock"
    monkeypatch.setattr(main_module.settings, "bot_token", "123456:TEST")
    monkeypatch.setattr(main_module, "SingleInstance", lambda: SingleInstance(path))
    monkeypatch.setattr(main_module, "_run_bot", AsyncMock())  # stand in for the whole bot

    await main_module.run()

    nxt = SingleInstance(path)
    nxt.acquire()  # a restart right after shutdown works
    try:
        assert nxt.held
    finally:
        nxt.release()


async def test_startup_failure_still_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bot.lock"
    monkeypatch.setattr(main_module.settings, "bot_token", "123456:TEST")
    monkeypatch.setattr(main_module, "SingleInstance", lambda: SingleInstance(path))
    monkeypatch.setattr(main_module, "_run_bot", AsyncMock(side_effect=RuntimeError("startup boom")))

    with pytest.raises(RuntimeError, match="startup boom"):
        await main_module.run()

    nxt = SingleInstance(path)
    nxt.acquire()  # not left locked by the failure
    try:
        assert nxt.held
    finally:
        nxt.release()



def test_acquire_waits_briefly_rather_than_failing_instantly(tmp_path: Path) -> None:
    """Regression: an immediate restart after a crash hit Windows' asynchronous
    lock release and exited with "another instance is running".
    """
    path = tmp_path / "bot.lock"
    holder = _spawn_holder(path)
    holder.kill()
    holder.wait(timeout=30)

    started = time.monotonic()
    lock = SingleInstance(path)
    lock.acquire()  # retries within the grace period instead of giving up
    try:
        assert time.monotonic() - started < ACQUIRE_TIMEOUT + 2
    finally:
        lock.release()
