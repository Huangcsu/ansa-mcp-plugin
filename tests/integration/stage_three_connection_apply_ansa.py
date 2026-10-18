"""Owned single SpotweldPoint RBAR realization and independent scope acceptance."""
from collections import Counter
from contextlib import asynccontextmanager
from datetime import timedelta
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import stage_three_ansa as common
import stage_three_api_catalog_ansa as owned
from stage_three_connections_ansa import independent_script

ROOT = Path(__file__).resolve().parents[2]

FIXED_SETTINGS = {
    "SpotweldPoint_Type": "RBAR", "SpotweldPoint_RBAR_CNA": "123456",
    "SpotweldPoint_RBAR_CNB": "0", "SpotweldPoint_RBAR_CMA": "0",
    "SpotweldPoint_RBAR_CMB": "0", "SpotweldPoint_RBAR_SearchDist": "",
    "SpotweldPoint_RBAR_UseNearestNode": "n",
    "SpotweldPoint_RBAR_FourQuadsAroundProjectionPoint": "n",
    "SpotweldPoint_RBAR_CutOffAdhesives": "0",
}

def remote_fixture(expected_pid, expected_args, fixture_path, seed_entity_id):
    """Import two independent shells and bind each to its own ANSAPART."""
    import os
    import threading
    from ansa import base, constants, session

    if (os.getpid() != expected_pid
            or list(session.ProgramArguments()) != expected_args
            or threading.current_thread() is not threading.main_thread()
            or base.CurrentDeck() != constants.NASTRAN):
        raise RuntimeError("Owned identity changed before connector fixture")

    deck = constants.NASTRAN
    if base.CollectEntities(deck, None, "__ELEMENTS__"):
        raise RuntimeError("Connector fixture requires an empty FE model")
    grids = base.CollectEntities(deck, None, "GRID")
    seed = next((item for item in grids if item._id == seed_entity_id), None)
    if seed is None:
        raise RuntimeError("Owned seed GRID is unavailable before fixture input")
    seed_fields = seed.card_fields(deck)
    seed_requested = tuple(name for name in ("X1", "X2", "X3") if name in seed_fields)
    seed_card = base.GetEntityCardValues(deck, seed, seed_requested)
    if (set(seed_card) != set(seed_requested)
            or [seed_card.get(name) for name in ("X1", "X2", "X3")] != [1.0, 2.0, 3.0]):
        raise RuntimeError("Owned seed GRID coordinates differ before fixture input")
    if base.CollectEntities(deck, None, "SpotweldPoint_Type"):
        raise RuntimeError("Connector fixture requires no preexisting SpotweldPoints")

    status = base.InputNastran(
        fixture_path,
        nodes_id="nooffset",
        elements_id="nooffset",
        properties_id="nooffset",
        materials_id="nooffset",
        header="skip",
        model_action="merge_model",
    )
    if status != 1:
        raise RuntimeError("Connector NASTRAN fixture input failed")

    shells = {item._id: item for item in base.CollectEntities(deck, None, "SHELL")}
    if set(shells) != {101, 102, 103}:
        raise RuntimeError("Connector fixture did not create the two target and outside witness shells")
    fixture_grids = base.CollectEntities(deck, None, "GRID")
    if {item._id for item in fixture_grids} != {
            seed_entity_id, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112}:
        raise RuntimeError("Connector fixture GRID range differs or the seed GRID changed")
    expected_grid_positions = {
        101: (0.0, 0.0, 0.0), 102: (10.0, 0.0, 0.0),
        103: (10.0, 10.0, 0.0), 104: (0.0, 10.0, 0.0),
        105: (0.0, 0.0, 2.0), 106: (10.0, 0.0, 2.0),
        107: (10.0, 10.0, 2.0), 108: (0.0, 10.0, 2.0),
        109: (30.0, 0.0, 0.0), 110: (40.0, 0.0, 0.0),
        111: (40.0, 10.0, 0.0), 112: (30.0, 10.0, 0.0),
    }
    for grid in fixture_grids:
        if grid._id not in expected_grid_positions:
            continue
        labels = grid.card_fields(deck)
        requested = tuple(name for name in ("X1", "X2", "X3") if name in labels)
        values = base.GetEntityCardValues(deck, grid, requested)
        actual = tuple(values.get(name) for name in ("X1", "X2", "X3"))
        if set(values) != set(requested) or actual != expected_grid_positions[grid._id]:
            raise RuntimeError("Connector fixture GRID coordinate differs: " + str(grid._id))

    expected_shell_cards = {
        101: {"type": "CQUAD4", "PID": 101, "G1": 101, "G2": 102, "G3": 103, "G4": 104},
        102: {"type": "CQUAD4", "PID": 102, "G1": 105, "G2": 106, "G3": 107, "G4": 108},
        103: {"type": "CQUAD4", "PID": 103, "G1": 109, "G2": 110, "G3": 111, "G4": 112},
    }
    for element_id, expected in expected_shell_cards.items():
        shell = shells[element_id]
        labels = shell.card_fields(deck)
        requested = tuple(name for name in expected if name in labels)
        values = base.GetEntityCardValues(deck, shell, requested)
        if set(values) != set(requested) or values != expected:
            raise RuntimeError("Connector fixture CQUAD4 card differs: " + str(element_id))

    pshells = base.CollectEntities(deck, None, "PSHELL")
    if len(pshells) != 3:
        raise RuntimeError("Connector fixture did not create three PSHELL properties")
    for prop in pshells:
        if prop._id not in (101, 102, 103):
            raise RuntimeError("Connector fixture PSHELL IDs differ")
        labels = prop.card_fields(deck)
        requested = tuple(name for name in ("MID1", "T") if name in labels)
        values = base.GetEntityCardValues(deck, prop, requested)
        if set(values) != set(requested) or values != {"MID1": 101, "T": 1.0}:
            raise RuntimeError("Connector fixture PSHELL card differs")
    materials = base.CollectEntities(deck, None, "MAT1")
    if len(materials) != 1 or materials[0]._id != 101:
        raise RuntimeError("Connector fixture did not create its MAT1 material")
    material_labels = materials[0].card_fields(deck)
    material_requested = tuple(name for name in ("E", "NU", "RHO") if name in material_labels)
    material_card = base.GetEntityCardValues(deck, materials[0], material_requested)
    if (set(material_card) != set(material_requested)
            or material_card != {"E": 210000.0, "NU": 0.3, "RHO": 7.85e-9}):
        raise RuntimeError("Connector fixture MAT1 card differs")

    parts = []
    for element_id, label in ((101, "a"), (102, "b"), (103, "outside")):
        part = base.NewPart("connection apply probe part " + label,
                            "connection-apply-probe-" + label)
        shell = shells[element_id]
        if (part is None or part.ansa_type(deck) != "ANSAPART"
                or base.SetEntityPart(shell, part) != 1):
            raise RuntimeError("Connector fixture Part assignment failed")
        assigned = base.CollectEntities(deck, part, "SHELL")
        if [item._id for item in assigned] != [element_id]:
            raise RuntimeError("Connector fixture Part assignment did not read back")
        if element_id in (101, 102):
            parts.append({"deck": deck, "type": part.ansa_type(deck), "id": part._id})

    seed_after = next((item for item in base.CollectEntities(deck, None, "GRID")
                       if item._id == seed_entity_id), None)
    if seed_after is None:
        raise RuntimeError("Owned seed GRID disappeared during fixture input")
    seed_after_fields = seed_after.card_fields(deck)
    seed_after_requested = tuple(name for name in ("X1", "X2", "X3")
                                 if name in seed_after_fields)
    seed_after_card = base.GetEntityCardValues(deck, seed_after, seed_after_requested)
    if ([seed_after_card.get(name) for name in ("X1", "X2", "X3")] != [1.0, 2.0, 3.0]
            or seed_after_card != seed_card):
        raise RuntimeError("Owned seed GRID changed during connector fixture input")

    return {
        "pid": os.getpid(),
        "program_args": list(session.ProgramArguments()),
        "main_thread": True,
        "deck": deck,
        "database": base.DataBaseName(),
        "seed_entity_id": seed_entity_id,
        "seed_card": seed_after_card,
        "parts": parts,
        "shell_ids": sorted(shells),
    }

