"""Backed-up native four-sided meshing with unchanged integer CONS seeds."""

import copy
import math
from pathlib import Path
import uuid

from . import cons_seed as seeds, entity_catalog as catalog, geometry_features as features
from . import inspection, meshing, structured_native as native, volume_meshing as guards
from .mapped_surface_topology import four_sides, verify_lattice

_MAX_SHELLS = 200
_ROUNDING_MM = 1e-12
_PROJECTION_MM = 1e-7


def preflight(state, base, mesh, constants, params):
    state.assert_main()
    inspection._params(params, ("deck", "faces", "element_type", "method", "seed_scope"))
    deck = params["deck"]
    native._check_nastran(constants, deck)
    if params["element_type"] != "quad" or params["method"] != "mapped_existing_seeds":
        raise ValueError("mapped_existing_seeds requires quad output")
    for owner, name in ((mesh, "Create4SidedMesh"), (mesh, "SaveMeshParams"),
                        (mesh, "ReadMeshParams"), (base, "SetEntityCardValues")):
        if not callable(getattr(owner, name, None)):
            raise NotImplementedError("native mapped mesh API unavailable: " + name)
    from ansa import calc
    if not callable(getattr(calc, "ProjectPointToContainer", None)):
        raise NotImplementedError("native ProjectPointToContainer is unavailable")
    context = guards._execution_context(state, deck)
    scope = params["seed_scope"]
    inspection._params(scope, ("face_ids", "cons_ids", "expected_seed_snapshot_sha256", "backup_path"), ("max_boundary_restore_mm", "allow_unmeshed_tessellation_updates"))
    allow_tessellation=scope.get('allow_unmeshed_tessellation_updates',False)
    if type(allow_tessellation) is not bool:raise ValueError('allow_unmeshed_tessellation_updates must be bool')
    restore_limit = scope.get("max_boundary_restore_mm", _ROUNDING_MM)
    if type(restore_limit) not in (int,float) or not math.isfinite(restore_limit) or not _ROUNDING_MM <= restore_limit <= 1e-5:
        raise ValueError("max_boundary_restore_mm must be a finite number between 1e-12 and 1e-5 mm")
    if restore_limit > _ROUNDING_MM and not callable(getattr(calc, "ProjectPointToCons", None)):
        raise NotImplementedError("explicit boundary restoration requires native ProjectPointToCons")
    expected = native._validate_digest(scope["expected_seed_snapshot_sha256"], "expected_seed_snapshot_sha256")
    witness = seeds.capture(state, base, deck, scope["face_ids"], scope["cons_ids"])
    if witness["seed_snapshot_sha256"] != expected:
        raise ValueError("native seed source/settings changed; obtain fresh mesh_seed_read")
    conditions = params["faces"]
    if type(conditions) is not list or not 1 <= len(conditions) <= 4:
        raise ValueError("mapped surface accepts 1..4 explicit FACE conditions")
    selected = features._ids([v.get("id") if type(v) is dict else None for v in conditions], 4, "faces.id")
    if not set(selected) <= set(scope["face_ids"]):
        raise ValueError("mapped source FACE must be included in seed_scope.face_ids")
    if any(not set(r["face_ids"]) <= set(scope["face_ids"]) for r in witness["boundaries"]):
        raise ValueError("seed_scope must include all adjacent FACE; only source FACE will be meshed")
    topology = {r["face_id"]: r["cons_ids"] for r in witness["geometry_witness"]["face_boundaries"]}
    wanted_cons = {c for f in selected for c in topology[f]}
    if wanted_cons != set(scope["cons_ids"]):
        raise ValueError("seed_scope.cons_ids must exactly cover all source FACE boundaries")
    faces, total = [], 0
    for item in conditions:
        inspection._params(item, ("id", "expected_fields", "expected_macro_id", "expected_shell_ids"))
        ident = item["id"]
        catalog._positive_int(item["expected_macro_id"], "expected_macro_id")
        meshing._ids(item["expected_shell_ids"], "expected_shell_ids", 2000, empty=True)
        catalog._validate_entity_values(base, deck, "FACE", ident, item["expected_fields"])
        face, snapshot, shells = meshing._face_snapshot(base, mesh, deck, ident)
        if any(k not in item["expected_fields"] or not catalog._strict_equal(v, item["expected_fields"][k])
               for k, v in snapshot["fields"].items()):
            raise ValueError("mapped surface requires complete current FACE expected_fields")
        if (item["expected_macro_id"] != snapshot["macro"]["id"]
                or item["expected_shell_ids"] != shells):
            raise ValueError("FACE macro or attached mesh changed since it was read")
        if snapshot["macro_face_ids"] != [ident] or shells or snapshot["fields"]["Frozen"] != "NO":
            raise ValueError("mapped surface requires one unfrozen, unmeshed FACE per macro")
        sides = four_sides(topology[ident], witness["boundaries"])
        count = sides[0]["segments"] * sides[1]["segments"]
        total += count
        faces.append({"id": ident, "entity": face, "snapshot": snapshot, "sides": sides,
                      "shell_count": count, "part_id": base.GetEntityPart(face)._id})
    if total > _MAX_SHELLS:
        raise ValueError("mapped surface exceeds the 200-SHELL output budget")
    unmeshed_adjacent=[]
    if allow_tessellation:
        adjacent={i for r in witness['boundaries'] for i in r['face_ids']}-set(selected)
        for ident in sorted(adjacent):
            _face,_snapshot,shells=meshing._face_snapshot(base,mesh,deck,ident)
            if not shells:unmeshed_adjacent.append(ident)
    before = seeds._guard_snapshot(state, base, deck)
    seeds._point_budget(base, deck, seeds._collect(base, deck, None, "CONS"),
                        len(before["entities"])+total+(total*4), len(before["grids"])+total*4)
    backup = native._backup_preflight(base, scope)
    guards._same_context(state, deck, context)
    return {"before": before, "witness": witness, "source_faces": faces,
            "assignments": [{"cons_id": r["cons_id"], "segments": r["segments"]} for r in witness["boundaries"]],
            "context": context, "backup_preflight": backup,
            "unmeshed_tessellation_face_ids": unmeshed_adjacent,
            "boundary_restore_tolerance_mm": restore_limit}


