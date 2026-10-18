"""User script failures must finish normally instead of stranding Journal claims."""
import gc
import types
import unittest
import warnings
from unittest.mock import patch

from ansa_mcp_bridge.operations.scripts import execute_script


def _fake_ansa():
    module = types.ModuleType("ansa")
    module.base = types.SimpleNamespace()
    module.constants = types.SimpleNamespace()
    return module


class ScriptResultChecks(unittest.TestCase):
    def test_entrypoint_return_result_none_and_main_not_implicit(self):
        cases = (
            ("def main():\n    return {'value': 7}", "main", {"value": 7}),
            ("def main():\n    return {'ignored': True}\nRESULT = {'result': 9}", "main", {"result": 9}),
            ("def main():\n    return None", "main", None),
            ("def main():\n    base.entrypoint_called = True\nRESULT = 5", None, 5),
        )
        for script, name, expected in cases:
            with self.subTest(name=name, expected=expected):
                module = _fake_ansa()
                module.base.entrypoint_called = False
                with patch.dict("sys.modules", {"ansa": module}):
                    result = execute_script({"script": script, "function_name": name})
                self.assertEqual(result["result"], expected)
                self.assertFalse(module.base.entrypoint_called)

    def test_invalid_params_are_rejected_before_script_side_effects(self):
        script = "base.started = True"
        cases = (
            ("params must be a dict", [script], TypeError),
            ("unknown parameter", {"script": script, "unexpected": True}, ValueError),
            ("validate must be bool", {"script": script, "validate": 1}, ValueError),
            ("allow_model_change must be bool", {"script": script, "allow_model_change": 0}, ValueError),
            ("empty name", {"script": script, "function_name": ""}, ValueError),
            ("dotted name", {"script": script, "function_name": "main.run"}, ValueError),
            ("keyword name", {"script": script, "function_name": "for"}, ValueError),
            ("non-string name", {"script": script, "function_name": 4}, ValueError),
            ("overlong name", {"script": script, "function_name": "x" * 129}, ValueError),
            ("name is not trimmed", {"script": script, "function_name": " main"}, ValueError),
        )
        for label, params, error in cases:
            with self.subTest(label=label):
                module = _fake_ansa()
                with patch.dict("sys.modules", {"ansa": module}):
                    with self.assertRaises(error):
                        execute_script(params)
                self.assertFalse(hasattr(module.base, "started"))

    def test_entrypoint_failures_preserve_top_level_effects_as_ordinary_failures(self):
        cases = (
            ("missing", "base.started = True", "missing"),
            ("not callable", "base.started = True\nmain = 1", "main"),
            ("requires arguments", "base.started = True\ndef main(value):\n    return value", "main"),
            ("raises", "base.started = True\ndef main():\n    raise RuntimeError('boom')", "main"),
            ("script exit", "base.started = True\ndef main():\n    raise SystemExit(3)", "main"),
            ("script interrupt", "base.started = True\ndef main():\n    raise KeyboardInterrupt()", "main"),
        )
        for label, script, name in cases:
            with self.subTest(label=label):
                module = _fake_ansa()
                with patch.dict("sys.modules", {"ansa": module}):
                    with self.assertRaisesRegex(RuntimeError, "Script failed; effects may exist"):
                        execute_script({"script": script, "function_name": name})
                self.assertTrue(module.base.started)

    def test_async_and_generator_entrypoints_are_rejected_without_unawaited_warnings(self):
        cases = (
            ("async function", "async def main():\n    base.called = True"),
            ("generator function", "def main():\n    base.called = True\n    yield 1"),
            ("async generator function", "async def main():\n    base.called = True\n    yield 1"),
            (
                "indirect coroutine",
                "async def later():\n    base.called = True\ndef main():\n    return later()",
            ),
            (
                "indirect generator",
                "def later():\n    base.called = True\n    yield 1\ndef main():\n    return later()",
            ),
            (
                "indirect async generator",
                "async def later():\n    base.called = True\n    yield 1\ndef main():\n    return later()",
            ),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            for label, definition in cases:
                with self.subTest(label=label):
                    module = _fake_ansa()
                    script = definition + "\nRESULT = {'top_level': True}\nbase.top_level_ran = True"
                    with patch.dict("sys.modules", {"ansa": module}):
                        with self.assertRaisesRegex(RuntimeError, "Script failed; effects may exist"):
                            execute_script({"script": script, "function_name": "main"})
                    self.assertTrue(module.base.top_level_ran)
                    self.assertFalse(getattr(module.base, "called", False))
                    del module
                    gc.collect()
        self.assertFalse(
            [warning for warning in caught if "was never awaited" in str(warning.message)]
        )

    def test_function_return_values_must_be_strict_json(self):
        for returned in ("(1, 2)", "{1: 'integer key'}", "float('nan')"):
            script = f"def main():\n    return {returned}"
            with self.subTest(returned=returned):
                module = _fake_ansa()
                with patch.dict("sys.modules", {"ansa": module}):
                    with self.assertRaisesRegex(RuntimeError, "Script failed; effects may exist"):
                        execute_script({"script": script, "function_name": "main"})

    def test_non_strict_json_result_is_an_ordinary_script_failure(self):
        module = _fake_ansa()
        with patch.dict("sys.modules", {"ansa": module}):
            for script in ("RESULT=(1,2)", "RESULT={1:'integer key'}", "RESULT=float('nan')"):
                with self.subTest(script=script), self.assertRaisesRegex(RuntimeError, "Script failed; effects may exist"):
                    execute_script({"script": script})


if __name__ == "__main__":
    unittest.main()
