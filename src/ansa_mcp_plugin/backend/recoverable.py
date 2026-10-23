"""Keep MCP discovery available while another client owns the backend."""

import threading
import time

from .lock import BackendBusyError
from .service import Service


class RecoverableService:
    """Retry only lock contention, without creating a second scheduler or queue."""

    def __init__(self, runtime_home):
        self._runtime_home = runtime_home
        self._service = None
        self._mutex = threading.Lock()
        self._closed = False
        self._retry_at = 0.0
        self._busy = None
        try:
            self._acquire()
        except BackendBusyError:
            pass

    def _acquire(self):
        with self._mutex:
            if self._closed:
                raise RuntimeError("MCP backend client is closed")
            if self._service is not None:
                return self._service
            if self._busy is not None and time.monotonic() < self._retry_at:
                raise BackendBusyError(str(self._busy))
            try:
                self._service = Service(self._runtime_home)
            except BackendBusyError as exc:
                self._busy = exc
                self._retry_at = time.monotonic() + 1.0
                raise
            self._busy = None
            return self._service

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._acquire(), name)

    def close(self):
        with self._mutex:
            self._closed = True
            if self._service is not None:
                self._service.close()
