"""Shared RPC authentication, uncertainty and lifecycle safety without ANSA."""

import asyncio
import json
import threading
import unittest
from unittest.mock import Mock, patch

from ansa_mcp_plugin.__main__ import create_server
from ansa_mcp_plugin.backend import daemon
from ansa_mcp_plugin.backend import shared_client as client
from ansa_mcp_plugin.backend.memory import MemoryConflictError
from ansa_mcp_plugin.backend.scheduler import TaskNotFoundError


class SharedRpcTests(unittest.TestCase):
    def setUp(self):
        self.service = Mock(token='a' * 64)
        self.service.tasks.pending.return_value = []
        self.service.status.return_value = {'backend_id': 'shared'}
        self.server = daemon._ThreadingTCPServer(('127.0.0.1', 0), daemon._handler_type)
        self.endpoint = {'pid': 1, 'process_start': '1', 'backend_id': 'shared',
                         'port': self.server.server_address[1], 'protocol': client._PROTOCOL, 'build': client._BUILD}
        self.state = daemon._DaemonState(self.service, self.endpoint)
        self.server.RequestHandlerClass = daemon._handler_type(self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method='status', **kwargs):
        return client._request(self.endpoint, self.service.token, method, timeout=2, **kwargs)

    def frontend(self):
        service = object.__new__(client.SharedService)
        service._condition = threading.Condition()
        service._endpoint, service._token = self.endpoint, self.service.token
        service._wait_ready = lambda: (self.endpoint, self.service.token)
        service.tasks = client._RemoteTasks(service)
        return create_server(service)

    def test_auth_protocol_and_method_allowlist(self):
        for changes in ({'token': 'bad'}, {'protocol': 'unknown'}, {'method': '__dict__'}):
            with self.subTest(changes=changes), self.assertRaises((client.BackendUnavailableError, ValueError)):
                client._request(self.endpoint, changes.get('token', self.service.token),
                                changes.get('method', 'status'), protocol=changes.get('protocol', client._PROTOCOL), timeout=2)
        self.service.status.assert_not_called()
        self.assertEqual(self.request(), {'backend_id': 'shared'})

    def test_remote_exceptions_keep_tool_semantics(self):
        for error in (TaskNotFoundError('absent'), MemoryConflictError('revision'), PermissionError('ACL'), TimeoutError('busy')):
            with self.subTest(error=error), self.assertRaises(type(error)):
                self.service.status.side_effect = error
                self.request()

    def test_sent_operation_with_lost_reply_is_not_replayed(self):
        target = {'backend_id': 'shared', 'instance_id': 'ansa', 'session_id': 'session', 'generation': 1}
        model = {'epoch': 1}
        record = {'task_id': 'original', 'operation_id': 'original-operation', 'state': 'succeeded',
                  'result': {'created': True}, 'dispatch': 'sent', 'cancel_pending': False, 'acked': True,
                  'task': {'target': target, 'model': model}}
        self.service.submit_task.return_value = record
        self.service.query_task.return_value = record
        server = self.frontend()
        with patch.object(daemon, '_send_frame', side_effect=OSError('response lost')):
            response = asyncio.run(server.call_tool('ansa_grid_create', {
                'operation_id': 'original-operation', 'expected_target': target,
                'expected_model': model, 'position': [0, 0, 0]}))
        self.assertTrue(response.isError)
        value = response.structuredContent
        self.assertEqual((value['status'], value['error']['code']), ('unknown', 'TASK_UNKNOWN'))
        self.assertEqual(value['operation_id'], 'original-operation')
        self.assertIsNone(value['task_id'])
        self.assertEqual((value['target'], value['model']), (target, model))
        self.assertIn('原 operation_id', value['warnings'][0])
        queried = asyncio.run(server.call_tool('ansa_task_query', {'operation_id': value['operation_id']}))
        self.assertEqual(queried.structuredContent['status'], 'succeeded')
        self.service.query_task.assert_called_once_with(task_id=None, operation_id='original-operation')
        self.service.submit_task.assert_called_once()

    def test_definite_backend_rejection_keeps_failed_semantics(self):
        self.service.submit_task.side_effect = ValueError('stale model')
        response = asyncio.run(self.frontend().call_tool('ansa_grid_create', {
            'operation_id': 'rejected', 'expected_target': {
                'backend_id': 'shared', 'instance_id': 'ansa', 'session_id': 'session', 'generation': 1},
            'expected_model': {'epoch': 1}, 'position': [0, 0, 0]}))
        self.assertEqual(response.structuredContent['status'], 'failed')
        self.assertEqual(response.structuredContent['error']['code'], 'TASK_REJECTED')

    def test_large_history_is_summarized_before_transport(self):
        large = 'PRIVATE_BODY' + 'x' * (600 * 1024)
        records = [{
            'task_id': str(index), 'operation_id': 'operation-' + str(index), 'state': 'succeeded',
            'dispatch': 'sent', 'cancel_pending': False, 'result': large, 'error': large,
            'task': {'tool': 'script_execute', 'params': {'script': large},
                     'target': {'instance_id': 'ansa'}, 'model': {'epoch': 1}},
        } for index in range(30)]
        self.assertGreater(30 * len(large), client._MAX_FRAME_BYTES)
        self.service.tasks.history_page.return_value = {
            'tasks': records, 'has_more': True, 'next_before_sequence': 71}
        page = self.request('tasks.history_page', args=(30, 'succeeded', 101))
        self.assertEqual((len(page['tasks']), page['has_more'], page['next_before_sequence']), (30, True, 71))
        self.assertEqual(set(page['tasks'][0]), {
            'task_id', 'operation_id', 'state', 'dispatch', 'cancel_pending', 'task'})
        self.assertEqual(set(page['tasks'][0]['task']), {'tool', 'target', 'model'})
        encoded = json.dumps(page)
        self.assertLess(len(encoded), 20000)
        self.assertNotIn('PRIVATE_BODY', encoded)
        self.service.tasks.history_page.assert_called_once_with(30, 'succeeded', 101)
        response = asyncio.run(self.frontend().call_tool('ansa_task_list', {
            'limit': 30, 'state': 'succeeded', 'before_sequence': 101}))
        self.assertFalse(response.isError)
        data = response.structuredContent['data']
        self.assertEqual((len(data['tasks']), data['has_more'], data['next_before_sequence']), (30, True, 71))
        self.assertEqual(data['tasks'][0]['tool'], 'script_execute')

    def test_shutdown_rejects_pending_tasks_and_lost_reply_still_stops(self):
        self.service.tasks.pending.return_value = [{'task_id': 'pending'}]
        with self.assertRaises(client.BackendUnavailableError):
            self.request('daemon.shutdown')
        self.assertTrue(self.state.accepting)
        self.service.tasks.pending.return_value = []
        with patch.object(daemon, '_send_frame', side_effect=OSError('response lost')):
            with self.assertRaises(client.BackendUnavailableError):
                self.request('daemon.shutdown')
        self.assertTrue(self.state.shutdown_requested.wait(1))
        self.assertFalse(self.state.accepting)
        with self.assertRaises(client.BackendUnavailableError):
            self.request()
        self.service.status.assert_not_called()


