"""续验已有 r12 官方包实例，不重建夹具，不改写原部分报告。"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import grpc
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_four_native_install as native
import stage_three_ansa as core


def readback():
    from ansa import base, constants
    deck = constants.ABAQUS
    result = {"path": base.DataBaseName(), "deck": base.CurrentDeck()}
    for kind, fields, key in (
        ("NODE", ["X", "Y", "Z"], "nodes"),
        ("SHELL", ["type", "PID", "G1", "G2", "G3", "G4"], "shells"),
    ):
        values = []
        for entity in base.CollectEntities(deck, None, kind):
            if not set(fields).issubset(entity.card_fields(deck)):
                raise RuntimeError("Required card fields unavailable: " + kind)
            values.append({"id": entity._id, **base.GetEntityCardValues(deck, entity, fields)})
        result[key] = sorted(values, key=lambda item: item["id"])
    return result


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_stop_proof(info, snapshot):
    folder = (Path(info["runtime_home"]) / "instances" / snapshot["instance_id"]
              / "sessions" / snapshot["session_id"])
    proof_path = folder / "closed.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    identity = {key: snapshot[key] for key in ("pid", "process_start", "instance_id", "session_id")}
    if any(proof.get(key) != value for key, value in identity.items()) or proof.get("executor_stopped") is not True:
        raise AssertionError("Persistent stop proof identity or executor status mismatch")
    authorization_path = folder / "authorization.json"
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    if authorization.get("active") is not False:
        raise AssertionError("Prior session authorization is not durably revoked")
    return {"path": str(proof_path), "sha256": sha(proof_path), "proof": proof,
            "authorization_revoked": True}


async def verify(args):
    run = args.run_dir.resolve(strict=True)
    follow = run / args.followup_name
    follow.mkdir(exist_ok=True)
    report_path = follow / "report.json"
    if report_path.exists():
        raise FileExistsError("Use a new follow-up name; prior evidence must be preserved")
    info = json.loads((run / "launch_instructions.json").read_text(encoding="utf-8"))
    original_path = run / "native-install-report.json"
    original_sha = sha(original_path)
    original_report = json.loads(original_path.read_text(encoding="utf-8"))
    cases = {case["name"]: case for case in original_report["cases"] if case.get("ok") is True}
    expected_case = cases["same_window_save_reopen_independent_abaqus_readback"]
    expected = expected_case["readback"]
    expected_session = original_report["identity"]["session_id"]
    ownership = json.loads((run / "cim-ownership.json").read_text(encoding="utf-8"))
    if isinstance(ownership, list):
        if len(ownership) != 1:
            raise AssertionError("Original ownership is not unique")
        ownership = ownership[0]
    report = {"success": False, "scope": "r12_official_installed_followup",
              "original_report": str(original_path), "original_report_sha256": original_sha,
              "original_success": original_report["success"], "cases": [],
              "visual_acceptance": False, "solver_executed": False,
              "started_at": datetime.now(timezone.utc).isoformat()}
    first, second = core._import_runtime_helpers()
    original_channel = core._bounded_grpc(grpc, first.DeadlineInterceptor)
    default_before = sha(info["default_xml"])
    result_before = sha(expected["path"])

    def persist():
        core._json_write(report_path, report)

    def record(name, **evidence):
        report["cases"].append({"name": name, "ok": True, **evidence})
        persist()
        print(json.dumps({"case": name, "ok": True}), flush=True)

    def state():
        value = second.rpc(info["port"], native.remote_state)
        core._verify_program_arguments(value, Path(info["translator"]), info["port"])
        if value["pid"] != ownership["ProcessId"] or value["profile"] != info["settings_profile"]:
            raise AssertionError("ANSA PID/profile mismatch")
        if value["main_thread"] is not True:
            raise AssertionError("Readback did not run on ANSA main thread")
        archive = Path(info["archive"]).resolve()
        for origin in value["module_origins"].values():
            Path(origin).resolve().relative_to(archive)
        if not any(name.endswith(".app") for name in value["module_origins"]):
            raise AssertionError("Installed native app is missing")
        if value["bridge_present"] and Path(value["runtime_home"]).resolve() != Path(info["runtime_home"]).resolve():
            raise AssertionError("Wrong runtime")
        if any(entry and Path(entry).is_absolute() and Path(entry).resolve() == core.PROJECT_ROOT / "src"
               for entry in value["sys_path"]):
            raise AssertionError("Repository src was loaded into ANSA")
        return value

    def unchanged():
        actual = second.rpc(info["port"], readback)
        if actual != expected:
            raise AssertionError("Existing saved fixture differs from original independent readback")
        if sha(expected["path"]) != result_before:
            raise AssertionError("Saved model bytes changed")
        return actual

    async def operator(stage):
        observation = await native.operator(follow, stage)
        if observation.get("ok") is not True or observation.get("pid") != ownership["ProcessId"]:
            raise AssertionError("UI observation does not confirm owned ANSA")
        screenshot = Path(observation["screenshot"]).resolve(strict=True)
        screenshot.relative_to(follow)
        if screenshot.stat().st_size < 1000:
            raise AssertionError("UI evidence is empty")
        return observation

    async def bound(h):
        status = await h.call("ansa_connection_status")
        h.target = status["data"]["active_target"]
        if h.target is None:
            raise AssertionError("Panel did not bind target")
        snap = await h.snapshot_context()
        actual = state()
        if (snap["pid"] != ownership["ProcessId"] or h.target["session_id"] != actual["session_id"]
                or h.target["instance_id"] != actual["instance_id"]
                or not snap["ui"]["alive"] or not snap["ui"]["visible"]):
            raise AssertionError("Bound target and installed visible native panel differ")
        return actual, snap

    try:
        if sha(info["archive"]) != info["archive_sha256"]:
            raise AssertionError("Installed ZIP has changed")
        initial = state()
        if initial["closed"] or initial["session_id"] != expected_session:
            raise AssertionError("Original r12 bridge is no longer active; audit separately")
        record("same_r12_instance_and_saved_model_unchanged", state=initial, readback=unchanged(),
               saved_model_sha256=result_before)
        observation_path = follow / "reopened-observation.json"
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        screenshot = Path(observation["screenshot"]).resolve(strict=True)
        screenshot.relative_to(follow)
        if observation.get("ok") is not True or observation.get("pid") != initial["pid"] or screenshot.stat().st_size < 1000:
            raise AssertionError("Missing owned reopened graphics evidence")
        report["visual_acceptance"] = True
        record("owned_reopened_graphics", observation=observation, screenshot_sha256=sha(screenshot))
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        params = StdioServerParameters(command=info["external_python"],
            args=["-I", "-m", "ansa_mcp_plugin", "--runtime-home", info["runtime_home"]],
            cwd=str(follow), env=env)
        with (follow / "backend.stderr.log").open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=30)) as client:
                    await client.initialize()
                    h = core.McpHarness(client, report, follow)
                    schemas = await h.list_schema()
                    await operator("recover_and_bind")
                    recovered, snap = await bound(h)
                    record("installed_wheel_backend_recovery_and_panel_binding", state=recovered,
                           target=h.target, snapshot=snap, schemas=schemas)
                    stop_observation = await operator("stop_bridge")
                    stopped = state()
                    if stopped["bridge_present"] and (not stopped["closed"] or stopped["ui"].get("alive")):
                        raise AssertionError("Official Stop Bridge did not close bridge/panel")
                    record("official_stop_bridge", state=stopped, observation=stop_observation,
                           stop_proof=verify_stop_proof(info, snap), readback=unchanged())
                    await operator("restart_and_bind")
                    restarted, snap = await bound(h)
                    if restarted["session_id"] == initial["session_id"]:
                        raise AssertionError("Bridge restart reused old session")
                    record("official_restart_rebind_new_session", state=restarted, target=h.target,
                           snapshot=snap, readback=unchanged())
        if sha(info["default_xml"]) != default_before:
            raise AssertionError("Default profile changed during follow-up")
        if sha(info["archive"]) != info["archive_sha256"] or sha(original_path) != original_sha:
            raise AssertionError("Installed package or original report changed")
        record("artifacts_and_default_profile_unchanged_during_followup",
               default_sha256=default_before, archive_sha256=info["archive_sha256"],
               original_report_sha256=original_sha, saved_model_sha256=result_before)
        report["success"] = True
    except BaseException as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist()
        grpc.insecure_channel = original_channel
    print(json.dumps({"success": True, "cases": len(report["cases"]), "report": str(report_path)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--followup-name", default="followup-20261018")
    asyncio.run(verify(parser.parse_args()))
