"""Large user script results are archived and returned through bounded paging."""

from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_bridge.operations import scripts, structured_evidence
from ansa_mcp_shared.framing import MAX_FRAME_BYTES


class ScriptResultPagingTests(unittest.TestCase):
    def _state(self, directory):
        return types.SimpleNamespace(
            assert_main=Mock(),
            session_dir=Path(directory),
            model_epoch=3,
            instance_id="test-instance",
            session_id="test-session",
            probe=Mock(),
        )

    def _fake_ansa(self):
        module = types.ModuleType("ansa")
        module.base = types.SimpleNamespace(CurrentDeck=Mock(return_value=1))
        return module

    def test_small_result_is_returned_unchanged(self):
        state = self._state("unused")
        original = {
            "result": {"value": 7},
            "stdout": "done",
            "stdout_truncated": False,
            "preflight": {"valid": True, "missing": []},
        }
        with patch.object(scripts, "execute_script", return_value=original) as run_script:
            response = scripts.execute(state, "script_execute", {"script": "RESULT = 7"})

        self.assertIs(response, original)
        run_script.assert_called_once_with({"script": "RESULT = 7"})
        state.assert_main.assert_called_once_with()

    def test_large_result_is_archived_once_and_nested_page_is_complete(self):
        repo_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(dir=repo_root) as directory:
            state = self._state(directory)
            ansa = self._fake_ansa()
            nested_text = "界" * 45000
            original = {
                "result": {"nested": {"items": [nested_text]}},
                "stdout": "",
                "stdout_truncated": False,
                "preflight": {"valid": True, "missing": []},
            }
            with patch.dict(sys.modules, {"ansa": ansa}), patch.object(
                scripts, "execute_script", return_value=original
            ) as run_script:
                response = scripts.execute(state, "script_execute", {"script": "RESULT = {}"})

            run_script.assert_called_once_with({"script": "RESULT = {}"})
            ansa.base.CurrentDeck.assert_called_once_with()
            self.assertTrue(response["result_paged"])
            self.assertEqual(response["stdout"], "")
            self.assertFalse(response["stdout_truncated"])
            self.assertEqual(response["preflight"], {"valid": True, "stored_in_report": True})
            from ansa_mcp_bridge.journal import Journal

            encoded_size = len(Journal._canonical(response, "test response").encode("utf-8"))
            self.assertLessEqual(encoded_size, MAX_FRAME_BYTES // 8)

            page = structured_evidence.read_page(
                state, 1, response["result"]["report_id"], "/result/nested/items", limit=10
            )
            self.assertTrue(page["complete"])
            self.assertEqual(page["rows"], [nested_text])

    def test_utf8_bytes_not_character_count_select_paging(self):
        state = self._state("unused")
        ascii_result = {
            "result": "x" * 700,
            "stdout": "",
            "stdout_truncated": False,
            "preflight": {"valid": True, "missing": []},
        }
        utf8_result = {**ascii_result, "result": "界" * 700}
        from ansa_mcp_bridge.journal import Journal

        budget = 16384 // 8
        ascii_size = len(
            Journal._canonical(ascii_result, "test result").encode("utf-8")
        )
        utf8_size = len(
            Journal._canonical(utf8_result, "test result").encode("utf-8")
        )
        self.assertEqual(len(ascii_result["result"]), len(utf8_result["result"]))
        self.assertLessEqual(ascii_size, budget)
        self.assertGreater(utf8_size, budget)
        descriptor = {
            "ok": True,
            "report_id": "a" * 32,
            "sha256": "b" * 64,
            "kind": "script_execute_result",
            "context": {"deck": 1, "epoch": 3, "instance_id": "test-instance", "session_id": "test-session"},
            "stored_complete": True,
            "usable_for_mutation": False,
            "sections": {"result": 1},
        }
        ansa = self._fake_ansa()
        with patch.dict(sys.modules, {"ansa": ansa}), patch.object(
            scripts, "MAX_FRAME_BYTES", 16384
        ), patch.object(scripts, "execute_script", side_effect=[ascii_result, utf8_result]) as run_script, patch.object(
            structured_evidence, "store_report", return_value=descriptor
        ) as store:
            ascii_response = scripts.execute(state, "script_execute", {"script": "RESULT = 'x'"})
            utf8_response = scripts.execute(state, "script_execute", {"script": "RESULT = '界'"})

        self.assertIs(ascii_response, ascii_result)
        self.assertTrue(utf8_response["result_paged"])
        run_script.assert_has_calls(
            [
                unittest.mock.call({"script": "RESULT = 'x'"}),
                unittest.mock.call({"script": "RESULT = '界'"}),
            ]
        )
        store.assert_called_once_with(state, 1, "script_execute_result", utf8_result)

    def test_oversize_scalar_is_retrievable_after_model_lifetime_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            state.probe.side_effect = lambda: setattr(state, "model_epoch", 4)
            payload = "界\n" * 200000
            original = {"result": {"blob": payload}, "stdout": "", "stdout_truncated": False, "preflight": {"valid": True}}
            with patch.dict(sys.modules, {"ansa": self._fake_ansa()}), patch.object(scripts, "execute_script", return_value=original) as execute:
                response = scripts.execute(state, "script_execute", {"script": "RESULT = {}", "allow_model_change": True})
            self.assertEqual(response["result"]["context"]["epoch"], 4)
            offset, chunks = 0, []
            while True:
                page = structured_evidence.read_page(state, 1, response["result"]["report_id"], "/result/blob", offset=offset)
                self.assertEqual(page["paging_unit"], "string_chunks")
                chunks.extend(row["text"] for row in page["rows"])
                if page["complete"]: break
                offset = page["next_offset"]
            self.assertEqual("".join(chunks), payload)
            execute.assert_called_once()

    def test_page_boundary_scalar_and_long_json_pointer_are_retrievable(self):
        for key, payload in (("boundary", "x"*196606), ("k"*129, "x"*300000)):
            with self.subTest(key_length=len(key)), tempfile.TemporaryDirectory() as directory:
                state = self._state(directory)
                original = {"result": {key: payload}, "stdout": "", "stdout_truncated": False, "preflight": {"valid": True}}
                with patch.dict(sys.modules, {"ansa": self._fake_ansa()}), patch.object(scripts, "execute_script", return_value=original):
                    response = scripts.execute(state, "script_execute", {"script": "RESULT = {}"})
                offset, text = 0, []
                while True:
                    page = structured_evidence.read_page(state, 1, response["result"]["report_id"], "/result/"+key, offset=offset)
                    self.assertEqual(page["paging_unit"], "string_chunks")
                    text.extend(row["text"] for row in page["rows"])
                    if page["complete"]: break
                    offset = page["next_offset"]
                self.assertEqual("".join(text), payload)

    def test_maximum_escaped_pointer_and_page_fit_complete_task_reply(self):
        from ansa_mcp_shared.framing import send_frame
        from ansa_mcp_bridge.journal import Journal
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            key = "\x00"*(MAX_FRAME_BYTES//16-len("/result/"))
            original = {"result": {key: "x"*300000}, "stdout": "", "stdout_truncated": False, "preflight": {"valid": True}}
            with patch.dict(sys.modules, {"ansa": self._fake_ansa()}), patch.object(scripts, "execute_script", return_value=original):
                response = scripts.execute(state, "script_execute", {"script": "RESULT = {}"})
            section = "/result/"+key
            page = structured_evidence.read_page(state, 1, response["result"]["report_id"], section)
            packet = {"version": 1, "kind": "reply", "request_id": "r"*128, "ok": True, "data": {
                "task_id": "t"*128, "state": "succeeded", "error": None, "acked": False,
                "task": {"tool": "mesh_evidence_page", "params": {"section": section, "deck": 1, "offset": 0, "limit": 100}},
                "result": page, "reserved_metadata": "m"*16384}}
            self.assertLess(len(Journal._canonical(packet, "full reply").encode("utf-8")), MAX_FRAME_BYTES)
            sock = Mock()
            send_frame(sock, packet)
            sock.sendall.assert_called_once()
            with self.assertRaises(ValueError):
                structured_evidence.read_page(state, 1, response["result"]["report_id"], section+"x")

    def test_storage_failure_is_propagated_after_single_execution(self):
        state = self._state("unused")
        ansa = self._fake_ansa()
        original = {
            "result": "x" * (MAX_FRAME_BYTES // 8 + 1),
            "stdout": "",
            "stdout_truncated": False,
            "preflight": {"valid": True, "missing": []},
        }
        with patch.dict(sys.modules, {"ansa": ansa}), patch.object(
            scripts, "execute_script", return_value=original
        ) as run_script, patch.object(
            structured_evidence, "store_report", side_effect=OSError("evidence storage failed")
        ):
            with self.assertRaisesRegex(OSError, "evidence storage failed"):
                scripts.execute(state, "script_execute", {"script": "RESULT = 'large'"})

        run_script.assert_called_once_with({"script": "RESULT = 'large'"})
        ansa.base.CurrentDeck.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
