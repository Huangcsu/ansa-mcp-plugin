"""Independently audit a saved native annular sweep's FE boundary and hole."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ansa_mcp_bridge.operations import annular_source, mesh_audit, structured_native


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _side_facets(loop: list[int], layers: dict[int, dict[int, int]], steps: int) -> list[list[int]]:
    result = []
    for first, second in zip(loop, loop[1:] + loop[:1]):
        for layer in range(steps):
            result.append([layers[first][layer], layers[second][layer],
                           layers[second][layer + 1], layers[first][layer + 1]])
    return result


def _native_capture_digest(snapshot: dict) -> str:
    """Recreate the native capture's schema order without changing any value.

    Native capture hashes preserve dict insertion order. A JSON file writer
    may sort object keys, so restore that documented capture order before
    rehashing; coordinates, element order and IDs remain exact.
    """
    def ordered(row, keys):
        if type(row) is not dict or set(row) != set(keys):
            raise ValueError("snapshot does not match the native capture schema")
        return {key: row[key] for key in keys}
    fields = ("deck", "complete", "nodes", "elements", "integrity", "ownership")
    body = ordered(snapshot, (*fields, "snapshot_sha256"))
    body.pop("snapshot_sha256")
    body["nodes"] = [ordered(row, ("id", "xyz")) for row in body["nodes"]]
    body["elements"] = [ordered(row, ("id", "type", "nodes", "property_id", "block_id")) for row in body["elements"]]
    body["integrity"] = ordered(body["integrity"], ("complete", "requested_element_count", "element_count",
        "node_count", "missing_node_ids", "element_types"))
    body["integrity"]["element_types"] = dict(sorted(body["integrity"]["element_types"].items()))
    owner_fields = ("origin", "complete", "known_count")
    if "reason" in body["ownership"]:
        owner_fields += ("reason",)
    body["ownership"] = ordered(body["ownership"], owner_fields)
    return structured_native._hash(body)


def audit(capture: dict, case: dict, public: dict | None = None) -> dict:
    source, output = capture["source"], capture["output"]
    for label, snapshot in (("source", source), ("output", output)):
        if snapshot.get("snapshot_sha256") != _native_capture_digest(snapshot):
            raise ValueError(label + " snapshot checksum does not match its actual content")
    options = {"center": case["center"], "axis": case["axis"],
               "inner_radius": case["inner_radius"], "outer_radius": case["outer_radius"],
               "max_chord_error": 0.2, "tolerance": 0.001}
    source_check = annular_source.audit(source, options)
    if source_check["status"] != "verified_mesh_template":
        raise ValueError("source annulus was not verified: " + str(source_check.get("reason")))
    source_ids = sorted(row["id"] for row in source["elements"])
    source_grids = {row["id"] for row in source["nodes"]}
    output_grids = {row["id"] for row in output["nodes"]}
    property_ids = {row["property_id"] for row in output["elements"]}
    if len(property_ids) != 1:
        raise ValueError("output SOLIDs do not have exactly one property")
    sweep = structured_native._verify_sweep_mesh(
        source, output, sorted(output_grids - source_grids), {
            "shell_ids": source_ids, "direction": case["axis"],
            "distance": case["distance"], "steps": case["steps"],
            "property_id": next(iter(property_ids))})
    layers = {row["source_grid_id"]: {item["layer"]: item["grid_id"] for item in row["layers"]}
              for row in sweep["node_map"]}
    cap_facets = []
    for element in source["elements"]:
        for layer in (0, case["steps"]):
            cap_facets.append([layers[node_id][layer] for node_id in element["nodes"]])
    inner = _side_facets(source_check["boundaries"]["inner"]["node_ids"], layers, case["steps"])
    outer = _side_facets(source_check["boundaries"]["outer"]["node_ids"], layers, case["steps"])
    expected_boundary = cap_facets + inner + outer
    owners = {row["block_id"] for row in output["elements"]}
    if output["ownership"]["complete"] is not True or len(owners) != 1 or None in owners:
        raise ValueError("complete native VOLUME ownership is required before declaring no cross-block interface")
    cylinders = [
        {"label": name, "center": case["center"], "axis": case["axis"],
         "radius": case[name + "_radius"], "tolerance": 0.001, "facets": facets}
        for name, facets in (("inner", inner), ("outer", outer))
    ]
    fe = mesh_audit.audit_snapshot(output, interfaces=[], expected_boundary=expected_boundary,
                                   protected_cylinders=cylinders, expected_components=1)
    polygon_volume = source_check["metrics"]["annular_polygon_area"] * case["distance"]
    analytic_volume = math.pi * (case["outer_radius"]**2 - case["inner_radius"]**2) * case["distance"]
    quality = public.get("quality") if public else None
    public_binding = public.get("before_save", {}) if public else {}
    quality_checked = bool(public and public.get("complete") is True
                           and public_binding.get("source_sha256") == source["snapshot_sha256"]
                           and public_binding.get("mesh_sha256") == output["snapshot_sha256"]
                           and public.get("save_reopen")
                           and all(public["save_reopen"].values())
                           and quality and quality.get("status") == "passed"
                           and quality.get("checked_count") == sweep["solid_count"]
                           and quality.get("failed_count") == 0 and quality.get("unknown_count") == 0)
    geometry_pass = bool(fe["topology_status"] == "pass" and fe["geometry_status"] == "pass"
                         and fe["sections"]["expected_boundary"]["comparison_complete"]
                         and not fe["sections"]["expected_boundary"]["missing_facets"]
                         and not fe["sections"]["expected_boundary"]["unexpected_facets"]
                         and math.isclose(sweep["total_volume"], polygon_volume, rel_tol=1e-9, abs_tol=1e-6))
    return {
        "status": "verified_polygonal_annular_domain" if geometry_pass and quality_checked else "incomplete",
        "source_sha256": source["snapshot_sha256"],
        "mesh_sha256": output["snapshot_sha256"],
        "source_element_types": source["integrity"]["element_types"],
        "solid_type_counts": sweep["solid_type_counts"],
        "positive_volume_count": sweep["positive_volume_count"],
        "source_layer_coverage": sweep["source_layer_coverage"],
        "source_boundary_edges": source_check["metrics"]["boundary_edge_count"],
        "expected_boundary_facets": len(expected_boundary),
        "actual_free_facets": fe["summary"]["actual_free_face_count"],
        "topology_status": fe["topology_status"],
        "geometry_status": fe["geometry_status"],
        "boundary_complete": fe["sections"]["expected_boundary"]["comparison_complete"],
        "protected_cylinders": [{"label": row["label"], "status": row["status"],
                                 "facet_count": row["facet_count"],
                                 "maximum_radial_error": row["maximum_absolute_radial_error"]}
                                for row in fe["sections"]["geometry"]["cylinders"]],
        "inner_max_chord_error": source_check["boundaries"]["inner"]["maximum_chord_error"],
        "outer_max_chord_error": source_check["boundaries"]["outer"]["maximum_chord_error"],
        "fe_volume": sweep["total_volume"],
        "polygon_volume": polygon_volume,
        "analytic_annular_volume": analytic_volume,
        "fe_to_analytic_volume_ratio": sweep["total_volume"] / analytic_volume,
        "quality_selected_criteria_passed": quality_checked,
        "quality_scope": quality,
        "original_CAD_continuous_coverage_proven": False,
        "solver_verified": False,
        "diagnostic_codes": [row["code"] for row in fe["diagnostics"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture-report", type=Path, required=True)
    parser.add_argument("--case-report", type=Path, required=True)
    parser.add_argument("--public-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError("refusing to overwrite an annular boundary report")
    capture = _load(args.capture_report.resolve(strict=True))["result"]
    case = _load(args.case_report.resolve(strict=True))["case_params"]
    public = _load(args.public_report.resolve(strict=True))
    if public.get("saved_capture"):
        digest = hashlib.sha256(args.capture_report.read_bytes()).hexdigest()
        if digest != public["saved_capture"]["sha256"]:
            raise ValueError("capture file checksum does not match the saved public readback")
    result = audit(capture, case, public)
    result["source_reports"] = {
        str(path.resolve(strict=True)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (args.capture_report, args.case_report, args.public_report)
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(output),
                      "expected_boundary_facets": result["expected_boundary_facets"],
                      "actual_free_facets": result["actual_free_facets"],
                      "geometry_status": result["geometry_status"],
                      "quality_selected_criteria_passed": result["quality_selected_criteria_passed"]},
                     ensure_ascii=False), flush=True)
    return 0 if result["status"] == "verified_polygonal_annular_domain" else 1


if __name__ == "__main__":
    raise SystemExit(main())
