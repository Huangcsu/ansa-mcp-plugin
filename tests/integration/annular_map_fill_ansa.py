"""独立 ANSA 实例中的公开 MCP 孔环 MapBlock 填充验收入口。"""
from __future__ import annotations

import argparse, asyncio, hashlib, json, os, shutil, socket, subprocess, sys, traceback, uuid
from pathlib import Path

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


def _options(raw: str) -> dict:
    if not raw.lstrip().startswith("{"):
        raw = Path(raw).read_text(encoding="utf-8")
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {"volume_ids", "annular_options"}:
        raise ValueError("--options 须包含 volume_ids 和 annular_options")
    ids, annular = value["volume_ids"], value["annular_options"]
    if type(ids) is not list or len(ids) != 4 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != 4:
        raise ValueError("volume_ids 须为四个不同的正整数")
    required = {"source_face_ids", "center", "axis", "inner_radius", "outer_radius", "distance", "max_chord_error"}
    if type(annular) is not dict or set(annular) != required:
        raise ValueError("annular_options 字段不符合公开填充工具契约")
    faces = annular["source_face_ids"]
    if type(faces) is not list or len(faces) != 4 or any(type(i) is not int or i <= 0 for i in faces) or len(set(faces)) != 4:
        raise ValueError("source_face_ids 须为四个不同的正整数")
    return value


def _readback_script(capture_path: Path | None = None, include_mesh: bool = True) -> str:
    code = """import hashlib,json
from pathlib import Path
from ansa import base,constants
from ansa_mcp_bridge.operations import geometry_features,structured_native
d=base.CurrentDeck()
shells=base.CollectEntities(d,None,'SHELL')
if type(shells) not in (list,tuple): raise RuntimeError('SHELL collection is incomplete')
shell_ids=sorted(e._id for e in shells)
elements=structured_native._read_elements(base,d,'SHELL',shell_ids,{'CQUAD4','CTRIA3'})
grids=structured_native._read_grids(base,d,{n for row in elements for n in row['nodes']})
surface_sha=geometry_features._hash({'elements':elements,'nodes':grids})
mesh=None; solid_ids=[]
if CAPTURE_MESH:
 solids=base.CollectEntities(d,None,'SOLID')
 if type(solids) not in (list,tuple): raise RuntimeError('SOLID collection is incomplete')
 solid_ids=sorted(e._id for e in solids)
 if solid_ids: mesh=structured_native.capture_mesh(base,constants,d,solid_ids)
capture={'surface':{'shell_ids':shell_ids,'shell_count':len(shell_ids),'grid_count':len(grids),'sha256':surface_sha},'mesh':mesh}
raw=json.dumps(capture,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
capture_path=CAPTURE_PATH
if capture_path:
 with Path(capture_path).open('xb') as stream: stream.write(raw)
RESULT={'surface_sha256':surface_sha,'shell_ids':shell_ids,'shell_count':len(shell_ids),'grid_count':len(grids),
 'mesh_sha256':None if mesh is None else mesh['snapshot_sha256'],'solid_ids':solid_ids,
 'solid_types':[] if mesh is None else [[r['id'],r['type']] for r in mesh['elements']],
 'solid_owners':[] if mesh is None else [[r['id'],r['block_id']] for r in mesh['elements']],
 'element_types':{} if mesh is None else mesh['integrity']['element_types'],
 'ownership':None if mesh is None else mesh['ownership'],'capture_sha256':hashlib.sha256(raw).hexdigest()}
"""
    return code.replace("CAPTURE_MESH", repr(include_mesh)).replace(
        "CAPTURE_PATH", repr(str(capture_path) if capture_path is not None else None))


