"""离线验证可恢复 MapBlock 填充编排。"""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ansa_mcp_plugin.workflows import MapBlockJobClient


TARGET = {
    "backend_id": "backend-test",
    "instance_id": "instance-test",
    "session_id": "session-test",
    "generation": 7,
}
MODEL = {"epoch": 3}
DECK = 2
PROFILE = {"template": "linear_solid"}


class FakeMCP:
    def __init__(self, *, apply_mode=None, quality_unknown=False):
        self.calls = []
        self.tasks = {}
        self.operations = {}
        self.reports = {}
        self.volume_plans = {}
        self.plan_count = 0
        self.apply_count = 0
        self.apply_mode = apply_mode
        self.quality_unknown = quality_unknown

    async def __call__(self, tool, args):
        args = copy.deepcopy(args)
        self.calls.append({"tool": tool, "args": args})
        if tool == "ansa_task_query":
            original = (self.tasks.get(args.get("task_id")) if args.get("task_id") is not None
                        else self.operations.get(args.get("operation_id")))
            if original is None:
                return {"ok": False, "status": "unknown", "operation_id": args.get("operation_id"),
                        "task_id": args.get("task_id"), "error": {"code": "TASK_UNKNOWN", "message": "unknown"}}
            return copy.deepcopy(original)

        if tool == "ansa_mesh_map_block_fill" and args["action"] == "plan":
            self.plan_count += 1
            number = self.plan_count
            report_id = f"{number:032x}"
            plan_sha = f"{number:064x}"
            report_sha = f"{number + 100:064x}"
            scope_sha = f"{number + 200:064x}"
            for volume_id in args["volume_ids"]:
                self.volume_plans[volume_id] = plan_sha
            self.reports[report_id] = {
                "sha256": report_sha,
                "scope_sha256": scope_sha,
                "predicted_solids": len(args["volume_ids"]),
            }
            result = {
                "plan_sha256": plan_sha,
                "report_id": report_id,
                "sha256": report_sha,
                "usable_for_mutation": False,
                "context": {
                    "epoch": MODEL["epoch"],
                    "instance_id": TARGET["instance_id"],
                    "session_id": TARGET["session_id"],
                    "deck": DECK,
                },
                "sections": ["scope_sha256", "predicted_solids"],
            }
            return self._success(args, result)

        if tool == "ansa_mesh_evidence_page":
            report = self.reports[args["report_id"]]
            result = {
                "report_id": args["report_id"],
                "sha256": report["sha256"],
                "section": args["section"],
                "offset": 0,
                "complete": True,
                "rows": [report[args["section"]]],
            }
            return self._success(args, result)

        if tool == "ansa_mesh_map_block_fill" and args["action"] == "apply":
            self.apply_count += 1
            mode = self.apply_mode if self.apply_count == 1 else None
            if mode == "unknown":
                return {"ok": False, "status": "unknown", "operation_id": args["operation_id"],
                        "target": copy.deepcopy(TARGET), "model": copy.deepcopy(MODEL),
                        "warnings": ["结果未知"]}
            if mode == "failed":
                return {"ok": False, "status": "failed", "operation_id": args["operation_id"],
                        "target": copy.deepcopy(TARGET), "model": copy.deepcopy(MODEL),
                        "error": {"code": "TASK_FAILED", "message": "fake failure"}}
            solid_ids = [5000 + self.apply_count * 10 + offset
                         for offset in range(len(args["volume_ids"]))]
            result = {
                "ok": True,
                "scope_verified": True,
                "provenance_stage": "verified",
                "plan_sha256": self.volume_plans[args["volume_ids"][0]],
                "new_solid_ids": solid_ids,
                "ledger_id": f"{self.apply_count:032x}",
                "ledger_hash": f"{self.apply_count + 300:064x}",
            }
            reply = self._success(args, result)
            if mode == "no_task_id":
                reply["task_id"] = None
                return reply
            if mode == "queued":
                queued = copy.deepcopy(reply)
                queued["status"] = "queued"
                queued["data"]["result"] = None
                return queued
            if mode == "timeout":
                raise TimeoutError("fake client timeout after task submission")
            return reply

        if tool == "ansa_model_save":
            Path(args["path"]).write_bytes(b"fake verified checkpoint")
            return self._success(args, {"saved": True, "path": args["path"]})

        if tool == "ansa_mesh_quality_report":
            count = len(args["entity_ids"])
            unknown = 1 if self.quality_unknown else 0
            passed = count - unknown
            return self._success(args, {
                "complete": not self.quality_unknown,
                "passed": not self.quality_unknown,
                "snapshot_verified": True,
                "summary": {
                    "status": "passed" if not self.quality_unknown else "unknown",
                    "checked_count": count,
                    "failed_count": 0,
                    "unknown_count": unknown,
                    "passed_count": passed,
                },
            })

        raise AssertionError(f"unexpected public MCP tool: {tool}")

    def _success(self, args, result):
        task_id = f"task-{len(self.tasks) + 1}"
        reply = {
            "ok": True,
            "status": "succeeded",
            "operation_id": args["operation_id"],
            "task_id": task_id,
            "target": copy.deepcopy(TARGET),
            "model": copy.deepcopy(MODEL),
            "data": {"result": {**result, "model_after": copy.deepcopy(MODEL)},
                     "dispatch": "complete", "cancel_pending": False, "acked": True},
            "error": None,
            "warnings": [],
        }
        self.tasks[task_id] = copy.deepcopy(reply)
        self.operations[args["operation_id"]] = copy.deepcopy(reply)
        return reply


