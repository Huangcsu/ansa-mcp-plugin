"""Strict, dependency-free IGES identity and analytic curve reader.

The module understands IGES directory/parameter binding and only the small
entity set needed for standalone lines and circular arcs.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass


_MAX_FILE_SIZE = 8 * 1024 * 1024
_SECTIONS = ("S", "G", "D", "P", "T")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?$")
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_HOLLERITH_RE = re.compile(rb"([0-9]{1,9})[Hh]")
_IDENTITY_TOLERANCE = 1e-10
_RIGID_TOLERANCE = 1e-8


class _Unsupported(Exception):
    """A well-formed IGES input uses an analytic form this module omits."""


class _Hollerith(str):
    """Retain IGES string type so numeric-looking text cannot become geometry."""


@dataclass(frozen=True)
class _Record:
    raw: bytes
    section: str
    sequence: int


@dataclass(frozen=True)
class _Entity:
    sequence: int
    entity_type: int
    parameter_pointer: int
    parameter_line_count: int
    transform_pointer: int
    form: int
    parameters: tuple[str, ...]


@dataclass(frozen=True)
class _ParsedFile:
    sections: dict[str, tuple[_Record, ...]]
    entities: tuple[_Entity, ...]
    entities_by_sequence: dict[int, _Entity]
    parameter_delimiter: int
    record_delimiter: int


def _invalid(message: str) -> ValueError:
    return ValueError(f"invalid IGES: {message}")


def _physical_records(raw: bytes) -> tuple[bytes, ...]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    if not raw:
        raise _invalid("empty input")
    if len(raw) > _MAX_FILE_SIZE:
        raise _invalid("input exceeds 8 MiB")

    chunks = raw.split(b"\n")
    if chunks[-1] == b"":
        chunks.pop()
    records = []
    for number, chunk in enumerate(chunks, 1):
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        if b"\r" in chunk:
            raise _invalid(f"record {number} has an unsupported line ending")
        if len(chunk) != 80:
            raise _invalid(f"record {number} is not exactly 80 columns")
        if any(byte < 32 or byte > 126 for byte in chunk):
            raise _invalid(f"record {number} contains non-printable ASCII")
        records.append(chunk)
    if not records:
        raise _invalid("no records")
    return tuple(records)


def _parse_integer(value: str, description: str) -> int:
    if isinstance(value, _Hollerith):
        raise _invalid(f"{description} cannot be a Hollerith string")
    value = value.strip()
    if not _INTEGER_RE.fullmatch(value):
        raise _invalid(f"{description} is not an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise _invalid(f"{description} is not an integer") from exc


def _field_integer(field: bytes, description: str, *, blank_is_zero: bool = False) -> int:
    try:
        value = field.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise _invalid(f"{description} is not ASCII") from exc
    if not value and blank_is_zero:
        return 0
    return _parse_integer(value, description)


def _read_hollerith(data: bytes, offset: int) -> tuple[bytes, int]:
    match = _HOLLERITH_RE.match(data, offset)
    if match is None:
        raise _invalid("expected a Hollerith string")
    length = int(match.group(1))
    start = match.end()
    end = start + length
    if end > len(data):
        raise _invalid("Hollerith string is truncated")
    return data[start:end], end


def _valid_delimiter(value: bytes) -> bool:
    return (
        len(value) == 1
        and 33 <= value[0] <= 126
        and not chr(value[0]).isalnum()
    )


def _parameter_delimiters(global_data: bytes) -> tuple[int, int]:
    if global_data.startswith(b","):
        default_parameter = ord(",")
        default_record = ord(";")
        tokens = _parameter_tokens(global_data, default_parameter, default_record)
        if len(tokens) >= 2 and tokens[:2] == ("", ""):
            return default_parameter, default_record

    first, offset = _read_hollerith(global_data, 0)
    if not _valid_delimiter(first):
        raise _invalid("Global section has an invalid parameter delimiter")
    parameter_delimiter = first[0]
    if offset >= len(global_data) or global_data[offset] != parameter_delimiter:
        raise _invalid("Global section does not bind its parameter delimiter")

    second, offset = _read_hollerith(global_data, offset + 1)
    if not _valid_delimiter(second):
        raise _invalid("Global section has an invalid record delimiter")
    record_delimiter = second[0]
    if parameter_delimiter == record_delimiter:
        raise _invalid("Global delimiters must differ")
    if offset >= len(global_data) or global_data[offset] != parameter_delimiter:
        raise _invalid("Global section does not bind its record delimiter")

    _parameter_tokens(global_data, parameter_delimiter, record_delimiter)
    return parameter_delimiter, record_delimiter


def _parameter_tokens(
    data: bytes, parameter_delimiter: int, record_delimiter: int
) -> tuple[str, ...]:
    tokens = []
    offset = 0
    while offset < len(data) and data[offset] == 32:
        offset += 1

    while offset < len(data):
        if data[offset] == record_delimiter:
            if not tokens:
                raise _invalid("parameter data is empty")
            if any(byte != 32 for byte in data[offset + 1 :]):
                raise _invalid("data follows a parameter record terminator")
            return tuple(tokens)

        if data[offset] == parameter_delimiter:
            tokens.append("")
            offset += 1
            continue

        hollerith = _HOLLERITH_RE.match(data, offset)
        if hollerith is not None:
            length = int(hollerith.group(1))
            start = hollerith.end()
            end = start + length
            if end >= len(data):
                raise _invalid("Hollerith parameter lacks a delimiter")
            value_bytes = data[start:end]
            delimiter = data[end]
            if delimiter not in (parameter_delimiter, record_delimiter):
                raise _invalid("Hollerith parameter is not delimiter-bound")
            value = _Hollerith(value_bytes.decode("ascii"))
            offset = end + 1
        else:
            end = offset
            while end < len(data) and data[end] not in (
                parameter_delimiter,
                record_delimiter,
            ):
                end += 1
            if end == len(data):
                raise _invalid("parameter data lacks a record terminator")
            value = data[offset:end].decode("ascii").strip()
            delimiter = data[end]
            offset = end + 1

        tokens.append(value)
        if delimiter == record_delimiter:
            if any(byte != 32 for byte in data[offset:]):
                raise _invalid("data follows a parameter record terminator")
            return tuple(tokens)

    raise _invalid("parameter data lacks a record terminator")


def _parse_iges(raw: bytes) -> _ParsedFile:
    physical = _physical_records(raw)
    sections: dict[str, list[_Record]] = {section: [] for section in _SECTIONS}
    previous_rank = -1

    for line_number, line in enumerate(physical, 1):
        section_byte = line[72:73]
        try:
            section = section_byte.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _invalid(f"record {line_number} has no section code") from exc
        if section not in sections:
            raise _invalid(f"record {line_number} has an unknown section code")
        rank = _SECTIONS.index(section)
        if rank < previous_rank:
            raise _invalid("sections are out of order")
        previous_rank = rank

        sequence_bytes = line[73:80]
        sequence = _field_integer(sequence_bytes, f"record {line_number} sequence")
        if sequence_bytes != f"{sequence:7d}".encode("ascii"):
            raise _invalid(f"record {line_number} has an invalid sequence number")
        expected = len(sections[section]) + 1
        if sequence != expected:
            raise _invalid(f"{section} sequence is not contiguous")
        sections[section].append(_Record(line, section, sequence))

    if any(not sections[section] for section in _SECTIONS):
        raise _invalid("all S, G, D, P, and T sections are required")
    if len(sections["T"]) != 1:
        raise _invalid("exactly one Terminate record is required")
    if len(sections["D"]) % 2:
        raise _invalid("Directory Entry records must occur in pairs")

    terminate = sections["T"][0].raw
    for index, section in enumerate(("S", "G", "D", "P")):
        field = terminate[index * 8 : (index + 1) * 8]
        if field[:1] != section.encode("ascii"):
            raise _invalid("Terminate record has an invalid section count")
        count = _field_integer(field[1:], f"Terminate {section} count")
        if field[1:] != f"{count:7d}".encode("ascii"):
            raise _invalid("Terminate record has an invalid section count")
        if count != len(sections[section]):
            raise _invalid(f"Terminate count for {section} does not match")
    if any(byte != 32 for byte in terminate[32:72]):
        raise _invalid("Terminate record has non-blank reserved fields")

    global_data = b"".join(record.raw[:72] for record in sections["G"])
    parameter_delimiter, record_delimiter = _parameter_delimiters(global_data)

    parameter_records = {record.sequence: record for record in sections["P"]}
    entities = []
    next_parameter_pointer = 1
    directory_records = sections["D"]

    for offset in range(0, len(directory_records), 2):
        first = directory_records[offset].raw
        second = directory_records[offset + 1].raw
        sequence = directory_records[offset].sequence

        entity_type = _field_integer(first[:8], "Directory entity type")
        second_type = _field_integer(second[:8], "second Directory entity type")
        if entity_type <= 0 or second_type != entity_type:
            raise _invalid("Directory Entry pair has mismatched entity types")
        if sequence != 2 * len(entities) + 1:
            raise _invalid("Directory Entry pair has an invalid sequence binding")
        if directory_records[offset + 1].sequence != sequence + 1:
            raise _invalid("Directory Entry pair is not contiguous")

        parameter_pointer = _field_integer(first[8:16], "parameter pointer")
        line_count = _field_integer(second[24:32], "parameter line count")
        form = _field_integer(second[32:40], "entity form", blank_is_zero=True)
        transform_pointer = _field_integer(
            first[48:56], "transformation pointer", blank_is_zero=True
        )
        if parameter_pointer != next_parameter_pointer or line_count <= 0:
            raise _invalid("Directory Entry does not bind a contiguous P range")
        if parameter_pointer + line_count - 1 > len(parameter_records):
            raise _invalid("Directory Entry points beyond the P section")

        p_data = bytearray()
        for p_sequence in range(parameter_pointer, parameter_pointer + line_count):
            record = parameter_records.get(p_sequence)
            if record is None:
                raise _invalid("Directory Entry points to a missing P record")
            owner = _field_integer(record.raw[64:72], "P Directory pointer")
            if owner != sequence:
                raise _invalid("P record points to a different Directory Entry")
            p_data.extend(record.raw[:64])

        parameters = _parameter_tokens(
            bytes(p_data), parameter_delimiter, record_delimiter
        )
        if not parameters or _parse_integer(parameters[0], "P entity type") != entity_type:
            raise _invalid("P entity type does not match its Directory Entry")

        entities.append(
            _Entity(
                sequence=sequence,
                entity_type=entity_type,
                parameter_pointer=parameter_pointer,
                parameter_line_count=line_count,
                transform_pointer=transform_pointer,
                form=form,
                parameters=parameters,
            )
        )
        next_parameter_pointer += line_count

    if next_parameter_pointer != len(parameter_records) + 1:
        raise _invalid("P section contains unbound parameter records")

    by_sequence = {entity.sequence: entity for entity in entities}
    for entity in entities:
        if entity.transform_pointer:
            matrix = by_sequence.get(entity.transform_pointer)
            if matrix is None or matrix.entity_type != 124:
                raise _invalid("transformation pointer does not reference type 124")

    frozen_sections = {
        section: tuple(records) for section, records in sections.items()
    }
    return _ParsedFile(
        sections=frozen_sections,
        entities=tuple(entities),
        entities_by_sequence=by_sequence,
        parameter_delimiter=parameter_delimiter,
        record_delimiter=record_delimiter,
    )


def canonical_digest(raw: bytes) -> str:
    """Return SHA-256 over ordered D/P records, excluding S/G metadata."""
    parsed = _parse_iges(raw)
    payload = b"".join(
        record.raw
        for section in ("D", "P")
        for record in parsed.sections[section]
    )
    return hashlib.sha256(payload).hexdigest()


def _finite_number(value: str, description: str) -> float:
    if isinstance(value, _Hollerith):
        raise _Unsupported(f"{description} cannot be a Hollerith string")
    value = value.strip()
    if not _NUMBER_RE.fullmatch(value):
        raise _Unsupported(f"{description} is not a supported real number")
    number = float(value.replace("D", "E").replace("d", "e"))
    if not math.isfinite(number):
        raise _Unsupported(f"{description} is not finite")
    return number


def _nonnegative_integer(value: str, description: str) -> int:
    try:
        result = _parse_integer(value, description)
    except ValueError as exc:
        raise _Unsupported(str(exc)) from exc
    if result < 0:
        raise _Unsupported(f"{description} must be non-negative")
    return result


def _require_zero_reference_tail(
    parameters: tuple[str, ...], base_length: int, entity_name: str
) -> None:
    tail = parameters[base_length:]
    if not tail:
        return
    if len(tail) == 2 and tail == ("0", "0") and all(type(x) is str for x in tail):
        return
    raise _Unsupported(f"{entity_name} has associated references")


def _determinant(rows: tuple[tuple[float, float, float], ...]) -> float:
    a, b, c = rows
    return (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )


def _resolve_matrix(
    entity: _Entity, parsed: _ParsedFile
) -> tuple[tuple[tuple[float, float, float], ...], tuple[float, float, float]]:
    matrix_entity = parsed.entities_by_sequence.get(entity.transform_pointer)
    if matrix_entity is None or matrix_entity.entity_type != 124:
        raise _Unsupported("transformation matrix is missing")
    if matrix_entity.form not in (0, 1):
        raise _Unsupported("unsupported type 124 form")
    if matrix_entity.transform_pointer:
        raise _Unsupported("a transformed type 124 is unsupported")

    parameters = matrix_entity.parameters
    if len(parameters) not in (13, 15):
        raise _Unsupported("type 124 must contain twelve matrix values")
    _require_zero_reference_tail(parameters, 13, "type 124")

    values = tuple(
        _finite_number(value, "type 124 matrix value")
        for value in parameters[1:13]
    )
    rows = (
        (values[0], values[1], values[2]),
        (values[4], values[5], values[6]),
        (values[8], values[9], values[10]),
    )
    translation = (values[3], values[7], values[11])
    for index, row in enumerate(rows):
        norm = sum(component * component for component in row)
        if abs(norm - 1.0) > _RIGID_TOLERANCE:
            raise _Unsupported("non-rigid type 124 scale")
        for other in rows[index + 1 :]:
            dot = sum(left * right for left, right in zip(row, other))
            if abs(dot) > _RIGID_TOLERANCE:
                raise _Unsupported("non-rigid type 124 shear")

    determinant = _determinant(rows)
    if abs(determinant + 1.0) <= _RIGID_TOLERANCE:
        raise _Unsupported("reflection type 124 is unsupported")
    if abs(determinant - 1.0) > _RIGID_TOLERANCE:
        raise _Unsupported("non-rigid type 124 determinant")
    return rows, translation


def _identity_transform(
    entity: _Entity, parsed: _ParsedFile, description: str
) -> None:
    if not entity.transform_pointer:
        return
    rows, translation = _resolve_matrix(entity, parsed)
    identity = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    if any(
        abs(rows[row][column] - identity[row][column]) > _IDENTITY_TOLERANCE
        for row in range(3)
        for column in range(3)
    ) or any(abs(value) > _IDENTITY_TOLERANCE for value in translation):
        raise _Unsupported(f"{description} has a non-identity transformation")


def _transform_point(
    point: tuple[float, float, float],
    rows: tuple[tuple[float, float, float], ...],
    translation: tuple[float, float, float],
) -> tuple[float, float, float]:
    result = tuple(
        sum(rows[row][column] * point[column] for column in range(3))
        + translation[row]
        for row in range(3)
    )
    if not all(math.isfinite(value) for value in result):
        raise _Unsupported("transformed coordinates are not finite")
    return result


def _validate_identity_instance(curve: _Entity, parsed: _ParsedFile) -> None:
    definitions = [entity for entity in parsed.entities if entity.entity_type == 308]
    instances = [entity for entity in parsed.entities if entity.entity_type == 408]
    if not definitions and not instances:
        return
    if len(definitions) != 1 or len(instances) != 1:
        raise _Unsupported("only one 308/408 instance wrapper is supported")

    definition, instance = definitions[0], instances[0]
    if definition.form != 0 or instance.form != 0:
        raise _Unsupported("unsupported 308/408 form")
    definition_parameters = definition.parameters
    if len(definition_parameters) < 4:
        raise _Unsupported("malformed type 308 definition")
    depth = _nonnegative_integer(definition_parameters[1], "type 308 depth")
    if depth != 0:
        raise _Unsupported("nested subfigure definitions are unsupported")
    member_count = _nonnegative_integer(
        definition_parameters[3], "type 308 member count"
    )
    member_start = 4
    member_end = member_start + member_count
    if member_count != 1 or member_end > len(definition_parameters):
        raise _Unsupported("type 308 must contain exactly one member")
    member = _nonnegative_integer(
        definition_parameters[member_start], "type 308 member pointer"
    )
    _require_zero_reference_tail(
        definition_parameters, member_end, "type 308"
    )
    if member != curve.sequence:
        raise _Unsupported("type 308 does not wrap the analytic curve")

    instance_parameters = instance.parameters
    if len(instance_parameters) < 6:
        raise _Unsupported("malformed type 408 instance")
    definition_pointer = _nonnegative_integer(
        instance_parameters[1], "type 408 definition pointer"
    )
    if definition_pointer != definition.sequence:
        raise _Unsupported("type 408 references a different definition")
    placement = tuple(
        _finite_number(value, "type 408 placement")
        for value in instance_parameters[2:5]
    )
    scale = _finite_number(instance_parameters[5], "type 408 scale")
    if any(abs(value) > _IDENTITY_TOLERANCE for value in placement):
        raise _Unsupported("type 408 has a non-identity translation")
    if abs(scale - 1.0) > _IDENTITY_TOLERANCE:
        raise _Unsupported("type 408 has a non-identity scale")
    _require_zero_reference_tail(instance_parameters, 6, "type 408")
    _identity_transform(definition, parsed, "type 308")
    _identity_transform(instance, parsed, "type 408")


def _unsupported(reason: str) -> dict[str, object]:
    return {"status": "unsupported", "reason": reason}


def _type112_line(curve: _Entity) -> dict[str, object]:
    """Read the narrow one-segment cubic-polynomial line subset of type 112."""
    parameters = curve.parameters
    if len(parameters) < 5:
        raise _Unsupported("type 112 is missing its spline header")

    curve_type = _nonnegative_integer(parameters[1], "type 112 spline type")
    continuity = _nonnegative_integer(parameters[2], "type 112 continuity")
    dimension = _nonnegative_integer(parameters[3], "type 112 dimension")
    segment_count = _nonnegative_integer(parameters[4], "type 112 segment count")
    if curve_type != 3:
        raise _Unsupported("type 112 supports only CTYPE 3 cubic splines")
    if continuity not in (0, 1, 2):
        raise _Unsupported("type 112 continuity must be 0, 1, or 2")
    if dimension != 3:
        raise _Unsupported("type 112 supports only three-dimensional curves")
    if segment_count != 1:
        raise _Unsupported("type 112 supports exactly one polynomial segment")

    # With N=1, parameters 0..30 are the spline definition and terminal
    # evaluations; the optional two reference pointers follow them.
    if len(parameters) not in (31, 33):
        raise _Unsupported("type 112 has an invalid one-segment parameter count")
    _require_zero_reference_tail(parameters, 31, "type 112")

    start_parameter = _finite_number(parameters[5], "type 112 first breakpoint")
    end_parameter = _finite_number(parameters[6], "type 112 last breakpoint")
    interval = end_parameter - start_parameter
    if not math.isfinite(interval) or interval <= 0.0:
        raise _Unsupported("type 112 breakpoints must define a positive finite interval")

    coefficients = tuple(
        _finite_number(value, "type 112 polynomial coefficient")
        for value in parameters[7:19]
    )
    # NISTIR 88-3813 §3.8 defines the three coordinate polynomials as
    # A + B*s + C*s^2 + D*s^3. The following twelve fields are redundant
    # endpoint values/derivatives, not polynomial coefficients; validate
    # their representation but use the defining coefficients for geometry.
    terminal_values = tuple(
        _finite_number(value, "type 112 terminal evaluation")
        for value in parameters[19:31]
    )

    starts = (coefficients[0], coefficients[4], coefficients[8])
    slopes = (coefficients[1], coefficients[5], coefficients[9])
    for coordinate, offset in zip("XYZ", (0, 4, 8)):
        if coefficients[offset + 2] != 0.0 or coefficients[offset + 3] != 0.0:
            raise _Unsupported(
                f"type 112 {coordinate} polynomial is not strictly linear"
            )
    if all(slope == 0.0 for slope in slopes):
        raise _Unsupported("type 112 polynomial is degenerate")

    expected_terminal_values = []
    for offset in (0, 4, 8):
        a, b, c, d = coefficients[offset : offset + 4]
        expected_terminal_values.extend(
            (
                ((d * interval + c) * interval + b) * interval + a,
                (3.0 * d * interval + 2.0 * c) * interval + b,
                3.0 * d * interval + c,
                d,
            )
        )
    terminal_status = (
        "consistent"
        if all(
            math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12)
            for actual, expected in zip(terminal_values, expected_terminal_values)
        )
        else "inconsistent"
    )

    end = tuple(
        start + slope * interval for start, slope in zip(starts, slopes)
    )
    if not all(math.isfinite(value) for value in (*starts, *end)):
        raise _Unsupported("type 112 endpoints are not finite")
    length = math.dist(starts, end)
    if not math.isfinite(length) or length <= 0.0:
        raise _Unsupported("type 112 polynomial is degenerate")
    return {
        "status": "supported",
        "kind": "line",
        "length": length,
        "start": list(starts),
        "end": list(end),
        "polynomial_domain_verified": True,
        "redundant_terminal_evaluations_status": terminal_status,
        "annotation": (
            "Line geometry is derived from the type 112 polynomial coefficients "
            "over the full breakpoint interval; terminal evaluations are redundant metadata."
        ),
    }


def analytic_curve(raw: bytes) -> dict[str, object]:
    """Read one IGES type 110 line, type 100 arc, or narrow type 112 line."""
    parsed = _parse_iges(raw)
    basic_curves = [
        entity
        for entity in parsed.entities
        if entity.entity_type in (100, 110, 112)
    ]
    if len(basic_curves) > 1:
        return _unsupported("multiple basic curves are unsupported")
    if not basic_curves:
        return _unsupported("no type 100, 110, or 112 curve was found")

    allowed_types = {100, 110, 112, 124, 314, 406, 308, 408}
    for entity in parsed.entities:
        if entity.entity_type not in allowed_types:
            return _unsupported(
                f"IGES entity type {entity.entity_type} is unsupported"
            )
    curve = basic_curves[0]
    if curve.form != 0:
        return _unsupported("non-zero analytic curve form is unsupported")

    try:
        _validate_identity_instance(curve, parsed)
        if curve.entity_type == 112:
            if curve.transform_pointer:
                raise _Unsupported("type 112 transformations are unsupported")
            return _type112_line(curve)

        if curve.transform_pointer:
            rows, translation = _resolve_matrix(curve, parsed)
        else:
            rows = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
            translation = (0.0, 0.0, 0.0)

        parameters = curve.parameters
        if curve.entity_type == 110:
            if len(parameters) not in (7, 9):
                raise _Unsupported("type 110 must contain six coordinates")
            _require_zero_reference_tail(parameters, 7, "type 110")
            values = tuple(
                _finite_number(value, "type 110 coordinate")
                for value in parameters[1:7]
            )
            start = _transform_point(values[:3], rows, translation)
            end = _transform_point(values[3:], rows, translation)
            length = math.dist(start, end)
            if not math.isfinite(length) or length <= 0.0:
                raise _Unsupported("type 110 is degenerate")
            return {
                "status": "supported",
                "kind": "line",
                "length": length,
                "start": list(start),
                "end": list(end),
            }

        if len(parameters) not in (8, 10):
            raise _Unsupported("type 100 must contain seven arc parameters")
        _require_zero_reference_tail(parameters, 8, "type 100")
        values = tuple(
            _finite_number(value, "type 100 coordinate")
            for value in parameters[1:8]
        )
        z_value, center_x, center_y, start_x, start_y, end_x, end_y = values
        radius = math.hypot(start_x - center_x, start_y - center_y)
        end_radius = math.hypot(end_x - center_x, end_y - center_y)
        if not math.isfinite(radius) or radius <= 0.0:
            raise _Unsupported("type 100 radius must be positive and finite")
        if not math.isclose(end_radius, radius, rel_tol=_RIGID_TOLERANCE, abs_tol=0.):
            raise _Unsupported("type 100 endpoints do not share one radius")

        start_angle = math.atan2(start_y - center_y, start_x - center_x)
        end_angle = math.atan2(end_y - center_y, end_x - center_x)
        sweep = (end_angle - start_angle) % (2.0 * math.pi)
        if start_x == end_x and start_y == end_y:
            sweep = 2.0 * math.pi
        if sweep <= 0.0:
            raise _Unsupported("distinct arc endpoints have an unresolved sweep angle")

        center = _transform_point((center_x, center_y, z_value), rows, translation)
        start = _transform_point((start_x, start_y, z_value), rows, translation)
        end = _transform_point((end_x, end_y, z_value), rows, translation)
        axis = (rows[0][2], rows[1][2], rows[2][2])
        axis_length = math.sqrt(sum(value * value for value in axis))
        if not math.isfinite(axis_length) or axis_length <= 0.0:
            raise _Unsupported("type 124 produces an invalid arc axis")
        axis = tuple(value / axis_length for value in axis)
        length = radius * sweep
        if not math.isfinite(length):
            raise _Unsupported("type 100 arc length is not finite")
        return {
            "status": "supported",
            "kind": "circular_arc",
            "length": length,
            "start": list(start),
            "end": list(end),
            "radius": radius,
            "center": list(center),
            "axis": list(axis),
            "sweep_angle_radians": sweep,
        }
    except _Unsupported as exc:
        return _unsupported(str(exc))
