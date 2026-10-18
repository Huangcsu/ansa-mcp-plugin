"""Explicit, deduplicated ABAQUS shell/solid mass scopes; no model changes."""

import hashlib
import json
import math

from . import entity_catalog as catalog


_ROOT_TYPES = frozenset({"MATERIAL", "SHELL_SECTION", "SOLID_SECTION", "ANSAPART",
                         "ANSAGROUP", "SHELL", "SOLID"})
_ELEMENT_TYPES = ("SHELL", "SOLID")
_MAX_ELEMENTS = 500000
_MAX_SCANNED = 1000000


def _collection(base, deck, roots, kind, budget):
    collect = getattr(base, "CollectEntitiesI", None)
    if not callable(collect):
        raise NotImplementedError("bounded mass scopes require ansa.base.CollectEntitiesI")
    for value in collect(deck, roots, kind, recursive=True):
        budget[0] += 1
        if budget[0] > _MAX_SCANNED:
            raise ValueError(f"mass scope exceeds {_MAX_SCANNED} scanned element references")
        yield value


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise RuntimeError(f"{label} must be a finite number")
    return float(value)


def _material_coverage(base, deck, selected):
    sections, materials = {}, {}
    # Read only the selected elements' PID. Expanding an entire shared section
    # to verify a one-element scope would scan arbitrarily many outside elements.
    for key, element in selected.items():
        reference = {"deck": deck, "type": key[0], "id": key[1]}
        if "PID" not in catalog._entity_card_labels(element, deck):
            raise RuntimeError("selected element has no verifiable PID section assignment")
        pid = catalog._read_card(base, deck, element, reference, ("PID",))["PID"]
        section_type = "SHELL_SECTION" if key[0] == "SHELL" else "SOLID_SECTION"
        pkey = (section_type, pid)
        if pkey in sections:
            continue
        if len(sections) >= 2000:
            raise ValueError("mass scope exceeds 2000 section assignments")
        prop, reference = catalog._get_entity(base, deck, section_type, pid)
        sections[pkey] = reference
        labels = catalog._entity_card_labels(prop, deck)
        if "MID" not in labels:
            raise RuntimeError("section has no verifiable MID material assignment")
        material_id = catalog._read_card(base, deck, prop, reference, ("MID",))["MID"]
        material, material_ref = catalog._get_entity(base, deck, "MATERIAL", material_id)
        mlabels = catalog._entity_card_labels(material, deck)
        if not {"*DENSITY", "DENS"}.issubset(mlabels):
            raise NotImplementedError("mass_properties requires an explicitly defined constant material density")
        density = catalog._read_card(base, deck, material, material_ref, ("*DENSITY", "DENS"))
        if density["*DENSITY"] != "YES" or _number(density["DENS"], "material density") <= 0:
            raise RuntimeError("mass scope contains an undefined or non-positive material density")
        materials[material_ref["id"]] = {"entity": material_ref, "density": density["DENS"]}
    return [sections[k] for k in sorted(sections)], [materials[k] for k in sorted(materials)]


