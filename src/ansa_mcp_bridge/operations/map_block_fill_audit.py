"""Pure write-scope audit for filling one existing native Map Block."""

from datetime import datetime
from collections import Counter
import hashlib
import json
import math
import re


_PART_FIELDS = (
    "Mass",
    "COG x",
    "COG y",
    "COG z",
    "Ixx",
    "Iyy",
    "Izz",
    "Ixy",
    "Ixz",
    "Iyz",
)
_PART_LENGTH_FIELDS = frozenset(("COG x", "COG y", "COG z"))
_VOLUME_TRANSITIONS = {
    "State": ("Ready to Mesh", "Meshed"),
    "Status": ("Unmeshed", "Meshed"),
    "Order": ("", "1st"),
}
_VOLUME_COUNTS = {
    "Hexas": "solids",
    "Grids": "output_nodes",
    "Internal grids": "new_grids",
}
_ZERO_VOLUME_COUNTS = ("Pentas", "Tetras", "Pyramids", "Polyhedrals")
_TIME_PATTERNS = (
    re.compile(r"(\d{1,2})-(\d{1,2})月-(\d{4}) (\d{1,2}):(\d{2}):(\d{2})\Z"),
    re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4}) (\d{1,2}):(\d{2}):(\d{2})\Z"),
)


def _positive_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _count(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _close(actual, expected, label, *, rel_tol=1e-12, abs_tol=1e-16):
    actual = _finite(actual, label)
    expected = _finite(expected, label)
    if not math.isclose(actual, expected, rel_tol=rel_tol, abs_tol=abs_tol):
        raise RuntimeError(f"{label} differs from its independent readback")


def _timestamp(value, label):
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a valid Last Edit timestamp")
    for pattern in _TIME_PATTERNS:
        match = pattern.fullmatch(value)
        if match:
            day, month, year, hour, minute, second = map(int, match.groups())
            try:
                return datetime(year, month, day, hour, minute, second)
            except ValueError as exc:
                raise ValueError(f"{label} must be a valid Last Edit timestamp") from exc
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%B-%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"{label} must be a valid Last Edit timestamp")


def _scope_map(snapshot, label):
    if type(snapshot) is not dict or type(snapshot.get("entities")) is not list:
        raise ValueError(f"{label} must be a complete raw entity snapshot")
    result = {}
    for row in snapshot["entities"]:
        if type(row) is not dict or type(row.get("entity")) is not dict:
            raise ValueError(f"{label} contains a malformed raw entity row")
        reference = row["entity"]
        entity_type = reference.get("type")
        entity_id = reference.get("id")
        if type(entity_type) is not str or not entity_type:
            raise ValueError(f"{label} contains an invalid entity type")
        _positive_id(entity_id, f"{label} entity ID")
        if type(row.get("fields")) is not dict:
            raise ValueError(f"{label} entity card fields are missing")
        card_fields = row.get("card_fields")
        if (type(card_fields) not in (list, tuple) or not card_fields
                or any(type(name) is not str or not name for name in card_fields)
                or len(card_fields) != len(set(card_fields))):
            raise ValueError(f"{label} entity card field inventory is incomplete")
        if not set(card_fields).issubset(row["fields"]):
            raise ValueError(f"{label} entity card values are incomplete")
        if "ignored_derived_fields" in row:
            raise ValueError(f"{label} snapshot omits raw derived card fields")
        key = (entity_type, entity_id)
        if key in result:
            raise ValueError(f"{label} contains duplicate entity identity {key}")
        result[key] = row
    return result


def _required_card(row, names, label):
    fields = row["fields"]
    card_fields = set(row["card_fields"])
    missing = [name for name in names if name not in fields or name not in card_fields]
    if missing:
        raise ValueError(f"{label} lacks raw card field(s): {', '.join(missing)}")
    return fields


def _grid_rows(snapshot, scope, label):
    rows = snapshot.get("grids")
    if type(rows) is not list:
        raise ValueError(f"{label} GRID summary is missing")
    result = {}
    entity_ids = {ident for kind, ident in scope if kind == "GRID"}
    for row in rows:
        if type(row) is not dict:
            raise ValueError(f"{label} contains a malformed GRID summary")
        ident = _positive_id(row.get("id"), f"{label} GRID summary ID")
        if ident in result:
            raise ValueError(f"{label} contains duplicate GRID summary ID {ident}")
        raw = scope.get(("GRID", ident))
        if raw is None or row.get("card") != {
            key: raw[key] for key in ("entity", "card_fields", "fields")
        }:
            raise RuntimeError(f"{label} GRID summary differs from its full raw card")
        result[ident] = row
    if set(result) != entity_ids:
        raise RuntimeError(f"{label} GRID summary does not cover every raw GRID")
    return result