def _restore_boundary_roundoff(base, deck, prepared, changes, *, state=None):
    """Restore bounded native reevaluation; keep final original GRID cards exact."""
    limit=prepared.get("boundary_restore_tolerance_mm", _ROUNDING_MM)
    if type(limit) not in (int,float) or not math.isfinite(limit) or not _ROUNDING_MM <= limit <= 1e-5:
        raise RuntimeError("invalid explicit native boundary restoration budget")
    ids = {p["id"] for r in prepared["witness"]["boundaries"] for p in r["points"]}
    old = {r["id"]: r["card"] for r in prepared["before"]["grids"] if r["id"] in ids}
    candidates = []
    for ident, card in sorted(old.items()):
        node = features._entity(base, deck, "GRID", ident)
        current = seeds._card(base, deck, node)
        if current == card:
            continue
        a, b = copy.deepcopy(card), copy.deepcopy(current)
        xyz = [native._finite(a["fields"].pop(k), k) for k in ("X1", "X2", "X3")]
        now = [native._finite(b["fields"].pop(k), k) for k in ("X1", "X2", "X3")]
        distance = math.dist(xyz, now)
        if a != b or distance > limit:
            raise RuntimeError("native mapped mesh changed protected boundary GRID beyond numerical roundoff")
        candidates.append((node, xyz, now, distance))
    if candidates and limit > _ROUNDING_MM:
        from ansa import calc
        from .geometry_fidelity import _projection_points
        # Before restoring any coordinate, verify unchanged CAD and that BOTH
        # original and reevaluated points remain on their original native CONS.
        if state is None:raise RuntimeError("explicit restoration requires the bound native main-thread context")
        guards._same_context(state,deck,prepared['context'])
        current=seeds._guard_snapshot(state,base,deck)
        if current["cad_sha256"] != prepared["before"]["cad_sha256"]:
            raise RuntimeError("native CAD changed; explicit perimeter restoration refused")
        for node,xyz,now,distance in candidates:
            edges=[r['cons_id'] for r in prepared['witness']['boundaries'] if any(p['id']==node._id for p in r['points'])]
            if not edges:raise RuntimeError("restoration node has no original CONS binding")
            for ident in edges:
                cons=features._entity(base,deck,'CONS',ident)
                for point in (xyz,now):
                    _extension,nearest,_mode=_projection_points(calc.ProjectPointToCons(point,cons))
                    if math.dist(point,nearest)>_PROJECTION_MM:
                        raise RuntimeError("perimeter restoration point is outside original native CONS")
    for node, xyz, now, distance in candidates:
        row = {"grid_id": node._id, "native_xyz": now, "original_xyz": xyz,
               "distance_mm": distance, "maximum_allowed_mm": limit, "restored": False,
               "policy": "explicit_native_perimeter_projection" if limit>_ROUNDING_MM else "numerical_roundoff"}
        changes.append(row)
        status = base.SetEntityCardValues(deck, node, dict(zip(("X1", "X2", "X3"), xyz)))
        row["return_code"] = status
        row["restored"] = seeds._card(base, deck, node) == old[node._id]
        if type(status) is not int or status != 0 or not row["restored"]:
            raise RuntimeError("boundary GRID rounding restoration was not confirmed")


