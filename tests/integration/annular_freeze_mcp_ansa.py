"""Owned ANSA public-MCP acceptance for the native frozen-source annular sweep."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "integration")]

import ansa
import grpc
import stage_three_ansa as stage
from map_block_batch_ansa import harness


def _port() -> int:
    for candidate in range(50051, 50151):
        with socket.socket() as guard:
            try:
                guard.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("no free owned ANSA gRPC port")


def _source_ids(path: Path) -> tuple[list[int], str]:
    value = json.loads(path.read_text(encoding="utf-8"))["result"]["freeze"]
    states = value["before"]
    if type(states) is not dict or not 1 <= len(states) <= 200 or set(states.values()) != {"NO"}:
        raise ValueError("source report is not a bounded unfrozen-source receipt")
    ids = sorted(int(key) for key in states)
    if any(ident <= 0 for ident in ids) or len(set(ids)) != len(ids):
        raise ValueError("source report contains invalid SHELL ids")
    digest = value["source_sha256_before"]
    if type(digest) is not str or len(digest) != 64:
        raise ValueError("source report has no source SHA-256")
    return ids, digest


async def _verify(run: Path, cfg: dict, identity: dict, helpers: tuple, report: dict,
                  input_model: Path, source_ids: list[int], source_sha256: str,
                  case: dict, property_id: int, prepare_options: dict | None = None) -> None:
    async with harness(run, cfg, identity, helpers, report) as h:
        async def task(name: str, fields: dict, *, timeout: int = 600) -> dict:
            operation_id = "annular-freeze-" + uuid.uuid4().hex
            report["pending"] = {"tool": name, "operation_id": operation_id}
            stage._json_write(run / "report.json", report)
            submitted = await h.call(name, {
                "operation_id": operation_id, "expected_target": h.target,
                "expected_model": h.model, **fields}, allow_error=True)
            task_id = submitted.get("task_id")
            if not task_id:
                raise RuntimeError(f"{name} did not return a task ID: {submitted}")
            report["pending"]["task_id"] = task_id
            stage._json_write(run / "report.json", report)
            done = await h.wait_task(task_id, None, timeout)
            filename = name + "-" + str(len(report["calls"])) + ".json"
            stage._json_write(run / filename, done)
            report["calls"].append({"tool": name, "operation_id": operation_id,
                                    "task_id": task_id, "status": done["status"],
                                    "receipt": filename})
            if done["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                report["pending"] = None
            stage._json_write(run / "report.json", report)
            if done["status"] != "succeeded":
                raise RuntimeError(f"{name} finished {done['status']}: {done.get('error')}")
            return done["data"]["result"]

        await task("ansa_model_open", {"path": str(input_model), "preservation": "backup",
                                       "backup_path": str(run / "before-public-open.ansa")})
        await h.snapshot_context()
        if prepare_options is not None:
            fields = {"deck": 1, "property_id": property_id, **prepare_options}
            preview = await task("ansa_mesh_annular_source_prepare", {"action": "plan", **fields})
            plan_sha256 = preview.get("plan_sha256")
            if plan_sha256 is None:
                page = await task("ansa_mesh_evidence_page", {
                    "deck": 1, "report_id": preview["report_id"],
                    "section": "plan_sha256", "offset": 0, "limit": 1})
                plan_sha256 = page["rows"][0]
            prepared = await task("ansa_mesh_annular_source_prepare", {
                "action": "apply", **fields, "expected_plan_sha256": plan_sha256,
                "backup_path": str(run / "before-annular-source-prepare.ansa"),
                "ttl_seconds": 900}, timeout=900)
            report["preparation"] = prepared
            stage._json_write(run / "report.json", report)
            if prepared.get("ok") is not True or prepared.get("scope_verified") is not True:
                raise RuntimeError("public native annular source preparation was not verified")
            source_code = (
                "from ansa import base,mesh,constants\n"
                "from ansa_mcp_bridge.operations import structured_boundary,structured_native\n"
                "d=base.CurrentDeck()\n"
                f"records,_=structured_boundary.capture_membership(base,mesh,constants,d,{prepare_options['source_face_ids']!r})\n"
                "assert all(row['membership_complete'] for row in records)\n"
                "ids=sorted({item for row in records for item in row['macro_shell_ids']})\n"
                "source=structured_native.capture_source(base,constants,d,ids)\n"
                "RESULT={'source_ids':ids,'source_sha256':source['snapshot_sha256']}\n")
            captured = await task("ansa_script_execute", {"script": source_code, "validate": True})
            if captured.get("result_paged"):
                raise RuntimeError("bounded prepared-source readback unexpectedly paged")
            source_ids = captured["result"]["source_ids"]
            source_sha256 = captured["result"]["source_sha256"]
            await task("ansa_model_save", {"path": str(run / "public-annular-source.ansa")})
        audit = await task("ansa_mesh_source_audit", {
            "deck": 1, "source_ids": source_ids, "direction": case["axis"],
            "layers": case["steps"], "target_length": case["distance"] / case["steps"],
            "annular_options": {"center": case["center"], "axis": case["axis"],
                                "inner_radius": case["inner_radius"], "outer_radius": case["outer_radius"],
                                "max_chord_error": 0.2, "tolerance": 0.001}})
        if not audit.get("report_id"):
            raise RuntimeError("public source audit did not create a report ID")
        page = await task("ansa_mesh_evidence_page", {
            "deck": 1, "report_id": audit["report_id"],
            "section": "source_snapshot_sha256", "offset": 0, "limit": 1})
        live_source = page["rows"][0]
        if live_source != source_sha256:
            raise RuntimeError("public source snapshot differs from the independently captured source")
        report["source"] = {"shell_count": len(source_ids), "sha256": live_source,
                            "audit_status": audit.get("status")}
        stage._json_write(run / "report.json", report)

        sweep = await task("ansa_mesh_sweep", {
            "deck": 1, "shell_ids": source_ids,
            "direction": case["axis"], "distance": case["distance"],
            "steps": case["steps"], "property_id": property_id,
            "expected_source_sha256": live_source,
            "backup_path": str(run / "before-public-freeze-sweep.ansa"),
            "freeze_source": True, "ttl_seconds": 900}, timeout=900)
        report["sweep"] = {"ok": sweep.get("ok"),
                            "scope_verified": sweep.get("scope_verified"),
                            "freeze_api_called": sweep.get("freeze", {}).get("api_called"),
                            "freeze_after_count": sum(value == "YES" for value in
                                                      sweep.get("freeze", {}).get("after_sweep", {}).values()),
                            "solid_count": len(sweep.get("new_solid_ids", [])),
                            "mesh_type_counts": sweep.get("new_mesh", {}).get("integrity", {}).get("element_types"),
                            "errors": sweep.get("errors")}
        stage._json_write(run / "report.json", report)
        if (sweep.get("ok") is not True or sweep.get("scope_verified") is not True
                or report["sweep"]["freeze_after_count"] != len(source_ids)):
            raise RuntimeError("public frozen-source sweep was not independently verified")

        ids_literal = repr(source_ids)
        readback_script = (
            "from ansa import base,constants\n"
            "from ansa_mcp_bridge.operations import structured_native\n"
            "d=base.CurrentDeck()\n"
            f"source_ids={ids_literal}\n"
            "source=structured_native.capture_source(base,constants,d,source_ids)\n"
            "solids=base.CollectEntities(d,None,'SOLID')\n"
            "solid_ids=sorted(item._id for item in solids)\n"
            "output=structured_native.capture_mesh(base,constants,d,solid_ids)\n"
            "frozen=[base.GetEntityCardValues(d,base.GetEntity(d,'SHELL',i),['FROZEN'])['FROZEN'] for i in source_ids]\n"
            "RESULT={'source_sha256':source['snapshot_sha256'],'mesh_sha256':output['snapshot_sha256'],"
            "'solid_ids':solid_ids,'solid_types':output['integrity']['element_types'],"
            "'frozen_all':all(value=='YES' for value in frozen)}\n"
        )

        async def readback(*, capture_path: Path | None = None) -> dict:
            code = readback_script
            if capture_path is not None:
                code += ("import json,hashlib\nfrom pathlib import Path\n"
                         "raw=json.dumps({'result':{'source':source,'output':output}},"
                         "sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')\n"
                         f"with Path({str(capture_path)!r}).open('xb') as stream: stream.write(raw)\n"
                         "RESULT['capture_sha256']=hashlib.sha256(raw).hexdigest()\n")
            value = await task("ansa_script_execute", {"script": code, "validate": True})
            if value.get("result_paged"):
                raise RuntimeError("bounded readback unexpectedly paged; inspect the original task")
            return value["result"]

        before_save = await readback()
        if before_save["source_sha256"] != live_source or not before_save["frozen_all"]:
            raise RuntimeError("post-sweep independent source readback failed")
        report["before_save"] = {key: value for key, value in before_save.items() if key != "solid_ids"}
        stage._json_write(run / "report.json", report)

        quality = await task("ansa_mesh_quality_report", {
            "deck": 1, "entity_ids": before_save["solid_ids"],
            "profile": {"analysis_type": "linear", "element_family": "solid",
                        "criteria": [{"name": "ASPECT", "max": 5.0},
                                     {"name": "JACOB", "min": 0.2}]}})
        summary = quality.get("report", quality).get("summary", {})
        report["quality"] = summary
        stage._json_write(run / "report.json", report)

        saved = run / "public-annular-solid.ansa"
        await task("ansa_model_save", {"path": str(saved)})
        await task("ansa_model_open", {"path": str(saved), "preservation": "backup",
                                       "backup_path": str(run / "before-public-reopen.ansa")})
        await h.snapshot_context()
        capture_path = run / "saved-mesh-capture.json"
        after_reopen = await readback(capture_path=capture_path)
        if hashlib.sha256(capture_path.read_bytes()).hexdigest() != after_reopen["capture_sha256"]:
            raise RuntimeError("saved independent mesh capture checksum differs")
        report["saved_capture"] = {"path": str(capture_path), "sha256": after_reopen["capture_sha256"]}
        report["save_reopen"] = {
            "source_exact": after_reopen["source_sha256"] == before_save["source_sha256"],
            "mesh_exact": after_reopen["mesh_sha256"] == before_save["mesh_sha256"],
            "solid_ids_exact": after_reopen["solid_ids"] == before_save["solid_ids"],
            "frozen_all": after_reopen["frozen_all"]}
        report["complete"] = bool(summary.get("status") == "passed"
                                  and all(report["save_reopen"].values()))
        stage._json_write(run / "report.json", report)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-report", type=Path)
    source_group.add_argument("--prepare-options", type=Path)
    parser.add_argument("--case-report", type=Path, required=True)
    parser.add_argument("--property-id", type=int, required=True)
    args = parser.parse_args()
    ansa_root = args.ansa_root.resolve(strict=True)
    input_source = args.input.resolve(strict=True)
    if not (ansa_root / "ansa64.bat").is_file() or args.property_id <= 0:
        raise ValueError("ANSA root or explicit property ID is invalid")
    prepare_options = None
    source_ids, source_sha256 = [], ""
    if args.source_report is not None:
        source_ids, source_sha256 = _source_ids(args.source_report.resolve(strict=True))
    else:
        prepare_options = json.loads(args.prepare_options.resolve(strict=True).read_text(encoding="utf-8"))
        reserved = {"action", "operation_id", "expected_target", "expected_model", "deck", "property_id",
                    "expected_plan_sha256", "backup_path", "ttl_seconds"}
        if type(prepare_options) is not dict or reserved & set(prepare_options) or not prepare_options.get("source_face_ids"):
            raise ValueError("preparation options must contain explicit source faces and no lifecycle/apply fields")
    case = json.loads(args.case_report.resolve(strict=True).read_text(encoding="utf-8"))["case_params"]

    run = ROOT / "runtime" / "verification" / ("annular-public-" + uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    translator = run / "owned_translator.py"
    translator.write_text("# Owned public frozen-source sweep acceptance.\n", encoding="utf-8")
    input_model = run / "input.ansa"
    input_model.write_bytes(input_source.read_bytes())
    runtime_home = Path.home() / "ANSA-MCP-Plugin-Runtime" / ("a" + uuid.uuid4().hex[:8])
    expected_ledger_path = (runtime_home / "instances" / ("i" * 36) / "sessions" /
                            ("s" * 36) / "structured-mesh" /
                            ("ledger-" + "0" * 64 + ".json"))
    if len(str(expected_ledger_path)) >= 240:
        raise ValueError("owned runtime path is too long for Windows ledger persistence")
    port = _port()
    instructions = {"port": port, "translator": str(translator), "runtime_home": str(runtime_home)}
    command = [str(ansa_root / "ansa64.bat"), "-grpcport", str(port), "-grpchost", "127.0.0.1",
               "-translfp", str(translator), "-nolauncher", "--skip-release-highlights", "--iconify"]
    report = {"complete": False, "run": str(run), "runtime_home": str(runtime_home),
              "input_sha256": hashlib.sha256(input_model.read_bytes()).hexdigest(),
              "command": command, "calls": [], "pending": None}
    stage._json_write(run / "report.json", report)
    process = None
    identity = None
    helpers = None
    log = (run / "ansa.log").open("wb")
    try:
        environment = {**os.environ, "ANSA_MCP_SUM_HOME": str(run / "unused-legacy"),
                       "ANSA_MCP_SUM_AUTO_POLL": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen(command, cwd=run, env=environment, stdout=log,
                                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        report["launcher_pid"] = process.pid
        stage._json_write(run / "report.json", report)
        channel = grpc.insecure_channel("127.0.0.1:" + str(port), options=[("grpc.enable_http_proxy", 0)])
        try:
            grpc.channel_ready_future(channel).result(timeout=100)
        finally:
            channel.close()
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        stage._json_write(run / "report.json", report)
        cfg = {"runtime_home": str(runtime_home), "translator": str(translator)}
        asyncio.run(_verify(run, cfg, identity, helpers, report, input_model,
                            source_ids, source_sha256, case, args.property_id, prepare_options))
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        if identity is not None and helpers is not None and report.get("pending") is None:
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except BaseException:
                report["cleanup_error"] = traceback.format_exc()
        elif identity is None and process is not None and process.poll() is None:
            process.terminate()
            report["unverified_launcher_terminated"] = True
        else:
            report["cleanup_skipped"] = "original MCP task state needs recovery"
        if process is not None:
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                report["launcher_still_running"] = True
        log.close()
        stage._json_write(run / "report.json", report)
    print(json.dumps({"complete": report["complete"], "run": str(run),
                      "sweep": report.get("sweep"), "quality": report.get("quality"),
                      "save_reopen": report.get("save_reopen"),
                      "error": report.get("error"), "cleanup_error": report.get("cleanup_error")},
                     ensure_ascii=False), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
