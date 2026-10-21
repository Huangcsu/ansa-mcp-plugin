"""Four native policies on owned ABAQUS/LS-DYNA shell/solid fixtures.

Requires a stage_three isolated translator and exact live PID/program arguments.
Direct native adapter verification, not an MCP-to-GUI or solver acceptance.
"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(Path(__file__).resolve().parent)]


def remote_case(pid, arguments, project, directory, family, analysis, require_native_fix=False, verify_gui=False):
    import importlib
    import os
    from pathlib import Path
    import re
    import sys
    import threading
    from ansa import base, constants, session
    if (os.getpid() != pid or list(session.ProgramArguments()) != arguments
            or threading.current_thread() is not threading.main_thread()
            or not any("stage_three_isolated_transl.py" in str(a) for a in arguments)):
        raise RuntimeError("owned native acceptance identity mismatch")
    sys.path.insert(0, str(Path(project)/"src"))
    import ansa_mcp_shared.quality_templates as templates
    import ansa_mcp_bridge.operations.quality_workflow as quality
    import ansa_mcp_bridge.operations.native_quality as native
    for module in (templates, quality, native):
        importlib.reload(module)
    class State:
        def assert_main(self):
            if threading.current_thread() is not threading.main_thread():
                raise RuntimeError("not main thread")
    state = State()
    session.New("discard")
    deck = constants.ABAQUS if analysis == "linear" else constants.LSDYNA
    base.SetCurrentDeck(deck)
    template = analysis + "_" + family
    node_ids, coordinates = {}, {}
    for z in range(1 if family == "shell" else 3):
        for y in range(3):
            for x in range(3):
                nid = len(coordinates) + 1
                is_center = (x,y,z) == ((1,1,0) if family == "shell" else (1,1,1))
                point = ((.6,.7,0.) if family == "shell" else (.65,.7,.6)) if is_center else (float(x),float(y),float(z))
                node_ids[x,y,z] = nid
                coordinates[nid] = point
    connections = []
    for z in range(1 if family == "shell" else 2):
        for y in range(2):
            for x in range(2):
                conn = [(x,y,z),(x+1,y,z),(x+1,y+1,z),(x,y+1,z)]
                if family == "solid":
                    conn += [(i,j,z+1) for i,j,_ in conn]
                connections.append([node_ids[k] for k in conn])
    scope_count = len(connections)
    if verify_gui:
        outside = []
        for x, y, z in ([(0,0,0),(1,0,0),(1,1,0),(0,1,0)] if family == "shell" else
                        [(0,0,0),(1,0,0),(1,1,0),(0,1,0),(0,0,1),(1,0,1),(1,1,1),(0,1,1)]):
            nid = len(coordinates) + 1
            coordinates[nid] = (float(x + 4), float(y), float(z))
            outside.append(nid)
        connections.append(outside)
    node_lines = [",".join(map(str, (nid,*point))) for nid,point in coordinates.items()]
    if analysis == "linear":
        element_type = "S4R" if family == "shell" else "C3D8R"
        text = ["*HEADING", "quality template fixture", "*NODE", *node_lines,
                "*ELEMENT, TYPE="+element_type+", ELSET=E"]
        text += [",".join(map(str,(i,*conn))) for i,conn in enumerate(connections,1)]
        text += ["*MATERIAL, NAME=M", "*ELASTIC", "210000.,0.3", "*DENSITY", "7.8e-9"]
        text += ["*SHELL SECTION, ELSET=E, MATERIAL=M", "1.0"] if family == "shell" else ["*SOLID SECTION, ELSET=E, MATERIAL=M", ","]
        source = Path(directory)/(template+".inp")
        source.write_text("\n".join(text)+"\n", encoding="ascii")
        code = base.InputAbaqus(str(source), model_action="overwrite_model", nodes_id="nooffset", elements_id="nooffset")
    else:
        element_type = "ELFORM=2" if family == "shell" else "ELFORM=1"
        text = ["*KEYWORD", "*TITLE", "quality template fixture", "*NODE", *node_lines,
                "*ELEMENT_SHELL" if family == "shell" else "*ELEMENT_SOLID"]
        text += [",".join(map(str,(i,1,*conn))) for i,conn in enumerate(connections,1)]
        text += ["*PART", "quality part", "1,1,1"]
        text += ["*SECTION_SHELL", "1,2,0.833333,5", "1.,1.,1.,1."] if family == "shell" else ["*SECTION_SOLID", "1,1"]
        text += ["*MAT_ELASTIC", "1,7.8e-9,210000.,0.3", "*END"]
        source = Path(directory)/(template+".k")
        source.write_text("\n".join(text)+"\n", encoding="ascii")
        code = base.InputLSDyna(str(source), model_action="overwrite_model", nodes_id="nooffset", elements_id="nooffset")
    if code != 1 or base.CurrentDeck() != deck:
        raise RuntimeError("fixture input failed or switched deck unexpectedly")
    all_ids = [e._id for e in base.CollectEntities(deck,None,quality._element_kind(constants,deck,family))]
    if sorted(all_ids) != list(range(1,len(connections)+1)):
        raise RuntimeError("fixture element IDs did not match solver file")
    ids = list(range(1, scope_count + 1))
    center_id = node_ids[(1,1,0) if family == "shell" else (1,1,1)]
    base.SaveAs(str(Path(directory)/(template+"-before.ansa")))
    original = quality._criteria(base)
    (Path(directory)/(template+"-original.ansa_qual")).write_text(original, encoding="utf-8")
    required = set(templates.get_template(template)["required_criteria"])
    family_key = "shells" if family == "shell" else "solids"
    limits = {"aspect ratio":1.3,"jacobian":.65,"warping":180.,"skewness":90.,"collapse":.01,
              "characteristic length":.5,"min angle quads":10.,"max angle quads":160.,
              "min angle trias":5.,"max angle trias":170.,"min angle tetras":5.,"max angle tetras":170.,
              "min angle pentas":10.,"max angle pentas":160.,"min angle hexas":10.,"max angle hexas":160.}
    lines = []
    for line in original.splitlines():
        match = native._ROW.match(line)
        if match:
            name,which,_,_ = match.groups()
            line = re.sub(r"=\s*(?:ON|OFF),", "= OFF,", line, count=1)
            if which == family_key and name in required:
                line = line.replace("= OFF,","= ON,",1)
                if name in limits:
                    parts = line.split("|",2)
                    line = parts[0]+"|"+parts[1]+"| "+" ".join(str(limits[name])+", -1./" for _ in range(4))
        lines.append(line)
    criteria_text = "\n".join(lines)
    (Path(directory)/(template+".ansa_qual")).write_text(criteria_text,encoding="utf-8")
    profile = {"template":template,"quality_criteria":criteria_text,
               "criteria_label":"synthetic_acceptance_only","formulation":element_type}
    if analysis == "explicit_crash":
        profile["target_time_step"] = 1.e-10
    params = {"deck":deck,"entity_ids":ids,"profile":profile}
    before = quality.execute(state,"quality_report",params)
    result = {"template":template,"deck":deck,"before":before,"main_thread":True,
              "pid":os.getpid(),"gui_verified":False,"solver_verified":False}
    if not before["complete"] or not before["failed_entity_ids"]:
        result["error"] = "fixture did not produce complete failing native checks"
        return result
    snapshot = quality._fe_snapshot(base,constants,deck)
    if verify_gui:
        located = quality.execute(state, "quality_locate", dict(
            params, expected_scope_sha256=before["scope_sha256"]))
        if (not located["display_changed"] or located["located_entity_ids"] != before["failed_entity_ids"]
                or quality._fe_snapshot(base, constants, deck) != snapshot):
            raise RuntimeError("native GUI location or unchanged FE readback failed")
        result["gui_location"] = located
        result["gui_location_readback_verified"] = True
        result["out_of_scope_element_ids"] = sorted(set(all_ids) - set(ids))
        from ansa import utils
        screenshot = Path(directory) / (template + "-located.png")
        if utils.SnapShot(str(screenshot), image_format="PNG", image_size=(1200, 800)) != 0 or not screenshot.is_file():
            raise RuntimeError("native GUI location snapshot failed")
        result["gui_location_snapshot"] = str(screenshot)
        # Follow the native Checks Manager grouping without changing check scope.
        groups = before["violation_summary"]
        result["criterion_locations"] = []
        for group in groups:
            criterion = group["criterion"]
            expected = sorted(row["id"] for row in before["rows"] if criterion in row["violations"])
            filtered = quality.execute(state, "quality_locate", dict(
                params, expected_scope_sha256=before["scope_sha256"], criterion=criterion))
            if (filtered["located_entity_ids"] != expected or len(expected) != group["failed_count"]
                    or quality._fe_snapshot(base, constants, deck) != snapshot):
                raise RuntimeError("criterion location or full FE preservation failed")
            result["criterion_locations"].append({"criterion": criterion, "entity_ids": expected,
                                                  "display_verified": filtered["display_changed"]})
        if groups:
            filtered_image = Path(directory) / (template + "-criterion-located.png")
            if utils.SnapShot(str(filtered_image), image_format="PNG", image_size=(1200, 800)) != 0:
                raise RuntimeError("filtered criterion snapshot failed")
            result["criterion_location_snapshot"] = str(filtered_image)
    if quality.execute(state,"quality_report",params) != before:
        raise RuntimeError("native report did not repeat exactly")
    inactive_text = re.sub(r"=\s*(?:ON|OFF),", "= OFF,", criteria_text)
    incomplete_params = dict(params, profile=dict(profile, quality_criteria=inactive_text))
    incomplete = quality.execute(state,"quality_report",incomplete_params)
    if incomplete["complete"] or incomplete["passed"] or not incomplete["criteria_snapshot"]["missing_criteria"]:
        raise RuntimeError("disabled required native checks were incorrectly accepted")
    try:
        quality.execute(state,"quality_repair",dict(incomplete_params,
            expected_scope_sha256=incomplete["scope_sha256"],
            backup_path=str(Path(directory)/(template+"-must-not-write.ansa")),
            movable_node_ids=[center_id],maximum_displacement=1.))
    except ValueError as exc:
        if "unknown or unsupported quality metrics" not in str(exc):
            raise
    else:
        raise RuntimeError("repair accepted an incomplete native template")
    if (Path(directory)/(template+"-must-not-write.ansa")).exists() or quality._fe_snapshot(base,constants,deck) != snapshot:
        raise RuntimeError("incomplete template rejection wrote a backup or changed mesh")
    result["incomplete_template_rejected"] = True
    if analysis == "explicit_crash":
        time_limit = max(r["metrics"]["CRASH"] for r in before["rows"])*2.
        dt_report = quality.execute(state,"quality_report",dict(params,profile=dict(profile,target_time_step=time_limit)))
        if not all("crash time step" in row["violations"] for row in dt_report["rows"]):
            raise RuntimeError("native timestep threshold did not detect the expected critical elements")
        result["native_timestep_threshold_verified"] = True
    repair_params = dict(params, expected_scope_sha256=before["scope_sha256"],
                         backup_path=str(Path(directory)/(template+"-fix-backup.ansa")),
                         movable_node_ids=[center_id],maximum_displacement=1.)
    fix_result = quality.execute(state,"quality_repair",repair_params)
    result["native_fix_probe"] = fix_result
    if require_native_fix and not (
            fix_result.get("ok") and fix_result.get("applied") and fix_result.get("improved")
            and fix_result.get("criteria_synchronized") and fix_result.get("criteria_restored")):
        result["error"] = "native Fix Quality did not improve and restore verified criteria"
        return result
    if fix_result.get("recovery_required"):
        result["error"] = "native fix requires model recovery"
        return result
    if fix_result["ok"] and fix_result["applied"]:
        repaired = fix_result
    else:
        if quality._fe_snapshot(base,constants,deck) != snapshot:
            raise RuntimeError("unapplied native fix did not preserve FE state")
        current = quality.execute(state,"quality_report",params)
        repair_params.update(expected_scope_sha256=current["scope_sha256"],method="smooth",
                             backup_path=str(Path(directory)/(template+"-smooth-backup.ansa")))
        repaired = quality.execute(state,"quality_repair",repair_params)
    result["repair"] = repaired
    if not repaired["ok"] or not repaired["applied"] or not repaired["improved"]:
        result["error"] = "native-rule repair was not verified"
        return result
    after = quality.execute(state,"quality_report",params)
    expected_outcome = "fully_repaired" if after["passed"] else "partially_repaired"
    if repaired["quality_passed"] != after["passed"] or repaired["repair_outcome"] != expected_outcome:
        raise RuntimeError("repair outcome misrepresents remaining quality failures")
    if repaired["after"] != after or quality._criteria(base) != original:
        raise RuntimeError("independent native repair readback mismatch")
    saved = Path(directory)/(template+"-after.ansa")
    full_after = quality._fe_snapshot(base,constants,deck)
    base.SaveAs(str(saved))
    base.Open(str(saved))
    if quality._fe_snapshot(base,constants,deck) != full_after or quality.execute(state,"quality_report",params) != after:
        raise RuntimeError("native template save/reopen readback mismatch")
    result.update(reopened_verified=True, saved_path=str(saved), passed=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity",type=Path,required=True)
    parser.add_argument("--run-dir",type=Path,required=True)
    parser.add_argument("--port",type=int,default=50051)
    parser.add_argument("--require-native-fix", action="store_true",
                        help="原生 Fix Quality 必须实际改善并恢复准则，不用平滑补验")
    parser.add_argument("--verify-gui-location", action="store_true",
                        help="在独立 GUI 修复前定位失败单元并核对可见集合；画布另行观察")
    args = parser.parse_args()
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    args.run_dir.mkdir(parents=True,exist_ok=False)
    import grpc
    import stage_one_ansa as one, stage_two_ansa as two, stage_three_ansa as stage
    stage._bounded_grpc(grpc,one.DeadlineInterceptor)
    basic = two.rpc(args.port,one.remote_basic)
    if any(basic[k] != identity[k] for k in ("pid","program_args")) or not basic["main_thread"]:
        raise RuntimeError("owned process changed")
    results=[]
    for analysis in ("linear","explicit_crash"):
        for family in ("shell","solid"):
            result = two.rpc(args.port,remote_case,identity["pid"],identity["program_args"],
                             str(ROOT),str(args.run_dir.resolve()),family,analysis,
                             args.require_native_fix, args.verify_gui_location)
            results.append(result)
            (args.run_dir/(analysis+"_"+family+".json")).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps({"template":result["template"],"deck":result["deck"],"passed":result.get("passed",False),
                              "complete":result["before"]["complete"],"before_failed":len(result["before"]["failed_entity_ids"]),
                              "repair_reason":result.get("repair",{}).get("reason"),"error":result.get("error")},ensure_ascii=False),flush=True)
            if result.get("native_fix_probe", {}).get("recovery_required") or result.get("repair", {}).get("recovery_required"):
                raise RuntimeError("owned acceptance model or criteria need recovery; refusing the next fixture")
    passed=all(r.get("passed") for r in results)
    (args.run_dir/"summary.json").write_text(json.dumps({"passed":passed,"cases":results},ensure_ascii=False,indent=2),encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