def remote_snapshot(expected_pid, expected_args, part_refs, seed_entity_id):
    """Independently capture homogeneous native cards and connection settings."""
    import math
    import os
    import threading
    from ansa import base, constants, connections, session, mesh

    if (os.getpid() != expected_pid
            or list(session.ProgramArguments()) != expected_args
            or threading.current_thread() is not threading.main_thread()
            or base.CurrentDeck() != constants.NASTRAN):
        raise RuntimeError("Owned identity changed before connector readback")
    deck = constants.NASTRAN

    def ref(entity):
        return {"deck": deck, "type": entity.ansa_type(deck), "id": entity._id}

    def plain(value):
        if value is None or type(value) in (str, int, float, bool):
            return value
        if isinstance(value, dict):
            return {str(key): plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        if hasattr(value, "_id") and hasattr(value, "ansa_type"):
            return ref(value)
        raise RuntimeError("Independent card contains an unsupported JSON value")

    def capture(entity, with_position=False):
        labels = entity.card_fields(deck)
        if (type(labels) not in (list, tuple)
                or any(type(label) is not str for label in labels)):
            raise RuntimeError("Native card_fields readback is unavailable")
        fields = tuple(labels)
        values = base.GetEntityCardValues(deck, entity, fields) if fields else {}
        if type(values) is not dict or set(values) != set(fields):
            raise RuntimeError("Native full-card readback is incomplete")
        row = {"entity": ref(entity), "card_fields": list(fields), "card": plain(values)}
        if with_position:
            position = list(entity.position)
            if (len(position) != 3
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                           for value in position)):
                raise RuntimeError("Point-like entity position readback is invalid")
            row["position"] = position
        return row

    def collect_rows(kind, with_position=False):
        entities = base.CollectEntities(deck, None, kind)
        if type(entities) not in (list, tuple):
            raise RuntimeError("Native entity collection is unavailable: " + kind)
        return [capture(entity, with_position)
                for entity in sorted(entities, key=lambda item: item._id)]

    grids = collect_rows("GRID", with_position=True)
    shells = collect_rows("SHELL")
    pshells = collect_rows("PSHELL")
    psolids = collect_rows("PSOLID")
    materials = collect_rows("MAT1")
    points = collect_rows("SpotweldPoint_Type", with_position=True)
    elements = base.CollectEntities(deck, None, "__ELEMENTS__")
    if type(elements) not in (list, tuple):
        raise RuntimeError("Native FE collection is unavailable")
    element_rows = [capture(entity) for entity in sorted(
        elements, key=lambda item: (item.ansa_type(deck), item._id))]

    grid_by_id = {row["entity"]["id"]: row["entity"] for row in grids}
    seed_rows = [row for row in grids if row["entity"]["id"] == seed_entity_id]
    if (len(seed_rows) != 1
            or [seed_rows[0]["card"].get(name) for name in ("X1", "X2", "X3")]
            != [1.0, 2.0, 3.0]):
        raise RuntimeError("Owned seed GRID1 card did not survive connector operation")

    point_entities = base.CollectEntities(deck, None, "SpotweldPoint_Type")
    if len(point_entities) != len(points):
        raise RuntimeError("SpotweldPoint_Type collection changed during homogeneous readback")
    point_by_id = {row["entity"]["id"]: row for row in points}
    for entity in point_entities:
        row = point_by_id.get(entity._id)
        if row is None or row["entity"]["type"] != entity.ansa_type(deck):
            raise RuntimeError("Point card was not captured from its same-type collected handle")
        labels = set(row["card_fields"])
        if not {"P1", "P2"}.issubset(labels):
            raise RuntimeError("SpotweldPoint card lacks explicit P1/P2 fields")
        row["parts"] = []
        for field in ("P1", "P2"):
            text = row["card"].get(field)
            if type(text) is not str:
                raise RuntimeError("SpotweldPoint connectivity is not a readable string")
            row["parts"].append([ref(part) for part in connections.ConnectivityStringToEnts(text)])
        settings = connections.GetAllConnectionSettings(entity)
        if settings is not None and type(settings) is not dict:
            raise RuntimeError("GetAllConnectionSettings returned an unexpected value")
        row["settings"] = plain(settings)

    all_part_entities = base.CollectEntities(deck, None, "ANSAPART")
    resolved_parts = {}
    for part_ref in part_refs:
        part = next((item for item in all_part_entities
                     if item._id == part_ref["id"]
                     and item.ansa_type(deck) == part_ref["type"]), None)
        if part is None:
            raise RuntimeError("Fixture ANSAPART cannot be independently resolved")
        resolved_parts[part._id] = part

    parts = []
    for part in sorted(all_part_entities, key=lambda item: item._id):
        members = base.CollectEntities(deck, part, "__ELEMENTS__")
        part_card = capture(part)
        parts.append({
            "entity": ref(part),
            "card_fields": part_card["card_fields"], "card": part_card["card"],
            "fixture_part": part._id in resolved_parts,
            "elements": sorted((ref(item) for item in members),
                               key=lambda item: (item["type"], item["id"])),
        })

    perimeters = []
    perimeter_entities = base.CollectEntities(deck, None, "FE PERIMETER")
    if type(perimeter_entities) not in (list, tuple) or len(perimeter_entities) > 10000:
        raise RuntimeError("Independent FE PERIMETER collection is unavailable or over limit")
    for perimeter in sorted(perimeter_entities, key=lambda item: item._id):
        row = capture(perimeter)
        adjacent = mesh.GetFEPerimeterShells([perimeter], False)
        if type(adjacent) not in (list, tuple) or len(adjacent) > 10000:
            raise RuntimeError("Independent FE PERIMETER edge-shell collection is incomplete")
        row["edge_shells"] = sorted((ref(item) for item in adjacent),
            key=lambda item: (item["type"], item["id"]))
        if (any(item["type"] != "SHELL" for item in row["edge_shells"])
                or len({item["id"] for item in row["edge_shells"]}) != len(adjacent)):
            raise RuntimeError("Independent FE PERIMETER has invalid or duplicate edge-shell references")
        perimeters.append(row)

    return {
        "pid": os.getpid(),
        "program_args": list(session.ProgramArguments()),
        "main_thread": True,
        "version": str(constants.app_version),
        "fe_perimeters": perimeters,
        "deck": deck,
        "database": base.DataBaseName(),
        "seed": seed_rows[0],
        "grids": grids,
        "shells": shells,
        "properties": {"PSHELL": pshells, "PSOLID": psolids},
        "materials": {"MAT1": materials},
        "elements": element_rows,
        "points": points,
        "parts": parts,
        "grid_ids": sorted(grid_by_id),
        "all_property_cards": collect_rows("__PROPERTIES__"),
        "all_material_cards": collect_rows("__MATERIALS__"),
        "references": {kind: sorted((ref(e) for e in base.CollectEntities(deck, None, kind)),
            key=lambda item: (item["type"], item["id"]))
            for kind in ("__ALL_ENTITIES__", "__ELEMENTS__", "__PROPERTIES__", "__MATERIALS__")},
    }


