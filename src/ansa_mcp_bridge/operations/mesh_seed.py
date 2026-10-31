"""Offline recommendations for integer mesh edge seeds."""

import math


_TAU = math.tau
_MAX_SEGMENTS = 10000
_MAX_SEED_CONSTRAINTS = 512
_MAX_SEED_EDGES_PER_CONSTRAINT = 128
_PLAN_FIELDS = {"edges", "equal_groups", "max_segments"}
_EDGE_FIELDS = {
    "id", "length", "target_length", "kind", "radius", "max_chord_error",
    "min_segments", "multiple",
}


def _object(value, required, allowed, label):
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    missing = set(required) - value.keys()
    unknown = [repr(key) for key in value if key not in allowed]
    if missing:
        raise ValueError(f"{label} is missing required field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label} has unsupported field(s): {', '.join(sorted(unknown))}")
    return value


def _edge_id(value, label):
    if type(value) is not str or not value or value != value.strip() or len(value) > 128:
        raise ValueError(f"{label} must be a non-empty string of at most 128 characters")
    return value


def _positive_number(value, label):
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} must be a finite positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _positive_integer(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_ratio(numerator, denominator, label):
    try:
        ratio = numerator / denominator
    except (OverflowError, ZeroDivisionError):
        raise ValueError(f"{label} ratio is not representable") from None
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError(f"{label} ratio must be finite and positive")
    return ratio


def _ceil_division(numerator, denominator, label):
    ratio = _finite_ratio(numerator, denominator, label)
    try:
        return math.ceil(ratio)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} ratio cannot be converted to an integer") from None


def _float_divided_by_int(value, divisor):
    numerator, denominator = value.as_integer_ratio()
    try:
        return numerator / (denominator * divisor)
    except (OverflowError, ZeroDivisionError):
        raise ValueError("arc angle division is not representable") from None


def _arc_chord_error(sweep_angle, radius, segments):
    quarter_segment_angle = _float_divided_by_int(sweep_angle, 4 * segments)
    sine = math.sin(quarter_segment_angle)
    error = (radius * sine * sine) * 2.0
    if not math.isfinite(error):
        raise ValueError("arc chord error is not representable as a finite number")
    return error


def _arc_chord_satisfies(sweep_angle, sine_limit, segments):
    quarter_segment_angle = _float_divided_by_int(sweep_angle, 4 * segments)
    return math.sin(quarter_segment_angle) <= sine_limit


def _required_arc_segments(sweep_angle, radius, chord_error, max_segments):
    try:
        error_ratio = chord_error / radius
    except (OverflowError, ZeroDivisionError):
        raise ValueError("max_chord_error/radius ratio is not representable") from None
    if not math.isfinite(error_ratio):
        raise ValueError("max_chord_error/radius ratio must be finite")
    if error_ratio >= 2.0:
        return 1

    # sqrt(error / (2 * radius)) is evaluated as separate square roots so a
    # very small but valid ratio does not underflow before taking its root.
    sine_limit = math.sqrt(chord_error) / math.sqrt(radius) / math.sqrt(2.0)
    if not math.isfinite(sine_limit) or sine_limit <= 0:
        raise ValueError("chord error is too small to resolve safely")
    max_segment_sweep = 4.0 * math.asin(sine_limit)
    if not math.isfinite(max_segment_sweep) or max_segment_sweep <= 0:
        raise ValueError("maximum arc segment angle is not representable")
    estimate = _ceil_division(sweep_angle, max_segment_sweep, "arc segment")

    if estimate <= max_segments + 2:
        # Resolve the small rounding boundary exactly within the useful budget.
        high = max(1, estimate)
        while not _arc_chord_satisfies(sweep_angle, sine_limit, high):
            high += 1
            if high > max_segments + 4:
                raise ValueError("arc segment count could not be resolved safely")
        low = 1
        while low < high:
            middle = (low + high) // 2
            if _arc_chord_satisfies(sweep_angle, sine_limit, middle):
                high = middle
            else:
                low = middle + 1
        return low

    # An over-budget count will never be sent as an executable seed. Check the
    # estimate and move upward if floating-point rounding put it just too low.
    for _ in range(4):
        if _arc_chord_satisfies(sweep_angle, sine_limit, estimate):
            return estimate
        estimate += 1
    raise ValueError("arc segment count could not be resolved safely")


