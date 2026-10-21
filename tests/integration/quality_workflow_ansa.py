"""在明确归属的独立 ANSA 进程验证壳/实体质量；不连接用户的工作模型。

identity 必须来自 stage_three_ansa.py 独立启动的 remote_basic 回读，包含
pid 和完整 program_args。只接受独立 translator 标识；每次写操作前在主线程
再次核对归属。此入口覆盖原生 API 与保存重开，不代表 GUI 或求解器验收。
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]


def remote_case(pid, args, project, directory, family, analysis):
    import importlib
    import os
    from pathlib import Path
    import sys
    import threading
    from ansa import base, constants, session

    if (os.getpid() != pid or list(session.ProgramArguments()) != args
            or threading.current_thread() is not threading.main_thread()):
        raise RuntimeError("owned ANSA main-thread identity mismatch")
    if not any("stage_three_isolated_transl.py" in str(a) for a in args):
        raise RuntimeError("not an isolated quality acceptance process")
    sys.path.insert(0, str(Path(project) / "src"))
    import ansa_mcp_shared.mesh_quality as profiles
    import ansa_mcp_bridge.operations.quality_workflow as quality
    importlib.reload(profiles)
    importlib.reload(quality)

    class State:
        def assert_main(self):
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError("not the ANSA main thread")

    state = State()
    session.New("discard")
    base.SetCurrentDeck(constants.NASTRAN)
    deck = base.CurrentDeck()
    material = base.CreateEntity(deck, "MAT1", {
        "DEFINED": "YES", "E": 210000., "NU": .3, "RHO": 7.8e-9})
    if family == "shell":
        prop = base.CreateEntity(deck, "PSHELL", {
            "MID1": material._id, "MID2": material._id,
            "MID3": material._id, "T": 1., "NSM": 0.})
    else:
        prop = base.CreateEntity(deck, "PSOLID", {"MID": material._id})
    nodes = {}
    for z in range(1 if family == "shell" else 3):
        for y in range(3):
            for x in range(3):
                center = (x, y, z) == ((1, 1, 0) if family == "shell" else (1, 1, 1))
                point = ((.6, .7, 0.) if family == "shell" else (.65, .7, .6)) if center else (float(x), float(y), float(z))
                node = base.CreateEntity(deck, "GRID", dict(zip(("X1", "X2", "X3"), point)))
                if node is None:
                    raise RuntimeError("fixture node creation failed")
                nodes[x, y, z] = node
    elements = []
    for z in range(1 if family == "shell" else 2):
        for y in range(2):
            for x in range(2):
                conn = [(x,y,z), (x+1,y,z), (x+1,y+1,z), (x,y+1,z)]
                if family == "solid":
                    conn += [(i,j,z+1) for i,j,_ in conn]
                fields = {"type": "CQUAD4" if family == "shell" else "CHEXA", "PID": prop._id}
                fields.update({"G"+str(i+1): nodes[n]._id for i,n in enumerate(conn)})
                entity = base.CreateEntity(deck, family.upper(), fields)
                if entity is None:
                    raise RuntimeError("fixture element creation failed")
                elements.append(entity)
    ids = [e._id for e in elements]
    center = nodes[(1, 1, 0) if family == "shell" else (1, 1, 1)]
    stem = family + "-" + analysis
    base.SaveAs(str(Path(directory) / (stem + "-before.ansa")))
    profile = {"analysis_type": analysis, "element_family": family,
               "criteria": [{"name": "ASPECT", "max": 1.3}, {"name": "JACOB", "min": .65}]}
    if analysis == "explicit_crash":
        profile.update(minimum_length=.85, wave_speed=5.e6, target_time_step=1.e-7)
    params = {"deck": deck, "entity_ids": ids, "profile": profile}
    before = quality.execute(state, "quality_report", params)
    if not before["complete"] or not before["failed_entity_ids"]:
        raise RuntimeError("fixture did not produce known failing quality metrics")
    snapshot = quality._fe_snapshot(base, constants, deck)
    if quality.execute(state, "quality_report", params) != before:
        raise RuntimeError("report is not repeatable")
    if quality._fe_snapshot(base, constants, deck) != snapshot:
        raise RuntimeError("report modified the FE model")
    from ansa import guitk
    location_rejected = False
    if not guitk.BCApplicationIsGuiMode():
        try:
            quality.execute(state, "quality_locate", dict(params, expected_scope_sha256=before["scope_sha256"]))
        except quality.inspection.CapabilityUnavailableError as exc:
            if "requires an ANSA GUI session" not in str(exc):
                raise
            location_rejected = True
        else:
            raise RuntimeError("nogui location was incorrectly reported as displayed")
    repair_params = dict(params, expected_scope_sha256=before["scope_sha256"],
                         backup_path=str(Path(directory)/(stem+"-backup.ansa")),
                         movable_node_ids=[center._id], maximum_displacement=1.0,
                         method="smooth")
    rollback_probe = None
    if analysis == "linear":
        limited = dict(repair_params, maximum_displacement=1.e-6,
                       backup_path=str(Path(directory)/(stem+"-limit-backup.ansa")))
        rollback_probe = quality.execute(state, "quality_repair", limited)
        if (rollback_probe["ok"] or not rollback_probe.get("coordinates_rolled_back")
                or rollback_probe.get("recovery_required")
                or quality._fe_snapshot(base, constants, deck) != snapshot):
            raise RuntimeError("over-displacement repair did not restore the exact FE state")
        try:
            quality.execute(state, "quality_repair", dict(repair_params, expected_scope_sha256="0"*64))
        except ValueError as exc:
            if "scope changed" not in str(exc):
                raise
        else:
            raise RuntimeError("stale quality report was not rejected")
    repaired = quality.execute(state, "quality_repair", repair_params)
    result = {"family": family, "analysis_type": analysis, "before": before,
              "repair": repaired, "pid": os.getpid(), "main_thread": True,
              "gui_verified": False, "solver_verified": False}
    result["location_rejected_in_nogui"] = location_rejected
    if rollback_probe is not None:
        result["rollback_probe"] = rollback_probe
    if not repaired["ok"] or not repaired["applied"] or not repaired["improved"]:
        return result
    after = quality.execute(state, "quality_report", params)
    if after != repaired["after"] or repaired["changed_node_ids"] != [center._id]:
        raise RuntimeError("independent repair readback mismatch")
    full_after = quality._fe_snapshot(base, constants, deck)
    saved = Path(directory)/(stem+"-after.ansa")
    base.SaveAs(str(saved))
    base.Open(str(saved))
    reopened = quality.execute(state, "quality_report", params)
    if quality._fe_snapshot(base, constants, deck) != full_after or reopened != after:
        raise RuntimeError("saved/reopened model does not match repair readback")
    result["reopened_verified"] = True
    result["saved_path"] = str(saved)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--port", type=int, default=50051)
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    args.run_dir.mkdir(parents=True, exist_ok=False)
    import grpc
    import stage_one_ansa as one
    import stage_two_ansa as two
    import stage_three_ansa as stage
    stage._bounded_grpc(grpc, one.DeadlineInterceptor)
    basic = two.rpc(args.port, one.remote_basic)
    if basic != identity or not basic["main_thread"]:
        raise RuntimeError("independent identity no longer matches the live process")
    results = []
    for family in ("shell", "solid"):
        for analysis in ("linear", "explicit_crash"):
            result = two.rpc(args.port, remote_case, identity["pid"], identity["program_args"],
                             str(ROOT), str(args.run_dir.resolve()), family, analysis)
            (args.run_dir/(family+"-"+analysis+".json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append(result)
            print(json.dumps({"family": family, "analysis_type": analysis,
                              "repair_ok": result["repair"]["ok"],
                              "applied": result["repair"]["applied"],
                              "error": result["repair"].get("error"),
                              "before_failed": len(result["before"]["failed_entity_ids"]),
                              "after_failed": len(result["repair"].get("after", {}).get("failed_entity_ids", [])),
                              "reopened_verified": result.get("reopened_verified", False)}, ensure_ascii=False))
    passed = all(r["repair"]["ok"] and r["repair"]["applied"] and r.get("reopened_verified") for r in results)
    (args.run_dir/"summary.json").write_text(json.dumps({"passed": passed, "cases": results},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