def write_fixture(path):
    lines = ["BEGIN BULK", "MAT1,101,210000.,,0.3,7.85E-9"]
    lines += [f"PSHELL,{n},101,1." for n in (101,102,103)]
    coordinates = [(0,0,0),(10,0,0),(10,10,0),(0,10,0),
        (0,0,2),(10,0,2),(10,10,2),(0,10,2),
        (30,0,0),(40,0,0),(40,10,0),(30,10,0)]
    lines += [f"GRID,{101+i},,{x}.,{y}.,{z}." for i,(x,y,z) in enumerate(coordinates)]
    lines += ["CQUAD4,101,101,101,102,103,104", "CQUAD4,102,102,105,106,107,108",
              "CQUAD4,103,103,109,110,111,112", "ENDDATA"]
    path.write_text("\n".join(lines)+"\n",encoding="ascii")


def rows(values):
    return {row["entity"]["id"]:row for row in values}


def stable(card, omitted=()):
    ignored={"numelem",*omitted}
    return {key:value for key,value in card.items()
        if "".join(c.casefold() for c in key if c.isalnum()) not in ignored}


def content(snapshot):
    return {key:value for key,value in snapshot.items() if key!="database"}


def point_at(snapshot, position):
    candidates=[row for row in snapshot["points"] if row["position"]==position]
    if len(candidates)!=1:raise AssertionError("Independent point position is ambiguous")
    return candidates[0]


