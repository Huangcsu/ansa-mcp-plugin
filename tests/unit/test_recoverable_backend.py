"""Lock contention remains visible through MCP and can recover in-session."""

import concurrent.futures
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_plugin.backend.lock import BackendBusyError
from ansa_mcp_plugin.backend.recoverable import RecoverableService


class RecoverableBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_discovery_then_same_client_recovers(self):
        backend = SimpleNamespace(status=lambda: {"backend_id": "new", "active_target": None}, close=Mock())
        with patch('ansa_mcp_plugin.backend.recoverable.Service', side_effect=[BackendBusyError('occupied'), backend]) as factory:
            service = RecoverableService('runtime')
            server = create_server(service)
            self.assertEqual(len(await server.list_tools()), 69)
            blocked = await server.call_tool('ansa_connection_status', {})
            self.assertEqual(blocked.structuredContent['error']['code'], 'BACKEND_BUSY')
            self.assertTrue(blocked.isError)
            self.assertEqual(factory.call_count, 1)
            with patch('ansa_mcp_plugin.backend.recoverable.time.monotonic', return_value=float('inf')):
                ready = await server.call_tool('ansa_connection_status', {})
            self.assertTrue(ready.structuredContent['ok'])
            self.assertIsNone(ready.structuredContent['data']['active_target'])
            service.close()
            backend.close.assert_called_once()

    async def test_busy_write_is_rejected_without_submission(self):
        with patch('ansa_mcp_plugin.backend.recoverable.Service', side_effect=BackendBusyError('occupied')) as factory:
            service = RecoverableService('runtime')
            result = await create_server(service).call_tool('ansa_grid_create', {
                'operation_id': 'never-submitted', 'expected_target': {
                    'backend_id': 'old', 'instance_id': 'instance', 'session_id': 'session', 'generation': 1},
                'expected_model': {'epoch': 1}, 'position': [0, 0, 0]})
            self.assertTrue(result.isError)
            self.assertIsNone(result.structuredContent['task_id'])
            self.assertEqual(factory.call_count, 1)
            service.close()
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                service.status()

    def test_concurrent_recovery_creates_only_one_backend(self):
        backend = SimpleNamespace(status=lambda: 'ready', close=Mock())
        with patch('ansa_mcp_plugin.backend.recoverable.Service', side_effect=[BackendBusyError('occupied'), backend]) as factory:
            service = RecoverableService('runtime')
            with patch('ansa_mcp_plugin.backend.recoverable.time.monotonic', return_value=float('inf')):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    self.assertEqual(list(pool.map(lambda _: service.status(), range(20))), ['ready'] * 20)
            self.assertEqual(factory.call_count, 2)
            service.close()

    def test_permission_failure_is_not_hidden_as_busy(self):
        with patch('ansa_mcp_plugin.backend.recoverable.Service', side_effect=PermissionError('unsafe ACL')):
            with self.assertRaisesRegex(PermissionError, 'unsafe ACL'):
                RecoverableService('runtime')


if __name__ == '__main__':
    unittest.main()