async def _verify(run: Path, cfg: dict, identity: dict, helpers: tuple, report: dict,
                  input_model: Path, options: dict) -> None:
    async with harness(run, cfg, identity, helpers, report) as h:
        async def task(name: str, fields: dict, timeout: int = 600) -> dict:
            op = "annular-fill-public-" + uuid.uuid4().hex
            report["pending"] = {"tool": name, "operation_id": op}; stage._json_write(run / "report.json", report)
            submitted = await h.call(name, {"operation_id": op, "expected_target": h.target,
                "expected_model": h.model, **fields}, allow_error=True)
            task_id = submitted.get("task_id")
            if not task_id: raise RuntimeError(f"{name} did not return a task ID: {submitted}")
            report["pending"]["task_id"] = task_id; stage._json_write(run / "report.json", report)
            done = await h.wait_task(task_id, None, timeout)
            receipt = f"{name}-{len(report['calls'])}.json"; stage._json_write(run / receipt, done)
            report["calls"].append({"tool": name, "operation_id": op, "task_id": task_id,
                "status": done.get("status"), "receipt": receipt})
            if done.get("status") in {"succeeded", "failed", "cancelled", "expired"}: report["pending"] = None
            stage._json_write(run / "report.json", report)
            if done.get("status") != "succeeded": raise RuntimeError(f"{name} finished {done.get('status')}: {done.get('error')}")
            result = done.get("data", {}).get("result")
            if type(result) is not dict: raise RuntimeError(f"{name} returned no result object")
            return result

        async def script(code: str) -> dict:
            value = await task("ansa_script_execute", {"script": code, "validate": True})
            if value.get("result_paged") or type(value.get("result")) is not dict:
                raise RuntimeError("script result is paged or incomplete; do not replay it")
            return value["result"]

        await task("ansa_model_open", {"path": str(input_model), "preservation": "backup",
            "backup_path": str(run / "before-public-open.ansa")})
        await h.snapshot_context()
        before = await script(_readback_script(include_mesh=False))
        report["original_surface"] = {k: before[k] for k in ("shell_count", "grid_count", "surface_sha256")}
        fields = {"deck": 1, **options}
        preview = await task("ansa_mesh_map_block_fill", {"action": "plan", **fields})
        if not preview.get("report_id"): raise RuntimeError("MapBlock fill plan did not return an evidence report")
        page = await task("ansa_mesh_evidence_page", {"deck": 1, "report_id": preview["report_id"],
            "section": "plan_sha256", "offset": 0, "limit": 1})
        rows = page.get("rows")
        if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not str:
            raise RuntimeError("plan_sha256 evidence page did not return one digest")
        plan_sha = rows[0]
        if preview.get("plan_sha256") not in (None, plan_sha): raise RuntimeError("plan SHA-256 evidence differs")
        report["plan"] = {"report_id": preview["report_id"], "plan_sha256": plan_sha, "status": preview.get("status")}
        backup_path = run / "before-annular-map-fill-apply.ansa"
        applied = await task("ansa_mesh_map_block_fill", {"action": "apply", **fields,
            "expected_plan_sha256": plan_sha, "backup_path": str(backup_path), "ttl_seconds": 900}, 900)
        apply_receipt = run / "apply-result-receipt.json"; stage._json_write(apply_receipt, applied)
        summary_keys = ("ok", "scope_verified", "new_solid_ids", "solid_count", "solid_type_counts",
                        "mode", "provenance_mapping_complete")
        report["apply"] = {k: applied[k] for k in summary_keys if k in applied}
        report["apply"].update(result_paged=applied.get("result_paged", False),
                                report_id=applied.get("report_id"), receipt=str(apply_receipt))
        report["backup"] = {"path": str(backup_path), "exists": backup_path.is_file(),
            "size_bytes": backup_path.stat().st_size if backup_path.is_file() else 0}
        stage._json_write(run / "report.json", report)
        if applied.get("ok") is not True or applied.get("scope_verified") is not True:
            raise RuntimeError("annular native fill did not verify; inspect the original receipt before any new write")

        filled = await script(_readback_script())
        report["filled_mesh"] = {k: filled[k] for k in ("mesh_sha256", "solid_ids", "solid_types",
            "solid_owners", "element_types", "ownership")}
        report["surface_unchanged_after_fill"] = before["surface_sha256"] == filled["surface_sha256"] and before["shell_ids"] == filled["shell_ids"]
        report["solid_scope_count"] = len(filled["solid_ids"])
        solid_scope_complete = (0 < len(filled["solid_ids"]) <= 5000
            and len(set(filled["solid_ids"])) == len(filled["solid_ids"])
            and sum(filled["element_types"].values()) == len(filled["solid_ids"]))
        quality_completed = False
        if solid_scope_complete:
            profile = {"analysis_type": "linear", "element_family": "solid",
                "criteria": [{"name": "ASPECT", "max": 5.0}, {"name": "JACOB", "min": 0.2}]}
            try:
                quality = await task("ansa_mesh_quality_report", {"deck": 1, "entity_ids": filled["solid_ids"],
                    "profile": profile, "paged": True, "batch_size": 1000}, 900)
                summary = quality.get("report", quality).get("summary", {})
                report["quality"] = summary
                quality_completed = type(summary) is dict and summary.get("status") in {"passed", "failed"}
            except Exception as exc:
                if report.get("pending") is not None: raise
                report["quality_error"] = f"{type(exc).__name__}: {exc}"
        else: report["quality_error"] = "quality scope requires the complete bounded generated SOLID inventory"
        report["quality_call_completed"] = quality_completed
        report["quality_accepted"] = report.get("quality", {}).get("status") == "passed"
        saved = run / "public-annular-map-fill.ansa"
        await task("ansa_model_save", {"path": str(saved)})
        await task("ansa_model_open", {"path": str(saved), "preservation": "backup",
            "backup_path": str(run / "before-public-reopen.ansa")})
        await h.snapshot_context()
        capture_path = run / "saved-mesh-capture.json"
        reopened = await script(_readback_script(capture_path))
        report["saved_capture"] = {"path": str(capture_path), "sha256": reopened["capture_sha256"]}
        report["save_reopen"] = {"surface_exact": before["surface_sha256"] == reopened["surface_sha256"],
            "mesh_exact": filled["mesh_sha256"] == reopened["mesh_sha256"],
            "solid_ids_exact": filled["solid_ids"] == reopened["solid_ids"],
            "capture_checksum_exact": hashlib.sha256(capture_path.read_bytes()).hexdigest() == reopened["capture_sha256"]}
        report["complete"] = all((applied.get("ok") is True, applied.get("scope_verified") is True,
            report["backup"]["size_bytes"] > 0,
            report["surface_unchanged_after_fill"], solid_scope_complete,
            filled["ownership"]["complete"] is True, quality_completed,
            saved.is_file() and saved.stat().st_size > 0, all(report["save_reopen"].values())))
        report["quality_accepted"] = report.get("quality", {}).get("status") == "passed"
        report["acceptance_complete"] = report["complete"] and report["quality_accepted"]
        stage._json_write(run / "report.json", report)


