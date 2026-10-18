"""Risk-focused checks for the controlled benchmark batch without ANSA."""

import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError

from ansa_mcp_shared.framing import MAX_FRAME_BYTES
from ansa_mcp_bridge.operations import benchmark
from ansa_mcp_plugin.mcp import benchmark as benchmark_mcp


_COUNTS = {"parts": 1, "faces": 1, "nodes": 2, "shells": 1, "solids": 0, "elements": 1}


class FakeBase:
    def __init__(self, root):
        self.database = root / "current.ansa"
        self.database.write_bytes(b"current model")
        self.deck = 1
        self.opens = []
        self.fail_inputs = False
        self.on_save = None
        self.on_open = None

    def DataBaseName(self):
        return str(self.database)

    def CurrentDeck(self):
        return self.deck

    def SaveAs(self, path, *, silent=False):
        if silent is not True:
            raise AssertionError("backup must use silent SaveAs")
        Path(path).write_bytes(b"verified saved model")
        if self.on_save:
            self.on_save()
        return 0

    def Open(self, path):
        self.opens.append(str(path))
        if self.fail_inputs and Path(path).name != "backup.ansa":
            return 1
        self.database = Path(path)
        self.deck = 1
        if self.on_open:
            self.on_open(path)
        return 0


class FakeState:
    def __init__(self, root, base):
        self.base = base
        self.identity_value = {
            "pid": 321,
            "process_start": 456,
            "instance_id": "instance-a",
            "session_id": "session-a",
        }
        self.authorization = {"active": True, "backend_id": "backend-a", "generation": 3}
        self.authorization_file = root / "authorization.json"
        self._write_authorization()
        self.connected_backend_id = "backend-a"
        self.connected = threading.Event()
        self.connected.set()
        self.stopped = threading.Event()
        self.model_epoch = 1
        self.invalidations = 0
        self.session_dir = root / "session"
        self.session_dir.mkdir()

    def _write_authorization(self):
        self.authorization_file.write_text(json.dumps(self.authorization), encoding="utf-8")

    def _read_authorization(self):
        return json.loads(self.authorization_file.read_text(encoding="utf-8"))

    def identity(self):
        return dict(self.identity_value)

    def check_conflict(self):
        return None

    def assert_main(self):
        return None

    def invalidate_model(self):
        self.invalidations += 1
        self.model_epoch += 1

    def probe(self):
        return {
            **self.identity_value,
            "thread_id": threading.get_ident(),
            "main_thread": True,
            "deck": self.base.CurrentDeck(),
            "model": {
                "epoch": self.model_epoch,
                "identity_hint": "fake-model",
                "detection": "retained_model_lifetime",
                "database": self.base.DataBaseName(),
                "write_enabled": True,
            },
            "authorization": dict(self.authorization),
            "connected": self.connected.is_set(),
            "stopped": self.stopped.is_set(),
            "persistence_blocked": False,
        }


class BenchmarkChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "inputs"
        self.input_dir.mkdir()
        self.source = self.input_dir / "sample.step"
        self.source.write_bytes(b"STEP source")
        self.base = FakeBase(self.root)
        self.state = FakeState(self.root, self.base)
        self.ansa = types.ModuleType("ansa")
        self.ansa.base = self.base
        self.ansa.constants = types.SimpleNamespace(NASTRAN=1)
        self.ansa_patch = patch.dict("sys.modules", {"ansa": self.ansa})
        self.ansa_patch.start()
        self.summary_patch = patch.object(benchmark.entity_catalog, "_model_summary", self._summary)
        self.summary_patch.start()
        self.geometry_patches = [
            patch.object(benchmark.geometry, "_source_faces", self._source_faces),
            patch.object(benchmark.geometry, "_face_rows", self._face_rows),
            patch.object(benchmark.geometry, "_bounds", lambda _base, _entities: [0, 0, 0, 1, 1, 8]),
            patch.object(benchmark, "verify_private_path", lambda _path: None),
        ]
        for item in self.geometry_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.geometry_patches):
            item.stop()
        self.summary_patch.stop()
        self.ansa_patch.stop()
        self.temp.cleanup()

    def _summary(self, _base, _constants, deck):
        values = dict(_COUNTS)
        if Path(self.base.DataBaseName()).name == self.source.name:
            values["faces"] = 2
        return {
            "deck": deck,
            "deck_name": "NASTRAN",
            "database": self.base.DataBaseName(),
            "entity_count": sum(values.values()),
            "entity_counts": values,
            "counts_complete": True,
            "count_errors": {},
        }

    @staticmethod
    def _source_faces(_base, _deck, _face_ids, _scope):
        return [(object(), {"deck": 1, "type": "FACE", "id": 7})]

    @staticmethod
    def _face_rows(_base, _deck, query, _point, pairs):
        entity = pairs[0][0]
        if query == "largest_area":
            return [{"area": 4.5, "_id": 7, "_entity": entity}]
        if query == "highest_z":
            return [{"area": 4.5, "_id": 7, "_entity": entity, "center": [0, 0, 3]}]
        raise AssertionError(f"unexpected geometry query: {query}")

    def _params(self, max_cases=10):
        return {
            "root_path": str(self.input_dir),
            "backup_path": str(self.root / "backup.ansa"),
            "max_cases": max_cases,
        }

    def test_discovery_uses_supported_files_and_legacy_size_path_order(self):
        nested = self.input_dir / "nested"
        nested.mkdir()
        (self.input_dir / "b.stp").write_bytes(b"123")
        (self.input_dir / "ignored.iges").write_bytes(b"x")
        (nested / "z.STEP").write_bytes(b"1")
        selected = benchmark._discover_files(self.input_dir, 2)
        self.assertEqual(["z.STEP", "b.stp"], [item["path"].name for item in selected])
        self.assertEqual(3, selected[1]["fingerprint"]["size_bytes"])

    def test_discovery_rejects_empty_inputs_and_scan_overflow(self):
        empty = self.input_dir / "empty.ansa"
        empty.touch()
        with self.assertRaisesRegex(ValueError, "empty"):
            benchmark._discover_files(self.input_dir, 1)
        empty.unlink()
        (self.input_dir / "second.ansa").write_bytes(b"12")
        with patch.object(benchmark, "_MAX_SCAN_ITEMS", 1):
            with self.assertRaisesRegex(ValueError, "scan limit"):
                benchmark._discover_files(self.input_dir, 1)

        class CountedEntries:
            def __init__(self):
                self.yielded = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                for index in range(4):
                    self.yielded += 1
                    yield types.SimpleNamespace(name=f"entry-{index}")

        entries = CountedEntries()
        with patch.object(benchmark.os, "scandir", return_value=entries):
            with patch.object(benchmark, "_MAX_SCAN_ITEMS", 1):
                with self.assertRaisesRegex(ValueError, "scan limit"):
                    benchmark._discover_files(self.input_dir, 1)
        self.assertEqual(2, entries.yielded)

        long_path = Path("x" * (benchmark._MAX_PATH_LENGTH + 1))
        selected = [{"path": long_path, "fingerprint": {"size_bytes": 1, "sha256": "a" * 64}}]
        with patch.object(benchmark, "_discover_files", return_value=selected):
            with self.assertRaisesRegex(ValueError, "4096 characters; no backup"):
                benchmark.execute(self.state, "benchmark_run", self._params())
        self.assertFalse((self.root / "backup.ansa").exists())
        self.assertEqual([], self.base.opens)

    def test_params_require_backup_strict_limit_no_extra_and_bounded_size(self):
        with self.assertRaisesRegex(ValueError, "missing parameter"):
            benchmark._validate_params({"root_path": str(self.input_dir)})
        with self.assertRaisesRegex(ValueError, "max_cases"):
            benchmark._validate_params({**self._params(True)})
        with self.assertRaisesRegex(ValueError, "unsupported parameter"):
            benchmark._validate_params({**self._params(), "overwrite": True})
        with patch.object(benchmark, "_REQUEST_BUDGET", 1):
            with self.assertRaisesRegex(ValueError, "128 KiB"):
                benchmark._validate_params(self._params())
        self.assertLessEqual(benchmark._REQUEST_BUDGET, MAX_FRAME_BYTES)
        self.assertLessEqual(benchmark._REPORT_BUDGET, MAX_FRAME_BYTES)
        adapter = TypeAdapter(benchmark_mcp.MaxCases)
        self.assertEqual(1, adapter.validate_python(1))
        self.assertEqual(10, adapter.validate_python(10))
        for invalid in (True, 0, 11, "1"):
            with self.subTest(max_cases=invalid):
                with self.assertRaises(ValidationError):
                    adapter.validate_python(invalid)

    def test_success_saves_hashes_inspects_and_opens_backup_at_end(self):
        result = benchmark.execute(self.state, "benchmark_run", self._params())
        backup = self.root / "backup.ansa"
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(benchmark._json_bytes(result)), benchmark._REPORT_BUDGET)
        self.assertTrue(result["backup"]["verified"])
        self.assertEqual(_COUNTS, result["backup"]["readback_counts"])
        self.assertEqual([str(backup), str(self.source), str(backup)], self.base.opens)
        self.assertEqual(3, self.state.invalidations)
        self.assertEqual(str(backup), self.base.DataBaseName())
        row = result["cases"][0]
        self.assertTrue(row["import_ok"])
        self.assertEqual("inspected", row["import_status"])
        self.assertEqual(2, row["faces"])
        self.assertEqual(7, row["largest_face_id"])
        self.assertEqual(8, row["top_face_max_z"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), row["sha256"])
        self.assertTrue(result["restore"]["confirmed"])
        artifacts = {item["kind"]: item for item in result["artifacts"]}
        for artifact in artifacts.values():
            path = Path(artifact["path"])
            self.assertEqual(path.stat().st_size, artifact["size_bytes"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"])
        report = json.loads(Path(artifacts["json"]["path"]).read_text(encoding="utf-8"))
        with Path(artifacts["csv"]["path"]).open(encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertTrue(report["restore"]["confirmed"])
        self.assertEqual("inspected", csv_rows[0]["import_status"])
        self.assertEqual(str(backup), result["current_model_path"])

        pairs = [
            (object(), {"deck": 1, "type": "FACE", "id": 7}),
            (object(), {"deck": 1, "type": "FACE", "id": 9}),
        ]

        def ordered_rows(_base, _deck, query, _point, source_pairs):
            if query == "largest_area":
                return [
                    {"area": 4.5, "_id": 7, "_entity": source_pairs[0][0]},
                    {"area": 4.5, "_id": 9, "_entity": source_pairs[1][0]},
                ]
            return [{"area": 1, "_id": 9, "_entity": source_pairs[1][0]}]

        with patch.object(benchmark.geometry, "_source_faces", return_value=pairs):
            with patch.object(benchmark.geometry, "_face_rows", side_effect=ordered_rows):
                tied = benchmark._rank_faces(self.base, 1)
        self.assertEqual(7, tied["largest_face_id"])

    def test_open_failure_is_partial_and_stops_without_retry_or_restore(self):
        self.base.fail_inputs = True
        result = benchmark.execute(self.state, "benchmark_run", self._params())
        self.assertFalse(result["ok"])
        backup_path = str(self.root / "backup.ansa")
        self.assertEqual([backup_path, str(self.source)], self.base.opens)
        self.assertFalse(result["cases"][0]["import_ok"])
        self.assertEqual("failed", result["cases"][0]["import_status"])
        self.assertIn("returned 1", result["cases"][0]["error"])
        self.assertLessEqual(len(result["cases"][0]["error"]), benchmark._MAX_ERROR_LENGTH)
        self.assertFalse(result["restore"]["confirmed"])
        self.assertFalse(result["restore"]["attempted"])
        self.assertTrue(result["restore"]["possible_effects"])
        self.assertTrue(result["possible_effects"])
        self.assertEqual(backup_path, self.base.DataBaseName())
        self.assertTrue(Path(backup_path).is_file())
        self.assertEqual({"json", "csv"}, {item["kind"] for item in result["artifacts"]})
        report_path = next(item["path"] for item in result["artifacts"] if item["kind"] == "json")
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        self.assertFalse(report["restore"]["confirmed"])

    def test_foreign_model_context_prevents_backup_restore_open(self):
        backup_path = self.root / "backup.ansa"
        foreign_path = self.root / "foreign-unsaved.ansa"
        foreign_path.write_bytes(b"foreign unsaved model")
        original_rank = benchmark._rank_faces
        opens_at_detection = []

        def rank_then_switch(base, deck):
            result = original_rank(base, deck)
            opens_at_detection.append(len(self.base.opens))
            self.base.database = foreign_path
            self.state.model_epoch += 1
            return result

        with patch.object(benchmark, "_rank_faces", side_effect=rank_then_switch):
            result = benchmark.execute(self.state, "benchmark_run", self._params())

        self.assertEqual([str(backup_path), str(self.source)], self.base.opens)
        self.assertEqual([len(self.base.opens)], opens_at_detection)
        self.assertTrue(backup_path.is_file())
        self.assertEqual(result["backup"]["size_bytes"], backup_path.stat().st_size)
        self.assertEqual(
            result["backup"]["sha256"], hashlib.sha256(backup_path.read_bytes()).hexdigest()
        )
        self.assertEqual(str(foreign_path), result["current_model_path"])
        self.assertFalse(result["restore"]["attempted"])
        self.assertFalse(result["restore"]["confirmed"])
        self.assertTrue(result["restore"]["possible_effects"])
        self.assertTrue(result["possible_effects"])
        self.assertIn("context changed", result["restore"]["error"])

    def test_backup_readback_mismatch_stops_before_opening_any_source(self):
        original = self._summary

        def mismatched_backup(base, constants, deck):
            result = original(base, constants, deck)
            if Path(base.DataBaseName()).name == "backup.ansa":
                result["entity_counts"]["faces"] += 1
            return result

        with patch.object(benchmark.entity_catalog, "_model_summary", mismatched_backup):
            result = benchmark.execute(self.state, "benchmark_run", self._params())

        backup_path = str(self.root / "backup.ansa")
        self.assertEqual([backup_path, backup_path], self.base.opens)
        self.assertFalse(result["backup"]["verified"])
        self.assertFalse(result["restore"]["confirmed"])
        self.assertEqual(0, result["attempted_case_count"])
        self.assertEqual(backup_path, result["current_model_path"])

        second = self.input_dir / "second.step"
        second.write_bytes(b"a later STEP source")
        batch = {"identity": self.state.identity(), "grant": dict(self.state.authorization)}
        context, _ = benchmark._capture_context(self.state, 1, batch)
        files = benchmark._discover_files(self.input_dir, 2)
        rows = benchmark._initial_rows(files)
        original_probe = self.state.probe
        probe_count = 0

        def one_unknown_probe():
            nonlocal probe_count
            probe_count += 1
            value = original_probe()
            if probe_count == 3:
                value["model"] = dict(value["model"])
                value["model"]["database"] = str(self.root / "unknown.ansa")
            return value

        self.base.opens.clear()
        with patch.object(self.state, "probe", side_effect=one_unknown_probe):
            attempted, _ = benchmark._run_cases(
                self.state, self.base, self.ansa.constants, 1, batch, files, rows, context
            )
        self.assertEqual(1, attempted)
        self.assertEqual([str(self.source)], self.base.opens)
        self.assertEqual("partial", rows[0]["import_status"])
        self.assertEqual("not_started", rows[1]["import_status"])

    def test_source_mutated_after_backup_is_not_opened(self):
        self.base.on_save = lambda: self.source.write_bytes(b"changed source")
        result = benchmark.execute(self.state, "benchmark_run", self._params())
        backup_path = str(self.root / "backup.ansa")
        self.assertEqual([backup_path, backup_path], self.base.opens)
        self.assertFalse(result["source_hash_unchanged"])
        self.assertFalse(result["cases"][0]["import_ok"])
        self.assertTrue(result["restore"]["confirmed"])

    def test_changed_grant_stops_and_prevents_unauthorized_restore(self):
        def revoke(_path):
            self.state.authorization = {
                "active": False, "backend_id": "backend-a", "generation": 3,
            }
            self.state._write_authorization()

        self.base.on_open = revoke
        result = benchmark.execute(self.state, "benchmark_run", self._params())
        backup_path = str(self.root / "backup.ansa")
        self.assertEqual([backup_path], self.base.opens)
        self.assertFalse(result["restore"]["confirmed"])
        self.assertFalse(result["restore"]["attempted"])
        self.assertIn("grant changed", result["restore"]["error"])
        self.assertEqual(backup_path, result["current_model_path"])

    def test_report_creation_is_exclusive_and_does_not_claim_existing_file(self):
        collision = self.state.session_dir / "benchmark-fixed.json"
        collision.write_text("preserve", encoding="utf-8")
        with patch.object(benchmark.uuid, "uuid4", return_value=types.SimpleNamespace(hex="fixed")):
            with self.assertRaises(benchmark._ReportWriteError) as raised:
                benchmark._write_reports(self.state.session_dir, {"ok": False}, [])
        self.assertEqual("preserve", collision.read_text(encoding="utf-8"))
        self.assertIsInstance(raised.exception.__cause__, FileExistsError)
        self.assertEqual([], raised.exception.artifacts)

        original_open = Path.open

        def fail_csv(path, *args, **kwargs):
            if path.suffix == ".csv":
                raise OSError("simulated CSV write failure")
            return original_open(path, *args, **kwargs)

        before_json = set(self.state.session_dir.glob("*.json"))
        with patch.object(Path, "open", fail_csv):
            failed = benchmark.execute(self.state, "benchmark_run", self._params())
        self.assertFalse(failed["ok"])
        self.assertIn("report write:", failed["error"])
        self.assertEqual([], failed["artifacts"])
        self.assertEqual(before_json, set(self.state.session_dir.glob("*.json")))
        for report_path in self.state.session_dir.glob("*.json"):
            try:
                written_report = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            self.assertIsNot(written_report.get("ok"), True)

        class PartialJsonWrite:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, value):
                self.stream.write(value[:5])
                self.stream.flush()
                raise OSError("simulated partial JSON write")

        def fail_json_after_partial(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path.suffix == ".json" and args and args[0] == "x":
                return PartialJsonWrite(stream)
            return stream

        with patch.object(benchmark.uuid, "uuid4", return_value=types.SimpleNamespace(hex="partial-json")):
            with patch.object(Path, "open", fail_json_after_partial):
                with self.assertRaises(benchmark._ReportWriteError) as partial:
                    benchmark._write_reports(self.state.session_dir, {"ok": False}, [])
        self.assertEqual({"csv", "json"}, {item["kind"] for item in partial.exception.artifacts})
        artifacts = {item["kind"]: item for item in partial.exception.artifacts}
        self.assertTrue(artifacts["csv"]["complete"])
        self.assertFalse(artifacts["json"]["complete"])
        self.assertFalse(artifacts["json"]["valid_json"])
        artifact = artifacts["json"]
        with self.assertRaises(json.JSONDecodeError):
            json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
        self.assertEqual(Path(artifact["path"]).stat().st_size, artifact["size_bytes"])
        self.assertEqual(
            hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest(), artifact["sha256"]
        )

    def test_oversized_result_returns_bounded_evidence_summary(self):
        oversized = "x" * (benchmark._REPORT_BUDGET + 1024)
        with patch.object(benchmark, "_rank_faces", return_value={"oversized": oversized}):
            result = benchmark.execute(self.state, "benchmark_run", self._params())

        self.assertFalse(result["ok"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(benchmark._json_bytes(result)), benchmark._REPORT_BUDGET)
        self.assertTrue(result["backup"]["verified"])
        self.assertGreater(result["backup"]["size_bytes"], 0)
        self.assertEqual(str(self.root / "backup.ansa"), result["current_model_path"])
        self.assertTrue(result["restore"]["confirmed"])
        self.assertIn("report write:", result["error"])
        self.assertLessEqual(len(result["error"]), benchmark._MAX_ERROR_LENGTH)
        for record in [*result["cases"], result["backup"], result["restore"]]:
            for key in ("path", "input_path", "current_model_path"):
                if type(record.get(key)) is str:
                    self.assertLessEqual(len(record[key]), benchmark._MAX_PATH_LENGTH)
            if type(record.get("error")) is str:
                self.assertLessEqual(len(record["error"]), benchmark._MAX_ERROR_LENGTH)
        self.assertIsNone(result["report_location"])


if __name__ == "__main__":
    unittest.main()
