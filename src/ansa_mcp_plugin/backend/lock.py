"""Exclusive backend lock; the ANSA bridge has a separate process mutex."""

import msvcrt


class BackendLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        try:
            self.file.seek(0)
            self.file.write(b"0")
            self.file.flush()
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("Another backend already owns this runtime directory") from exc

    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