def main(*, verify=None, run_prefix="annular-fill-public-", options_loader=None, options_help=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "退出码0表示功能流程complete；质量验收请同时检查报告中的"
        "quality_accepted和acceptance_complete。"))
    parser.add_argument("--ansa-root", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--options", required=True, help=options_help or "JSON 对象：volume_ids 与 annular_options")
    args = parser.parse_args()
    ansa_root, input_source, options = args.ansa_root.resolve(strict=True), args.input.resolve(strict=True), (options_loader or _options)(args.options)
    if not (ansa_root / "ansa64.bat").is_file() or not input_source.is_file(): raise ValueError("ANSA root 或 prepared input 无效")
    run = ROOT / "runtime" / "verification" / (run_prefix + uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    translator = run / "owned_translator.py"
    translator.write_text("# Unique translator for this public MCP acceptance.\n", encoding="utf-8")
    input_model = run / "input.ansa"; shutil.copy2(input_source, input_model)
    runtime_home = Path.home() / "ANSA-MCP-Plugin-Runtime" / ("a" + uuid.uuid4().hex[:8])
    port = _port(); instructions = {"port": port, "translator": str(translator), "runtime_home": str(runtime_home)}
    command = [str(ansa_root / "ansa64.bat"), "-grpcport", str(port), "-grpchost", "127.0.0.1",
        "-translfp", str(translator), "-nolauncher", "--skip-release-highlights", "--iconify"]
    report = {"complete": False, "quality_accepted": False, "acceptance_complete": False,
        "run": str(run), "runtime_home": str(runtime_home),
        "input_sha256": hashlib.sha256(input_model.read_bytes()).hexdigest(), "command": command,
        "calls": [], "pending": None}
    stage._json_write(run / "report.json", report)
    process = identity = helpers = None; log = (run / "ansa.log").open("wb")
    try:
        env = {**os.environ, "ANSA_MCP_SUM_HOME": str(run / "unused-legacy"),
            "ANSA_MCP_SUM_AUTO_POLL": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen(command, cwd=run, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        report["launcher_pid"] = process.pid; stage._json_write(run / "report.json", report)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}", options=[("grpc.enable_http_proxy", 0)])
        try: grpc.channel_ready_future(channel).result(timeout=100)
        finally: channel.close()
        identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(run, instructions)
        report["identity"] = identity
        asyncio.run((verify or _verify)(run, {"runtime_home": str(runtime_home), "translator": str(translator)},
                            identity, helpers, report, input_model, options))
    except BaseException: report["error"] = traceback.format_exc()
    finally:
        if (identity is not None and helpers is not None and report.get("pending") is None
                and not report.get("keep_owned_instance")):
            try: report["cleanup"] = stage._cleanup_owned_ansa(identity, instructions, helpers)
            except BaseException: report["cleanup_error"] = traceback.format_exc()
        else:
            report["cleanup_skipped"] = ("original MCP task state needs recovery" if report.get("pending") else
                "owned case retained for readback" if report.get("keep_owned_instance") else "owned identity unverified; process left untouched")
        if process is not None:
            try: process.wait(timeout=12)
            except subprocess.TimeoutExpired: report["launcher_still_running"] = True
        log.close(); stage._json_write(run / "report.json", report)
    print(json.dumps({k: report.get(k) for k in ("complete", "quality_accepted", "acceptance_complete",
        "quality", "cleanup_skipped", "error")}, ensure_ascii=False), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__": raise SystemExit(main())