class SharedFrontendTests(unittest.TestCase):
    def test_unsent_input_or_connect_failure_is_not_an_unknown_outcome(self):
        with patch.object(client.socket, 'create_connection') as connect:
            for args in ((float('nan'),), (object(),), ('x' * client._MAX_FRAME_BYTES,)):
                with self.subTest(args_type=type(args[0]).__name__), self.assertRaises((ValueError, TypeError)):
                    client._request({'port': 1}, 'token', 'submit_task', args=args)
            connect.assert_not_called()
            connect.side_effect = ConnectionRefusedError('offline')
            with self.assertRaises(client.BackendUnavailableError) as raised:
                client._request({'port': 1}, 'token', 'submit_task')
            self.assertNotIsInstance(raised.exception, client.BackendOutcomeUnknownError)
            connect.assert_called_once()

    def test_partial_send_and_invalid_reply_have_unknown_outcomes(self):
        with patch.object(client.socket, 'create_connection') as connect:
            conn = connect.return_value.__enter__.return_value
            conn.sendall.side_effect = OSError('partial send')
            with self.assertRaises(client.BackendOutcomeUnknownError):
                client._request({'port': 1}, 'token', 'submit_task')
            conn.sendall.assert_called_once()
            conn.sendall.side_effect = None
            for response in ({'protocol': 'wrong'}, {'protocol': client._PROTOCOL, 'ok': True},
                             {'protocol': client._PROTOCOL, 'ok': False, 'error': {}}):
                with self.subTest(response=response), patch.object(client, '_recv_frame', return_value=response):
                    with self.assertRaises(client.BackendOutcomeUnknownError):
                        client._request({'port': 1}, 'token', 'submit_task')

    def test_constructor_does_not_wait_for_backend_and_close_does_not_stop_it(self):
        entered, release = threading.Event(), threading.Event()
        def slow_start(_self):
            entered.set()
            release.wait(2)
            return {'pid': 1}, 'a' * 64
        with patch.object(client.SharedService, '_discover_or_start', slow_start), patch.object(client, '_request') as request:
            service = client.SharedService('runtime')
            self.assertTrue(entered.wait(1))
            release.set()
            service.close()
            request.assert_not_called()

    def test_dead_process_or_pid_reuse_never_trusts_old_endpoint(self):
        with patch.object(client, 'process_start_filetime', return_value='new'):
            self.assertFalse(client._process_is_live(123, 'old'))
        with patch.object(client, 'process_start_filetime', return_value='old'), patch.object(client, 'same_process_alive', return_value=False):
            self.assertFalse(client._process_is_live(123, 'old'))

    def test_frontend_never_retries_an_uncertain_request(self):
        service = object.__new__(client.SharedService)
        service._condition = threading.Condition()
        service._endpoint, service._token = {'port': 1}, 'a' * 64
        with patch.object(service, '_wait_ready', return_value=(service._endpoint, service._token)), patch.object(
                client, '_request', side_effect=client.BackendUnavailableError('OFFLINE', 'uncertain')) as request:
            with self.assertRaises(client.BackendUnavailableError):
                service.submit_task('write', {}, 'original', {}, {})
            request.assert_called_once()
            self.assertIsNone(service._endpoint)


if __name__ == '__main__':
    unittest.main()
