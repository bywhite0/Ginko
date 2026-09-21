"""Process-owned lock for one local data directory; the OS releases it after a crash."""

import os
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class InstanceRunningError(RuntimeError):
    """Another process already owns this data directory."""


class InstanceLock:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir.resolve() / "ginko.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise InstanceRunningError("this instance already holds the runtime lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise InstanceRunningError("another instance owns this data directory") from None
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        handle, self._file = self._file, None
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        # Never unlink: an existing waiter must keep locking the same file.

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
