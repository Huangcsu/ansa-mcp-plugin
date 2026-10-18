"""Version-matched ANSA help parsing and preflight use no ANSA API calls."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ansa_mcp_bridge.operations import api_documentation, scripts


_HTML = """<!doctype html>
<dl class="py function">
<dt class="sig sig-object py" id="ansa.kinetics.CalculateMassProperties">
<span class="sig-prename descclassname"><span class="pre">ansa.kinetics.</span></span>
<span class="sig-name descname"><span class="pre">CalculateMassProperties</span></span>
<span class="sig-paren">(</span><em class="sig-param"><span class="pre">entity</span></em><span class="sig-paren">)</span>
<span class="sig-return"><span class="pre">int</span></span>
</dt>
<dd><div class="line">Calculates mass and reports a mass property.</div>
<dl class="field-list">
<dt class="field-odd">Parameters<span class="colon">:</span></dt>
<dd class="field-odd"><dl><dt><strong>entity</strong><span class="classifier">base.Entity</span></dt>
<dd><div class="line">The KIN_RBODY entity to inspect.</div></dd></dl></dd>
<dt class="field-even">Returns<span class="colon">:</span></dt>
<dd class="field-even"><dl><dt>int</dt><dd><div class="line">Returns 1 on success and 0 on invalid input.</div></dd></dl></dd>
</dl>
<p class="rubric">Examples</p>
<div class="highlight-default notranslate"><div class="highlight"><pre>
from ansa import kinetics
kinetics.CalculateMassProperties(body)
</pre></div></div>
</dd></dl>
"""


class ApiDocumentationChecks(unittest.TestCase):
    def make_docs(self, root, html=_HTML):
        docs = Path(root)
        generated = docs / "reference" / "api_ref_ansa" / "generated"
        generated.mkdir(parents=True)
        (generated / "ansa.kinetics.html").write_text(html, encoding="utf-8")
        (docs / "objects.inv").write_text(
            "# Sphinx inventory version 2\n# Project: BETA Suite API\n# Version: 2026.1.0\n# compressed\n",
            encoding="utf-8",
        )
        return docs

    def fake_ansa_constants(self):
        ansa = types.ModuleType("ansa")
        ansa.__path__ = []
        constants = types.ModuleType("ansa.constants")
        constants.app_version = "2026.1.0"
        ansa.constants = constants
        return {"ansa": ansa, "ansa.constants": constants}

    def test_official_html_lookup_search_and_preflight(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            docs = self.make_docs(temporary)
            with patch.object(api_documentation, "find_docs_root", return_value=docs), \
                    patch.dict(sys.modules, self.fake_ansa_constants()), \
                    patch.object(scripts, "resolve_api", return_value=lambda entity: None) as resolver:
                preflight = scripts.validate(
                    "from ansa import kinetics\nkinetics.CalculateMassProperties(None)"
                )
                lookup = scripts.api_reference({
                    "action": "lookup", "module": "kinetics", "query": "CalculateMassProperties"
                })
                search = scripts.api_reference({
                    "action": "search", "module": "kinetics", "query": "mass properties", "limit": 1
                })

        self.assertTrue(preflight["valid"])
        resolver.assert_called_once_with("ansa.kinetics.CalculateMassProperties")
        self.assertEqual(preflight["missing"], [])
        self.assertEqual(preflight["checked_apis"], ["ansa.kinetics.CalculateMassProperties"])
        self.assertIn("CalculateMassProperties(entity)", lookup["signature"])
        self.assertIn("KIN_RBODY entity", lookup["parameters"])
        self.assertIn("Returns 1 on success", lookup["returns"])
        self.assertIn("CalculateMassProperties(body)", lookup["examples"][0])
        self.assertEqual(lookup["source_type"], "official_html")
        self.assertTrue(lookup["complete"])
        self.assertFalse(lookup["source_truncated"])
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["matches"][0]["api"], "ansa.kinetics.CalculateMassProperties")
        self.assertEqual(search["source_type"], "official_help")

    def test_find_docs_root_requires_exact_versioned_installation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            base = Path(temporary)
            docs = base / "Apps" / "BETA_CAE_Systems" / "ansa_2026.1.0" / "docs" / "extending" / "python_api" / "html"
            self.make_docs(docs)
            with patch.dict("os.environ", {"LOCALAPPDATA": str(base)}):
                self.assertEqual(api_documentation.find_docs_root("2026.1.0"), docs.resolve())
                self.assertIsNone(api_documentation.find_docs_root("../2026.1.0"))
                self.assertIsNone(api_documentation.find_docs_root("2025.1.0"))

    def test_find_docs_root_uses_shared_executable_sibling_installation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            systems = Path(temporary) / "BETA_CAE_Systems"
            docs = systems / "ansa_2026.1.0" / "docs" / "extending" / "python_api" / "html"
            self.make_docs(docs)
            executable = systems / "shared_2026.1.0" / "win64" / "ansa_win64.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch.object(api_documentation.sys, "executable", str(executable)), \
                    patch.dict("os.environ", {"LOCALAPPDATA": str(Path(temporary) / "empty")}):
                self.assertEqual(api_documentation.find_docs_root("2026.1.0"), docs.resolve())

    def test_lookup_reports_documentation_truncation(self):
        long_html = _HTML.replace(
            "Calculates mass and reports a mass property.", "A" * 17000
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            docs = self.make_docs(temporary, long_html)
            with patch.object(api_documentation, "find_docs_root", return_value=docs):
                result = api_documentation.lookup(
                    "ansa.kinetics.CalculateMassProperties", "2026.1.0", scripts.MODULES
                )
        self.assertTrue(result["source_truncated"])
        self.assertFalse(result["complete"])

    def test_preflight_requires_exact_runtime_api_even_when_documented(self):
        def resolve(path):
            if path != "ansa.base.CalcElementMass":
                raise AttributeError(path)
            return lambda entities: None
        with patch.object(scripts, "resolve_api", side_effect=resolve), \
                patch.object(api_documentation, "api_exists", return_value=True):
            wrong_case = scripts.validate("from ansa import base\nbase.calcelementmass([])")
            missing = scripts.validate("from ansa import base\nbase.DocumentedButMissing([])")
            correct = scripts.validate("from ansa import base\nbase.CalcElementMass([])")
        self.assertFalse(wrong_case["valid"])
        self.assertFalse(missing["valid"])
        self.assertTrue(correct["valid"])

    def test_stub_fallback_slices_once_and_caches_module(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            docs = self.make_docs(temporary)
            stub = docs / api_documentation._STUBS / "base" / "__init__.py"
            stub.parent.mkdir(parents=True)
            stub.write_text("\n".join(
                f'def Test{i}(value: list[str], count: int=2) -> str:\n    """Entry {i}."""\n    pass\n'
                for i in range(1000)), encoding="utf-8")
            with patch.object(api_documentation.ast, "get_source_segment", side_effect=AssertionError("full-source rescan")), \
                    patch.object(api_documentation.ast, "parse", wraps=api_documentation.ast.parse) as parse:
                entries, complete = api_documentation._stub_entries(docs, "base")
                again, _ = api_documentation._stub_entries(docs, "base")
            self.assertTrue(complete)
            self.assertIs(entries, again)
            self.assertEqual(parse.call_count, 1)
            self.assertIn("count: int=2", entries["ansa.base.Test999"]["signature"])

    def test_stub_lookup_is_bounded_and_reports_actual_source(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            docs = self.make_docs(temporary)
            stub = docs / api_documentation._STUBS / "base" / "__init__.py"
            stub.parent.mkdir(parents=True)
            doc = "Examples\n--------\nprint(1)\n" + "A" * 17000
            stub.write_text("def Long(value: int) -> int:\n    " + repr(doc) + "\n    pass\n", encoding="utf-8")
            with patch.object(api_documentation, "find_docs_root", return_value=docs):
                value = api_documentation.lookup("ansa.base.Long", "2026.1.0", scripts.MODULES)
        self.assertEqual(len(value["documentation"]), 16384)
        self.assertTrue(value["source_truncated"])
        self.assertFalse(value["complete"])
        self.assertTrue(value["source_file"].endswith("base/__init__.py"))
        self.assertIn("print(1)", value["examples"][0])

    def test_runtime_fallback_total_does_not_depend_on_page_limit(self):
        runtime = types.ModuleType("ansa.base")
        def a():
            """Unique search hit."""
        def b():
            """Unique search hit."""
        runtime.A, runtime.B = a, b
        def docs(query, modules, version, limit):
            rows = [{"api": "ansa.base.A"}, {"api": "ansa.base.B"}]
            return {"matches": rows[:limit], "_api_ids": [r["api"] for r in rows],
                    "source_truncated": False, "matches_truncated": limit < 2,
                    "source": "test help", "complete": False}
        with patch.dict(sys.modules, self.fake_ansa_constants()), \
                patch.object(api_documentation, "search", side_effect=docs), \
                patch.object(scripts.api_catalog, "load_namespace", return_value=runtime):
            totals = [scripts.api_reference({"action": "search", "module": "base", "query": "unique search", "limit": n})["total"]
                      for n in (1, 2)]
        self.assertEqual(totals, [2, 2])


if __name__ == "__main__":
    unittest.main()
