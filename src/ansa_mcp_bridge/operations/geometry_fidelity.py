"""Bounded, read-only samples of native CURVE-to-CONS deviation."""

import math


_SAMPLE_PARAMETERS = (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0)


def _xyz(value, label):
    if type(value) not in (tuple, list) or len(value) != 3:
        raise ValueError(f"{label} must be a three-item tuple or list")
    point = list(value)
    for index, coordinate in enumerate(point):
        if type(coordinate) not in (int, float):
            raise ValueError(f"{label}[{index}] must be an int or float, not bool")
        if type(coordinate) is float and not math.isfinite(coordinate):
            raise ValueError(f"{label}[{index}] must be finite")
    return point


def _is_zero_sentinel(value):
    return type(value) in (int, float) and value == 0


def _projection_points(value):
    if _is_zero_sentinel(value):
        raise ValueError("ProjectPointToCons returned 0")
    if type(value) not in (tuple, list):
        raise ValueError("ProjectPointToCons must return a tuple or list")
    if len(value) == 1:
        projection = _xyz(value[0], "projection point")
        return projection, projection, "interior"
    if len(value) == 2:
        projection = _xyz(value[0], "projection point")
        snap = _xyz(value[1], "endpoint snap point")
        return projection, snap, "endpoint_snap"
    raise ValueError("ProjectPointToCons must return one or two points")


def _distance(first, second):
    try:
        distance = math.dist(first, second)
    except (OverflowError, ValueError) as exc:
        raise ValueError("distance is not representable as a finite float") from exc
    if not math.isfinite(distance):
        raise ValueError("distance is not finite")
    return distance


def _append_error(row, stage, message):
    row["errors"].append(f"{stage}: {message}")


def _exception_text(exc):
    try:
        detail = str(exc)
    except Exception:
        detail = "exception message unavailable"
    return f"raised {type(exc).__name__}: {detail}"


def _rms(values):
    if not values:
        return None
    norm = math.hypot(*values)
    count_root = math.sqrt(len(values))
    if math.isfinite(norm):
        return norm / count_root

    # Keep the equivalent hypot/sqrt(n) calculation finite when the total
    # norm exceeds float range even though its RMS is representable.
    scale = max(values)
    ratio = math.hypot(*(value / scale for value in values)) / count_root
    return scale if ratio >= 1.0 else scale * ratio


def capture(base, calc, cons, curve):
    """Measure nine fixed curve samples against one CONS without changing the model."""
    samples = []
    distances = []
    evaluate_curve_calls = 0
    project_point_to_cons_calls = 0

    for u in _SAMPLE_PARAMETERS:
        row = {
            "u": u,
            "curve_point": None,
            "projection_point": None,
            "closest_point": None,
            "projection_mode": None,
            "distance": None,
            "errors": [],
        }
        samples.append(row)

        evaluate_curve_calls += 1
        try:
            raw_curve_point = base.EvaluateCurvePoint(curve, u)
        except Exception as exc:
            _append_error(row, "EvaluateCurvePoint", _exception_text(exc))
            continue
        if _is_zero_sentinel(raw_curve_point):
            _append_error(row, "EvaluateCurvePoint", "returned 0")
            continue
        try:
            curve_point = _xyz(raw_curve_point, "curve point")
        except ValueError as exc:
            _append_error(row, "EvaluateCurvePoint", str(exc))
            continue
        row["curve_point"] = curve_point

        project_point_to_cons_calls += 1
        try:
            raw_projection = calc.ProjectPointToCons(curve_point, cons)
        except Exception as exc:
            _append_error(row, "ProjectPointToCons", _exception_text(exc))
            continue
        try:
            projection_point, closest_point, projection_mode = _projection_points(raw_projection)
        except ValueError as exc:
            _append_error(row, "ProjectPointToCons", str(exc))
            continue

        row["projection_point"] = projection_point
        row["closest_point"] = closest_point
        row["projection_mode"] = projection_mode
        try:
            distance = _distance(curve_point, closest_point)
        except ValueError as exc:
            _append_error(row, "distance", str(exc))
            continue

        row["distance"] = distance
        distances.append(distance)

    return {
        "status": "sampled_only" if len(distances) == len(_SAMPLE_PARAMETERS) else "incomplete",
        "samples": samples,
        "maximum_observed_distance": max(distances) if distances else None,
        "rms_observed_distance": _rms(distances),
        "valid_sample_count": len(distances),
        "requested_sample_count": len(_SAMPLE_PARAMETERS),
        "evaluate_curve_calls": evaluate_curve_calls,
        "project_point_to_cons_calls": project_point_to_cons_calls,
        "continuous_equivalence_proven": False,
        "reverse_coverage_proven": False,
        "usable_for_mutation": False,
    }