def _boundary_paths(elements, rows, sides):
    uses = {}
    for row in elements:
        nodes = row["nodes"]
        for i in range(4):
            key = tuple(sorted((nodes[i], nodes[(i+1) % 4])))
            uses[key] = uses.get(key, 0)+1
    free = {key for key, n in uses.items() if n == 1}
    paths = []
    for side in sides:
        row = rows[side["cons_id"]]
        points = row["points"]
        hp = {v["id"]: v["xyz"] for v in row["hot_points"]}
        ends = []
        for key in ("start", "end"):
            matches = [p["id"] for p in points if math.dist(p["xyz"], hp[side[key]]) <= _PROJECTION_MM]
            if len(matches) != 1:
                raise RuntimeError("mapped boundary endpoint GRID is ambiguous")
            ends.append(matches[0])
        ids = {p["id"] for p in points}
        adjacency = {i: set() for i in ids}
        for a, b in free:
            if a in ids and b in ids:
                adjacency[a].add(b); adjacency[b].add(a)
        path = [ends[0]]
        while path[-1] != ends[1]:
            choices = adjacency[path[-1]] - set(path)
            if len(choices) != 1:
                raise RuntimeError("CONS points do not form a unique mapped free-edge path")
            path.append(choices.pop())
        if set(path) != ids or len(path) != side["segments"]+1:
            raise RuntimeError("mapped boundary does not preserve all native CONS points")
        paths.append(path)
    return paths


