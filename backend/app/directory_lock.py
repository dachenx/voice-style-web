"""One service owns a data directory, including startup orphan recovery."""

import os
from pathlib import Path
from typing import BinaryIO


class DirectoryLock:
    def __init__(self, directory: Path) -> None:
        self.path = directory / ".service.lock"
        self.file: BinaryIO | None = None

    def acquire(self) -> None:
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                # Windows-only APIs are absent from POSIX type stubs.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        self.file = handle

    def close(self) -> None:
        if self.file is not None:
            # Closing releases the OS lock even after process crashes; do not unlink its inode.
            self.file.close()
            self.file = None