def _aggregate_values(part_aggregate, part_id):
    if type(part_aggregate) is not dict:
        raise ValueError("fresh native Part aggregate evidence is required")
    if (part_aggregate.get("origin") != "CalcElementMass(Part)"
            or part_aggregate.get("part_id") != part_id):
        raise ValueError("native Part aggregate binding or origin is invalid")
    values = part_aggregate.get("values")
    if type(values) is not dict or set(values) != set(_PART_FIELDS):
        raise ValueError("native Part aggregate must contain exactly ten mass and inertia values")
    parsed = {name: _finite(values[name], "native Part " + name) for name in _PART_FIELDS}
    if parsed["Mass"] <= 0:
        raise ValueError("native Part aggregate mass must be positive")
    native_values = part_aggregate.get("native_values")
    if native_values is not None:
        if type(native_values) not in (list, tuple) or len(native_values) < len(_PART_FIELDS):
            raise ValueError("native Part aggregate raw values are incomplete")
        for name, value in zip(_PART_FIELDS, native_values):
            _close(value, parsed[name], "native Part " + name)
    return parsed


def _output_mesh(output, after_scope, new_grid_ids, volume_id, part_id, property_id, *, allow_penta=False):
    if type(output) is not dict or output.get("complete") is not True:
        raise ValueError("complete native SOLID mesh capture is required")
    integrity = output.get("integrity")
    ownership = output.get("ownership")
    nodes = output.get("nodes")
    elements = output.get("elements")
    if (type(integrity) is not dict or integrity.get("complete") is not True
            or type(ownership) is not dict or ownership.get("complete") is not True
            or ownership.get("origin") != "native_VOLUME_membership"
            or type(nodes) is not list or type(elements) is not list or not elements):
        raise ValueError("native mesh capture lacks complete elements, nodes, or VOLUME ownership")

    node_map = {}
    for node in nodes:
        if type(node) is not dict:
            raise ValueError("native mesh capture contains a malformed GRID")
        ident = _positive_id(node.get("id"), "captured GRID ID")
        xyz = node.get("xyz")
        if type(xyz) is not list or len(xyz) != 3:
            raise ValueError("captured GRID coordinates are incomplete")
        coords = [_finite(value, f"captured GRID {ident} coordinate") for value in xyz]
        if ident in node_map:
            raise ValueError(f"native mesh capture contains duplicate GRID {ident}")
        row = after_scope.get(("GRID", ident))
        if row is None:
            raise RuntimeError(f"captured GRID {ident} is absent from the after snapshot")
        fields = _required_card(row, ("TYPE", "NID", "X1", "X2", "X3"), f"GRID {ident}")
        if (fields["TYPE"] != "GRID"
                or _positive_id(fields["NID"], f"GRID {ident} NID") != ident):
            raise RuntimeError(f"GRID {ident} raw NID does not match its identity")
        if ident in new_grid_ids:
            _required_card(row, ("CD", "PS", "SEID"), f"new GRID {ident}")
            for name in ("CD", "PS", "SEID"):
                value = fields[name]
                if not (value is None or type(value) is str and value == ""
                        or type(value) in (int, float) and value == 0):
                    raise RuntimeError(f"new GRID {ident} has a nondefault {name}")
        for axis, name in enumerate(("X1", "X2", "X3")):
            _close(fields[name], coords[axis], f"GRID {ident} {name}", rel_tol=0, abs_tol=1e-16)
        node_map[ident] = coords

    if not set(new_grid_ids).issubset(node_map):
        raise RuntimeError("a new GRID is unused by the captured SOLID mesh")
    element_ids = []
    used_node_ids = set()
    for element in elements:
        if type(element) is not dict:
            raise ValueError("native mesh capture contains a malformed SOLID")
        ident = _positive_id(element.get("id"), "captured SOLID ID")
        kind = element.get("type")
        arity = {"CHEXA": 8, **({"CPENTA": 6} if allow_penta else {})}.get(kind)
        if arity is None:
            raise RuntimeError(f"captured SOLID {ident} is not an allowed linear element")
        if _positive_id(element.get("property_id"), f"SOLID {ident} property ID") != property_id:
            raise RuntimeError(f"captured SOLID {ident} has the wrong property")
        if element.get("block_id") != f"VOLUME:{volume_id}":
            raise RuntimeError(f"captured SOLID {ident} is not owned by the target VOLUME")
        node_ids = element.get("nodes")
        if (type(node_ids) is not list or len(node_ids) != arity
                or any(type(node_id) is not int or node_id <= 0 for node_id in node_ids)
                or len(set(node_ids)) != arity or not set(node_ids).issubset(node_map)):
            raise RuntimeError(f"captured {kind} {ident} has invalid or missing GRID references")
        used_node_ids.update(node_ids)
        raw = after_scope.get(("SOLID", ident))
        if raw is None:
            raise RuntimeError(f"captured SOLID {ident} is absent from the after snapshot")
        fields = _required_card(raw, ("type", "EID", "PID", *(f"G{i}" for i in range(1, arity+1))),
                                f"SOLID {ident}")
        if (fields["type"] != kind or _positive_id(fields["EID"], f"SOLID {ident} EID") != ident
                or _positive_id(fields["PID"], f"SOLID {ident} PID") != property_id
                or raw.get("part_id") != part_id):
            raise RuntimeError(f"SOLID {ident} raw card binding is invalid")
        raw_nodes = [_positive_id(fields[f"G{i}"], f"SOLID {ident} G{i}") for i in range(1, arity+1)]
        if raw_nodes != node_ids:
            raise RuntimeError(f"SOLID {ident} capture differs from its raw connectivity")
        element_ids.append(ident)

    if len(element_ids) != len(set(element_ids)):
        raise RuntimeError("native mesh capture contains duplicate SOLID IDs")
    if used_node_ids != set(node_map):
        raise RuntimeError("native mesh capture includes GRID nodes unused by its SOLIDs")
    if (integrity.get("requested_element_count") != len(element_ids)
            or integrity.get("element_count") != len(element_ids)
            or integrity.get("node_count") != len(node_map)
            or integrity.get("missing_node_ids") != []
            or integrity.get("element_types") != dict(Counter(e["type"] for e in elements))
            or ownership.get("known_count") != len(element_ids)):
        raise RuntimeError("native mesh capture integrity or ownership counts disagree")
    return set(element_ids), node_map


