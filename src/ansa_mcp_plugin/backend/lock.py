"""Exclusive backend lock; the ANSA bridge has a separate process mutex."""

import msvcrt
import errno


class BackendBusyError(RuntimeError):
    """The runtime is owned by another backend; a later request may retry."""


class BackendLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        try:
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, "winerror", None) == 33:
                    raise BackendBusyError(
                        "Another backend already owns this runtime directory: " + str(path.parent)
                        + ". Close the other MCP client, then retry ansa_connection_status; "
                        "do not delete the lock file or restart ANSA."
                    ) from exc
                raise
            self.file.seek(0)
            self.file.write(b"0")
            self.file.flush()
        except BaseException:
            self.file.close()
            raise

    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