def _normalize_edge(raw, index):
    label = f"edges[{index}]"
    _object(raw, ("id", "length", "target_length", "kind"), _EDGE_FIELDS, label)
    kind = raw["kind"]
    if type(kind) is not str or kind not in ("line", "circular_arc"):
        raise ValueError(f"{label}.kind must be 'line' or 'circular_arc'")
    if kind == "circular_arc":
        missing = {"radius", "max_chord_error"} - raw.keys()
        if missing:
            raise ValueError(f"{label} is missing required field(s): {', '.join(sorted(missing))}")
    elif "radius" in raw or "max_chord_error" in raw:
        raise ValueError(f"{label} arc fields are only valid for circular_arc edges")

    edge_id = _edge_id(raw["id"], f"{label}.id")
    length = _positive_number(raw["length"], f"{label}.length")
    target_length = _positive_number(raw["target_length"], f"{label}.target_length")
    min_segments = _positive_integer(raw.get("min_segments", 1), f"{label}.min_segments")
    multiple = _positive_integer(raw.get("multiple", 1), f"{label}.multiple")
    required = max(
        min_segments,
        _ceil_division(length, target_length, f"{label}.length/target_length"),
    )

    edge = {
        "id": edge_id,
        "length": length,
        "target_length": target_length,
        "kind": kind,
        "min_segments": min_segments,
        "multiple": multiple,
        "required_segments": required,
        "sweep_angle": None,
        "closed_arc": None,
        "max_chord_error": None,
        "required_chord_error": None,
    }
    if kind == "circular_arc":
        radius = _positive_number(raw["radius"], f"{label}.radius")
        max_chord_error = _positive_number(raw["max_chord_error"], f"{label}.max_chord_error")
        sweep_angle = _finite_ratio(length, radius, f"{label}.length/radius")
        if sweep_angle > _TAU:
            raise ValueError(f"{label} arc length/radius must be at most 2*pi")
        closed_arc = sweep_angle == _TAU
        if closed_arc:
            required = max(required, 3)
        chord_segments = _required_arc_segments(
            sweep_angle, radius, max_chord_error, _MAX_SEGMENTS
        )
        required = max(required, chord_segments)
        edge.update({
            "required_segments": required,
            "sweep_angle": sweep_angle,
            "closed_arc": closed_arc,
            "max_chord_error": max_chord_error,
            "required_chord_error": _arc_chord_error(sweep_angle, radius, required),
            "radius": radius,
        })
    return edge


