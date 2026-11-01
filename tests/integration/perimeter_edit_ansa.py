"""在已准备的独立 ANSA 中验收受控 CONS 周边编辑。

本入口不启动 ANSA，只覆盖 checked-in rectangle STEP 的两个相邻 Macro、
已物化内部点和直线同向对齐。通过 stdio MCP、TCP 与 ANSA 主线程执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests" / "integration")]

import mesh_scope_ansa as mesh_scope
import stage_three_ansa as stage


REPORT_NAME = "perimeter-edit-report.json"
IDENTITY_NAME = "owned-identity.json"
RECTANGLE_STEP = ROOT / "tests" / "fixtures" / "rectangle-20x10-mm.step"
TARGET_LENGTH = 2.5


def _fixture_script() -> str:
    """复用 mesh-scope 夹具，跳过原探针的首个面网格并补足分段基线。"""
    source = mesh_scope.FIXTURE_SCRIPT
    start = source.index("    mesh_result = mesh.Create4SidedMesh(")
    end = source.index('    macro_mesh["api_return_type"]', start)
    source = source[:start] + "    mesh_result = []\n" + source[end:]
    original_lookup = (
        '    "shell_macro_for_macro_mesh": ids(mesh.GetShellMacro(\n'
        '        [base.GetEntity(deck, "SHELL", ident) for ident in '
        'macro_mesh["shell_ids"]])),'
    )
    if original_lookup not in source:
        raise RuntimeError("mesh-scope fixture Macro-shell lookup format changed")
    source = source.replace(
        original_lookup, '    "shell_macro_for_macro_mesh": [],', 1
    )
    return "import math\nRUN_NATIVE_PROBE = False\n" + source + r'''

target_cons = sorted({
    eid(cons)
    for face in (face_a, face_b)
    for cons in perimeters(face)
})
returned = mesh.NumberPerimeters(
    [base.GetEntity(deck, "CONS", ident) for ident in target_cons],
    "4",
    apply_multiple_number=False,
    remesh_macros=False,
    apply_number_of="edges",
    use_ansa_defaults_values=False,
)
if type(returned) is not int or returned != 1:
    raise RuntimeError("NumberPerimeters four-edge setup failed")

def cons_fields(cons):
    names = [str(name) for name in cons.card_fields(deck)]
    wanted = [name for name in (
        "Num. of Nodes", "Order of Nodes", "Frozen", "Collapsed", "Biasing"
    ) if name in names]
    if len(wanted) != 5:
        raise RuntimeError("CONS lacks complete seed fields")
    return {str(key): value
            for key, value in base.GetEntityCardValues(deck, cons, wanted).items()}

def position(entity):
    values = base.GetEntityCardValues(deck, entity, ["X1", "X2", "X3"])
    return [float(values[name]) for name in ("X1", "X2", "X3")]

def hot_points(cons):
    return [{"id": eid(item), "xyz": [float(v) for v in item.position]}
            for item in sorted(
                entities(base.CollectEntities(deck, cons, "HOT POINT")), key=eid
            )]

def perimeter_points(cons):
    return [{"id": eid(item), "xyz": position(item)}
            for item in sorted(
                entities(base.CollectEntities(deck, cons, "GRID")), key=eid
            )]

cons_rows = []
for ident in target_cons:
    cons = base.GetEntity(deck, "CONS", ident)
    fields = cons_fields(cons)
    points = perimeter_points(cons)
    internal = int(fields["Num. of Nodes"])
    if len(points) != internal + 2:
        raise RuntimeError("CONS perimeter points were not materialized")
    cons_rows.append({
        "cons_id": ident,
        "face_ids": ids(base.GetFacesOfCons(cons)),
        "fields": fields,
        "segments": internal + 1,
        "point_count": len(points),
        "points": points,
        "hot_points": hot_points(cons),
    })

macro_shells = {
    str(eid(macro)): ids(mesh.GetMacroShells([macro]))
    for macro in (macro_a, macro_b)
}
if any(macro_shells.values()):
    raise RuntimeError("the two target CAD Macros must remain unmeshed")
fe_after = fe_snapshot(all_test_elements, all_test_grids)
if fe_after != fe_initial:
    raise RuntimeError("CONS setup changed the independent FE fixture")

shared = next(row for row in cons_rows if row["cons_id"] == shared_cons_id)
shared_hot = sorted(shared["hot_points"], key=lambda row: row["id"])
if len(shared_hot) != 2:
    raise RuntimeError("shared CONS lacks two distinct endpoints")
shared_delta = [
    shared_hot[1]["xyz"][axis] - shared_hot[0]["xyz"][axis] for axis in range(3)
]
shared_length = math.sqrt(sum(value * value for value in shared_delta))
if shared_length <= 1e-12:
    raise RuntimeError("shared CONS lacks two distinct endpoints")
shared_unit = [value / shared_length for value in shared_delta]

def parallel_outer(face):
    selected = []
    for row in cons_rows:
        if row["cons_id"] == shared_cons_id or row["face_ids"] != [eid(face)]:
            continue
        hot = sorted(row["hot_points"], key=lambda item: item["id"])
        if len(hot) != 2:
            continue
        vector = [
            hot[1]["xyz"][axis] - hot[0]["xyz"][axis] for axis in range(3)
        ]
        length = math.sqrt(sum(value * value for value in vector))
        if length <= 1e-12:
            continue
        cosine = abs(
            sum(vector[axis] * shared_unit[axis] for axis in range(3)) / length
        )
        if cosine >= 1.0 - 1e-8:
            selected.append((row, hot, vector))
    return selected

source_candidates = parallel_outer(face_a)
target_candidates = parallel_outer(face_b)
if len(source_candidates) != 1 or len(target_candidates) != 1:
    raise RuntimeError("fixture needs one parallel outer CONS per Macro")
source_row, source_hot, source_vector = source_candidates[0]
target_row, target_hot, target_vector = target_candidates[0]
if sum(a*b for a, b in zip(source_vector, target_vector)) < 0:
    target_hot.reverse()
source_start = source_hot[0]
target_start = target_hot[0]
source_len = math.sqrt(sum(value * value for value in source_vector))
target_delta = [
    target_hot[1]["xyz"][axis] - target_hot[0]["xyz"][axis] for axis in range(3)
]
target_len = math.sqrt(sum(value * value for value in target_delta))
direction_dot = sum(
    source_vector[axis] * target_delta[axis] for axis in range(3)
) / (source_len * target_len)
if direction_dot < 1.0 - 1e-8:
    raise RuntimeError("fixture outer CONS endpoints do not share one direction")

RESULT["perimeter_setup"] = {
    "number_perimeters_return": returned,
    "cons": cons_rows,
    "macro_shells": macro_shells,
    "fe_after_setup": fe_after,
}
RESULT["align_fixture"] = {
    "shared_cons_id": shared_cons_id,
    "source_cons_id": source_row["cons_id"],
    "source_start_hot_point_id": source_start["id"],
    "targets": [{
        "cons_id": target_row["cons_id"],
        "start_hot_point_id": target_start["id"],
    }],
    "direction_dot": direction_dot,
}
'''


FIXTURE_SCRIPT = _fixture_script()


CREATE_MACRO_MESH_SCRIPT = r'''from ansa import base, constants, mesh

deck = constants.NASTRAN
face = base.GetEntity(deck, "FACE", __FACE_ID__)
macro = base.GetFaceMacro(face)
raw_before = mesh.GetMacroShells([macro])
if type(raw_before) is int and raw_before == 0:
    raw_before = []
before = [int(item._id) for item in raw_before]
if face is None or macro is None or before:
    raise RuntimeError("selected FACE/Macro must be valid and unmeshed")
mesh.Create4SidedMesh(
    [face], True, True, 60.0, "standard", "none", "off", "quad", "directional",
    only_unmeshed=True,
)
shell_ids = sorted(int(item._id) for item in mesh.GetMacroShells([macro]))
if not shell_ids:
    raise RuntimeError("Create4SidedMesh generated no SHELL")
RESULT = {"macro_id": int(macro._id), "macro_shell_ids": shell_ids}
'''


FE_READ_SCRIPT = r'''import json
from ansa import base, constants

deck = constants.NASTRAN
spec = json.loads(SPEC_JSON)

def card_values(entity, names):
    return {str(key): value
            for key, value in base.GetEntityCardValues(deck, entity, names).items()}

result = {"shells": {}, "solids": {}, "grids": {}}
for ident in spec["shell_ids"]:
    entity = base.GetEntity(deck, "SHELL", ident)
    if entity is None:
        result["shells"][str(ident)] = None
    else:
        names = [str(name) for name in entity.card_fields(deck)
                 if name in ("EID", "PID", "type", "G1", "G2", "G3", "G4")]
        result["shells"][str(ident)] = card_values(entity, names)
solid = base.GetEntity(deck, "SOLID", spec["solid_id"])
if solid is None:
    result["solids"][str(spec["solid_id"])] = None
else:
    names = [str(name) for name in solid.card_fields(deck)
             if name in ("EID", "PID", "type", "G1", "G2", "G3", "G4")]
    result["solids"][str(spec["solid_id"])] = card_values(solid, names)
for ident in spec["grid_ids"]:
    entity = base.GetEntity(deck, "GRID", ident)
    result["grids"][str(ident)] = (
        None if entity is None else card_values(entity, ["X1", "X2", "X3"])
    )
RESULT = result
'''


def _operation(label: str) -> str:
    return "perimeter-edit-" + label + "-" + uuid.uuid4().hex


def _leaf(completed: dict) -> dict:
    value = mesh_scope._result(completed)
    if not isinstance(value, dict):
        raise RuntimeError("task has no leaf operation_result: " + repr(completed))
    return value


async def _task(
    harness,
    run_dir: Path,
    report: dict,
    tool: str,
    fields: dict,
    label: str,
    *,
    expected_status: str | None = "succeeded",
) -> dict:
    return await mesh_scope._task(
        harness,
        run_dir,
        report,
        tool,
        fields,
        operation_id=_operation(label),
        expected_status=expected_status,
    )


def _digest(result: dict, key: str) -> str:
    value = result.get(key)
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise RuntimeError(key + " is not a lowercase SHA-256")
    return value


async def _seed(
    harness, run_dir: Path, report: dict, deck: int, faces: list[int],
    cons: list[int], label: str,
) -> dict:
    completed = await _task(
        harness,
        run_dir,
        report,
        "ansa_mesh_seed_read",
        {"deck": deck, "face_ids": faces, "cons_ids": cons},
        "seed-" + label,
    )
    result = _leaf(completed)
    _digest(result, "seed_snapshot_sha256")
    return result


async def _scope(
    harness, run_dir: Path, report: dict, deck: int, entity_type: str,
    ids: list[int], label: str,
) -> dict:
    completed = await _task(
        harness,
        run_dir,
        report,
        "ansa_mesh_scope_read",
        {"deck": deck, "entity_type": entity_type, "entity_ids": ids},
        "scope-" + label,
    )
    result = _leaf(completed)
    if result.get("ok") is not True:
        raise RuntimeError("mesh scope read failed")
    _digest(result, "scope_sha256")
    return result


async def _bindings(
    harness, run_dir: Path, report: dict, deck: int, faces: list[int],
    cons: list[int], label: str,
) -> tuple[dict, dict]:
    seed = await _seed(harness, run_dir, report, deck, faces, cons, label)
    scope = await _scope(harness, run_dir, report, deck, "CONS", cons, label)
    report["latest_binding"] = {
        "deck": deck,
        "faces": faces,
        "cons": cons,
        "seed_sha256": seed["seed_snapshot_sha256"],
        "scope_sha256": scope["scope_sha256"],
    }
    return seed, scope


async def _edit(
    harness,
    run_dir: Path,
    report: dict,
    deck: int,
    faces: list[int],
    cons: list[int],
    edit: dict,
    backup: Path,
    label: str,
    *,
    stale_scope: str | None = None,
) -> dict:
    cons = _edit_cons(edit)
    seed, scope = await _bindings(
        harness, run_dir, report, deck, faces, cons, label
    )
    request_edit = {
        **edit,
        "expected_scope_sha256": stale_scope or scope["scope_sha256"],
    }
    completed = await _task(
        harness,
        run_dir,
        report,
        "ansa_mesh_seed_apply",
        {
            "deck": deck,
            "face_ids": faces,
            "edit": request_edit,
            "expected_seed_snapshot_sha256": seed["seed_snapshot_sha256"],
            "backup_path": str(backup.resolve()),
        },
        "edit-" + label,
        expected_status=None,
    )
    result = _leaf(completed)
    call = report["calls"][-1]
    report.setdefault("edits", {})[label] = {
        "operation_id": call["operation_id"],
        "task_id": call["task_id"],
        "status": completed["status"],
        "edit": request_edit,
        "expected_seed_snapshot_sha256": seed["seed_snapshot_sha256"],
        "backup_path": str(backup.resolve()),
        "result": result,
    }
    mesh_scope._write_json(run_dir / REPORT_NAME, report)
    return report["edits"][label]


def _edit_cons(edit: dict) -> list[int]:
    if edit["action"] == "number":
        return sorted(row["cons_id"] for row in edit["assignments"])
    if edit["action"] == "length":
        return sorted(edit["cons_ids"])
    return sorted([edit["source_cons_id"]] + [row["cons_id"] for row in edit["targets"]])


def _rows(result: dict, key: str, nested: bool = False) -> dict[int, dict]:
    source = result.get("readback") if nested else result
    values = source.get(key) if isinstance(source, dict) else None
    if not isinstance(values, list):
        raise RuntimeError(key + " readback is missing")
    return {row["cons_id"]: row for row in values if isinstance(row, dict)}


def _check_counts(result: dict, expected: dict[int, tuple[int, int]]) -> dict:
    boundaries = _rows(result, "boundaries", True)
    points = _rows(result, "boundary_point_readback")
    observed = {}
    for ident, (segments, point_count) in expected.items():
        if boundaries.get(ident, {}).get("segments") != segments:
            raise RuntimeError(f"CONS {ident} edge count differs")
        if points.get(ident, {}).get("segments") != segments:
            raise RuntimeError(f"CONS {ident} point-row edge count differs")
        if points.get(ident, {}).get("point_count") != point_count:
            raise RuntimeError(f"CONS {ident} point count differs")
        observed[str(ident)] = {"segments": segments, "points": point_count}
    return observed


async def _success(
    harness,
    run_dir: Path,
    report: dict,
    deck: int,
    faces: list[int],
    cons: list[int],
    edit: dict,
    backup: Path,
    label: str,
    expected: dict[int, tuple[int, int]],
) -> dict:
    cons = _edit_cons(edit)
    attempt = await _edit(
        harness, run_dir, report, deck, faces, cons, edit, backup, label
    )
    result = attempt["result"]
    if attempt["status"] != "succeeded":
        raise RuntimeError(label + " task did not succeed")
    for key in ("ok", "scope_verified", "configuration_unchanged"):
        if result.get(key) is not True:
            raise RuntimeError(label + f" has {key}={result.get(key)!r}")
    if result.get("recovery_required") is not False:
        raise RuntimeError(label + " requires recovery")
    if type(result.get("native_calls")) is not list or not result["native_calls"]:
        raise RuntimeError(label + " has no native_calls")
    attempt["counts"] = _check_counts(result, expected)
    await harness.snapshot_context()
    seed = await _seed(
        harness, run_dir, report, deck, faces, cons, label + "-after"
    )
    scope = await _scope(
        harness, run_dir, report, deck, "CONS", cons, label + "-after"
    )
    if seed["seed_snapshot_sha256"] != _digest(result, "seed_after_sha256"):
        raise RuntimeError(label + " seed_after_sha256 mismatch")
    if scope["scope_sha256"] != _digest(result, "scope_sha256_after"):
        raise RuntimeError(label + " scope_sha256_after mismatch")
    return attempt


def _rejected(result: dict, backup: Path, text: tuple[str, ...] = ()) -> None:
    if result.get("ok") is not False:
        raise RuntimeError("expected a false leaf ok")
    if result.get("scope_verified") is not False:
        raise RuntimeError("rejection did not report scope_verified=false")
    if (
        result.get("api_called") is True
        or result.get("backup_attempted") is True
        or result.get("possible_effects") is not False
        or result.get("recovery_required") is not False
        or result.get("native_calls") != []
    ):
        raise RuntimeError("preflight rejection reached backup/native or retained effects")
    if backup.exists():
        raise RuntimeError("preflight rejection created a backup")
    encoded = json.dumps(result, ensure_ascii=False).casefold()
    if any(word.casefold() not in encoded for word in text):
        raise RuntimeError("rejection diagnostic lacks expected text")


def _scope_row(scope: dict, section: str, ident: int) -> dict:
    for row in scope.get(section, []):
        if row.get("entity", {}).get("id") == ident:
            return row
    raise RuntimeError(f"{section} {ident} missing from scope")


def _protection(scope: dict, cons_id: int) -> bool:
    value = _scope_row(scope, "cons", cons_id).get("effective_macro_protection")
    if not isinstance(value, dict) or type(value.get("value")) is not bool:
        raise RuntimeError("CONS effective Macro protection is unknown")
    return value["value"]


async def _freeze(
    harness, run_dir: Path, report: dict, deck: int, face: int, action: str
) -> dict:
    before = await _scope(
        harness, run_dir, report, deck, "FACE", [face], "face-" + action
    )
    completed = await _task(
        harness,
        run_dir,
        report,
        "ansa_mesh_freeze",
        {
            "deck": deck,
            "entity_type": "FACE",
            "entity_ids": [face],
            "action": action,
            "expected_scope_sha256": before["scope_sha256"],
            "backup_path": str(
                (run_dir / f"before-perimeter-edit-{action}.ansa").resolve()
            ),
        },
        action,
    )
    result = _leaf(completed)
    if result.get("ok") is not True or result.get("scope_verified") is not True:
        raise RuntimeError(f"FACE {action} failed")
    await harness.snapshot_context()
    return result


async def _script(
    harness, run_dir: Path, report: dict, script: str, label: str
) -> dict:
    completed = await _task(
        harness,
        run_dir,
        report,
        "ansa_script_execute",
        {"script": script, "validate": True, "allow_model_change": True},
        label,
    )
    leaf = _leaf(completed)
    result = leaf.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(label + " returned no object result")
    await harness.snapshot_context()
    return result


async def _verify(
    run_dir: Path,
    instructions: dict,
    identity: dict,
    helpers: tuple,
    report: dict,
) -> None:
    async with mesh_scope._harness(
        run_dir, instructions, identity, helpers, report
    ) as harness:
        start = run_dir / "perimeter-edit-fixture-start.ansa"
        await _task(
            harness,
            run_dir,
            report,
            "ansa_model_import",
            {
                "path": str(RECTANGLE_STEP),
                "format": "STEP",
                "mode": "replace",
                "preservation": "backup",
                "backup_path": str(
                    (run_dir / "before-perimeter-edit-step.ansa").resolve()
                ),
            },
            "step-import",
        )
        await harness.snapshot_context()
        await _task(
            harness,
            run_dir,
            report,
            "ansa_model_save",
            {"path": str(start.resolve())},
            "save-start",
        )
        await harness.snapshot_context()

        fixture = await _script(
            harness, run_dir, report, FIXTURE_SCRIPT, "fixture"
        )
        report["fixture"] = fixture
        setup = fixture["perimeter_setup"]
        align = fixture["align_fixture"]
        deck = fixture["deck"]
        faces = [fixture["fixture"]["face_a_id"], fixture["fixture"]["face_b_id"]]
        shared = align["shared_cons_id"]
        source = align["source_cons_id"]
        targets = align["targets"]
        target_ids = [row["cons_id"] for row in targets]
        cons = sorted({shared, source, *target_ids})
        cons_rows = {row["cons_id"]: row for row in setup["cons"]}
        if any(row["segments"] != 4 or row["point_count"] != 5
               for row in cons_rows.values()):
            raise RuntimeError("fixture CONS are not four-edge baselines")
        if any(setup["macro_shells"].values()):
            raise RuntimeError("fixture left a target Macro meshed")
        if harness.snapshot.get("deck") != deck:
            raise RuntimeError("fixture deck differs from model snapshot")

        fe_spec = {
            "shell_ids": fixture["fe_fixture"]["shell_ids"],
            "solid_id": fixture["fe_fixture"]["solid_id"],
            "grid_ids": sorted(set(fixture["fe_fixture"]["grid_ids"].values())),
        }
        fe_script = "SPEC_JSON = " + repr(json.dumps(fe_spec)) + "\n" + FE_READ_SCRIPT
        fe_baseline = await _script(harness, run_dir, report, fe_script, "fe-baseline")

        initial_seed, initial_scope = await _bindings(
            harness, run_dir, report, deck, faces, cons, "initial"
        )
        stale_scope = initial_scope["scope_sha256"]

        number_edges = await _success(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "number",
                "count_type": "edges",
                "assignments": [{"cons_id": shared, "count": 6}],
            },
            run_dir / "before-number-6-edges.ansa",
            "number-6-edges",
            {shared: (6, 7)},
        )
        repeated = await harness.call(
            "ansa_mesh_seed_apply",
            {
                "operation_id": number_edges["operation_id"],
                "expected_target": dict(harness.target),
                "expected_model": dict(harness.model),
                "deck": deck,
                "face_ids": faces,
                "edit": number_edges["edit"],
                "expected_seed_snapshot_sha256": number_edges["expected_seed_snapshot_sha256"],
                "backup_path": number_edges["backup_path"],
            },
            allow_error=True,
        )
        if repeated.get("task_id") != number_edges["task_id"]:
            raise RuntimeError("duplicate operation_id returned a different task")
        report["idempotency"] = {
            "operation_id": number_edges["operation_id"],
            "task_id": number_edges["task_id"],
        }

        number_nodes = await _success(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "number",
                "count_type": "nodes",
                "assignments": [{"cons_id": shared, "count": 8}],
            },
            run_dir / "before-number-8-nodes.ansa",
            "number-8-nodes",
            {shared: (7, 8)},
        )
        shared_hot = sorted(
            cons_rows[shared]["hot_points"], key=lambda row: row["id"]
        )
        expected_edges = max(
            1, math.ceil(math.dist(shared_hot[0]["xyz"], shared_hot[1]["xyz"])
                         / TARGET_LENGTH - 1e-12)
        )
        length_edit = await _success(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "length",
                "cons_ids": [shared],
                "target_length": TARGET_LENGTH,
            },
            run_dir / "before-length-2.5.ansa",
            "length-2.5",
            {shared: (expected_edges, expected_edges + 1)},
        )
        align_edit = await _success(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "align",
                "source_cons_id": source,
                "source_start_hot_point_id": align[
                    "source_start_hot_point_id"
                ],
                "targets": targets,
            },
            run_dir / "before-align.ansa",
            "align",
            {source: (4, 5), target_ids[0]: (4, 5)},
        )
        align_call = next(
            (
                call for call in align_edit["result"]["native_calls"]
                if set(call.get("cons_ids", [])) >= {source, *target_ids}
            ),
            None,
        )
        if align_call is None:
            raise RuntimeError("align native call did not pass source+targets union")

        stale_backup = run_dir / "must-not-exist-stale.ansa"
        stale = await _edit(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "number",
                "count_type": "edges",
                "assignments": [{"cons_id": shared, "count": 5}],
            },
            stale_backup,
            "stale-scope",
            stale_scope=stale_scope,
        )
        _rejected(stale["result"], stale_backup, ("scope",))

        await _freeze(harness, run_dir, report, deck, faces[0], "freeze")
        frozen_scope = await _scope(
            harness, run_dir, report, deck, "CONS", cons, "frozen"
        )
        if _protection(frozen_scope, shared) is not True:
            raise RuntimeError("Macro freeze did not protect shared CONS")
        frozen_backup = run_dir / "must-not-exist-frozen.ansa"
        frozen = await _edit(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "number",
                "count_type": "edges",
                "assignments": [{"cons_id": shared, "count": 5}],
            },
            frozen_backup,
            "frozen-macro",
        )
        _rejected(frozen["result"], frozen_backup, ("frozen",))
        await _freeze(harness, run_dir, report, deck, faces[0], "unfreeze")
        unfrozen = await _scope(
            harness, run_dir, report, deck, "CONS", cons, "unfrozen"
        )
        if _protection(unfrozen, shared) is not False:
            raise RuntimeError("unfreeze did not clear Macro protection")

        final_mesh = await _script(
            harness,
            run_dir,
            report,
            CREATE_MACRO_MESH_SCRIPT.replace("__FACE_ID__", str(faces[0])),
            "create-macro-mesh",
        )
        meshed_shells = final_mesh["macro_shell_ids"]
        blocked_backup = run_dir / "must-not-exist-blocked.ansa"
        blocked = await _edit(
            harness,
            run_dir,
            report,
            deck,
            faces,
            cons,
            {
                "action": "number",
                "count_type": "edges",
                "assignments": [{"cons_id": shared, "count": 5}],
            },
            blocked_backup,
            "meshed-macro",
        )
        _rejected(blocked["result"], blocked_backup, ("macro", "shell"))
        if not set(meshed_shells) <= set(
            blocked["result"].get("blocked_macro_shell_ids", [])
        ):
            raise RuntimeError("meshed-Macro rejection lacks blocked shell IDs")

        final_model = run_dir / "perimeter-edit-final.ansa"
        await _task(
            harness,
            run_dir,
            report,
            "ansa_model_save",
            {"path": str(final_model.resolve())},
            "save-final",
        )
        await harness.snapshot_context()
        settings_before = _leaf(
            await _task(
                harness,
                run_dir,
                report,
                "ansa_mesh_settings",
                {"action": "read"},
                "settings-before-reopen",
            )
        )
        await _task(
            harness,
            run_dir,
            report,
            "ansa_model_open",
            {
                "path": str(final_model.resolve()),
                "preservation": "backup",
                "backup_path": str(
                    (run_dir / "before-perimeter-edit-reopen.ansa").resolve()
                ),
            },
            "reopen-final",
        )
        await harness.snapshot_context()
        settings_after = _leaf(
            await _task(
                harness,
                run_dir,
                report,
                "ansa_mesh_settings",
                {"action": "read"},
                "settings-after-reopen",
            )
        )
        if settings_before["settings"] != settings_after["settings"]:
            raise RuntimeError("mesh settings changed across reopen")

        final_seed = await _seed(
            harness, run_dir, report, deck, faces, cons, "reopened"
        )
        final_scope = await _scope(
            harness, run_dir, report, deck, "CONS", cons, "reopened"
        )
        final_counts = {}
        for ident in cons:
            row = _scope_row(final_scope, "cons", ident)
            internal = row.get("fields", {}).get("Num. of Nodes")
            if type(internal) is not int:
                raise RuntimeError("reopened CONS has no numeric node count")
            final_counts[str(ident)] = internal + 1
        if final_counts.get(str(shared)) != expected_edges:
            raise RuntimeError("shared CONS count changed on reopen")
        if _protection(final_scope, shared) is not False:
            raise RuntimeError("Macro protection changed on reopen")

        fe_spec = {
            "shell_ids": fixture["fe_fixture"]["shell_ids"],
            "solid_id": fixture["fe_fixture"]["solid_id"],
            "grid_ids": sorted(set(fixture["fe_fixture"]["grid_ids"].values())),
        }
        fe_script = (
            "SPEC_JSON = "
            + json.dumps(json.dumps(fe_spec, separators=(",", ":")))
            + "\n"
            + FE_READ_SCRIPT
        )
        fe_after_reopen = await _script(
            harness, run_dir, report, fe_script, "fe-reopen"
        )
        if any(value is None for section in fe_after_reopen.values()
               for value in section.values()):
            raise RuntimeError("independent FE is incomplete after reopen")
        if fe_after_reopen != fe_baseline:
            raise RuntimeError("independent FE cards or coordinates changed across editing/save/reopen")

        report["accepted"] = {
            "number_edges": number_edges["counts"],
            "number_nodes": number_nodes["counts"],
            "length": length_edit["counts"],
            "align": align_edit["counts"],
            "stale_scope": stale["result"],
            "frozen_macro": frozen["result"],
            "blocked_macro_shell_ids": blocked["result"][
                "blocked_macro_shell_ids"
            ],
        }
        report["save_reopen"] = {
            "path": str(final_model.resolve()),
            "seed_snapshot_sha256": final_seed["seed_snapshot_sha256"],
            "scope_sha256": final_scope["scope_sha256"],
            "counts": final_counts,
            "fe": fe_after_reopen,
            "settings": settings_after,
        }
        report["complete"] = True
        report["final_model_saved"] = True
        report["status"] = "complete"
        mesh_scope._write_json(run_dir / REPORT_NAME, report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        help="stage_three_ansa.py --prepare-only 生成的独立运行目录",
    )
    parser.add_argument("--keep-owned-ansa", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=60)
    args = parser.parse_args(argv)
    if not 1 <= args.startup_timeout <= 60:
        parser.error("--startup-timeout must be 1..60")

    run_dir = args.run_dir.expanduser().resolve(strict=True)
    report_path = run_dir / REPORT_NAME
    if report_path.exists():
        raise FileExistsError("report exists; query original tasks instead of replaying")
    instructions = json.loads(
        (run_dir / "launch_instructions.json").read_text(encoding="utf-8")
    )
    if Path(instructions["run_dir"]).resolve() != run_dir:
        raise ValueError("launch instructions belong to another run")

    report = {
        "status": "preparing",
        "complete": False,
        "report_name": REPORT_NAME,
        "run_dir": str(run_dir),
        "phase": "perimeter-edit",
        "calls": [],
        "pending": None,
        "scope": [
            "checked-in rectangle STEP with two adjacent CAD Macros",
            "materialized first-order open CONS and straight-line align only",
        ],
    }
    mesh_scope._write_json(report_path, report)

    identity = helpers = None
    try:
        stage.PANEL_WAIT_SECONDS = args.startup_timeout
        identity_path = run_dir / IDENTITY_NAME
        if identity_path.is_file():
            stored = json.loads(identity_path.read_text(encoding="utf-8"))
            identity, helpers, basic = mesh_scope._resume_owned_ansa(
                instructions, {"identity": stored.get("identity", stored)}
            )
            report["identity_check"] = {
                "pid": basic["pid"],
                "main_thread": basic["main_thread"],
            }
        else:
            identity, _ansa, _grpc, helpers = stage._prepare_owned_ansa(
                run_dir, instructions
            )
            mesh_scope._write_json(identity_path, {"identity": identity})
        report["identity"] = identity
        report["status"] = "running"
        mesh_scope._write_json(report_path, report)
        asyncio.run(_verify(run_dir, instructions, identity, helpers, report))
    except Exception:
        report["error"] = traceback.format_exc()
        report["status"] = "failed"
    finally:
        if (
            identity is not None
            and helpers is not None
            and report.get("complete") is True
            and report.get("final_model_saved") is True
            and report.get("pending") is None
            and not args.keep_owned_ansa
        ):
            try:
                report["cleanup"] = stage._cleanup_owned_ansa(
                    identity, instructions, helpers
                )
            except Exception:
                report["cleanup_error"] = traceback.format_exc()
                report["status"] = "cleanup_failed"
        elif identity is not None:
            report["cleanup"] = {
                "stopped_owned_process": False,
                "reason": "keep requested or verification incomplete",
            }
        mesh_scope._write_json(report_path, report)

    print(
        json.dumps(
            {
                "status": report.get("status"),
                "complete": report.get("complete"),
                "report": str(report_path),
                "cleanup": report.get("cleanup"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if report.get("complete") is True and report.get("status") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