def _audit(prepared, after, created_shells, created_nodes, *, observed=None):
    before = prepared["before"]
    if before["cad_sha256"] != after["cad_sha256"]:
        raise RuntimeError("native CAD export changed during mapped meshing")
    if before["perimeters"] != after["perimeters"]:
        raise RuntimeError("mapped meshing changed native perimeter point identities")
    old, new = native._scope_map(before), native._scope_map(after)
    expected_added = {("SHELL", i) for i in created_shells} | {("GRID", i) for i in created_nodes}
    if old.keys()-new.keys() or new.keys()-old.keys() != expected_added:
        raise RuntimeError("mapped meshing removed original entities or created unrelated entities")
    allowed_parts = {f["part_id"] for f in prepared["source_faces"]}
    sources = {f["id"] for f in prepared["source_faces"]}
    source_cons = {r['cons_id'] for r in prepared['witness']['boundaries']}
    adjacent = {i for r in prepared["witness"]["boundaries"] for i in r["face_ids"]}
    changes = []
    for key, row in old.items():
        a, b = copy.deepcopy(row), copy.deepcopy(new[key])
        if key[0]=='FACE' and key[1] in sources:
            source_face=next(f for f in prepared['source_faces'] if f['id']==key[1])
            expected={'Meshed With':'4 SIDED','Type':'QUAD','Num.Trias':0,'Num.Quads':source_face.get('shell_count')}
            for field,value in expected.items():
                if field in a['fields'] or field in b['fields']:
                    if (field not in a['fields'] or field not in b['fields'] or value is None
                            or type(b['fields'][field]) is not type(value) or b['fields'][field]!=value):
                        raise RuntimeError('source FACE native mapped state/count disagrees with independently read CQUAD4: '+field)
                    x,y=a['fields'].pop(field),b['fields'].pop(field)
                    if x!=y:changes.append({'type':'FACE','id':key[1],'field':field,'before':x,'after':y,'reason':'verified_source_mesh_state'})
        if key[0] == "CONS" and key[1] in source_cons:
            # These vectors are cached perimeter chord measurements, not CAD
            # definitions. Native 4Side reevaluation can refresh the display
            # even after all original GRID coordinates have been restored.
            for field in ('Start Vector','End Vector'):
                x,y=a['fields'].pop(field,None),b['fields'].pop(field,None)
                if x!=y:changes.append({'type':'CONS','id':key[1],'field':field,'before':x,'after':y,'reason':'native_perimeter_display'})
        if (key[0]=='CONS' and key[1] in source_cons) or (key[0]=='FACE' and key[1] in adjacent):
            x,y=a.get('geometry',{}).get('bounds'),b.get('geometry',{}).get('bounds')
            if x!=y and isinstance(x,list) and isinstance(y,list) and len(x)==len(y)==6:
                if all(type(u) in (int,float) and type(v) in (int,float) and math.isfinite(u) and math.isfinite(v)
                       and abs(u-v)<=seeds._BOUNDING_DISPLAY_TOLERANCE for u,v in zip(x,y)):
                    b['geometry']['bounds']=x
                    changes.append({'type':key[0],'id':key[1],'field':'bounds','before':x,'after':y,'reason':'native_bounding_display'})
        if key[0]=='FACE' and key[1] in adjacent:
            x,y=a.get('geometry',{}).get('normal'),b.get('geometry',{}).get('normal')
            if x!=y and isinstance(x,list) and isinstance(y,list) and len(x)==len(y)==3:
                if (all(type(u) in (int,float) and math.isfinite(u) for u in x+y)
                        and all(abs(u-v)<=8*math.ulp(1.) for u,v in zip(x,y))
                        and math.isclose(math.hypot(*x),1.,abs_tol=1e-12)
                        and math.isclose(math.hypot(*y),1.,abs_tol=1e-12)):
                    b['geometry']['normal']=x
                    changes.append({'type':'FACE','id':key[1],'field':'normal','before':x,'after':y,'reason':'unit_normal_roundoff'})
        if key[0] == "ANSAPART" and key[1] in allowed_parts:
            for field in seeds._PART_TESSELLATION:
                x, y = a["fields"].pop(field, None), b["fields"].pop(field, None)
                if x != y:
                    changes.append({"type": key[0], "id": key[1], "field": field, "before": x, "after": y})
        if key[0] == "FACE" and key[1] in adjacent:
            for field in ("area", "center"):
                x = a.get("geometry", {}).get(field)
                y = b.get("geometry", {}).get(field)
                allow_adjacent=key[1] in prepared.get('unmeshed_tessellation_face_ids',[])
                if allow_adjacent and x != y:
                    valid=(isinstance(x,list) and isinstance(y,list) and len(x)==len(y)==3
                           and all(type(u) in (int,float) and math.isfinite(u) for u in x+y)) if field=='center' else (
                           type(x) in (int,float) and type(y) in (int,float) and math.isfinite(x) and math.isfinite(y) and x>0 and y>0)
                    if not valid:raise RuntimeError('unmeshed FACE has invalid native tessellation measurement')
                elif key[1] not in sources and x != y:
                    if field == "center":
                        valid = (isinstance(x, list) and isinstance(y, list) and len(x) == len(y) == 3
                                 and all(type(u) in (int,float) and type(v) in (int,float)
                                         and math.isfinite(u) and math.isfinite(v) and abs(u-v) <= _ROUNDING_MM
                                         for u,v in zip(x,y)))
                    else:
                        valid = (type(x) in (int,float) and type(y) in (int,float)
                                 and math.isfinite(x) and math.isfinite(y) and x > 0 and y > 0
                                 and math.isclose(x,y,rel_tol=1e-12,abs_tol=0.))
                    if not valid:
                        raise RuntimeError("adjacent FACE display measurement changed beyond numerical roundoff")
                a.get("geometry", {}).pop(field, None)
                b.get("geometry", {}).pop(field, None)
                if x != y:
                    changes.append({"type": "FACE", "id": key[1], "field": field, "before": x, "after": y,
                                    "reason": "source_tessellation" if key[1] in sources else (
                                        "unmeshed_native_tessellation" if allow_adjacent else "adjacent_display_roundoff")})
        if a != b:
            if observed is not None:
                observed['protected_scope_differences']=[{'entity':{'type':key[0],'id':key[1]},
                    'differences':{field:{'before':a.get(field),'after':b.get(field)}
                        for field in a.keys()|b.keys() if a.get(field)!=b.get(field)}}]
            raise RuntimeError("mapped meshing changed protected definition, geometry, ownership or FE: " + str(key))
    return changes


