"""Risk-focused unit checks for model-file operations without an ANSA runtime."""

import hashlib
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import model_files


_VALID_STEP = (
    b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n"
    b"END-ISO-10303-21;\n"
)


class FakeFace:
    def __init__(self, entity_id, area):
        self._id = entity_id
        self.area = area

    def is_usable(self):
        return True


class FakeBase:
    def __init__(self, root):
        self.database = root / "current.ansa"
        self.database.write_bytes(b"model-before")
        self.entity_count = 3
        self.faces = [FakeFace(1, 2.5)]
        self.visible_cad = [object()]
        self.calls = []
        self.step_payload = _VALID_STEP

    def DataBaseName(self):
        return str(self.database)

    def CurrentDeck(self):
        return 1

    def GetFaceArea(self, face):
        return face.area

    def CollectEntities(self, deck, containers, search_types, *args, **kwargs):
        self.calls.append(("CollectEntities", deck, search_types, args, kwargs))
        if search_types == "__ALL_ENTITIES__":
            return [object() for _ in range(self.entity_count)]
        if search_types == "FACE":
            return list(self.faces)
        return list(self.visible_cad)

    def Open(self, *args, **kwargs):
        self.calls.append(("Open", args, kwargs))
        if len(args) != 1 or kwargs:
            raise TypeError("Open must receive the source path positionally")
        self.database = Path(args[0])
        self.entity_count += 2
        self.faces = [FakeFace(101, 4.25), FakeFace(102, 1.5)]
        return 0

    def OutputNastran(self, **kwargs):
        self.calls.append(("OutputNastran", kwargs))
        Path(kwargs["filename"]).write_bytes(b"NASTRAN bulk output\n")
        return 1

    def SaveFileAsStep(self, *args, **kwargs):
        self.calls.append(("SaveFileAsStep", args, kwargs))
        Path(args[0]).write_bytes(self.step_payload)
        return 0

    def InputAbaqus(self, **kwargs):
        self.calls.append(("InputAbaqus", kwargs))
        self.entity_count += 2
        return 1

    def InputNastran(self, **kwargs):
        self.calls.append(("InputNastran", kwargs))
        self.entity_count += 1
        raise RuntimeError("simulated parse failure after a partial import")

    def SaveAs(self, path, *, silent=False):
        self.calls.append(("SaveAs", path, silent))
        Path(path).write_bytes(b"ANSA autosave\n")
        return 0

    def Save(self):
        self.calls.append(("Save",))
        self.database.write_bytes(self.database.read_bytes() + b"\nsaved")
        return 0


class FakeState:
    def __init__(self, base):
        self.base = base
        self.invalidations = 0

    def assert_main(self):
        return None

    def invalidate_model(self):
        self.invalidations += 1

    def probe(self):
        return {"model": {"epoch": self.invalidations, "database": self.base.DataBaseName()}}


class ModelFileChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.base = FakeBase(self.root)
        self.state = FakeState(self.base)
        ansa = types.ModuleType("ansa")
        ansa.base = self.base
        ansa.constants = types.SimpleNamespace(NASTRAN=1, ABAQUS=2, LSDYNA=3)
        self.ansa_patch = patch.dict("sys.modules", {"ansa": ansa})
        self.ansa_patch.start()

    def tearDown(self):
        self.ansa_patch.stop()
        self.temp.cleanup()

    def test_solver_export_pins_single_file_options_and_verifies_hash(self):
        target = self.root / "model.nas"
        result = model_files.execute(self.state, "model_export", {
            "format": "NASTRAN", "path": str(target),
        })
        call = next(item for item in self.base.calls if item[0] == "OutputNastran")
        options = call[1]
        self.assertEqual("all", options["mode"])
        self.assertEqual("on", options["disregard_includes"])
        self.assertEqual("contents", options["include_output_mode"])
        self.assertEqual("on", options["output_only_main_file"])
        self.assertEqual("on", options["monolithic_output"])
        self.assertEqual("off", options["output_parts_in_xml"])
        self.assertEqual("off", options["update_include_fname"])
        self.assertEqual(target.stat().st_size, result["size_bytes"])
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), result["sha256"])
        with self.assertRaises(FileExistsError):
            model_files.execute(self.state, "model_export", {
                "format": "NASTRAN", "path": str(target),
            })
        with self.assertRaises(ValueError):
            model_files.execute(self.state, "model_export", {
                "format": "NASTRAN", "path": str(self.root / ".." / "traversal.nas"),
            })

    def test_step_requires_visible_scope_and_checks_created_step_contents(self):
        target = self.root / "visible.step"
        with self.assertRaisesRegex(ValueError, "scope='visible'"):
            model_files.execute(self.state, "model_export", {
                "format": "STEP", "path": str(target),
            })
        result = model_files.execute(self.state, "model_export", {
            "format": "STEP", "path": str(target), "scope": "visible",
        })
        call = next(item for item in self.base.calls if item[0] == "SaveFileAsStep")
        self.assertEqual((str(target),), call[1])
        self.assertEqual({}, call[2])
        visibility_call = next(item for item in self.base.calls if item[0] == "CollectEntities")
        self.assertTrue(visibility_call[4]["filter_visible"])
        self.assertFalse(visibility_call[4]["hidden_entities"])
        self.assertEqual(0, result["return_code"])
        self.assertFalse(result["return_code_is_diagnostic"])
        self.assertEqual("ISO-10303-21", result["verified_format"])

        self.base.step_payload = b"not a STEP file"
        invalid = self.root / "invalid.stp"
        with self.assertRaisesRegex(RuntimeError, "valid ISO-10303-21"):
            model_files.execute(self.state, "model_export", {
                "format": "STEP", "path": str(invalid), "scope": "visible",
            })

    def test_import_defaults_to_backup_and_merges_with_free_ids(self):
        source = self.root / "incoming.inp"
        source.write_text("*HEADING\n", encoding="ascii")
        backup = self.root / "before-import.ansa"
        result = model_files.execute(self.state, "model_import", {
            "format": "ABAQUS", "path": str(source), "backup_path": str(backup),
        })
        options = next(item[1] for item in self.base.calls if item[0] == "InputAbaqus")
        self.assertTrue(backup.is_file())
        self.assertEqual("merge_model", options["model_action"])
        self.assertEqual("off", options["new_include"])
        self.assertEqual("merge", options["header"])
        self.assertEqual("off", options["merge_parts"])
        for key in ("nodes_id", "elements_id", "properties_id", "materials_id"):
            self.assertEqual("offset-freeid", options[key])
        self.assertEqual("offset", options["sets_id"])
        self.assertEqual(3, result["before"]["entity_count"])
        self.assertEqual(5, result["after"]["entity_count"])
        self.assertEqual(2, result["entity_count_delta"])
        self.assertTrue(result["imported"])
        self.assertEqual(1, self.state.invalidations)
        self.assertEqual(1, result["model_after"]["model"]["epoch"])
        self.assertEqual("merge", result["mode"])

    def test_step_import_requires_explicit_replace_and_verified_backup(self):
        source = self.root / "assembly.step"
        source.write_bytes(_VALID_STEP)
        backup = self.root / "before-step.ansa"
        operation_order = []
        invalidate_model = self.state.invalidate_model
        open_model = self.base.Open

        def tracked_invalidation():
            operation_order.append("invalidate_model")
            return invalidate_model()

        def tracked_open(*args, **kwargs):
            operation_order.append("Open")
            return open_model(*args, **kwargs)

        with patch.object(self.state, "invalidate_model", side_effect=tracked_invalidation), \
                patch.object(self.base, "Open", side_effect=tracked_open) as open_api:
            result = model_files.execute(self.state, "model_import", {
                "format": "STEP", "path": str(source), "mode": "replace",
                "backup_path": str(backup),
            })

        open_api.assert_called_once_with(str(source))
        self.assertEqual(["invalidate_model", "Open"], operation_order)
        open_call = next(call for call in self.base.calls if call[0] == "Open")
        self.assertEqual(((str(source),), {}), open_call[1:])
        self.assertEqual("STEP", result["format"])
        self.assertEqual("replace", result["mode"])
        self.assertTrue(result["imported"])
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["return_code"])
        self.assertTrue(result["geometry_verified"])
        self.assertTrue(result["source_unchanged"])
        self.assertEqual({
            "path": str(source), "size_bytes": len(_VALID_STEP),
            "sha256": hashlib.sha256(_VALID_STEP).hexdigest(),
        }, result["source"])
        summary_fields = {
            "entity_count", "face_ids", "face_count", "face_area_total",
            "current_model_path",
        }
        for snapshot in (result["before"], result["after"]):
            self.assertTrue(summary_fields.issubset(snapshot))
        self.assertEqual([1], result["before"]["face_ids"])
        self.assertEqual([101, 102], result["after"]["face_ids"])
        self.assertEqual(str(source), result["after"]["current_model_path"])
        self.assertEqual(1, self.state.invalidations)
        self.assertEqual(1, result["model_after"]["model"]["epoch"])
        self.assertTrue(backup.is_file())
        self.assertEqual(str(backup), result["backup"]["path"])
        backup_bytes = backup.read_bytes()
        self.assertEqual(len(backup_bytes), result["backup"]["size_bytes"])
        self.assertEqual(hashlib.sha256(backup_bytes).hexdigest(), result["backup"]["sha256"])

    def test_step_and_solver_import_preflight_rejections_do_not_open_or_invalidate(self):
        valid_step = self.root / "valid.stp"
        valid_step.write_bytes(_VALID_STEP)
        bad_head = self.root / "bad-head.step"
        bad_head.write_bytes(b"HEADER;\nDATA;\nEND-ISO-10303-21;\n")
        bad_tail = self.root / "bad-tail.step"
        bad_tail.write_bytes(_VALID_STEP.replace(b"END-ISO-10303-21;", b"NO-END;"))
        empty_step = self.root / "empty.step"
        empty_step.write_bytes(b"")
        directory_step = self.root / "directory.step"
        directory_step.mkdir()
        solver_source = self.root / "solver.inp"
        solver_source.write_text("*HEADING\n", encoding="ascii")

        cases = (
            ("missing mode", {"format": "STEP", "path": str(valid_step)}),
            ("bad STEP header", {"format": "STEP", "path": str(bad_head), "mode": "replace"}),
            ("bad STEP footer", {"format": "STEP", "path": str(bad_tail), "mode": "replace"}),
            ("empty source", {"format": "STEP", "path": str(empty_step), "mode": "replace"}),
            ("non-file source", {"format": "STEP", "path": str(directory_step), "mode": "replace"}),
            ("missing default backup", {"format": "STEP", "path": str(valid_step), "mode": "replace"}),
            ("solver replace", {"format": "ABAQUS", "path": str(solver_source), "mode": "replace"}),
        )
        for label, params in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    model_files.execute(self.state, "model_import", params)
                self.assertEqual(0, self.state.invalidations)
                self.assertFalse(any(call[0] == "Open" for call in self.base.calls))

    def test_large_step_result_fits_transport_frame_with_complete_totals(self):
        from ansa_mcp_shared.framing import MAX_FRAME_BYTES, send_frame
        source = self.root / "large.step"
        source.write_bytes(_VALID_STEP)

        def large_import(path):
            self.base.database = Path(path)
            self.base.entity_count = 180000
            self.base.faces = [FakeFace(entity_id, 1.0) for entity_id in range(180000, 0, -1)]
            return 0

        with patch.object(self.base, "Open", side_effect=large_import):
            result = model_files.execute(self.state, "model_import", {
                "format": "STEP", "path": str(source), "mode": "replace", "preservation": "discard",
            })
        self.assertTrue(result["ok"])
        self.assertEqual(180000, result["after"]["face_count"])
        self.assertEqual(180000.0, result["after"]["face_area_total"])
        self.assertEqual(list(range(1, 201)), result["after"]["face_ids"])
        self.assertTrue(result["after"]["face_ids_truncated"])
        self.assertFalse(result["before"]["face_ids_truncated"])
        frames = []
        send_frame(types.SimpleNamespace(sendall=frames.append), {"result": result, "status": "succeeded"})
        self.assertLess(len(frames[0]), MAX_FRAME_BYTES)

    def test_default_solver_retry_matches_pre_upgrade_persisted_request(self):
        from ansa_mcp_plugin.backend.tasks import TaskStore
        from ansa_mcp_plugin.mcp.organization import register
        target = {"backend_id": "backend", "instance_id": "ansa", "session_id": "session", "generation": 1}
        model = {"epoch": 2}
        source = str(self.root / "prior.inp")
        backup = str(self.root / "prior-backup.ansa")
        legacy = {"tool": "model_import", "params": {"path": source, "format": "ABAQUS",
                  "preservation": "backup", "backup_path": backup}, "target": target, "model": model,
                  "ttl_seconds": 300}
        store = TaskStore(self.root / "prior-tasks.sqlite3")
        original = store.create("prior-op", legacy, {"pid": 1}, 300)
        store.close()
        store = TaskStore(self.root / "prior-tasks.sqlite3")
        try:
            registered = {}

            def decorator(**_options):
                def keep(function):
                    registered[function.__name__] = function
                    return function
                return keep

            def submit(tool, params, operation_id, expected_target, expected_model, ttl):
                return store.lookup(operation_id, {"tool": tool, "params": params,
                    "target": expected_target, "model": expected_model, "ttl_seconds": ttl})

            register(types.SimpleNamespace(tool=decorator), submit)
            imported = registered["ansa_model_import"]
            for extra in ({}, {"mode": "merge"}):
                retried = imported("prior-op", target, model, source, "ABAQUS", backup_path=backup, **extra)
                self.assertEqual(original["task_id"], retried["task_id"])
            with self.assertRaisesRegex(ValueError, "different request"):
                imported("prior-op", target, model, source, "ABAQUS", mode="replace", backup_path=backup)
        finally:
            store.close()

    def test_step_import_failures_read_back_and_never_report_success(self):
        def partial_exception(_path):
            self.base.entity_count += 1
            self.base.faces.append(FakeFace(2, 1.25))
            raise RuntimeError("STEP parser failed after a partial change")

        def empty_geometry(path):
            self.base.database = Path(path)
            self.base.entity_count = 0
            self.base.faces = []
            return 0

        def false_return(path):
            self.base.database = Path(path)
            self.base.entity_count += 1
            self.base.faces = [FakeFace(20, 3.0)]
            return False

        def negative_area(path):
            self.base.database = Path(path)
            self.base.faces = [FakeFace(30, -1.0)]
            return 0

        cases = (
            ("partial exception", partial_exception, False, True, None),
            ("empty geometry", empty_geometry, True, False, 0),
            ("boolean false", false_return, False, True, None),
            ("negative face area", negative_area, True, False, 0),
        )
        required_result_fields = {
            "format", "mode", "imported", "ok", "return_code", "before",
            "after", "geometry_verified", "source", "source_unchanged",
            "backup", "model_after",
        }
        for index, (label, handler, imported, geometry_verified, return_code) in enumerate(cases):
            with self.subTest(case=label):
                self.base.database = self.root / "current.ansa"
                self.base.entity_count = 3
                self.base.faces = [FakeFace(1, 2.5)]
                self.base.calls.clear()
                self.state.invalidations = 0
                source = self.root / f"failure-{index}.step"
                source.write_bytes(_VALID_STEP)
                backup = self.root / f"failure-{index}-backup.ansa"

                with patch.object(self.base, "Open", side_effect=handler) as open_api:
                    result = model_files.execute(self.state, "model_import", {
                        "format": "STEP", "path": str(source), "mode": "replace",
                        "backup_path": str(backup),
                    })

                open_api.assert_called_once_with(str(source))
                self.assertTrue(required_result_fields.issubset(result))
                self.assertEqual("STEP", result["format"])
                self.assertEqual("replace", result["mode"])
                self.assertEqual(imported, result["imported"])
                self.assertFalse(result["ok"])
                self.assertEqual(return_code, result["return_code"])
                self.assertEqual(geometry_verified, result["geometry_verified"])
                self.assertTrue(result["source_unchanged"])
                self.assertIn("entity_count", result["before"])
                self.assertIn("model_after", result)
                self.assertEqual(1, self.state.invalidations)
                self.assertEqual("partial_changes_possible", result["effect_assessment"])
                if label == "partial exception":
                    self.assertEqual(3, result["before"]["entity_count"])
                    self.assertEqual(4, result["after"]["entity_count"])
                    self.assertIn("STEP parser failed", result["error"])
                elif label == "empty geometry":
                    self.assertEqual(0, result["after"]["face_count"])
                    self.assertEqual([], result["after"]["face_ids"])
                elif label == "negative face area":
                    self.assertIn("error", result["after"])
                    self.assertFalse(result["geometry_verified"])

    def test_import_failure_still_reports_partial_entity_changes(self):
        source = self.root / "partial.nas"
        source.write_text("$ simulated", encoding="ascii")
        result = model_files.execute(self.state, "model_import", {
            "format": "NASTRAN", "path": str(source), "preservation": "discard",
        })
        self.assertFalse(result["imported"])
        self.assertFalse(result["ok"])
        self.assertIn("simulated parse failure", result["error"])
        self.assertEqual(3, result["before"]["entity_count"])
        self.assertEqual(4, result["after"]["entity_count"])
        self.assertEqual(1, result["entity_count_delta"])
        self.assertEqual("partial_changes_possible", result["effect_assessment"])
        self.assertEqual(1, self.state.invalidations)
        self.assertIn("model_after", result)
        with patch.object(self.base, "CollectEntities", side_effect=[[1, 2, 3], None]):
            unavailable = model_files.execute(self.state, "model_import", {
                "format": "ABAQUS", "path": str(source), "preservation": "discard",
            })
        self.assertFalse(unavailable["ok"])
        self.assertFalse(unavailable["entity_summary_verified"])
        self.assertIn("error", unavailable["after"])

    def test_autosave_preserves_database_and_current_save_checks_exact_target(self):
        database_before = self.base.DataBaseName()
        autosave_dir = self.root / "autosaves"
        autosave_dir.mkdir()
        autosave = model_files.execute(self.state, "model_autosave", {
            "directory": str(autosave_dir),
        })
        self.assertTrue(Path(autosave["path"]).is_file())
        self.assertEqual(database_before, self.base.DataBaseName())
        self.assertEqual(database_before, autosave["current_model_path"])
        self.assertTrue(any(call[:1] == ("SaveAs",) and call[2] is True for call in self.base.calls))

        with self.assertRaises(ValueError):
            model_files.execute(self.state, "model_save_current", {
                "expected_path": database_before, "overwrite": False,
            })
        other_model = self.root / "other.ansa"
        other_model.write_bytes(b"another model")
        with self.assertRaises(ValueError):
            model_files.execute(self.state, "model_save_current", {
                "expected_path": str(other_model), "overwrite": True,
            })
        saved = model_files.execute(self.state, "model_save_current", {
            "expected_path": database_before, "overwrite": True,
        })
        self.assertTrue(saved["saved"])
        self.assertEqual(database_before, saved["current_model_path"])
        self.assertEqual(0, saved["return_code"])
        self.assertEqual(Path(database_before).stat().st_size, saved["size_bytes"])

    def test_failed_import_retains_observed_evidence_when_model_probe_also_fails(self):
        from ansa_mcp_bridge.executor import MainThreadExecutor

        recorded = {}
        task = {"task_id": "failed-import", "tool": "model_import", "params": {}, "model": {"epoch": 1}}
        observed = {"ok": False, "error": "InputNastran failed", "backup": {"path": "before.ansa"},
                    "before": {"entity_count": 3}, "after": {"entity_count": 4}}
        journal = types.SimpleNamespace(
            blocked=False, next_queued=lambda: task,
            claim=lambda _task_id, _validate: {"state": "running"},
            finish=lambda _task_id, **fields: recorded.update(fields),
        )
        connected = threading.Event()
        connected.set()

        def unavailable_probe():
            raise RuntimeError("Model identity unavailable")

        state = types.SimpleNamespace(
            journal=journal, stopped=threading.Event(), connected=connected,
            authorization={"backend_id": "backend"}, connected_backend_id="backend",
            validate_task=lambda _task: None, probe=unavailable_probe,
            identity=lambda: {"instance_id": "isolated", "session_id": "session"}, last_error=None,
        )
        with patch("ansa_mcp_bridge.operations.execute", return_value=observed):
            MainThreadExecutor(state)._task_tick()
        self.assertEqual(recorded["error"], "InputNastran failed")
        self.assertEqual(recorded["result"]["operation_result"], observed)
        self.assertTrue(recorded["result"]["possible_effects"])
        self.assertIsNone(recorded["result"]["model_after"])
        self.assertIn("unavailable", recorded["result"]["model_probe_error"])


if __name__ == "__main__":
    unittest.main()
