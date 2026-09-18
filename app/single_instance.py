"""One bot process per machine.

Two LuckyNum processes polling Telegram with the same bot token fight over
getUpdates: Telegram hands each update to only one of them, and the extra
request volume invites "Flood control exceeded on method 'GetUpdates'". The
application cannot detect that from the outside, so it takes an exclusive
OS lock on a file at startup and refuses to run if another live process
already holds it.

The lock is the operating system's, not a PID file: the kernel owns it and
drops it the moment the holding process ends, however it ends (normal exit,
kill, crash, power loss). So there is no stale lock to clean up by hand and a
restart after a crash always works.

The file itself stays empty: its contents are never read or trusted, and no
token or other secret is stored in it. (On Windows the locked byte cannot be
read by another handle anyway, so writing a pid there would help nobody.)

The lock path is derived from the OS user's runtime/application-data
directory, never from the working directory, so the guard holds no matter
where the bot is started from.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import TracebackType

from app.logging import get_logger

logger = get_logger(__name__)

APP_DIR_NAME = "LuckyNum"
LOCK_FILE_NAME = "bot.lock"
# Grace period for a just-killed instance's lock to be dropped by the OS.
ACQUIRE_TIMEOUT = 3.0
RETRY_INTERVAL = 0.1


class AlreadyRunningError(RuntimeError):
    """Another LuckyNum process holds the single-instance lock."""


def runtime_dir() -> Path:
    """Per-user directory for runtime state, independent of the cwd."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"  # noqa: S108 - conventional fallback
    return Path(base) / APP_DIR_NAME


def lock_path() -> Path:
    return runtime_dir() / LOCK_FILE_NAME


def _lock_file(fd: int) -> None:
    """Take an exclusive, non-blocking lock; raise OSError if held elsewhere."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class SingleInstance:
    """Context manager around the exclusive lock file.

    Usage::

        with SingleInstance():   # raises AlreadyRunningError if held
            ...
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else lock_path()
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float = ACQUIRE_TIMEOUT) -> None:
        """Take the lock, or raise AlreadyRunningError.

        Windows releases a killed process's file lock asynchronously, so an
        immediate restart after a crash can still see it held for a moment.
        The attempt is therefore retried for a short grace period: a live
        instance holds the lock for as long as it runs, so waiting a couple of
        seconds never turns a running instance into a false "free", while a
        dying one lets go within milliseconds.
        """
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(timeout, 0.0)
        last: OSError | None = None
        while True:
            try:
                # O_CREAT without O_TRUNC: the file is a lock target, not
                # storage, and another process may hold a lock on it right now.
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:  # e.g. a sharing violation from a dying holder
                last = exc
            else:
                try:
                    _lock_file(fd)
                except OSError as exc:
                    os.close(fd)
                    last = exc
                else:
                    self._fd = fd
                    return
            if time.monotonic() >= deadline:
                raise AlreadyRunningError(
                    f"Another LuckyNum instance is already running (lock: {self.path}). "
                    "Stop it before starting a new one."
                ) from last
            time.sleep(RETRY_INTERVAL)

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        except OSError:  # already gone (e.g. the file was removed): closing is enough
            logger.debug("Single-instance lock could not be unlocked cleanly", exc_info=True)
        finally:
            os.close(fd)

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