def _check_new_grids(base, deck, node_ids):
    for ident in node_ids:
        row = seeds._card(base, deck, features._entity(base, deck, "GRID", ident))
        for name in ("CD", "PS", "SEID"):
            if name not in row["fields"]:
                raise RuntimeError("new mapped GRID lacks readable " + name)
            value = row["fields"][name]
            if not (value is None or type(value) is str and value == ""
                    or type(value) in (int, float) and value == 0):
                raise RuntimeError("new mapped GRID has nondefault displacement coordinates, constraint or superelement: " + name)


def _observe_entities(base, deck, prepared, observed):
    old = native._scope_map(prepared["before"])
    entities = seeds._collect(base, deck, None, "__ALL_ENTITIES__")
    current = {(e.ansa_type(deck), e._id) for e in entities}
    added, removed = current-old.keys(), old.keys()-current
    observed.update(created_shell_ids=sorted(i for t,i in added if t == "SHELL"),
                    new_grid_ids=sorted(i for t,i in added if t == "GRID"),
                    observed_added_entities=[{"type":t,"id":i} for t,i in sorted(added)],
                    observed_removed_entities=[{"type":t,"id":i} for t,i in sorted(removed)])
    observed["created_shell_count"] = len(observed["created_shell_ids"])


def _readback(state, base, mesh, deck, prepared, observed=None):
    from ansa import calc
    if observed is None:
        observed = {}
    guards._same_context(state, deck, prepared["context"])
    _observe_entities(base, deck, prepared, observed)
    after = seeds._guard_snapshot(state, base, deck, materialize=True, budget_base=prepared["budget_base"])
    old, new = native._scope_map(prepared["before"]), native._scope_map(after)
    created = sorted(i for t, i in new.keys()-old.keys() if t == "SHELL")
    node_ids = sorted(i for t, i in new.keys()-old.keys() if t == "GRID")
    observed.update(created_shell_ids=created, created_shell_count=len(created), new_grid_ids=node_ids,
                    source_geometry_unchanged=after["cad_sha256"] == prepared["before"]["cad_sha256"],
                    after_scope_sha256=features._hash(after))
    if not created or len(created) > _MAX_SHELLS:
        raise RuntimeError("native mapped mesh produced no SHELL or exceeded the output budget")
    elements = native._read_elements(base, deck, "SHELL", created, {"CQUAD4"})
    _check_new_grids(base, deck, node_ids)
    coords = {r["id"]: r["xyz"] for r in native._read_grids(base, deck, sorted({n for r in elements for n in r["nodes"]}))}
    rows = {r["cons_id"]: seeds._boundary(base, deck, features._entity(base, deck, "CONS", r["cons_id"]), include_points=True)
            for r in prepared["witness"]["boundaries"]}
    if list(rows.values()) != prepared["witness"]["boundaries"]:
        raise RuntimeError("mapped meshing changed existing CONS seeds, settings or point coordinates")
    attached, face_rows, projections = set(), [], []
    for prior in prepared["source_faces"]:
        face, snapshot, ids = meshing._face_snapshot(base, mesh, deck, prior["id"])
        stable = {k:v for k,v in snapshot.items() if k not in ("shell_ids", "shell_count", "shell_ids_truncated", "meshed")}
        original = {k:v for k,v in prior["snapshot"].items() if k not in ("shell_ids", "shell_count", "shell_ids_truncated", "meshed")}
        if stable != original or len(ids) != prior["shell_count"] or attached.intersection(ids):
            raise RuntimeError("mapped FACE identity, macro membership or SHELL attribution changed")
        attached.update(ids)
        own = [r for r in elements if r["id"] in set(ids)]
        if len(own) != len(ids) or any(r["property_id"] != meshing._field_id(snapshot["fields"]["PID"], "FACE PID") for r in own):
            raise RuntimeError("mapped SHELL property or FACE attribution is inconsistent")
        for ident in ids:
            part = base.GetEntityPart(features._entity(base, deck, "SHELL", ident))
            if part is None or part._id != prior["part_id"]:
                raise RuntimeError("mapped SHELL has incorrect Part ownership")
        paths = _boundary_paths(own, rows, prior["sides"])
        lattice = verify_lattice(own, paths)
        maximum = 0.
        for node_id in sorted({n for r in own for n in r["nodes"]}):
            point = coords[node_id]
            projected = calc.ProjectPointToContainer(point, [face])
            if not isinstance(projected, (list, tuple)) or len(projected) != 5:
                raise RuntimeError("native source FACE projection is unavailable")
            nearest = native._point(list(projected[0]), "projected coordinates")
            distance = native._finite(projected[1], "native projection distance")
            observed_distance = math.dist(point, nearest)
            entity = projected[4]
            if (distance < 0 or observed_distance > _PROJECTION_MM or distance > _PROJECTION_MM
                    or abs(distance-observed_distance) > _ROUNDING_MM or entity is None
                    or catalog._entity_reference(base, deck, entity, "FACE")["id"] != prior["id"]):
                raise RuntimeError("mapped GRID does not project onto its original source FACE")
            maximum = max(maximum, distance, observed_distance)
        projections.append({"face_id": prior["id"], "maximum_distance_mm": maximum,
                            "tolerance_mm": _PROJECTION_MM, "point_count": lattice["node_count"]})
        face_rows.append({"face_id": prior["id"], "shell_ids": ids, "topology": lattice})
    if attached != set(created) or not set(node_ids) <= set(coords):
        raise RuntimeError("created mapped mesh is not confined to the selected FACE macros")
    changes = _audit(prepared, after, created, node_ids, observed=observed)
    guards._same_context(state, deck, prepared["context"])
    return {"created_shell_ids": created, "created_shell_count": len(created), "new_grid_ids": node_ids,
            "faces_after": face_rows, "elements": elements,
            "nodes": [{"id": k, "xyz": v} for k,v in sorted(coords.items())],
            "source_geometry_unchanged": True, "seeds_preserved": True,
            "lattice_verified": True, "point_projections": projections, "tessellation_changes": changes,
            "after_scope_sha256": features._hash(after)}


