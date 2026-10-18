"""Check arguments rejected by MCP must never reach task admission."""

import unittest


class CheckSchemaChecks(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_check_schema_and_read_only_classification(self):
        from mcp.server.fastmcp.exceptions import ToolError
        from ansa_mcp_plugin.__main__ import create_server
        from ansa_mcp_shared.operations import READ_ONLY_TOOLS, writes_model

        class Service:
            def __init__(self):
                self.calls = []

            def submit_task(self, tool, params, operation_id, target, model, ttl):
                self.calls.append((tool, params))
                return {"task": {"target": target, "model": model}, "state": "queued",
                        "error": None, "result": None, "dispatch": "not_sent", "acked": False,
                        "cancel_pending": False, "operation_id": operation_id, "task_id": "check-schema"}

        service = Service()
        server = create_server(service)
        args = {"operation_id": "check-schema", "expected_target": {
            "backend_id": "b", "instance_id": "i", "session_id": "s", "generation": 1},
            "expected_model": {"epoch": 1}, "check": "free_nodes", "deck": 1, "entity_ids": [1, 2]}
        for changed in ({"deck": True}, {"deck": "1"}, {"check": "repair"},
                        {"entity_ids": []}, {"entity_ids": [True]}, {"entity_ids": [0]},
                        {"entity_ids": list(range(1, 202))}, {"consider_nodes_with_mass_free": 1},
                        {"consider_nodes_with_mass_free": "false"}):
            with self.subTest(changed=changed):
                with self.assertRaises(ToolError):
                    await server.call_tool("ansa_check_run", {**args, **changed})
        self.assertEqual(service.calls, [])
        for flag in (True, False):
            await server.call_tool("ansa_check_run", {**args, "consider_nodes_with_mass_free": flag})
            self.assertEqual(service.calls[-1], ("check_run", {
                "check": "free_nodes", "deck": 1, "entity_ids": [1, 2],
                "consider_nodes_with_mass_free": flag}))
        self.assertIn("check_run", READ_ONLY_TOOLS)
        self.assertFalse(writes_model("check_run"))


if __name__ == "__main__":
    unittest.main()
