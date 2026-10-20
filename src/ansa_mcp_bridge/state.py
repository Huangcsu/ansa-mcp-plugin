"""Main-thread-only authorization and conservative model context."""

import json
import os
from pathlib import Path
import threading
import time
from ansa_mcp_shared.operations import writes_model
import sys
import uuid

from ansa_mcp_shared.identity import process_start_filetime


class BridgeState:
    def __init__(self, runtime_home):
        self.pid = os.getpid()
        self.process_start = process_start_filetime()
        self.instance_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.main_thread_id = threading.get_ident()
        self.runtime_home = Path(runtime_home)
        self.instance_dir = self.runtime_home / "instances" / self.instance_id
        self.session_dir = self.instance_dir / "sessions" / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.authorization_file = self.session_dir / "authorization.json"
        self.authorization = self._read_authorization()
        self.model_epoch = 1
        self.model_key = None
        self.model_detection = "incomplete_same_path_open"
        self._witness = None
        self._model_witness = None
        self._guard_observed = False
        self._multiple_models_seen = False
        self.cached_probe = None
        self.cached_probe_at = None
        self.cached_probe_monotonic = None
        self.cached_probe_backend_id = None
        self.startup_probe = {
            "state": "idle", "backend_id": None, "started_at": None,
            "attempt": 0, "error": None,
        }
        self.connected = threading.Event()
        self.connected_backend_id = None
        self.stopped = threading.Event()
        self.last_error = None
        self.journal = None
        self.panel_refresh = None
        self.panel_info = {"alive": False, "visible": False, "error": None, "close_reason": None,
                           "session_id": self.session_id, "timer_id": None,
                           "paint_count": 0, "last_paint_tick": 0}
        self.backend_status = {}

    def identity(self):
        return {key: getattr(self, key) for key in ("pid", "process_start", "instance_id", "session_id")}

    def check_conflict(self):
        self.assert_main()
        plugin = sys.modules.get("ansa_mcp_sum.plugin")
        legacy = getattr(plugin, "GUI_HANDLES", {}).get("bridge_runtime_state", {}) if plugin else {}
        if legacy and not legacy.get("stop_requested") and (legacy.get("poller") is not None or legacy.get("timer") is not None):
            raise RuntimeError("The legacy ANSA MCP-Sum bridge is active in this process")

    def validate_task(self, task):
        self.assert_main()
        if not self.connected.is_set() or self.stopped.is_set():
            raise RuntimeError("Executor is stopped or disconnected")
        expected = task["target"]
        if task["identity"] != self.identity():
            raise RuntimeError("Task process identity changed")
        prior = self.authorization
        if not prior or not prior.get("active") or (expected["backend_id"], expected["generation"]) != (prior["backend_id"], prior["generation"]):
            raise RuntimeError("Task authorization is stale")
        if (expected["instance_id"], expected["session_id"]) != (self.instance_id, self.session_id):
            raise RuntimeError("Task session is stale")
        model = self.probe()["model"]
        if task["model"]["epoch"] != model["epoch"]:
            raise RuntimeError("Task model context is stale")
        if writes_model(task["tool"]):
            self.check_conflict()
            if not model["write_enabled"]:
                raise RuntimeError("Reliable model replacement detection is unavailable; writes are closed")

    def _read_authorization(self):
        if self.authorization_file.is_file():
            return json.loads(self.authorization_file.read_text(encoding="utf-8"))
        return None

    def assert_main(self):
        if threading.get_ident() != self.main_thread_id:
            raise RuntimeError("ANSA API called outside the owning main thread")

    def save_authorization(self, value):
        self.assert_main()
        temporary = self.authorization_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.authorization_file)
        self.authorization = value

    def mark_closed(self):
        self.assert_main()
        value = {k: getattr(self, k) for k in ("pid", "process_start", "instance_id", "session_id")}
        value["executor_stopped"] = True
        path = self.session_dir / "closed.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)

    def probe(self):
        self.assert_main()
        from ansa import base, constants
        deck = base.CurrentDeck()
        database = base.DataBaseName()
        model = base.GetCurrentAnsaModel()
        # Model proxy text is only auxiliary evidence. A retained GRID lifetime
        # guards same-path reloads within the verified single-model scope.
        hint = repr(model)
        version = str(constants.app_version)
        try:
            windows = base.GetAllAnsaWindows()
            models = [base.GetAnsaModelsOfAnsaWindow(window) for window in windows]
            counts = tuple(len(items) for items in models)
            if len(windows) != 1 or counts != (1,):
                self._multiple_models_seen = True
            scope_supported = version == "2026.1.0" and counts == (1,) and not self._multiple_models_seen
        except Exception:
            models = []
            counts = ()
            scope_supported = False
        # A usable retained model reference is authoritative. Its text may
        # appear only after ANSA initializes the current-model convenience API.
        key = (database, deck, counts, scope_supported)
        replaced = False
        if self.model_key is None:
            self.model_key = key
        elif key != self.model_key:
            replaced = True
            self.model_key = key
        lifetime_witness = self._model_witness if self._model_witness is not None else self._witness
        if lifetime_witness is not None:
            try:
                if not lifetime_witness.is_usable():
                    replaced = True
            except Exception:
                replaced = True
        if self._model_witness is not None and model is not None and model != self._model_witness:
            replaced = True
        if replaced:
            self.model_epoch += 1
            self._witness = None
            self._model_witness = None
        if self._model_witness is None and scope_supported:
            candidate = model if model is not None else models[0][0]
            try:
                if candidate.is_usable():
                    self._model_witness = candidate
                    if self._guard_observed and not replaced:
                        self.model_epoch += 1
            except Exception:
                pass
        if self._witness is None and deck == constants.NASTRAN:
            nodes = base.CollectEntities(deck, None, "GRID")
            if nodes:
                self._witness = nodes[0]
                if self._guard_observed and not replaced and self._model_witness is None:
                    self.model_epoch += 1  # Context observed without a witness becomes invalid.
        self._guard_observed = True
        model_guarded = bool(self._model_witness is not None and self._model_witness.is_usable())
        grid_guarded = bool(self._witness is not None and self._witness.is_usable() and deck == constants.NASTRAN)
        guarded = bool(scope_supported and deck in (constants.NASTRAN, constants.ABAQUS) and (model_guarded or grid_guarded))
        self.model_detection = "retained_model_lifetime" if guarded and model_guarded else "retained_grid_lifetime" if guarded else "unwitnessed_write_closed"
        snapshot = {
            "pid": self.pid, "process_start": self.process_start,
            "instance_id": self.instance_id, "session_id": self.session_id,
            "thread_id": threading.get_ident(), "main_thread": threading.current_thread() is threading.main_thread(),
            "deck": deck,
            "deck_name": next((name for name in ("NASTRAN", "ABAQUS", "LSDYNA", "ANSYS", "PAMCRASH", "RADIOSS")
                               if getattr(constants, name, None) == deck), "未知求解器"),
            "model": {"epoch": self.model_epoch, "identity_hint": hint,
                                     "detection": self.model_detection, "database": database,
                                     "write_enabled": guarded,
                                     "write_scope": "ANSA 2026.1.0, one window/model, NASTRAN or ABAQUS with a retained usable model witness"},
            "version": version, "model_counts": list(counts), "multiple_models_seen": self._multiple_models_seen,
            "authorization": self.authorization,
            "connected": self.connected.is_set(), "stopped": self.stopped.is_set(),
            "persistence_blocked": bool(self.journal and self.journal.blocked),
            "last_error": self.last_error,
            "ui": dict(getattr(self, "panel_info", {})),
        }
        self.cached_probe = snapshot
        self.cached_probe_at = time.time()
        self.cached_probe_monotonic = time.monotonic()
        self.cached_probe_backend_id = self.connected_backend_id if self.connected.is_set() else None
        return snapshot

    def invalidate_model(self):
        self.assert_main()
        self.model_epoch += 1
        self.model_key = None
        self._witness = None
        self._model_witness = None
        self._guard_observed = False
        self.cached_probe = None