def recommend_seeds(plan):
    """Recommend integer edge seeds from caller-declared geometry only.

    Equal groups, including overlapping groups, share one count. If any
    coordinated count exceeds the budget, the result is incomplete and its
    ``seed_constraints`` is empty so it cannot be applied downstream.
    """
    _object(plan, ("edges",), _PLAN_FIELDS, "plan")
    raw_edges = plan["edges"]
    if type(raw_edges) is not list or not raw_edges:
        raise ValueError("edges must be a non-empty list")
    if len(raw_edges) > _MAX_SEED_CONSTRAINTS * _MAX_SEED_EDGES_PER_CONSTRAINT:
        raise ValueError("edges exceed the mesh_planning seed constraint capacity")
    max_segments = plan.get("max_segments", _MAX_SEGMENTS)
    if type(max_segments) is not int or not 1 <= max_segments <= _MAX_SEGMENTS:
        raise ValueError(f"max_segments must be an integer from 1 to {_MAX_SEGMENTS}")

    edges = [_normalize_edge(raw, index) for index, raw in enumerate(raw_edges)]
    by_id = {}
    for edge in edges:
        if edge["id"] in by_id:
            raise ValueError(f"duplicate edge id {edge['id']!r}")
        by_id[edge["id"]] = edge

    parent = {edge_id: edge_id for edge_id in by_id}

    def find(edge_id):
        while parent[edge_id] != edge_id:
            parent[edge_id] = parent[parent[edge_id]]
            edge_id = parent[edge_id]
        return edge_id

    def union(left, right):
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    raw_groups = plan.get("equal_groups", [])
    if type(raw_groups) is not list:
        raise ValueError("equal_groups must be a list of edge ID lists")
    for group_index, raw_group in enumerate(raw_groups):
        label = f"equal_groups[{group_index}]"
        if type(raw_group) is not list or len(raw_group) < 2:
            raise ValueError(f"{label} must contain at least two edge IDs")
        group = [_edge_id(item, f"{label}[{i}]") for i, item in enumerate(raw_group)]
        if len(group) != len(set(group)):
            raise ValueError(f"{label} contains a duplicate edge ID")
        missing = [edge_id for edge_id in group if edge_id not in by_id]
        if missing:
            raise ValueError(f"{label} refers to missing edge IDs {missing!r}")
        for edge_id in group[1:]:
            union(group[0], edge_id)

    components = {}
    for edge_id in by_id:
        components.setdefault(find(edge_id), []).append(edge_id)
    groups = [sorted(members) for members in components.values()]
    groups.sort(key=lambda members: members[0])

    group_results = []
    edge_group = {}
    for index, members in enumerate(groups, start=1):
        group_edges = [by_id[edge_id] for edge_id in members]
        geometric_required = max(edge["required_segments"] for edge in group_edges)
        common_multiple = math.lcm(*(edge["multiple"] for edge in group_edges))
        required_count = ((geometric_required + common_multiple - 1) // common_multiple) * common_multiple
        group = {
            "id": f"seed-group-{index:04d}",
            "edge_ids": members,
            "required_segments": required_count,
            "recommended_segments": required_count if required_count <= max_segments else None,
        }
        group_results.append(group)
        for edge_id in members:
            edge_group[edge_id] = group

    over_budget = [group for group in group_results if group["recommended_segments"] is None]
    complete = not over_budget
    constraints = []
    if complete:
        edge_ids_by_count = {}
        for edge_id in sorted(by_id):
            count = edge_group[edge_id]["recommended_segments"]
            edge_ids_by_count.setdefault(count, []).append(edge_id)
        for count in sorted(edge_ids_by_count):
            edge_ids = edge_ids_by_count[count]
            for offset in range(0, len(edge_ids), _MAX_SEED_EDGES_PER_CONSTRAINT):
                if len(constraints) >= _MAX_SEED_CONSTRAINTS:
                    raise ValueError("seed plan exceeds mesh_planning constraint capacity")
                constraints.append({
                    "id": f"seed-constraint-{len(constraints) + 1:04d}",
                    "edge_ids": edge_ids[offset:offset + _MAX_SEED_EDGES_PER_CONSTRAINT],
                    "segments": count,
                })

    edge_segments = []
    for edge_id in sorted(by_id):
        edge = by_id[edge_id]
        group = edge_group[edge_id]
        count = group["recommended_segments"]
        chord_error = None
        if edge["kind"] == "circular_arc" and count is not None:
            chord_error = _arc_chord_error(edge["sweep_angle"], edge["radius"], count)
        edge_segments.append({
            "edge_id": edge_id,
            "kind": edge["kind"],
            "length": edge["length"],
            "target_length": edge["target_length"],
            "required_segments": edge["required_segments"],
            "group_required_segments": group["required_segments"],
            "recommended_segments": count,
            "count": count,
            "min_segments": edge["min_segments"],
            "multiple": edge["multiple"],
            "equal_group_id": group["id"] if len(group["edge_ids"]) > 1 else None,
            "closed_arc": edge["closed_arc"],
            "sweep_angle_radians": edge["sweep_angle"],
            "max_chord_error": edge["max_chord_error"],
            "required_chord_error": edge["required_chord_error"],
            "chord_error": chord_error,
        })

    summary = {
        "edge_count": len(edges),
        "equal_group_count": sum(len(group["edge_ids"]) > 1 for group in group_results),
        "constraint_component_count": len(group_results),
        "seed_constraint_count": len(constraints),
        "over_budget_groups": [
            (
                {"equal_group_id": group["id"], "required_segments": group["required_segments"]}
                if len(group["edge_ids"]) > 1
                else {"edge_id": group["edge_ids"][0], "required_segments": group["required_segments"]}
            )
            for group in over_budget
        ],
        "total_recommended_segments": (
            sum(edge["count"] for edge in edge_segments) if complete else None
        ),
    }
    return {
        "status": "complete" if complete else "incomplete",
        "edge_segments": edge_segments,
        "equal_groups": [group for group in group_results if len(group["edge_ids"]) > 1],
        "seed_constraints": constraints,
        "summary": summary,
        "input_origin": "caller_declared_geometry",
        "native_geometry_verified": False,
    }