def apply_prepared(state, base, mesh, constants, params, prepared):
    deck = params["deck"]
    result = {"ok": False, "scope_verified": False, "tool": "mesh_surface", "deck": deck,
              "method": "mapped_existing_seeds", "element_type": "quad", "mesh_started": False,
              "parameters_restored": False, "roundoff_restorations": [], "possible_effects": False,
              "source_geometry_unchanged": False, "seeds_preserved": False,
              "seed_snapshot_sha256": prepared["witness"]["seed_snapshot_sha256"]}
    errors, backup = [], None
    try:
        guards._same_context(state, deck, prepared["context"])
        backup = native._verify_backup(base, constants, *prepared["backup_preflight"])
        result["backup"] = backup
        result["possible_effects"] = True  # A failed save/materialization may have effects.
        guards._same_context(state, deck, prepared["context"])
        if seeds._guard_snapshot(state, base, deck) != prepared["before"]:
            raise RuntimeError("model changed during backup; mapped meshing was not started")
        audited, materialized = seeds._materialize_points(state, base, deck, prepared)
        audited["budget_base"] = prepared["before"]
        result["perimeter_materialization"] = materialized
        prepared = audited
        result["before_scope_sha256"] = features._hash(prepared["before"])
        before_settings = meshing._settings(base)
        result["settings_before"] = before_settings
        path = Path(state.session_dir) / ("mapped-parameters-" + uuid.uuid4().hex + ".ansa_mpar")
        result["parameter_backup"] = str(path)
        saved = mesh.SaveMeshParams(str(path))
        if type(saved) is not int or saved != 1 or not path.is_file() or not path.stat().st_size:
            raise RuntimeError("native mesh parameter backup was not confirmed")
        try:
            status = base.BCSettingsSetValues({"mesh_change_option":"mesh", "element_type":"quad", "element_order":"1st Order"})
            current = meshing._settings(base)
            result["settings_used"] = current
            if (type(status) is not int or status != 0
                    or current["mesh_change_option"] != "mesh" or current["element_type"].casefold() != "quad"
                    or current["element_order"] != "1st Order"):
                raise RuntimeError("mapped mesh temporary settings were not confirmed")
            guards._same_context(state, deck, prepared["context"])
            result["mesh_started"] = True
            returned = mesh.Create4SidedMesh([f["entity"] for f in prepared["source_faces"]],
                only_4sided=True, only_aligned=True, corner_angle=60., smooth_type="standard",
                split_method="none", spacing="off", elem_type="quad", quad_pattern="directional",
                align_hots="off", only_unmeshed=True, protect_selection_bounds=True)
            meshed = getattr(returned, "meshed_ents", None)
            if not isinstance(meshed, (list, tuple)):
                raise RuntimeError("Create4SidedMesh did not return readable meshed_ents")
            ids = [catalog._entity_reference(base, deck, e, "FACE")["id"] for e in meshed]
            result["native_meshed_face_ids"] = sorted(ids)
            if sorted(ids) != sorted(f["id"] for f in prepared["source_faces"]):
                raise RuntimeError("native mapped mesher skipped or added selected FACE")
        finally:
            # Restoration attempts are independent, even when a native call raises.
            try:
                restored = mesh.ReadMeshParams(str(path))
                if type(restored) is not int or restored != 1:
                    errors.append("ReadMeshParams did not confirm restoration")
            except Exception as exc:
                errors.append("ReadMeshParams: " + native._error_text(exc))
            try:
                mode = base.BCSettingsSetValues({"mesh_change_option": before_settings["mesh_change_option"]})
                if type(mode) is not int or mode != 0:
                    errors.append("mesh_change_option restoration failed")
            except Exception as exc:
                errors.append("mesh_change_option restoration: " + native._error_text(exc))
            result["settings_after"] = meshing._settings(base)
            result["parameters_restored"] = not errors and result["settings_after"] == before_settings
            if not result["parameters_restored"]:
                errors.append("native mesh parameters were not restored exactly")
    except Exception as exc:
        errors.append(native._error_text(exc))
        if backup is None:
            result["backup"] = native._unconfirmed_backup(prepared["backup_preflight"][0])
    try:
        guards._same_context(state, deck, prepared["context"])
        _observe_entities(base, deck, prepared, result)
    except Exception as exc:
        errors.append("mapped entity observation: " + native._error_text(exc))
    if result["mesh_started"]:
        try:
            guards._same_context(state, deck, prepared["context"])
            _restore_boundary_roundoff(base, deck, prepared, result["roundoff_restorations"], state=state)
        except Exception as exc:
            errors.append("mapped boundary restoration: " + native._error_text(exc))
        try:
            guards._same_context(state, deck, prepared["context"])
            result.update(_readback(state, base, mesh, deck, prepared, observed=result))
        except Exception as exc:
            errors.append("independent mapped readback: " + native._error_text(exc))
    result.update(ok=not errors, scope_verified=not errors, errors=errors,
                  error="; ".join(errors) if errors else None, recovery_required=bool(errors),
                  original_operation_must_be_queried=bool(errors), usable_for_mutation=False,
                  quality_verified=False,
                  limitations=["four native open CONS and one unmeshed FACE per macro; at most 200 CQUAD4",
                    "native integer seeds and all final original GRID cards are preserved exactly",
                    "only measured target-boundary rounding <=1e-12 mm may be explicitly restored and recorded",
                    "native source-node projection and lattice checks are not continuous CAD coverage proof",
                    "mesh quality, source/target sweep correspondence and solid volume coverage require separate checks",
                    "no automatic reseed, length application, rollback, reopen or retry"])
    return result