def request(snapshot, part_refs, backup):
    point=point_at(snapshot,[5.,5.,1.])
    parts=rows(snapshot["parts"]);shells=rows(snapshot["shells"])
    props=rows(snapshot["properties"]["PSHELL"]);materials=rows(snapshot["materials"]["MAT1"])
    targets=[]
    for part_ref,shell_id in zip(part_refs,(101,102)):
        part=parts[part_ref["id"]];shell=shells[shell_id]
        prop_id=shell["card"]["PID"];prop=props[prop_id]
        mat_id=prop["card"]["MID1"];material=materials[mat_id]
        targets.append({"part_id":part_ref["id"],"part_expected_fields":stable(part["card"]),
            "shell_id":shell_id,"shell_expected_fields":stable(shell["card"]),
            "property_id":prop_id,"property_expected_fields":stable(prop["card"]),
            "material_id":mat_id,"material_expected_fields":stable(material["card"])})
    return {"deck":snapshot["deck"],"point_id":point["entity"]["id"],
        "expected_fields":point["card"],"target_parts":targets,"backup_path":str(backup)}


def check_lumped_part_summary(snapshot, part, recognition):
    """Independently sum area-derived nodal masses and their central moments."""
    shells=rows(snapshot["shells"]);grids=rows(snapshot["grids"])
    props=rows(snapshot["properties"]["PSHELL"]);materials=rows(snapshot["materials"]["MAT1"])
    weighted_nodes=[];area=0.
    for member in part["elements"]:
        values=shells[member["id"]]["card"]
        n=4 if values["type"]=="CQUAD4" else 3
        positions=[grids[values[f"G{i}"]]["position"] for i in range(1,n+1)]
        polygon_area=abs(sum(positions[i][0]*positions[(i+1)%n][1]-positions[(i+1)%n][0]*positions[i][1]
            for i in range(n)))/2.
        prop=props[values["PID"]]["card"];rho=materials[prop["MID1"]]["card"]["RHO"]
        if (type(rho) not in (int,float) or not math.isfinite(rho) or rho<=0
                or prop["NSM"] is not None and (type(prop["NSM"]) not in (int,float) or prop["NSM"]!=0)):
            raise AssertionError("Independent lumped Part measures lack verified density/NSM")
        mass=polygon_area*prop["T"]*rho
        weighted_nodes.extend((position,mass/n) for position in positions)
        area+=polygon_area
    mass=sum(m for _,m in weighted_nodes)
    center=[sum(p[i]*m for p,m in weighted_nodes)/mass for i in range(3)]
    inertia={"Ixx":0.,"Iyy":0.,"Izz":0.,"Ixy":0.,"Ixz":0.,"Iyz":0.}
    for position,weight in weighted_nodes:
        x,y,z=[position[i]-center[i] for i in range(3)]
        for field,value in {"Ixx":y*y+z*z,"Iyy":x*x+z*z,"Izz":x*x+y*y,
                "Ixy":-x*y,"Ixz":-x*z,"Iyz":-y*z}.items():
            inertia[field]+=weight*value
    for field,value in {"Area":area,"Mass":mass,**inertia}.items():
        actual=part["card"].get(field)
        if type(actual) not in (int,float) or not math.isfinite(actual) or not math.isclose(actual,value,rel_tol=1e-9,abs_tol=1e-20):
            raise AssertionError("Independent lumped Part measure differs: "+field)
    for field,value in zip(("COG x","COG y","COG z"),center):
        actual=part["card"].get(field)
        if type(actual) not in (int,float) or not math.isfinite(actual) or not math.isclose(actual,value,rel_tol=1e-9,abs_tol=1e-9):
            raise AssertionError("Independent Part center differs: "+field)
    stamp=part["card"].get("Last Edit")
    if part["card"].get("Features Status")!=recognition or type(stamp) is not str or not stamp.strip() or len(stamp)>128:
        raise AssertionError("Part recognition state or edit stamp is unavailable")


def same_persisted_model(reference, current, part_refs):
    """Allow only independently recomputed target summary roundoff after Open."""
    left,right=copy.deepcopy(content(reference)),copy.deepcopy(content(current))
    old_parts,new_parts=rows(left["parts"]),rows(right["parts"])
    measures=("Area","Mass","COG x","COG y","COG z","Ixx","Iyy","Izz","Ixy","Ixz","Iyz")
    for part_ref in part_refs:
        key=part_ref["id"]
        if key not in old_parts or key not in new_parts:return False
        recognition=old_parts[key]["card"].get("Features Status")
        if recognition not in ("Not Recognized","Recognized (FE Perimeters)"):return False
        check_lumped_part_summary(reference,old_parts[key],recognition)
        check_lumped_part_summary(current,new_parts[key],recognition)
        for field in measures:
            new_parts[key]["card"][field]=old_parts[key]["card"][field]
    return left==right


