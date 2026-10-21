"""LS-DYNA limited-write admission and claim gates."""

import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ansa_mcp_bridge.state import BridgeState
from ansa_mcp_plugin.backend.scheduler import TaskScheduler
from ansa_mcp_shared.operations import LIMITED_WRITE_TOOLS, model_write_allowed


class ModelProxy:
    def __init__(self, usable=True):
        self.usable = usable

    def is_usable(self):
        return self.usable

    def __bool__(self):
        return self.usable


class LsdynaGateChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.ansa = types.ModuleType("ansa")
        self.ansa.constants = types.SimpleNamespace(
            NASTRAN=1, ABAQUS=2, LSDYNA=3, app_version="2026.1.0",
        )
        self.current = None
        self.models = []
        self.windows = [object()]
        self.ansa.base = types.SimpleNamespace(
            CurrentDeck=lambda: self.ansa.constants.LSDYNA,
            DataBaseName=lambda: "same.key",
            GetCurrentAnsaModel=lambda: self.current,
            GetAllAnsaWindows=lambda: self.windows,
            GetAnsaModelsOfAnsaWindow=lambda _window: self.models,
            CollectEntities=lambda *_args: [],
        )
        self.ansa_patch = patch.dict("sys.modules", {"ansa": self.ansa})
        self.ansa_patch.start()
        self.addCleanup(self.ansa_patch.stop)
        self.state, self.model = self.new_state()
        self.target = {
            "backend_id": "backend-1",
            "generation": 1,
            "instance_id": self.state.instance_id,
            "session_id": self.state.session_id,
        }

    def new_state(self, *, version="2026.1.0", models=None, usable=True):
        model = ModelProxy(usable)
        self.current = model
        self.models = [model] if models is None else models(model)
        self.ansa.constants.app_version = version
        state = BridgeState(self.temp.name)
        state.authorization = {
            "active": True,
            "backend_id": "backend-1",
            "generation": 1,
        }
        state.connected.set()
        return state, model

    def make_task(self, tool, epoch):
        return {
            "tool": tool,
            "target": self.target,
            "model": {"epoch": epoch},
            "identity": self.state.identity(),
        }

    @staticmethod
    def snapshot_for(state, model):
        return {
            **state.identity(),
            "model": model,
            "stopped": False,
            "persistence_blocked": False,
        }

    def make_scheduler(self, model_snapshot=None):
        scheduler = TaskScheduler()
        scheduler._lock = threading.RLock()
        scheduler.closed = False
        scheduler.dispatch_blocked = False
        scheduler.tasks = types.SimpleNamespace(
            lookup=Mock(return_value=None),
            create=Mock(return_value={"task_id": "created"}),
        )
        scheduler.model_snapshot = Mock(
            return_value=self.snapshot_for(self.state, model_snapshot)
            if model_snapshot is not None else self.state.probe()
        )
        return scheduler

    def submit(self, scheduler, tool, epoch, operation_id):
        return scheduler.submit_task(
            tool, {}, operation_id, self.target, {"epoch": epoch},
        )

    def test_limited_tools_are_admitted_and_claimed(self):
        initial = self.state.probe()["model"]
        self.assertFalse(initial["write_enabled"])
        self.assertEqual(initial["write_tools"], sorted(LIMITED_WRITE_TOOLS))

        for tool in sorted(LIMITED_WRITE_TOOLS):
            with self.subTest(tool=tool):
                scheduler = self.make_scheduler()
                self.submit(scheduler, tool, initial["epoch"], f"op-{tool}")
                scheduler.tasks.create.assert_called_once()
                self.state.validate_task(self.make_task(tool, initial["epoch"]))

    def test_general_write_gate_and_forged_limited_lists(self):
        self.assertTrue(model_write_allowed({"write_enabled": True}, "entity_create"))
        forged = {
            "write_enabled": False,
            "write_tools": sorted(LIMITED_WRITE_TOOLS) + [
                "entity_create", "script_execute", "model_new",
            ],
        }
        for tool in ("entity_create", "script_execute", "model_new"):
            with self.subTest(tool=tool):
                self.assertFalse(model_write_allowed(forged, tool))

        self.state.probe = lambda: {"model": {"epoch": 1, **forged}}
        for tool in ("entity_create", "script_execute", "model_new"):
            with self.subTest(claim=tool), self.assertRaisesRegex(RuntimeError, "writes are closed"):
                self.state.validate_task(self.make_task(tool, 1))

        scheduler = self.make_scheduler(forged | {"epoch": 1})
        with self.assertRaisesRegex(RuntimeError, "writes are closed"):
            self.submit(scheduler, "entity_create", 1, "op-forged")
        scheduler.tasks.create.assert_not_called()

    def test_legacy_snapshot_without_write_tools_is_closed(self):
        legacy = {"epoch": 1, "write_enabled": False}
        self.assertFalse(model_write_allowed(legacy, "quality_locate"))

        self.state.probe = lambda: {"model": legacy}
        with self.assertRaisesRegex(RuntimeError, "writes are closed"):
            self.state.validate_task(self.make_task("quality_locate", 1))

        scheduler = self.make_scheduler(legacy)
        with self.assertRaisesRegex(RuntimeError, "writes are closed"):
            self.submit(scheduler, "quality_repair", 1, "op-legacy")
        scheduler.tasks.create.assert_not_called()

    def test_same_path_reopen_advances_epoch_and_rejects_old_claim(self):
        before = self.state.probe()["model"]
        old_task = self.make_task("quality_repair", before["epoch"])

        self.model.usable = False
        fresh = ModelProxy()
        self.current = fresh
        self.models = [fresh]
        after = self.state.probe()["model"]

        self.assertEqual(after["database"], "same.key")
        self.assertGreater(after["epoch"], before["epoch"])
        self.assertEqual(after["write_tools"], sorted(LIMITED_WRITE_TOOLS))
        scheduler = self.make_scheduler()
        with self.assertRaisesRegex(RuntimeError, "Model context is missing or stale"):
            self.submit(scheduler, "quality_repair", before["epoch"], "op-stale")
        scheduler.tasks.create.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "model context is stale"):
            self.state.validate_task(old_task)

    def test_multiple_models_unknown_version_and_missing_witness_stay_closed(self):
        state, first = self.new_state(models=lambda model: [model, ModelProxy()])
        self.assertEqual(state.probe()["model"]["write_tools"], [])
        self.models = [first]
        self.assertEqual(state.probe()["model"]["write_tools"], [])

        unknown, _model = self.new_state(version="2026.2.0")
        unknown_model = unknown.probe()["model"]
        self.assertFalse(unknown_model["write_enabled"])
        self.assertEqual(unknown_model["write_tools"], [])

        invalidated, model = self.new_state()
        self.assertEqual(invalidated.probe()["model"]["write_tools"], sorted(LIMITED_WRITE_TOOLS))
        model.usable = False
        unwitnessed_model = invalidated.probe()["model"]
        self.assertFalse(unwitnessed_model["write_enabled"])
        self.assertEqual(unwitnessed_model["write_tools"], [])


if __name__ == "__main__":
    unittest.main()
