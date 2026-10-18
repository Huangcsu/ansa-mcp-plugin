"""Runtime API catalog stays read-only and reports incomplete inventories honestly."""

import sys
import types
import unittest
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from ansa_mcp_bridge.operations import api_catalog, api_documentation, scripts


class ApiCatalogChecks(unittest.TestCase):
    def fake_ansa(self, modules=None, version="2026.1.0"):
        ansa = types.ModuleType("ansa")
        ansa.__path__ = []
        constants = types.ModuleType("ansa.constants")
        constants.app_version = version
        ansa.constants = constants
        entries = {"ansa": ansa, "ansa.constants": constants}
        for name, values in (modules or {}).items():
            runtime_module = types.ModuleType("ansa." + name)
            for key, value in values.items():
                setattr(runtime_module, key, value)
            entries["ansa." + name] = runtime_module
        return patch.dict(sys.modules, entries)

    def test_module_filter_and_inventory_metadata(self):
        with self.fake_ansa({"base": {"get_item": lambda: None}, "mesh": {"DeleteMesh": lambda: None}}):
            result = scripts.api_reference({"action": "modules", "module": "mesh", "query": "", "limit": 1})

        self.assertEqual(result["version"], "2026.1.0")
        self.assertEqual(result["modules"], list(scripts.MODULES))
        self.assertIn("whitelist", result["source"])
        self.assertEqual(result["module_details"], [
            {"module": "mesh", "available": True, "function_count": 1}
        ])
        self.assertEqual(result["function_count"], 1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["scope"], "top_level_public_routines")

        class NativeConstants:
            __slots__ = ()
            app_version = "2026.1.0"
            NASTRAN = 1

        with self.fake_ansa():
            # Reproduce the official runtime: from-import works, submodule import does not.
            sys.modules["ansa"].constants = NativeConstants()
            sys.modules.pop("ansa.constants")
            native = scripts.api_reference({"action": "modules", "module": "constants"})
            self.assertIs(scripts.resolve_api("ansa.constants.NASTRAN"), NativeConstants.NASTRAN)
        self.assertTrue(native["complete"])
        self.assertEqual(native["module_details"], [
            {"module": "constants", "available": True, "function_count": 0}
        ])

    def test_classification_priority_sorting_and_count_conservation(self):
        names = {
            "base": {
                "get_and_delete": lambda: None,
                "set_value": lambda: None,
                "create_quality": lambda: None,
                "quality_transform": lambda: None,
                "rotate_part": lambda: None,
                "unclassified": lambda: None,
            },
            "mesh": {
                "clear_quality": lambda: None,
                "aspect_check": lambda: None,
                "boundary_inflation": lambda: None,
                "quality_inflation": lambda: None,
                "mesh_other": lambda: None,
            },
            "session": {"start_session": lambda: None},
            "kinetics": {"CalculateMassProperties": lambda: None},
        }
        names.update({name: {} for name in scripts.MODULES if name not in names})
        with self.fake_ansa(names):
            result = scripts.api_reference({"action": "categories"})

        categories = result["categories"]
        self.assertEqual(categories, sorted(categories, key=lambda row: (row["category"], row["module"])))
        self.assertEqual([item["module"] for item in result["module_details"]], sorted(scripts.MODULES))
        counts = {(row["module"], row["category"]): row["function_count"] for row in categories}
        self.assertEqual(counts[("base", "base_query")], 1)
        self.assertEqual(counts[("base", "base_modify")], 2)
        self.assertEqual(counts[("base", "base_check")], 1)
        self.assertEqual(counts[("base", "base_transform")], 1)
        self.assertEqual(counts[("base", "base_other")], 1)
        self.assertEqual(counts[("mesh", "mesh_edit")], 1)
        self.assertEqual(counts[("mesh", "mesh_quality")], 2)
        self.assertEqual(counts[("mesh", "mesh_cfd")], 1)
        self.assertEqual(counts[("mesh", "mesh_other")], 1)
        self.assertEqual(counts[("session", "session")], 1)
        self.assertEqual(counts[("kinetics", "kinetics")], 1)
        self.assertEqual(sum(row["function_count"] for row in categories), result["function_count"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["classification_source"], "plugin-defined function-name rules; not an official API taxonomy")
        self.assertIn("other_modules", result["category_rules"])

    def test_only_public_top_level_routines_are_counted_without_execution(self):
        calls = []

        def routine():
            calls.append("routine")

        class DangerousDescriptor:
            def __init__(self):
                calls.append("descriptor constructor")

            def __get__(self, instance, owner):
                calls.append("descriptor")
                raise AssertionError("descriptor must not run")

            def __set__(self, instance, value):
                calls.append("descriptor")
                raise AssertionError("descriptor must not run")

        class PublicClass:
            member = DangerousDescriptor()

            def __init__(self):
                calls.append("constructor")

            def method(self):
                calls.append("method")

        class CallableObject:
            def __init__(self):
                calls.append("callable constructor")

            def __call__(self):
                calls.append("callable")

        values = {
            "public_routine": routine,
            "_private_routine": lambda: None,
            "PublicClass": PublicClass,
            "callable_object": CallableObject(),
            "data": 123,
            "descriptor": DangerousDescriptor(),
        }
        calls.clear()
        with self.fake_ansa({"base": values}):
            result = scripts.api_reference({"action": "modules", "module": "base"})

        self.assertEqual(result["module_details"], [
            {"module": "base", "available": True, "function_count": 1}
        ])
        self.assertEqual(calls, [])

    def test_unavailable_modules_are_explicit_and_make_catalog_incomplete(self):
        with self.fake_ansa({"base": {"get_item": lambda: None}}):
            result = scripts.api_reference({"action": "modules"})

        self.assertEqual(result["module_details"][0], {
            "module": "base", "available": True, "function_count": 1
        })
        unavailable = next(row for row in result["module_details"] if row["module"] == "mesh")
        self.assertEqual(unavailable["module"], "mesh")
        self.assertIs(unavailable["available"], False)
        self.assertIsNone(unavailable["function_count"])
        self.assertEqual(unavailable["error_type"], "ModuleNotFoundError")
        self.assertNotIn("error", unavailable)
        self.assertFalse(result["complete"])
        self.assertEqual(result["function_count"], 1)

    def test_catalog_inputs_and_runtime_version_are_strict(self):
        with self.fake_ansa({name: {} for name in scripts.MODULES}):
            for params in (
                {"action": "modules", "query": "base"},
                {"action": "categories", "query": " ", "limit": 1},
                {"action": "modules", "module": 1},
                {"action": "modules", "unknown": True},
                {"action": "modules", "limit": True},
                {"action": "modules", "limit": 21},
            ):
                with self.subTest(params=params), self.assertRaises((TypeError, ValueError)):
                    scripts.api_reference(params)

        invalid_version_calls = (
            {"action": "modules", "module": "base"},
            {"action": "categories", "module": "base"},
            {"action": "lookup", "module": "base", "query": "Missing"},
            {"action": "search", "module": "base", "query": "Missing"},
        )
        for version in (None, "", 2026):
            for params in invalid_version_calls:
                with self.subTest(version=version, params=params), self.fake_ansa({"base": {}}, version=version):
                    with self.assertRaisesRegex(ValueError, "app_version"):
                        scripts.api_reference(params)

    def test_lookup_search_resolve_and_main_thread_guard_remain_compatible(self):
        def documented_api():
            """A unique documented API for compatibility checks."""

        modules = {name: {} for name in scripts.MODULES}
        modules["base"] = {"DocumentedApi": documented_api}
        with self.fake_ansa(modules), patch.object(api_documentation, "find_docs_root", return_value=None):
            self.assertIs(scripts.resolve_api("ansa.base.DocumentedApi"), documented_api)
            lookup = scripts.api_reference({"action": "lookup", "module": "base", "query": "DocumentedApi"})
            search = scripts.api_reference({"action": "search", "module": "base", "query": "unique documented"})

        self.assertEqual(lookup["version"], "2026.1.0")
        self.assertEqual(lookup["api"], "ansa.base.DocumentedApi")
        self.assertEqual(lookup["documentation"], "A unique documented API for compatibility checks.")
        self.assertEqual(lookup["source_type"], "runtime_docstring")
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["matches"][0]["api"], "ansa.base.DocumentedApi")

        class WrongThread:
            def assert_main(self):
                raise RuntimeError("main-thread assertion")

        with self.assertRaisesRegex(RuntimeError, "main-thread assertion"):
            scripts.execute(WrongThread(), "api_lookup", {"action": "modules"})

    def test_native_preparation_failure_still_cleans_verified_owned_process(self):
        integration = Path(__file__).resolve().parents[1] / "integration"
        spec = importlib.util.spec_from_file_location(
            "api_catalog_acceptance_test", integration / "stage_three_api_catalog_ansa.py")
        acceptance = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(integration), *sys.path]):
            spec.loader.exec_module(acceptance)
        grpc = types.ModuleType("grpc")
        original_channel = grpc.insecure_channel = lambda *args, **kwargs: None
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            run_dir = Path(temporary)
            translator = str(run_dir / "unique-translator.py")
            instructions = {"runtime_home": str(run_dir / "private-home"), "port": 50051,
                            "translator": translator}
            arguments = ["ansa.exe", "-grpcport", "50051", "-grpchost", "127.0.0.1", "-translfp", translator]
            basic = {"pid": 1234, "main_thread": True, "program_args": arguments}
            (run_dir / "normal-user-process-before.json").write_text(json.dumps([
                {"ProcessId": 1234, "CreationDate": "verified-start", "CommandLine": " ".join(arguments)}
            ]), encoding="utf-8")
            one = SimpleNamespace(DeadlineInterceptor=object, remote_basic=object())
            two = SimpleNamespace(rpc=lambda *args: basic)
            with patch.object(acceptance.common, "_load_run", return_value=(run_dir, instructions)), \
                    patch.dict(sys.modules, {"grpc": grpc}), \
                    patch.object(acceptance.common, "_import_runtime_helpers", return_value=(one, two)), \
                    patch.object(acceptance.common, "_bounded_grpc", return_value=original_channel), \
                    patch("ansa_mcp_shared.identity.process_start_filetime", return_value="verified-start"), \
                    patch.object(acceptance.common, "_prepare_owned_ansa", side_effect=RuntimeError("preparation failed after bridge start")), \
                    patch.object(acceptance.common, "_cleanup_owned_ansa", return_value={"stopped_owned_process": True}) as cleanup, \
                    contextlib.redirect_stdout(io.StringIO()):
                exit_code = acceptance.main(["--ansa-root", str(run_dir), "--runtime-home", instructions["runtime_home"],
                                             "--run-dir", str(run_dir)])
            self.assertEqual(exit_code, 1)
            identity, _, helpers = cleanup.call_args.args
            self.assertEqual(identity["pid"], 1234)
            self.assertEqual(identity["process_start"], "verified-start")
            self.assertEqual(helpers, (one, two, original_channel))
            report = json.loads((run_dir / "api-catalog-report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["success"])
            self.assertEqual(report["identity"], identity)
            self.assertTrue(report["cleanup"]["stopped_owned_process"])
            self.assertIn("preparation failed", report["error"])
        self.assertIs(grpc.insecure_channel, original_channel)


if __name__ == "__main__":
    unittest.main()