def mass_properties(base, constants, deck, params):
    """Return scalar mass and its exact shell/solid selection, never whole-model mass."""
    catalog._object(params, ("deck", "entities"), ("element_types", "no_nsm"))
    if type(params["deck"]) is not int or params["deck"] != deck:
        raise ValueError("params.deck must match the current deck")
    if deck != getattr(constants, "ABAQUS", None):
        raise ValueError("mass_properties supports only the ABAQUS deck")
    inputs = params["entities"]
    if type(inputs) is not list or not 1 <= len(inputs) <= 200:
        raise ValueError("entities must contain 1 to 200 explicit references")
    kinds = params.get("element_types", list(_ELEMENT_TYPES))
    if (type(kinds) is not list or not kinds or any(type(k) is not str or k not in _ELEMENT_TYPES for k in kinds)
            or len(set(kinds)) != len(kinds)):
        raise ValueError("element_types must contain unique SHELL and/or SOLID values")
    no_nsm = params.get("no_nsm", False)
    if type(no_nsm) is not bool:
        raise TypeError("no_nsm must be a boolean")

    roots, references, keys = [], [], set()
    for value in inputs:
        catalog._object(value, ("deck", "type", "id"), label="entity reference")
        if type(value["deck"]) is not int or value["deck"] != deck:
            raise ValueError("entity reference deck must match the current deck")
        kind = value["type"]
        if type(kind) is not str or kind not in _ROOT_TYPES:
            raise ValueError("unsupported ABAQUS mass root; use a material, section, Part, Group, SHELL or SOLID")
        entity, reference = catalog._get_entity(base, deck, kind, value["id"])
        key = (kind, reference["id"])
        if key in keys:
            raise ValueError("entities must not contain duplicate references")
        keys.add(key)
        roots.append(entity)
        references.append(reference)

    selected, budget = {}, [0]
    for root, reference in zip(roots, references):
        if reference["type"] in _ELEMENT_TYPES:
            collections = [(reference["type"], [root])] if reference["type"] in kinds else []
        else:
            collections = [(kind, _collection(base, deck, root, kind, budget)) for kind in kinds]
        for kind, elements in collections:
            for element in elements:
                ref = catalog._entity_reference(base, deck, element, kind)
                selected[(kind, ref["id"])] = element
                if len(selected) > _MAX_ELEMENTS:
                    raise ValueError(f"mass scope exceeds {_MAX_ELEMENTS} shell/solid elements")
    if not selected:
        raise ValueError("explicit mass scope contains no selected shell/solid elements; empty is not zero mass")
    ordered = sorted(selected)
    elements = [selected[key] for key in ordered]
    sections, materials = _material_coverage(base, deck, selected)
    # CalcElementMass(MATERIAL) returns zero/NaNs on ANSA 2026.1. Resolve the
    # actual, deduplicated FE elements first, including for material selections.
    api = getattr(base, "CalcElementMass", None)
    if not callable(api):
        raise NotImplementedError("ansa.base.CalcElementMass is unavailable")
    native = api(elements, no_nsm=no_nsm, deck=deck)
    if type(native) not in (list, tuple) or len(native) != 22:
        raise RuntimeError("CalcElementMass returned no complete 22-value mass-properties result")
    values = [_number(v, f"CalcElementMass[{i}]") for i, v in enumerate(native)]
    if values[0] <= 0:
        raise RuntimeError("CalcElementMass returned non-positive mass for a nonempty scope; check material definitions")
    encoded = json.dumps(ordered, separators=(",", ":")).encode("utf-8")
    counts = {kind: sum(key[0] == kind for key in ordered) for kind in _ELEMENT_TYPES}
    return {
        "deck": deck, "read_only": True, "complete": True,
        "api": "ansa.base.CalcElementMass", "value_kind": "scalar_mass", "value": values[0],
        "mass": values[0], "center_of_gravity": values[1:4],
        "inertia": dict(zip(("Ixx", "Iyy", "Izz", "Ixy", "Ixz", "Iyz"), values[4:10])),
        "principal_moments": values[10:13],
        "principal_axes_columns": [[values[13+i], values[16+i], values[19+i]] for i in range(3)],
        "scope": {"roots": references, "element_types": kinds, "counts": counts,
                  "element_count": len(ordered), "element_keys_sha256": hashlib.sha256(encoded).hexdigest(),
                  "sample": [{"deck": deck, "type": k, "id": i} for k, i in ordered[:20]],
                  "sample_truncated": len(ordered) > 20, "deduplicated": True,
                  "scanned_element_references": budget[0], "scan_limit": _MAX_SCANNED,
                  "excluded_types": [k for k in _ELEMENT_TYPES if k not in kinds],
                  "other_element_types": "excluded; this result covers selected SHELL/SOLID only",
                  "sections": sections, "materials": materials, "material_assignments_verified": True,
                  "no_nsm": no_nsm},
        "units": "Current model units; no unit conversion performed",
    }
