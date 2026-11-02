"""Owned-process cleanup must reject stale or missing identity before RPC."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, call, patch

from ansa_mcp_shared import identity as process_identity
from tests.integration import stage_three_ansa as stage


class OwnedAnsaCleanupTests(unittest.TestCase):
    def setUp(self):
        translator = str((Path.cwd() / "owned-cleanup-fixture.py").resolve())
        arguments = ["-translfp", translator, "-grpcport", "50051", "-grpchost", "127.0.0.1"]
        self.identity = {"pid": 1234, "process_start": "saved-start", "port": 50051,
                         "program_args": arguments}
        basic = {"pid": 1234, "main_thread": True, "program_args": arguments}
        self.instructions = {"translator": translator}
        self.one = SimpleNamespace(remote_basic=object())
        self.two = SimpleNamespace(rpc=Mock(return_value=basic))
        self.original_channel = object()
        self.helpers = (self.one, self.two, self.original_channel)
        self.ansa = ModuleType("ansa")
        self.ansa.connect = Mock(return_value=object())
        self.ansa.execute_function = Mock(return_value=basic)
        self.ansa.stop = Mock()
        self.ansa.disconnect = Mock()
        self.grpc = ModuleType("grpc")
        self.grpc.insecure_channel = object()
        self.enterContext(patch.dict(sys.modules, {"ansa": self.ansa, "grpc": self.grpc}))
        self.current_start = self.enterContext(patch.object(
            process_identity, "process_start_filetime",
            side_effect=AssertionError("cleanup must not infer a missing saved identity")))

    def test_unverified_identity_is_rejected_before_remote_access(self):
        missing = {key: value for key, value in self.identity.items() if key != "process_start"}
        for label, identity, result, error in (
            ("missing", missing, None, ValueError("Persisted process identity is missing")),
            ("reused_or_exited", self.identity, False, None),
            ("unverifiable", self.identity, None, OSError("access denied")),
        ):
            with self.subTest(reason=label), patch.object(
                    process_identity, "same_process_alive", return_value=result, side_effect=error) as alive:
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    stage._cleanup_owned_ansa(identity, self.instructions, self.helpers)
                alive.assert_called_once_with(identity["pid"], identity.get("process_start"))
                self.two.rpc.assert_not_called()
                self.ansa.connect.assert_not_called()
                self.ansa.execute_function.assert_not_called()
                self.ansa.stop.assert_not_called()
                self.current_start.assert_not_called()

    def test_verified_saved_identity_keeps_guarded_cleanup(self):
        events = Mock()
        events.attach_mock(self.two.rpc, "rpc")
        with patch.object(process_identity, "same_process_alive", side_effect=[True, True, False]) as alive:
            events.attach_mock(alive, "identity")
            result = stage._cleanup_owned_ansa(self.identity, self.instructions, self.helpers)
        self.assertEqual(events.mock_calls[0], call.identity(1234, "saved-start"))
        self.two.rpc.assert_called_once_with(50051, self.one.remote_basic)
        self.assertEqual(alive.call_args_list, [call(1234, "saved-start")] * 3)
        self.ansa.execute_function.assert_called_once_with(self.one.remote_basic)
        self.ansa.stop.assert_called_once_with(self.ansa.connect.return_value)
        self.assertEqual(result, {"pid": 1234, "stopped_owned_process": True})
        self.assertIs(self.grpc.insecure_channel, self.original_channel)
        self.current_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
