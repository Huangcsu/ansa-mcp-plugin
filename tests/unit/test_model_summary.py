"""Model information keeps empty results distinct from unavailable reads."""
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import entity_catalog
from ansa_mcp_plugin.__main__ import create_server


class ModelSummaryChecks(unittest.TestCase):
    def model(self, entities, database="", deck=1, categories=None):
        def collect(_deck, _container, name, _recursive):
            return (categories or {}).get(name, entities)
        base = SimpleNamespace(CurrentDeck=Mock(return_value=deck), DataBaseName=Mock(return_value=database),
                               TypesInCategory=Mock(return_value=["ANSAPART", "FACE", "GRID", "NODE", "SHELL", "SOLID"]),
                               CollectEntities=Mock(side_effect=collect))
        ansa = ModuleType("ansa")
        ansa.base, ansa.constants = base, SimpleNamespace(NASTRAN=1, ABAQUS=2)
        ansa.session = SimpleNamespace(DeckName=Mock(return_value={1: "NASTRAN", 2: "ABAQUS"}.get(deck, "OTHER")))
        return base, ansa

    def test_known_empty_and_populated_results_keep_existing_fields(self):
        for entities in ([], (), [object(), object()], (object(),)):
            for database in ("", "C:/models/example.ansa"):
                with self.subTest(entities=entities, database=database):
                    base, ansa = self.model(entities, database)
                    state = SimpleNamespace(assert_main=Mock())
                    with patch.dict("sys.modules", {"ansa": ansa}):
                        result = entity_catalog.execute(state, "model_summary", {})
                    self.assertEqual((result["deck"], result["database"], result["entity_count"]), (1, database, len(entities)))
                    self.assertEqual(result["deck_name"], "NASTRAN")
                    self.assertEqual(result["node_type"], "GRID")
                    self.assertEqual(set(result["entity_counts"]), {"parts", "materials", "properties", "faces", "nodes", "shells", "solids", "elements"})
                    self.assertEqual(set(result["entity_counts"].values()), {len(entities)})
                    self.assertTrue(result["counts_complete"])
                    self.assertEqual(result["count_errors"], {})
                    self.assertEqual(result["count_types"]["materials"], "__MATERIALS__")
                    self.assertEqual(result["count_types"]["properties"], "__PROPERTIES__")
                    self.assertEqual(result["count_types"]["elements"], "__ELEMENTS__")
                    self.assertEqual(base.CollectEntities.call_count, 9)
                    base.CollectEntities.assert_any_call(1, None, "__ALL_ENTITIES__", False)
                    state.assert_main.assert_called_once()

        base, ansa = self.model([object()], deck=2, categories={"NODE": [object(), object()], "GRID": None})
        with patch.dict("sys.modules", {"ansa": ansa}):
            result = entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
        self.assertEqual((result["deck_name"], result["node_type"], result["entity_counts"]["nodes"]), ("ABAQUS", "NODE", 2))
        self.assertTrue(result["counts_complete"])
        self.assertNotIn("GRID", [call.args[2] for call in base.CollectEntities.call_args_list])

    def test_unavailable_or_malformed_reads_never_become_zero(self):
        for entities in (None, False, 0, {}, "", iter(())):
            base, ansa = self.model(entities)
            with self.subTest(entities=entities), patch.dict("sys.modules", {"ansa": ansa}):
                with self.assertRaisesRegex(RuntimeError, "did not return an entity list"):
                    entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
        for database in (None, False, 123):
            base, ansa = self.model([], database)
            with self.subTest(database=database), patch.dict("sys.modules", {"ansa": ansa}):
                with self.assertRaises(TypeError):
                    entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
            base.CollectEntities.assert_not_called()
        for malformed in (None, False, 0, {}, "", iter(())):
            base, ansa = self.model([], categories={"__MATERIALS__": malformed})
            with self.subTest(category_result=malformed), patch.dict("sys.modules", {"ansa": ansa}):
                result = entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
            self.assertIsNone(result["entity_counts"]["materials"])
            self.assertEqual(result["entity_counts"]["nodes"], 0)
            self.assertFalse(result["counts_complete"])
            self.assertEqual(set(result["count_errors"]), {"materials"})
        base, ansa = self.model([], deck=3)
        with patch.dict("sys.modules", {"ansa": ansa}):
            result = entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
        self.assertIsNone(result["node_type"])
        self.assertIsNone(result["entity_counts"]["nodes"])
        self.assertIn("only verified", result["count_errors"]["nodes"])
        base, ansa = self.model([])
        base.TypesInCategory.return_value = None
        with patch.dict("sys.modules", {"ansa": ansa}):
            result = entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
        self.assertIsNone(result["entity_counts"]["nodes"])
        self.assertEqual(result["entity_counts"]["materials"], 0)
        self.assertEqual(set(result["count_errors"]), {"parts", "faces", "nodes", "shells", "solids"})
        for bad_name in (None, False, ""):
            base, ansa = self.model([])
            ansa.session.DeckName.return_value = bad_name
            with self.subTest(deck_name=bad_name), patch.dict("sys.modules", {"ansa": ansa}), self.assertRaises(TypeError):
                entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", {})
            base.CollectEntities.assert_not_called()

    def test_wrong_deck_extra_parameters_and_wrong_thread_do_not_collect(self):
        for params in ({"deck": 2}, {"deck": True}, {"deck": "1"}, {"unexpected": True}):
            base, ansa = self.model([])
            with self.subTest(params=params), patch.dict("sys.modules", {"ansa": ansa}):
                with self.assertRaises((TypeError, ValueError)):
                    entity_catalog.execute(SimpleNamespace(assert_main=lambda: None), "model_summary", params)
            base.CollectEntities.assert_not_called()
        base, ansa = self.model([])
        with patch.dict("sys.modules", {"ansa": ansa}):
            with self.assertRaisesRegex(RuntimeError, "owning thread"):
                entity_catalog.execute(SimpleNamespace(assert_main=Mock(side_effect=RuntimeError("owning thread"))),
                                       "model_summary", {})
        base.CurrentDeck.assert_not_called()

    def test_mcp_deck_is_strict_raw_and_rejects_extra_arguments(self):
        server = create_server(SimpleNamespace())
        tool = server._tool_manager.get_tool("ansa_model_summary")
        metadata = tool.fn_metadata
        request = {"operation_id": "model-summary", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}}
        for deck in (None, 1):
            self.assertEqual(metadata.arg_model.model_validate({**request, "deck": deck}).deck, deck)
        self.assertEqual(metadata.pre_parse_json({"deck": "null"}), {"deck": "null"})
        for extra in ({"deck": True}, {"deck": "1"}, {"deck": "null"}, {"extra": 1}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                metadata.arg_model.model_validate(metadata.pre_parse_json({**request, **extra}))
        self.assertIs(tool.parameters["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
