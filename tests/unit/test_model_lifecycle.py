"""Protect same-path reloads, including false-valued invalid model proxies."""
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.state import BridgeState
from ansa_mcp_bridge.operations.models import execute


class ModelProxy:
    def __init__(self):
        self.usable = True
    def is_usable(self):
        return self.usable
    def __bool__(self):
        return self.usable


class ModelLifecycleChecks(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / "runtime/unit-test-temp"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.model = ModelProxy()
        self.current = None
        self.models = [self.model]
        self.module = types.ModuleType("ansa")
        self.calls = []
        self.module.constants = types.SimpleNamespace(NASTRAN=1, ABAQUS=4, app_version="2026.1.0")
        self.module.base = types.SimpleNamespace(CurrentDeck=lambda: 1, DataBaseName=lambda: "same.ansa",
            GetCurrentAnsaModel=lambda: self.current, GetAllAnsaWindows=lambda: [object()],
            GetAnsaModelsOfAnsaWindow=lambda window: self.models, CollectEntities=lambda *args: [])
        self.module.session = types.SimpleNamespace(New=lambda *args: self.calls.append(args))
        self.patch = patch.dict("sys.modules", {"ansa": self.module})
        self.patch.start()
        self.state = BridgeState(self.temp.name)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_initial_empty_model_is_guarded_without_creating_entities(self):
        first = self.state.probe()["model"]
        self.assertTrue(first["write_enabled"])
        self.assertEqual("retained_model_lifetime", first["detection"])
        self.current = self.model
        self.assertEqual(first["epoch"], self.state.probe()["model"]["epoch"])

    def test_same_path_reload_invalidates_retained_false_proxy(self):
        before = self.state.probe()["model"]
        self.model.usable = False
        fresh = ModelProxy()
        self.models = [fresh]
        self.current = fresh
        after = self.state.probe()["model"]
        self.assertGreater(after["epoch"], before["epoch"])
        self.assertTrue(after["write_enabled"])
        self.assertIs(self.state._model_witness, fresh)

    def test_multiple_models_latch_closes_write_gate(self):
        self.state.probe()
        self.models = [self.model, ModelProxy()]
        self.assertFalse(self.state.probe()["model"]["write_enabled"])
        self.models = [self.model]
        self.assertFalse(self.state.probe()["model"]["write_enabled"])

    def test_new_requires_default_backup_before_any_model_mutation(self):
        before = self.state.probe()["model"]["epoch"]
        with self.assertRaisesRegex(ValueError, "backup_path"):
            execute(self.state, "model_new", {})
        self.assertEqual([], self.calls)
        self.assertEqual(before, self.state.model_epoch)


if __name__ == "__main__":
    unittest.main()