class CrashAfterApplyClient(MapBlockJobClient):
    def _complete_phase(self, manifest, path, call, payload):
        if call["phase"] == "fill_apply":
            raise asyncio.CancelledError("simulated process stop after apply result arrived")
        return super()._complete_phase(manifest, path, call, payload)


class CrashBeforeCheckpointClient(MapBlockJobClient):
    async def _start_checkpoint(self, manifest, path, index):
        raise asyncio.CancelledError("simulated process stop after verified manifest was persisted")


class MapBlockJobTests(unittest.IsolatedAsyncioTestCase):
    async def _run_job(self, root, fake, *, groups=((10,), (20,)), client_type=MapBlockJobClient):
        job_dir = Path(root) / "job"
        client = client_type(fake, poll_interval=0, wait_timeout_seconds=0)
        result = await client.run(
            deck=DECK,
            fill_groups=[list(group) for group in groups],
            quality_profile=PROFILE,
            job_dir=job_dir,
            expected_target=TARGET,
            expected_model=MODEL,
        )
        return result, job_dir / "manifest.json", client

    async def test_two_groups_checkpoint_and_explicit_quality_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP()
            result, manifest_path, _ = await self._run_job(temp, fake)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["completed_groups"], 2)
            self.assertEqual(len(result["solid_ids"]), 2)
            self.assertEqual(result["quality_summary"]["unknown_count"], 0)
            self.assertEqual(fake.apply_count, 2)
            self.assertEqual(sum(call["tool"] == "ansa_model_save" for call in fake.calls), 2)
            quality_args = next(call["args"] for call in fake.calls
                                if call["tool"] == "ansa_mesh_quality_report")
            self.assertEqual(quality_args["profile"]["template"], PROFILE["template"])
            self.assertEqual(quality_args["profile"]["criteria_label"], "current_session")
            self.assertEqual(set(quality_args["entity_ids"]), set(result["solid_ids"]))

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual([group["state"] for group in manifest["groups"]], ["checkpointed"] * 2)
            self.assertTrue(all(group["provenance"]["ledger_id"] for group in manifest["groups"]))
            self.assertTrue(all(len(group["checkpoint"]["sha256"]) == 64 for group in manifest["groups"]))
            for call in manifest["calls"]:
                request = call["request"]
                digest = hashlib.sha256(json.dumps(
                    request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                ).encode("utf-8")).hexdigest()
                self.assertEqual(call["request_sha256"], digest)
                self.assertEqual(call["binding"]["expected_model"], MODEL)

    async def test_resume_after_apply_reply_queries_original_and_does_not_apply_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP()
            with self.assertRaises(asyncio.CancelledError):
                await self._run_job(temp, fake, groups=((10,),), client_type=CrashAfterApplyClient)

            manifest_path = Path(temp) / "job" / "manifest.json"
            pending = json.loads(manifest_path.read_text(encoding="utf-8"))["pending_call_id"]
            self.assertIsNotNone(pending)
            result = await MapBlockJobClient(fake, poll_interval=0, wait_timeout_seconds=0).resume(
                manifest_path, expected_target=TARGET, expected_model=MODEL,
            )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(fake.apply_count, 1)
            self.assertTrue(any(call["tool"] == "ansa_task_query" for call in fake.calls))

    async def test_resume_after_verified_manifest_before_checkpoint_does_not_reapply(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP()
            with self.assertRaises(asyncio.CancelledError):
                await self._run_job(temp, fake, groups=((10,),), client_type=CrashBeforeCheckpointClient)

            manifest_path = Path(temp) / "job" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "verified")
            self.assertIsNone(manifest["pending_call_id"])
            self.assertEqual(manifest["groups"][0]["state"], "verified")
            self.assertEqual(manifest["groups"][0]["solid_ids"], manifest["solid_ids"])
            self.assertFalse(any(call["tool"] == "ansa_model_save" for call in fake.calls))

            result = await MapBlockJobClient(fake, poll_interval=0, wait_timeout_seconds=0).resume(
                manifest_path, expected_target=TARGET, expected_model=MODEL,
            )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(fake.apply_count, 1)
            self.assertTrue(any(call["tool"] == "ansa_task_query" for call in fake.calls))
            self.assertEqual(sum(call["tool"] == "ansa_model_save" for call in fake.calls), 1)

    async def test_submission_timeout_queries_the_original_operation_id(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP(apply_mode="timeout")
            result, _, _ = await self._run_job(temp, fake, groups=((10,),))

            self.assertEqual(result["status"], "complete")
            self.assertEqual(fake.apply_count, 1)
            apply_call = next(call for call in fake.calls
                              if call["tool"] == "ansa_mesh_map_block_fill" and call["args"]["action"] == "apply")
            query = next(call for call in fake.calls if call["tool"] == "ansa_task_query")
            self.assertEqual(query["args"], {"operation_id": apply_call["args"]["operation_id"]})

    async def test_queued_original_task_is_queried_without_resubmitting_apply(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP(apply_mode="queued")
            result, manifest_path, _ = await self._run_job(temp, fake, groups=((10,),))
            self.assertEqual(result["status"], "awaiting_original_result")
            self.assertEqual(fake.apply_count, 1)

            result = await MapBlockJobClient(fake, poll_interval=0, wait_timeout_seconds=0).resume(
                manifest_path, expected_target=TARGET, expected_model=MODEL,
            )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(fake.apply_count, 1)
            apply_call = next(call for call in fake.calls
                              if call["tool"] == "ansa_mesh_map_block_fill" and call["args"]["action"] == "apply")
            query = next(call for call in fake.calls if call["tool"] == "ansa_task_query")
            self.assertEqual(set(query["args"]), {"task_id"})
            self.assertEqual(fake.tasks[query["args"]["task_id"]]["operation_id"],
                             apply_call["args"]["operation_id"])

    async def test_unknown_failed_and_missing_task_id_never_replay_apply(self):
        for mode in ("unknown", "failed", "no_task_id"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                fake = FakeMCP(apply_mode=mode)
                result, _, _ = await self._run_job(temp, fake, groups=((10,),))
                self.assertEqual(result["status"], "needs_attention")
                self.assertEqual(fake.apply_count, 1)
                self.assertEqual(sum(
                    call["tool"] == "ansa_mesh_map_block_fill" and call["args"]["action"] == "apply"
                    for call in fake.calls
                ), 1)

    async def test_resume_refuses_a_different_model_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP()
            result, manifest_path, _ = await self._run_job(temp, fake, groups=((10,),))
            self.assertEqual(result["status"], "complete")
            call_count = len(fake.calls)
            mismatches = [
                (TARGET, {"epoch": MODEL["epoch"] + 1}),
                ({**TARGET, "instance_id": "other-instance"}, MODEL),
            ]
            for target, model in mismatches:
                with self.assertRaisesRegex(ValueError, "target/model differs"):
                    await MapBlockJobClient(fake).resume(
                        manifest_path, expected_target=target, expected_model=model,
                    )
            self.assertEqual(len(fake.calls), call_count)

    async def test_quality_unknown_does_not_mark_job_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeMCP(quality_unknown=True)
            result, manifest_path, _ = await self._run_job(temp, fake, groups=((10,),))

            self.assertEqual(result["status"], "needs_attention")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotEqual(manifest["status"], "complete")
            self.assertIsNone(manifest["quality_summary"])
            self.assertEqual(fake.apply_count, 1)


if __name__ == "__main__":
    unittest.main()