def _without_allowed_fields(row, names, *, drop_solid_ids=False):
    clean = json.loads(json.dumps(row, ensure_ascii=False, allow_nan=False))
    for name in names:
        clean["fields"].pop(name, None)
    if drop_solid_ids:
        clean.pop("solid_ids", None)
    return clean


def audit_changes(
    before,
    after,
    *,
    volume_id,
    part_id,
    property_id,
    solid_ids,
    output,
    part_aggregate,
    expected_volume,
    expected_mass,
):
    """Prove one existing Map Block was filled within the declared write scope.

    ``before`` and ``after`` are complete ``cad_map._snapshot`` results. The
    mesh and Part aggregate must be fresh captures from the ANSA main thread.
    """
    volume_id = _positive_id(volume_id, "volume_id")
    part_id = _positive_id(part_id, "part_id")
    property_id = _positive_id(property_id, "property_id")
    expected_volume = _finite(expected_volume, "expected_volume")
    expected_mass = _finite(expected_mass, "expected_mass")
    if expected_volume <= 0 or expected_mass <= 0:
        raise ValueError("expected physical volume and mass must be positive")
    if type(solid_ids) not in (list, tuple) or not solid_ids:
        raise ValueError("solid_ids must be a non-empty list or tuple")
    expected_solid_ids = [_positive_id(value, "solid_id") for value in solid_ids]
    if len(expected_solid_ids) != len(set(expected_solid_ids)):
        raise ValueError("solid_ids must not contain duplicates")
    expected_solid_set = set(expected_solid_ids)

    old = _scope_map(before, "before")
    new = _scope_map(after, "after")
    if old.keys() - new.keys():
        raise RuntimeError("Map Block fill removed original entities")
    additions = new.keys() - old.keys()
    if any(kind not in ("GRID", "SOLID") for kind, _ in additions):
        raise RuntimeError("Map Block fill added an entity outside GRID/SOLID")
    new_solid_ids = {ident for kind, ident in additions if kind == "SOLID"}
    new_grid_ids = {ident for kind, ident in additions if kind == "GRID"}
    if new_solid_ids != expected_solid_set:
        raise RuntimeError("actual new SOLIDs differ from solid_ids")

    old_grids = _grid_rows(before, old, "before")
    new_grids = _grid_rows(after, new, "after")
    if any(new_grids.get(ident) != row for ident, row in old_grids.items()):
        raise RuntimeError("Map Block fill changed an original GRID summary or raw coordinate")
    cad_hash = before.get("cad_sha256")
    if (type(cad_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", cad_hash) is None
            or cad_hash != after.get("cad_sha256")):
        raise RuntimeError("Map Block fill changed the protected CAD hash")
    if "perimeters" not in before or before.get("perimeters") != after.get("perimeters"):
        raise RuntimeError("Map Block fill changed protected CAD perimeters")
    if ({key: value for key, value in before.items() if key not in ("entities", "grids")}
            != {key: value for key, value in after.items() if key not in ("entities", "grids")}):
        raise RuntimeError("Map Block fill changed protected snapshot metadata")

    part_key = ("ANSAPART", part_id)
    property_key = ("PSOLID", property_id)
    volume_key = ("VOLUME", volume_id)
    if part_key not in old or property_key not in old or volume_key not in old:
        raise ValueError("target Part, PSOLID, and existing VOLUME must all exist before fill")
    map_blocks = [key for key, row in old.items()
                  if key[0] == "VOLUME" and row["fields"].get("Type") == "Map Block"]
    if map_blocks != [volume_key]:
        raise RuntimeError("target must be the unique existing Map Block VOLUME")

    aggregate = _aggregate_values(part_aggregate, part_id)
    part_fields = _required_card(old[part_key], (*_PART_FIELDS, "Last Edit"), "ANSAPART")
    after_part_fields = _required_card(new[part_key], (*_PART_FIELDS, "Last Edit"), "ANSAPART")
    if _finite(after_part_fields["Mass"], "ANSAPART Mass") <= 0:
        raise RuntimeError("recomputed Part mass must be positive")
    for name in _PART_FIELDS:
        _close(after_part_fields[name], aggregate[name], "ANSAPART " + name)
    before_edit = _timestamp(part_fields["Last Edit"], "before ANSAPART Last Edit")
    after_edit = _timestamp(after_part_fields["Last Edit"], "after ANSAPART Last Edit")
    if after_edit < before_edit:
        raise RuntimeError("ANSAPART Last Edit moved backwards")

    property_fields = _required_card(old[property_key], ("PID", "Num.Elem"), "PSOLID")
    after_property_fields = _required_card(new[property_key], ("PID", "Num.Elem"), "PSOLID")
    if (_positive_id(property_fields["PID"], "PSOLID PID") != property_id
            or _positive_id(after_property_fields["PID"], "PSOLID PID") != property_id
            or _count(property_fields["Num.Elem"], "before PSOLID Num.Elem") != 0
            or _count(after_property_fields["Num.Elem"], "after PSOLID Num.Elem") != len(expected_solid_ids)):
        raise RuntimeError("target PSOLID Num.Elem or identity does not match the new SOLID inventory")

    volume_fields = _required_card(
        old[volume_key],
        ("PID", "Type", "State", "Status", "Order", "Frozen", "Volume", "Mass",
         "Hexas", "Grids", "Internal grids", *_ZERO_VOLUME_COUNTS),
        "VOLUME",
    )
    after_volume_fields = _required_card(
        new[volume_key],
        ("PID", "Type", "State", "Status", "Order", "Frozen", "Volume", "Mass",
         "Hexas", "Grids", "Internal grids", *_ZERO_VOLUME_COUNTS),
        "VOLUME",
    )
    if (old[volume_key].get("part_id") != part_id or new[volume_key].get("part_id") != part_id
            or _positive_id(volume_fields["PID"], "VOLUME PID") != property_id
            or _positive_id(after_volume_fields["PID"], "VOLUME PID") != property_id
            or volume_fields["Type"] != "Map Block" or after_volume_fields["Type"] != "Map Block"
            or volume_fields["Frozen"] != "NO" or after_volume_fields["Frozen"] != "NO"):
        raise RuntimeError("target VOLUME identity, Map Block type, or frozen state changed")
    if old[volume_key].get("solid_ids") != []:
        raise RuntimeError("target Map Block must have no SOLID members before fill")
    if any(volume_fields[name] != old_value or after_volume_fields[name] != 0
           for name, old_value in ((name, 0) for name in _ZERO_VOLUME_COUNTS)):
        raise RuntimeError("target VOLUME contains non-HEXA elements")
    for name, (old_value, new_value) in _VOLUME_TRANSITIONS.items():
        if volume_fields[name] != old_value or after_volume_fields[name] != new_value:
            raise RuntimeError(f"target VOLUME {name} has an invalid state transition")
    if (_count(volume_fields["Hexas"], "before VOLUME Hexas") != 0
            or _count(after_volume_fields["Hexas"], "after VOLUME Hexas") != len(expected_solid_ids)):
        raise RuntimeError("target VOLUME Hexas count differs from the new SOLID inventory")

    output_solid_ids, output_nodes = _output_mesh(
        output, new, new_grid_ids, volume_id, part_id, property_id
    )
    if output_solid_ids != expected_solid_set:
        raise RuntimeError("captured native SOLID IDs differ from the actual additions")
    if type(output.get("nodes")) is not list or not output_nodes:
        raise RuntimeError("captured native SOLID mesh has no GRID nodes")
    for name in ("Grids", "Internal grids"):
        if _count(volume_fields[name], "before VOLUME " + name) != 0:
            raise RuntimeError(f"target VOLUME {name} count was nonzero before fill")
    if (_count(after_volume_fields["Grids"], "after VOLUME Grids") != len(output_nodes)
            or _count(after_volume_fields["Internal grids"], "after VOLUME Internal grids") != len(new_grid_ids)):
        raise RuntimeError("target VOLUME GRID counts differ from the captured native mesh")
    if new[volume_key].get("solid_ids") != sorted(expected_solid_set):
        raise RuntimeError("target VOLUME membership does not equal every new SOLID")

    _close(after_volume_fields["Volume"], expected_volume, "target VOLUME Volume",
           rel_tol=1e-8, abs_tol=1e-12)
    _close(after_volume_fields["Mass"], expected_mass, "target VOLUME Mass",
           rel_tol=1e-8, abs_tol=1e-16)

    for key in old:
        if key == part_key:
            allowed = (*_PART_FIELDS, "Last Edit")
            before_row = _without_allowed_fields(old[key], allowed)
            after_row = _without_allowed_fields(new[key], allowed)
        elif key == property_key:
            before_row = _without_allowed_fields(old[key], ("Num.Elem",))
            after_row = _without_allowed_fields(new[key], ("Num.Elem",))
        elif key == volume_key:
            allowed = (*_VOLUME_TRANSITIONS, *_VOLUME_COUNTS, *_ZERO_VOLUME_COUNTS)
            before_row = _without_allowed_fields(old[key], allowed, drop_solid_ids=True)
            after_row = _without_allowed_fields(new[key], allowed, drop_solid_ids=True)
        else:
            before_row, after_row = old[key], new[key]
        if before_row != after_row:
            raise RuntimeError(f"Map Block fill changed a protected original entity: {key}")

    try:
        encoded = json.dumps(after, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("after snapshot cannot be hashed as complete finite JSON") from exc
    return {
        "new_solid_ids": sorted(new_solid_ids),
        "new_grid_ids": sorted(new_grid_ids),
        "filled_volume_id": volume_id,
        "after_scope_sha256": hashlib.sha256(encoded).hexdigest(),
        "scope_verified": True,
        "solid_count": len(new_solid_ids),
        "captured_grid_count": len(output_nodes),
    }


def _captured_node_ids(output, label):
    if type(output) is not dict or type(output.get("nodes")) is not list:
        raise ValueError(f"{label} lacks a complete captured GRID list")
    if any(type(node) is not dict for node in output["nodes"]):
        raise ValueError(f"{label} contains a malformed captured GRID")
    return {
        _positive_id(node.get("id"), f"{label} captured GRID ID")
        for node in output["nodes"]
    }


def _solid_inventory_by_property(scope, label):
    counts = {}
    for (kind, ident), row in scope.items():
        if kind != "SOLID":
            continue
        fields = _required_card(row, ("EID", "PID"), f"{label} SOLID {ident}")
        if _positive_id(fields["EID"], f"{label} SOLID {ident} EID") != ident:
            raise RuntimeError(f"{label} SOLID {ident} raw EID does not match its identity")
        property_id = _positive_id(fields["PID"], f"{label} SOLID {ident} PID")
        counts[property_id] = counts.get(property_id, 0) + 1
    return counts


def audit_many_changes(before, after, *, blocks, part_aggregate, allow_penta=False):
    """Prove several existing, empty Map Blocks were filled within their scopes.

    Every block uses the existing ``capture_mesh`` format accepted by
    :func:`_output_mesh`. All selected blocks must belong to one Part. Existing
    entities and GRID cards are protected; only selected VOLUMEs, the selected
    Part aggregate fields, and the affected PSOLID element counts may change.
    ``allow_penta=True`` additionally accepts linear CPENTA and checks its
    separate native Pentas count. The default remains CHEXA-only.
    """
    if type(allow_penta) is not bool:
        raise ValueError("allow_penta must be a boolean")
    if type(blocks) is not list or not blocks:
        raise ValueError("blocks must be a non-empty list")

    old = _scope_map(before, "before")
    new = _scope_map(after, "after")
    if old.keys() - new.keys():
        raise RuntimeError("multi-Map Block fill removed original entities")
    additions = new.keys() - old.keys()
    if any(kind not in ("GRID", "SOLID") for kind, _ in additions):
        raise RuntimeError("multi-Map Block fill added an entity outside GRID/SOLID")
    new_solid_ids = {ident for kind, ident in additions if kind == "SOLID"}
    new_grid_ids = {ident for kind, ident in additions if kind == "GRID"}

    old_grids = _grid_rows(before, old, "before")
    new_grids = _grid_rows(after, new, "after")
    if any(new_grids.get(ident) != row for ident, row in old_grids.items()):
        raise RuntimeError("multi-Map Block fill changed an original GRID summary or raw coordinate")
    cad_hash = before.get("cad_sha256")
    if (type(cad_hash) is not str or re.fullmatch(r"[0-9a-f]{64}", cad_hash) is None
            or cad_hash != after.get("cad_sha256")):
        raise RuntimeError("multi-Map Block fill changed the protected CAD hash")
    if "perimeters" not in before or before.get("perimeters") != after.get("perimeters"):
        raise RuntimeError("multi-Map Block fill changed protected CAD perimeters")
    if ({key: value for key, value in before.items() if key not in ("entities", "grids")}
            != {key: value for key, value in after.items() if key not in ("entities", "grids")}):
        raise RuntimeError("multi-Map Block fill changed protected snapshot metadata")

    parsed_blocks = []
    selected_volumes = set()
    declared_solids = set()
    part_ids = set()
    new_solids_by_property = {}
    for index, block in enumerate(blocks):
        label = f"blocks[{index}]"
        if type(block) is not dict:
            raise ValueError(f"{label} must be a mapping")
        required = {
            "volume_id", "part_id", "property_id", "solid_ids", "output",
            "expected_volume", "expected_mass",
        }
        missing = required - block.keys()
        if missing:
            raise ValueError(f"{label} lacks required field(s): {', '.join(sorted(missing))}")
        volume_id = _positive_id(block["volume_id"], f"{label} volume_id")
        part_id = _positive_id(block["part_id"], f"{label} part_id")
        property_id = _positive_id(block["property_id"], f"{label} property_id")
        expected_volume = _finite(block["expected_volume"], f"{label} expected_volume")
        expected_mass = _finite(block["expected_mass"], f"{label} expected_mass")
        if expected_volume <= 0 or expected_mass <= 0:
            raise ValueError(f"{label} expected physical volume and mass must be positive")
        solid_ids = block["solid_ids"]
        if type(solid_ids) not in (list, tuple) or not solid_ids:
            raise ValueError(f"{label} solid_ids must be a non-empty list or tuple")
        expected_ids = [_positive_id(value, f"{label} solid_id") for value in solid_ids]
        expected_set = set(expected_ids)
        if len(expected_ids) != len(expected_set):
            raise ValueError(f"{label} solid_ids must not contain duplicates")
        if volume_id in selected_volumes:
            raise ValueError("selected Map Block volume IDs must be unique")
        if declared_solids.intersection(expected_set):
            raise ValueError("new SOLID IDs must be disjoint across selected blocks")
        selected_volumes.add(volume_id)
        declared_solids.update(expected_set)
        part_ids.add(part_id)
        new_solids_by_property[property_id] = (
            new_solids_by_property.get(property_id, 0) + len(expected_set)
        )
        parsed_blocks.append({
            "volume_id": volume_id,
            "part_id": part_id,
            "property_id": property_id,
            "solid_ids": expected_set,
            "output": block["output"],
            "expected_volume": expected_volume,
            "expected_mass": expected_mass,
        })

    if len(part_ids) != 1:
        raise ValueError("all selected Map Blocks must belong to the same Part")
    if declared_solids != new_solid_ids:
        raise RuntimeError("actual new SOLIDs differ from the disjoint per-block solid_ids")
    part_id = next(iter(part_ids))
    part_key = ("ANSAPART", part_id)
    if part_key not in old:
        raise ValueError("the selected Part must exist before fill")

    before_solid_counts = _solid_inventory_by_property(old, "before")
    after_solid_counts = _solid_inventory_by_property(new, "after")
    selected_properties = set(new_solids_by_property)
    for property_id in selected_properties:
        property_key = ("PSOLID", property_id)
        if property_key not in old:
            raise ValueError("each selected PSOLID must exist before fill")
        before_fields = _required_card(old[property_key], ("PID", "Num.Elem"), "before PSOLID")
        after_fields = _required_card(new[property_key], ("PID", "Num.Elem"), "after PSOLID")
        before_count = before_solid_counts.get(property_id, 0)
        expected_after_count = before_count + new_solids_by_property[property_id]
        if (_positive_id(before_fields["PID"], "before PSOLID PID") != property_id
                or _positive_id(after_fields["PID"], "after PSOLID PID") != property_id
                or _count(before_fields["Num.Elem"], "before PSOLID Num.Elem") != before_count
                or after_solid_counts.get(property_id, 0) != expected_after_count
                or _count(after_fields["Num.Elem"], "after PSOLID Num.Elem") != expected_after_count):
            raise RuntimeError(
                "PSOLID identity or Num.Elem differs from the complete before/after SOLID inventory"
            )

    aggregate = _aggregate_values(part_aggregate, part_id)
    part_fields = _required_card(old[part_key], (*_PART_FIELDS, "Last Edit"), "ANSAPART")
    after_part_fields = _required_card(new[part_key], (*_PART_FIELDS, "Last Edit"), "ANSAPART")
    if _finite(after_part_fields["Mass"], "ANSAPART Mass") <= 0:
        raise RuntimeError("recomputed Part mass must be positive")
    for name in _PART_FIELDS:
        _close(after_part_fields[name], aggregate[name], "ANSAPART " + name)
    before_edit = _timestamp(part_fields["Last Edit"], "before ANSAPART Last Edit")
    after_edit = _timestamp(after_part_fields["Last Edit"], "after ANSAPART Last Edit")
    if after_edit < before_edit:
        raise RuntimeError("ANSAPART Last Edit moved backwards")

    before_grid_ids = set(old_grids)
    assigned_new_grids = set()
    output_node_ids = set()
    block_results = []
    for block in parsed_blocks:
        volume_id = block["volume_id"]
        volume_key = ("VOLUME", volume_id)
        property_id = block["property_id"]
        if volume_key not in old:
            raise ValueError("each selected Map Block VOLUME must exist before fill")
        volume_fields = _required_card(
            old[volume_key],
            ("ID", "PID", "Type", "State", "Status", "Order", "Frozen", "Volume", "Mass",
             "Hexas", "Grids", "Internal grids", *_ZERO_VOLUME_COUNTS),
            "before VOLUME",
        )
        after_volume_fields = _required_card(
            new[volume_key],
            ("ID", "PID", "Type", "State", "Status", "Order", "Frozen", "Volume", "Mass",
             "Hexas", "Grids", "Internal grids", *_ZERO_VOLUME_COUNTS),
            "after VOLUME",
        )
        if (_positive_id(volume_fields["ID"], "before VOLUME ID") != volume_id
                or _positive_id(after_volume_fields["ID"], "after VOLUME ID") != volume_id
                or old[volume_key].get("part_id") != part_id
                or new[volume_key].get("part_id") != part_id
                or _positive_id(volume_fields["PID"], "before VOLUME PID") != property_id
                or _positive_id(after_volume_fields["PID"], "after VOLUME PID") != property_id
                or volume_fields["Type"] != "Map Block"
                or after_volume_fields["Type"] != "Map Block"
                or volume_fields["Frozen"] != "NO"
                or after_volume_fields["Frozen"] != "NO"):
            raise RuntimeError("selected VOLUME identity, Map Block type, Part, or frozen state is invalid")
        if old[volume_key].get("solid_ids") != []:
            raise RuntimeError("each selected Map Block must have no SOLID members before fill")
        if (volume_fields["State"] != "Ready to Mesh"
                or volume_fields["Status"] != "Unmeshed"
                or volume_fields["Order"] != ""):
            raise RuntimeError("each selected Map Block must be ready and unmeshed before fill")
        if (after_volume_fields["State"] != "Meshed"
                or after_volume_fields["Status"] != "Meshed"
                or after_volume_fields["Order"] != "1st"):
            raise RuntimeError("each selected Map Block must transition to Meshed")
        for name in ("Hexas", "Grids", "Internal grids", *_ZERO_VOLUME_COUNTS):
            if _count(volume_fields[name], "before VOLUME " + name) != 0:
                raise RuntimeError(f"selected VOLUME {name} count was nonzero before fill")
        for name in _ZERO_VOLUME_COUNTS:
            if name == "Pentas" and allow_penta:
                continue
            if _count(after_volume_fields[name], "after VOLUME " + name) != 0:
                raise RuntimeError("selected VOLUME contains non-HEXA elements")

        captured_node_ids = _captured_node_ids(block["output"], f"VOLUME:{volume_id}")
        block_new_grid_ids = captured_node_ids - before_grid_ids
        output_solid_ids, node_map = _output_mesh(
            block["output"], new, block_new_grid_ids, volume_id, part_id, property_id,
            allow_penta=allow_penta,
        )
        if output_solid_ids != block["solid_ids"]:
            raise RuntimeError(f"VOLUME:{volume_id} output SOLIDs differ from its declared IDs")
        if not (set(node_map) & before_grid_ids):
            raise RuntimeError(f"VOLUME:{volume_id} output has no pre-existing boundary GRID")
        if assigned_new_grids.intersection(block_new_grid_ids):
            raise RuntimeError("a new GRID is used as an internal node by multiple selected blocks")
        assigned_new_grids.update(block_new_grid_ids)
        output_node_ids.update(node_map)
        internal_count = len(block_new_grid_ids)
        element_counts = Counter(e["type"] for e in block["output"]["elements"])
        if (_count(after_volume_fields["Hexas"], "after VOLUME Hexas") != element_counts["CHEXA"]
                or _count(after_volume_fields["Pentas"], "after VOLUME Pentas") != element_counts["CPENTA"]
                or _count(after_volume_fields["Grids"], "after VOLUME Grids") != len(node_map)
                or _count(after_volume_fields["Internal grids"], "after VOLUME Internal grids") != internal_count):
            raise RuntimeError(f"VOLUME:{volume_id} GRID/SOLID counts differ from its captured output")
        if new[volume_key].get("solid_ids") != sorted(block["solid_ids"]):
            raise RuntimeError(f"VOLUME:{volume_id} membership differs from its declared SOLIDs")
        _close(after_volume_fields["Volume"], block["expected_volume"],
               f"VOLUME:{volume_id} Volume", rel_tol=1e-8, abs_tol=1e-12)
        _close(after_volume_fields["Mass"], block["expected_mass"],
               f"VOLUME:{volume_id} Mass", rel_tol=1e-8, abs_tol=1e-16)
        block_results.append({
            "volume_id": volume_id,
            "property_id": property_id,
            "new_solid_ids": sorted(output_solid_ids),
            "new_grid_ids": sorted(block_new_grid_ids),
            "boundary_grid_ids": sorted(set(node_map) & before_grid_ids),
            "captured_grid_count": len(node_map),
            "internal_grid_count": internal_count,
        })

    if assigned_new_grids != new_grid_ids:
        raise RuntimeError("new GRID inventory differs from the disjoint per-block output nodes")

    for key in old:
        if key == part_key:
            allowed = (*_PART_FIELDS, "Last Edit")
            before_row = _without_allowed_fields(old[key], allowed)
            after_row = _without_allowed_fields(new[key], allowed)
        elif key[0] == "PSOLID" and key[1] in selected_properties:
            before_row = _without_allowed_fields(old[key], ("Num.Elem",))
            after_row = _without_allowed_fields(new[key], ("Num.Elem",))
        elif key[0] == "VOLUME" and key[1] in selected_volumes:
            allowed = (*_VOLUME_TRANSITIONS, *_VOLUME_COUNTS, *_ZERO_VOLUME_COUNTS)
            before_row = _without_allowed_fields(old[key], allowed, drop_solid_ids=True)
            after_row = _without_allowed_fields(new[key], allowed, drop_solid_ids=True)
        else:
            before_row, after_row = old[key], new[key]
        if before_row != after_row:
            raise RuntimeError(f"multi-Map Block fill changed a protected original entity: {key}")

    try:
        encoded = json.dumps(after, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("after snapshot cannot be hashed as complete finite JSON") from exc
    return {
        "new_solid_ids": sorted(new_solid_ids),
        "new_grid_ids": sorted(new_grid_ids),
        "filled_volume_ids": sorted(selected_volumes),
        "block_results": block_results,
        "after_scope_sha256": hashlib.sha256(encoded).hexdigest(),
        "scope_verified": True,
        "solid_count": len(new_solid_ids),
        "captured_grid_count": len(output_node_ids),
    }
