"""Public-MCP repair of the five defects in the saved, finite annular fixture.

Scope selection and evidence collection are test scripts. Mesh improvement uses
the existing controlled quality tool and native SolidSmooth; no mesh is rebuilt.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "integration")]
import annular_map_fill_ansa as owned
import stage_three_ansa as stage
from map_block_batch_ansa import harness

MOVABLE = [879, 880]
SCOPE = [1191, 1192, 1193, 1200, 1201, 1202, 1230, 1231, 1232, 1248, 1249, 1250]
FAILED = [1230, 1231, 1232, 1249, 1250]
PROFILE = {"analysis_type": "linear", "element_family": "solid",
           "criteria": [{"name": "ASPECT", "max": 5.0}, {"name": "JACOB", "min": .2}]}


def remote_capture(path, volume_ids, movable, tolerance, audit=False):
    import builtins, copy, hashlib, json, threading
    from pathlib import Path
    from ansa import base, constants, mesh
    from ansa_mcp_bridge.operations import cad_map, geometry_features as f
    from ansa_mcp_bridge.operations import map_block_batch, map_block_inspection, meshing_context
    from ansa_mcp_bridge.operations import part_aggregates, structured_native as n
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("annular readback lost ANSA main thread")
    state = builtins._ansa_mcp_plugin_bridge.state
    deck = base.CurrentDeck()
    if deck != constants.NASTRAN:
        raise RuntimeError("finite repair fixture requires NASTRAN")
    block = map_block_inspection.capture(state, base, mesh, constants, deck, volume_ids)
    solid_ids = sorted(s._id for s in base.CollectEntities(deck, None, "SOLID"))
    output = n.capture_mesh(base, constants, deck, solid_ids)
    scope = cad_map._snapshot(state, base, deck)
    protected = {r["id"] for r in block["surface_mesh"]["nodes"]}
    coords = {r["id"]: r["xyz"] for r in output["nodes"]}
    if len(solid_ids) != 610 or len(protected) != 616 or set(movable) & protected or not set(movable) <= coords.keys():
        raise RuntimeError("fixture or movable nodes differ from the declared finite case")
    closure = sorted(e["id"] for e in output["elements"] if set(e["nodes"]) & set(movable))
    definitions = copy.deepcopy(scope)
    definitions.pop("grids")  # Full GRID cards are already present in entities.
    for row in definitions["entities"]:
        kind = row["entity"]["type"]
        names = ("X1", "X2", "X3") if kind == "GRID" else (
            *part_aggregates.MASS_FIELDS, "Last Edit") if kind == "ANSAPART" else ()
        for name in names:
            row["fields"].pop(name, None)
    settings = meshing_context.capture(state, base, mesh)
    boundaries = [{"volume_id": v["volume_id"], "face_ids": v["face_ids"], "caps": v["caps"]} for v in block["volumes"]]
    facts = {"mesh": output, "block": block, "scope": scope, "settings": settings,
             "derived_fields_excluded_from_definition_digest": [*part_aggregates.MASS_FIELDS, "Last Edit"]}
    if audit:
        owners = {}
        for volume in block["volumes"]:
            for face in volume["face_ids"]:
                owners.setdefault(face, []).append(volume["volume_id"])
        prepared = {"clean": {"deck": deck}, "proofs": [{"volume_id": i} for i in volume_ids],
                    "layout": {"face_owners": owners}, "block": block}
        facts["audit"] = map_block_batch._active_audit(state, base, mesh, constants, prepared, scope, tolerance=tolerance)
    raw = json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    with Path(path).open("xb") as stream:
        stream.write(raw)
    return {"capture_path": path, "capture_sha256": hashlib.sha256(raw).hexdigest(),
            "solid_ids": solid_ids, "closure": closure, "movable_positions": {str(i): coords[i] for i in movable},
            "surface_sha256": f._hash(block["surface_mesh"]), "boundary_sha256": f._hash(boundaries),
            "definitions_sha256": f._hash(definitions), "cad_sha256": scope["cad_sha256"],
            "mesh_sha256": output["snapshot_sha256"], "topology_sha256": f._hash(output["elements"]),
            "settings_sha256": settings["snapshot_sha256"], "ownership": output["ownership"],
            "element_types": output["integrity"]["element_types"],
            "mesh_audit": facts.get("audit", {}).get("mesh_audit", {}).get("summary")}


async def verify(run, cfg, identity, helpers, report, input_model, options):
    import math
    async with harness(run, cfg, identity, helpers, report) as h:
        async def task(name, fields, timeout=600):
            op = "annular-repair-" + uuid.uuid4().hex
            report["pending"] = {"tool": name, "operation_id": op}
            stage._json_write(run / "report.json", report)
            submitted = await h.call(name, {"operation_id": op, "expected_target": h.target,
                "expected_model": h.model, **fields}, allow_error=True)
            task_id = submitted.get("task_id")
            if not task_id:
                raise RuntimeError(f"no task ID: {submitted}")
            report["pending"]["task_id"] = task_id
            stage._json_write(run / "report.json", report)
            done = await h.wait_task(task_id, None, timeout)
            filename = f"{name}-{len(report['calls'])}.json"
            stage._json_write(run / filename, done)
            report["calls"].append({"tool": name, "operation_id": op, "task_id": task_id,
                                    "status": done["status"], "receipt": filename})
            if done["status"] in {"succeeded", "failed", "expired", "cancelled"}:
                report["pending"] = None
            stage._json_write(run / "report.json", report)
            if done["status"] != "succeeded":
                raise RuntimeError(f"{name}: {done.get('error')}")
            return done["data"]["result"]

        async def capture(label, audit=False):
            target = run / (label + ".json")
            code = inspect.getsource(remote_capture) + "\nRESULT=remote_capture(" + repr(str(target)) + "," + repr(
                options["volume_ids"]) + "," + repr(MOVABLE) + "," + repr(options["annular_options"]["max_chord_error"]) + "," + repr(audit) + ")\n"
            receipt = await task("ansa_script_execute", {"script": code, "validate": True})
            if receipt.get("result_paged"):
                raise RuntimeError("bounded readback unexpectedly paged; query original task")
            value = receipt["result"]
            if hashlib.sha256(target.read_bytes()).hexdigest() != value["capture_sha256"]:
                raise RuntimeError("independent readback checksum differs")
            return value

        await task("ansa_model_open", {"path": str(input_model), "preservation": "backup",
                                      "backup_path": str(run / "before-open.ansa")})
        await h.snapshot_context()
        before = await capture("before-repair")
        if before["closure"] != SCOPE:
            raise RuntimeError("live movable-node closure differs from the declared 12-element scope")
        report["before"] = before
        scope = await task("ansa_mesh_quality_report", {"deck": 1, "entity_ids": SCOPE, "profile": PROFILE})
        if sorted(scope["failed_entity_ids"]) != FAILED or not scope["complete"]:
            raise RuntimeError("fresh quality report differs from the five known defects")
        report["scope"] = {"entity_ids": SCOPE, "movable_node_ids": MOVABLE,
                           "scope_sha256": scope["scope_sha256"], "maximum_total_displacement_mm": 1.0}
        repaired = await task("ansa_mesh_quality_repair", {"deck": 1, "entity_ids": SCOPE, "profile": PROFILE,
            "expected_scope_sha256": scope["scope_sha256"], "expected_scope_components": scope["scope_components"],
            "backup_path": str(run / "before-native-smooth.ansa"), "movable_node_ids": MOVABLE,
            "method": "smooth", "maximum_displacement": 1.0, "maximum_total_displacement": 1.0,
            "max_iterations": 3}, 900)
        report["repair"] = repaired
        report["keep_owned_instance"] = True  # Release only after independent readback.
        stage._json_write(run / "report.json", report)
        after = await capture("after-repair", audit=True)
        report["after"] = after
        protected = ("surface_sha256", "boundary_sha256", "definitions_sha256", "cad_sha256", "topology_sha256", "settings_sha256")
        report["protection"] = {key: before[key] == after[key] for key in protected}
        if not all(report["protection"].values()) or repaired.get("recovery_required"):
            raise RuntimeError("repair requires recovery or changed a protected engineering definition")
        displacement = {str(i): math.dist(before["movable_positions"][str(i)], after["movable_positions"][str(i)]) for i in MOVABLE}
        report["maximum_displacement_mm"] = max(displacement.values())
        if max(displacement.values()) > 1.0:
            raise RuntimeError("independent cumulative displacement exceeds 1 mm")
        quality = await task("ansa_mesh_quality_report", {"deck": 1, "entity_ids": before["solid_ids"],
            "profile": PROFILE, "paged": True})
        report["quality"] = quality["summary"]
        report["quality_extrema"] = quality["metric_extrema"]
        saved = run / "public-annular-repaired.ansa"
        await task("ansa_model_save", {"path": str(saved)})
        await task("ansa_model_open", {"path": str(saved), "preservation": "backup",
                                      "backup_path": str(run / "before-reopen.ansa")})
        await h.snapshot_context()
        reopened = await capture("reopened-repair")
        report["save_reopen"] = {key: after[key] == reopened[key] for key in
            ("surface_sha256", "boundary_sha256", "definitions_sha256", "cad_sha256", "mesh_sha256", "topology_sha256", "settings_sha256")}
        report["complete"] = (all(report["save_reopen"].values()) and all(report["protection"].values())
                              and repaired.get("scope_verified") is True and quality.get("complete") is True)
        report["quality_accepted"] = report["quality"]["status"] == "passed"
        report["acceptance_complete"] = report["complete"] and report["quality_accepted"]
        report["keep_owned_instance"] = not report["quality_accepted"]
        stage._json_write(run / "report.json", report)


if __name__ == "__main__":
    raise SystemExit(owned.main(verify=verify, run_prefix="annular-repair-public-"))
