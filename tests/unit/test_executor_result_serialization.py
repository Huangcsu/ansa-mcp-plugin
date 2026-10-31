"""A bad operation return must not strand a completed native call as running."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from ansa_mcp_bridge.executor import MainThreadExecutor
from ansa_mcp_bridge.journal import Journal,JournalPersistenceError
from test_journal import IDENTITY,make_task


class ExecutorResultSerializationTests(unittest.TestCase):
    def run_case(self,bad_result,persistence_failure=False):
        root=Path(__file__).resolve().parents[2]/'runtime'/'unit-test-temp';root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            journal=Journal(directory,IDENTITY)
            try:
                task=make_task();task['tool']='mesh_map_blocks_apply';journal.submit(task)
                connected=threading.Event();connected.set()
                state=SimpleNamespace(assert_main=Mock(),journal=journal,stopped=threading.Event(),connected=connected,
                    authorization={'backend_id':'backend-a'},connected_backend_id='backend-a',validate_task=lambda task:True,
                    probe=lambda:{'model':{'epoch':7,'database':'fixture.ansa'}},identity=lambda:dict(IDENTITY),main_thread_id=123,last_error=None)
                executor=MainThreadExecutor(state)
                with patch('ansa_mcp_bridge.operations.execute',return_value=bad_result) as native:
                    if persistence_failure:
                        with patch.object(journal,'finish',side_effect=JournalPersistenceError('disk failure')) as finish:
                            executor.tick();finish.assert_called_once()
                    else:
                        executor.tick();executor.tick()
                    native.assert_called_once()
                record=journal.get(task['task_id'])
                self.assertFalse(executor._in_tick)
                if persistence_failure:
                    self.assertEqual(record['state'],'running');self.assertIn('disk failure',state.last_error)
                else:
                    self.assertEqual(record['state'],'failed');self.assertFalse(journal.busy())
                    self.assertEqual(record['result']['error_phase'],'result_serialization')
                    self.assertTrue(record['result']['possible_effects']);self.assertTrue(record['result']['recovery_required'])
                    self.assertFalse(record['result']['native_operation_replayed'])
            finally:journal.close()

    def test_invalid_result_finishes_as_failed_without_replaying_native_operation(self):
        for result in ({'ok':True,'layout':{1:[2]}},{'ok':True,'cells':(1,2)},{'ok':True,'bad':float('nan')}):
            with self.subTest(result=result):self.run_case(result)

    def test_persistence_failure_is_not_misreported_as_a_serialization_recovery(self):
        self.run_case({'ok':True},True)


if __name__=='__main__':unittest.main()