def independent_apply_checks(before, after, part_refs):
    if before["deck"]!=after["deck"] or before["database"]!=after["database"]:
        raise AssertionError("Connection realization changed database or deck")
    old_grids,new_grids=rows(before["grids"]),rows(after["grids"])
    if any(new_grids.get(key)!=row for key,row in old_grids.items()):
        raise AssertionError("Connection realization changed original GRID cards")
    added_grids=set(new_grids)-set(old_grids)
    if len(added_grids)!=2:raise AssertionError("Expected exactly two projection GRID entities")
    centers={}
    for key in added_grids:
        row=new_grids[key];position=row["position"]
        if position not in ([5.,5.,0.],[5.,5.,2.]):
            raise AssertionError("New GRID is not a target projection center")
        for name in ("CP","CD","PS","SEID"):
            if row["card"].get(name) not in (0,None,""):
                raise AssertionError("New projection GRID has unverified coordinate or constraint data")
        centers[position[2]]=key
    if len(centers)!=2:raise AssertionError("Projection GRID positions are duplicated")
    old_shells,new_shells=rows(before["shells"]),rows(after["shells"])
    if not set(old_shells).issubset(new_shells) or len(new_shells)!=9:
        raise AssertionError("Source shell IDs or expected nine-shell range changed")
    if new_shells[103]!=old_shells[103]:raise AssertionError("Outside witness SHELL changed")
    old_parts,new_parts=rows(before["parts"]),rows(after["parts"])
    selected={p["id"] for p in part_refs}
    if set(old_parts)!=set(new_parts):raise AssertionError("Part identity set changed")
    for key,row in old_parts.items():
        if key not in selected and new_parts[key]!=row:
            raise AssertionError("Outside Part or membership changed")
        derived={"area","mass","cogx","cogy","cogz","ixx","ixy","ixz","iyy","iyz","izz","featuresstatus","lastedit"}
        if key in selected and stable(row["card"],derived)!=stable(new_parts[key]["card"],derived):
            raise AssertionError("Selected Part stable card changed")
        if key in selected:
            check_lumped_part_summary(before,row,"Not Recognized")
            check_lumped_part_summary(after,new_parts[key],"Recognized (FE Perimeters)")
    for part_ref,source_id in zip(part_refs,(101,102)):
        source=old_shells[source_id]["card"]
        corner=[source[f"G{i}"] for i in range(1,5)]
        z=old_grids[corner[0]]["position"][2];center=centers[z]
        members=new_parts[part_ref["id"]]["elements"]
        if len(members)!=4 or any(r["type"]!="SHELL" for r in members):
            raise AssertionError("Target Part must contain exactly four triangle shells")
        triangles=[new_shells[r["id"]] for r in members]
        actual=set();edges=Counter();area=0.
        expected={tuple(sorted((corner[i],corner[(i+1)%4],center))) for i in range(4)}
        for tri in triangles:
            values=tri["card"]
            if values.get("type")!="CTRIA3" or values.get("PID")!=source["PID"]:
                raise AssertionError("Target triangle type or property differs")
            ids=[values[f"G{i}"] for i in (1,2,3)]
            if len(set(ids))!=3:raise AssertionError("Repeated triangle nodes")
            canonical=tuple(sorted(ids))
            if canonical in actual:raise AssertionError("Duplicate triangle connectivity")
            actual.add(canonical)
            points=[new_grids[n]["position"] for n in ids]
            if any(p[2]!=z for p in points):raise AssertionError("Triangle left the source plane")
            a,b,c=points
            signed=((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2
            if not math.isfinite(signed) or signed<=1e-9:
                raise AssertionError("Triangle has reversed or degenerate signed area")
            area+=signed
            for i in range(3):edges[tuple(sorted((ids[i],ids[(i+1)%3])))]+=1
            if stable(values,{"type","eid","g1","g2","g3","g4","t4"})!=stable(
                    source,{"type","eid","g1","g2","g3","g4","t4"}):
                raise AssertionError("Target triangle stable physical fields changed")
        if actual!=expected or not math.isclose(area,100.,rel_tol=1e-9):
            raise AssertionError("Triangles do not exactly partition the original quad")
        boundary={tuple(sorted((corner[i],corner[(i+1)%4]))) for i in range(4)}
        if any(count!=(1 if edge in boundary else 2) for edge,count in edges.items()):
            raise AssertionError("Target triangle boundary or internal edges are not closed")
    target_properties={old_shells[source_id]["card"]["PID"] for source_id in (101,102)}
    for kind in ("all_property_cards","all_material_cards"):
        old={(r["entity"]["type"],r["entity"]["id"]):r for r in before[kind]}
        new={(r["entity"]["type"],r["entity"]["id"]):r for r in after[kind]}
        if old.keys()!=new.keys():raise AssertionError("Property or material identities changed")
        for key,row in old.items():
            current=new[key]
            if kind=="all_property_cards" and key[0]=="PSHELL" and key[1] in target_properties:
                if (row["card_fields"]!=current["card_fields"]
                        or type(row["card"].get("Num.Elem")) is not int or row["card"]["Num.Elem"]!=1
                        or type(current["card"].get("Num.Elem")) is not int or current["card"]["Num.Elem"]!=4
                        or stable(row["card"])!=stable(current["card"])):
                    raise AssertionError("Target PSHELL must change only Num.Elem from one to four")
            elif row!=current:
                raise AssertionError("Outside property or material full card changed")
    old_elements={(r["entity"]["type"],r["entity"]["id"]):r for r in before["elements"]}
    new_elements={(r["entity"]["type"],r["entity"]["id"]):r for r in after["elements"]}
    if not set(old_elements).issubset(new_elements):raise AssertionError("Original FE removed")
    rbars=[r for r in after["elements"] if r["entity"]["type"]=="RBAR"]
    if len(rbars)!=1:raise AssertionError("Expected one independently read RBAR")
    rbar=rbars[0]["card"]
    if rbar.get("GA")!=centers[0.] or rbar.get("GB")!=centers[2.]:
        raise AssertionError("RBAR endpoints do not match the two ordered projection nodes")
    if any(type(rbar.get(k)) is not int or rbar[k]!=v
            for k,v in {"CNA":123456,"CNB":0,"CMA":0,"CMB":0}.items()):
        raise AssertionError("RBAR connection degrees of freedom differ")
    before_points,after_points=rows(before["points"]),rows(after["points"])
    if set(before_points)!=set(after_points):raise AssertionError("Point identity range changed")
    point=point_at(after,[5.,5.,1.]);old_point=before_points[point["entity"]["id"]]
    added_labels={"CNA","CNB","CMA","CMB","Search Dist","Use Nearest Node",
        "Four Quads around Projection point","Cut off Adhesives"}
    if set(point["card_fields"])!=set(old_point["card_fields"])|added_labels:
        raise AssertionError("Selected point dynamic field set exceeds the verified RBAR fields")
    mutable={"status","errorclass","fereptype","cna","cnb","cma","cmb","searchdist",
        "usenearestnode","fourquadsaroundprojectionpoint","cutoffadhesives"}
    if stable(point["card"],mutable)!=stable(old_point["card"],mutable) or point["parts"]!=old_point["parts"]:
        raise AssertionError("Selected point stable fields or Part connectivity changed")
    if point["settings"]!=FIXED_SETTINGS or point["card"].get("Status")!="Ok" or point["card"].get("FE Rep Type")!="RBAR":
        raise AssertionError("Selected point realization status or complete settings differs")
    if point["card"].get("Error Class")!="RBAR":
        raise AssertionError("Selected point Error Class differs from RBAR")
    for field,value in {"CNA":123456,"CNB":0,"CMA":0,"CMB":0}.items():
        if type(point["card"].get(field)) is not int or point["card"][field]!=value:
            raise AssertionError("Selected point rigid card degree differs: "+field)
    if (point["card"].get("Search Dist") is not None
            or any(point["card"].get(field)!="no" for field in (
                "Use Nearest Node","Four Quads around Projection point","Cut off Adhesives"))):
        raise AssertionError("Selected point realization card options differ from their fixed defaults")
    if any(after_points[key]!=row for key,row in before_points.items() if key!=point["entity"]["id"]):
        raise AssertionError("Unselected point or settings changed")
    def keys(values):return {(r["type"],r["id"]) for r in values}
    added_fe=set(new_elements)-set(old_elements)
    if len(added_fe)!=7 or any(kind not in {"SHELL","RBAR"} for kind,_ in added_fe):
        raise AssertionError("Unexpected FE additions")
    old_perimeters,new_perimeters=rows(before["fe_perimeters"]),rows(after["fe_perimeters"])
    if any(new_perimeters.get(key)!=row for key,row in old_perimeters.items()):
        raise AssertionError("Existing FE PERIMETER cards or edge-shell relations changed")
    new_perimeter_ids=set(new_perimeters)-set(old_perimeters)
    if len(new_perimeter_ids)!=8:
        raise AssertionError("Expected exactly eight new single-sided FE PERIMETER entities")
    target_triangles={r["id"] for p in part_refs for r in new_parts[p["id"]]["elements"]}
    source_ids={101,102}
    if any(r["id"] in source_ids for row in old_perimeters.values() for r in row["edge_shells"]):
        raise AssertionError("Source shells already had FE PERIMETER relations")
    edge_coverage=[]
    for perimeter_id in new_perimeter_ids:
        row=new_perimeters[perimeter_id]
        expected_card={"Id":perimeter_id,"Sides":1,"Angle":None,
            "Prop. Bound":"NO","Parts Bound":"NO","Frozen":"NO","Name":"","Comment":""}
        if (row["entity"]!={"deck":before["deck"],"type":"FE PERIMETER","id":perimeter_id}
                or len(row["card_fields"])!=len(expected_card)
                or set(row["card_fields"])!=set(expected_card)
                or json.dumps(row["card"],sort_keys=True,allow_nan=False)!=json.dumps(expected_card,sort_keys=True)):
            raise AssertionError("New FE PERIMETER full card differs from the verified boundary defaults")
        adjacent=row["edge_shells"]
        if (len(adjacent)!=1 or adjacent[0]!={"deck":before["deck"],"type":"SHELL","id":adjacent[0]["id"]}
                or adjacent[0]["id"] not in target_triangles):
            raise AssertionError("New FE PERIMETER does not belong to exactly one target fan triangle")
        edge_coverage.append(adjacent[0]["id"])
    if len(set(edge_coverage))!=8 or set(edge_coverage)!=target_triangles:
        raise AssertionError("New FE PERIMETER relations do not cover each target triangle exactly once")
    expected_added=added_fe|{("GRID",n) for n in added_grids}|{("FE PERIMETER",n) for n in new_perimeter_ids}
    if keys(after["references"]["__ALL_ENTITIES__"])!=keys(before["references"]["__ALL_ENTITIES__"])|expected_added:
        raise AssertionError("Complete entity references changed outside the permitted additions")
    if keys(after["references"]["__ELEMENTS__"])!=set(new_elements):
        raise AssertionError("Complete FE references differ from independently read cards")
    for kind in ("__PROPERTIES__","__MATERIALS__"):
        if before["references"][kind]!=after["references"][kind]:
            raise AssertionError("Property or material references changed")
    return {"new_rbar_id":rbars[0]["entity"]["id"],"new_grid_ids":sorted(added_grids),
        "new_shell_count":6,"target_triangle_count":8,"outside_shell_id":103,
        "new_fe_perimeter_ids":sorted(new_perimeter_ids),"perimeter_edge_shell_scope_verified":True,
        "two_exact_oriented_quad_partitions":True,"full_reference_scope_verified":True}


async def _verify_cases(instructions, identity, helpers, run_dir, report, path):
    one,two,_=helpers
    fixture=run_dir/"connection-apply-fixture.nas"
    if fixture.exists():raise RuntimeError("Refusing to replace an earlier connection fixture")
    write_fixture(fixture)
    fixture_hash=hashlib.sha256(fixture.read_bytes()).hexdigest()
    parameters=StdioServerParameters(command=sys.executable,
        args=["-B","-m","ansa_mcp_plugin","--runtime-home",instructions["runtime_home"]],
        env={**os.environ,"PYTHONPATH":str(ROOT/"src"),"PYTHONDONTWRITEBYTECODE":"1"})
    receipts=[]

    @asynccontextmanager
    async def backend(log_name):
        with (run_dir/log_name).open("w",encoding="utf-8") as log:
            async with stdio_client(parameters,errlog=log) as (reader,writer):
                async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=180)) as client:
                    await client.initialize()
                    h=common.McpHarness(client,report,run_dir)
                    report["schema"]=await h.list_schema()
                    await common._bind_for_run(h,identity,one,two,identity["port"])
                    yield h

    async def oracle(h):
        return await independent_script(h,remote_snapshot,identity["pid"],identity["program_args"],
            part_refs,identity["seed_entity_id"])

    async def apply_once(h, arguments):
        op="connection-apply-"+uuid.uuid4().hex
        call={"operation_id":op,"expected_target":dict(h.target),"expected_model":dict(h.model),**arguments}
        submitted=await h.call("ansa_connection_apply",call,allow_error=True)
        if not submitted.get("task_id"):raise AssertionError("Apply did not create its original task: "+str(submitted))
        report["pending_apply"]={"operation_id":op,"task_id":submitted["task_id"],"arguments":call}
        common._json_write(path,report)
        try:
            completed=await h.wait_task(submitted["task_id"],expected=None,timeout_seconds=240)
        except TimeoutError as exc:
            report["pending_apply"]["last_query"]=await h.call("ansa_task_query",{"task_id":submitted["task_id"]},allow_error=True)
            common._json_write(path,report)
            raise RuntimeError("Apply timeout retained original task; no replay attempted") from exc
        report.pop("pending_apply",None)
        report["last_apply_result"]=completed
        common._json_write(path,report)
        result=completed["data"]["result"]
        if result.get("ok") is not True or result.get("scope_verified") is not True or result.get("api_return_value")!=1:
            raise AssertionError("Apply did not verify its native result: "+str(completed))
        receipt={"request":call,"operation_id":op,"task_id":submitted["task_id"],
            "result":result,"target":completed["target"],"model":completed["model"]}
        receipts.append(receipt)
        report["original_receipts"]=receipts
        common._json_write(path,report)
        return result

    async with backend("connection-apply-backend.stderr.log") as h:
        fixture_receipt=await independent_script(h,remote_fixture,identity["pid"],identity["program_args"],
            str(fixture),identity["seed_entity_id"],allow_model_change=True)
        await h.snapshot_context()
        part_refs=fixture_receipt["parts"]
        for position in ([5.,5.,1.],[8.,8.,1.]):
            await h.direct_result("ansa_connection_create",{"position":position,"parts":part_refs,
                "diameter":1.,"connection_type":"SpotweldPoint_Type","entity_id":0})
        # Native first Open normalizes Part Is Color Active. Establish the owned
        # persisted baseline before collecting any apply conditions; keep strict
        # Part comparisons throughout the actual realization and restore checks.
        source=run_dir/"connection-apply-source.ansa"
        await h.direct_result("ansa_model_save",{"path":str(source)})
        await h.direct_result("ansa_model_open",{"path":str(source),
            "backup_path":str(run_dir/"before-fixture-normalization.ansa")})
        await h.snapshot_context()
        before=await oracle(h)
        if len(before["grids"])!=13 or len(before["shells"])!=3 or len(before["points"])!=2:
            raise AssertionError("Connection fixture independent counts differ")
        first_request=request(before,part_refs,run_dir/"first-apply-backup.ansa")
        report["independent_before"]=before
        owned.record(report,path,"owned_two_target_parts_outside_shell_bait_point_and_complete_cards",
            {"fixture":fixture_receipt,"before":before,"fixture_sha256":fixture_hash})

        rejected=[]
        for index,change in enumerate((
                {"point_id":2147483646},
                {"expected_fields":{**first_request["expected_fields"],"D":2.}},
                {"target_parts":[{**first_request["target_parts"][0],"shell_id":103},first_request["target_parts"][1]]},
                {"deck":2})):
            invalid={**first_request,**change,"backup_path":str(run_dir/f"rejected-apply-{index}.ansa")}
            _,failed=await h.task("ansa_connection_apply",invalid,expected="failed")
            if Path(invalid["backup_path"]).exists() or await oracle(h)!=before:
                raise AssertionError("Rejected apply created a backup or model effects")
            rejected.append({"task_id":failed["task_id"],"error":failed["error"]})
        schema_rejections=({"point_id":True},{"deck":True},{"target_parts":first_request["target_parts"][:1]},
            {"expected_fields":{}},{"unexpected":True})
        for change in schema_rejections:
            invalid={"operation_id":"invalid-"+uuid.uuid4().hex,"expected_target":h.target,"expected_model":h.model,
                **first_request,**change}
            response=await h.client.call_tool("ansa_connection_apply",invalid)
            structured=response.structuredContent
            if (response.isError is not True
                    or (isinstance(structured,dict) and structured.get("task_id") is not None)):
                raise AssertionError("Invalid raw apply schema reached a model task")
        if await oracle(h)!=before or Path(first_request["backup_path"]).exists():
            raise AssertionError("Schema rejection changed the model or created a backup")
        owned.record(report,path,"strict_schema_stale_point_part_and_deck_rejected_without_effects",
            {"failed_tasks":rejected,"schema_rejections":len(schema_rejections)})

        first=await apply_once(h,first_request)
        after=await oracle(h)
        independent=independent_apply_checks(before,after,part_refs)
        backup=Path(first_request["backup_path"])
        backup_hash=hashlib.sha256(backup.read_bytes()).hexdigest()
        owned.record(report,path,"single_point_rbar_projection_oriented_fans_and_full_outside_preservation",
            {"result":first,"independent":independent,"after":after,"backup_sha256":backup_hash})

        original=receipts[0]
        repeat=await h.call("ansa_connection_apply",original["request"],allow_error=True)
        queried=await h.call("ansa_task_query",{"task_id":original["task_id"]})
        conflict=await h.call("ansa_connection_apply",{**original["request"],"backup_path":str(run_dir/"conflicting-apply.ansa")},allow_error=True)
        already=request(after,part_refs,run_dir/"already-realized.ansa")
        _,failed=await h.task("ansa_connection_apply",already,expected="failed")
        if (repeat.get("task_id")!=original["task_id"] or queried["data"]["result"]!=first
                or conflict.get("ok") is not False or conflict.get("task_id") is not None
                or Path(already["backup_path"]).exists() or await oracle(h)!=after
                or hashlib.sha256(backup.read_bytes()).hexdigest()!=backup_hash):
            raise AssertionError("Original/conflicting/already-realized apply repeated or changed its result")
        owned.record(report,path,"original_receipt_conflict_and_realized_guard_do_not_repeat_native_call",
            {"original_task_id":original["task_id"],"rejected_realized":failed["error"]})

        await h.direct_result("ansa_model_open",{"path":str(backup),"backup_path":str(run_dir/"before-apply-restore.ansa")})
        await h.snapshot_context()
        restored=await oracle(h)
        report["independent_restored"]=restored
        common._json_write(path,report)
        if not same_persisted_model(before,restored,part_refs):raise AssertionError("Actual backup restore did not recover source quads and unrealized points")
        second_request=request(restored,part_refs,run_dir/"second-apply-backup.ansa")
        second=await apply_once(h,second_request)
        current=await oracle(h)
        report["independent_second_after"]=current
        common._json_write(path,report)
        second_independent=independent_apply_checks(restored,current,part_refs)
        saved=run_dir/"accepted-connection-apply.ansa"
        await h.direct_result("ansa_model_save",{"path":str(saved)})
        saved_hash=hashlib.sha256(saved.read_bytes()).hexdigest()
        for index in (1,2):
            old_target,old_model=dict(h.target),dict(h.model)
            await h.direct_result("ansa_model_open",{"path":str(saved),"backup_path":str(run_dir/f"apply-reopen-{index}-backup.ansa")})
            await h.snapshot_context()
            reopened=await oracle(h)
            report.setdefault("independent_reopens",[]).append(reopened)
            common._json_write(path,report)
            if h.model["epoch"]==old_model["epoch"] or not same_persisted_model(current,reopened,part_refs):
                raise AssertionError("Saved connection model or epoch differs after reopen")
            current=reopened
            stale=await h.call("ansa_connection_apply",{"operation_id":"stale-"+uuid.uuid4().hex,
                "expected_target":old_target,"expected_model":old_model,**second_request},allow_error=True)
            if stale.get("ok") is not False or stale.get("task_id") is not None:
                raise AssertionError("Stale model apply was accepted")
        if hashlib.sha256(saved.read_bytes()).hexdigest()!=saved_hash or hashlib.sha256(fixture.read_bytes()).hexdigest()!=fixture_hash:
            raise AssertionError("Apply modified its input or saved file")
        previous_backend=h.target["backend_id"]
        owned.record(report,path,"actual_backup_restore_second_realization_saved_reopens_and_stale_model",
            {"second_result":second,"independent":second_independent,"saved":str(saved),"sha256":saved_hash})

    async with backend("connection-apply-restarted-backend.stderr.log") as h:
        if h.target["backend_id"]==previous_backend:raise AssertionError("Backend did not restart")
        for receipt in receipts:
            value=await h.call("ansa_task_query",{"operation_id":receipt["operation_id"]})
            if any(value[key]!=receipt[key] for key in ("task_id","target","model")) or value["data"]["result"]!=receipt["result"]:
                raise AssertionError("Persistent apply receipt changed")
        if await oracle(h)!=current or hashlib.sha256(saved.read_bytes()).hexdigest()!=saved_hash:
            raise AssertionError("Backend receipt query changed the accepted connection model")
        owned.record(report,path,"backend_restart_original_receipts_and_independent_no_replay",
            {"original_task_ids":[r["task_id"] for r in receipts],"unchanged":True})
    report["visual_acceptance"]=False


async def verify(instructions, identity, helpers, run_dir, report, path):
    try:
        await _verify_cases(instructions, identity, helpers, run_dir, report, path)
    except BaseException:
        report["error_details"]=traceback.format_exc()[-32768:]
        common._json_write(path,report)
        raise


if __name__=="__main__":
    raise SystemExit(owned.main(report_name="connection-apply-report.json",
        scope="Owned NASTRAN single SpotweldPoint RBAR with two explicit quad-to-triangle targets; no canvas acceptance",
        verify_callback=verify))
