import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ansa_mcp_bridge.operations.geometry_features_iges import (  # noqa: E402
    analytic_curve,
    canonical_digest,
)


def _section_records(data, width, section, sequence_start=1):
    chunks = [data[index : index + width] for index in range(0, len(data), width)]
    records = []
    for index, chunk in enumerate(chunks, sequence_start):
        records.append(
            chunk.ljust(width)
            + (b" " * (72 - width))
            + section
            + f"{index:7d}".encode("ascii")
        )
    return records


def _directory_record(fields, sequence):
    data = "".join(f"{str(field):>8}" for field in fields).encode("ascii")
    if len(data) != 72:
        raise AssertionError("Directory Entry fields must occupy 72 columns")
    return data + b"D" + f"{sequence:7d}".encode("ascii")


def _entity(entity_type, parameters, *, transform=0, form=0, label=""):
    return {
        "entity_type": entity_type,
        "parameters": [str(entity_type), *[str(value) for value in parameters]],
        "transform": transform,
        "form": form,
        "label": label,
    }


def _build_iges(entities, *, start="synthetic", metadata="path-a,date-a", crlf=False):
    start_records = _section_records(start.encode("ascii"), 72, b"S")
    global_text = (
        b"1H,,1H;,"
        + f"{len(metadata)}H{metadata}".encode("ascii")
        + b";"
    )
    global_records = _section_records(global_text, 72, b"G")

    directory_records = []
    parameter_records = []
    next_parameter_sequence = 1
    for index, entity in enumerate(entities):
        de_sequence = index * 2 + 1
        parameter_text = (
            ",".join(entity["parameters"]).encode("ascii") + b";"
        )
        parameter_chunks = [
            parameter_text[offset : offset + 64]
            for offset in range(0, len(parameter_text), 64)
        ]
        parameter_count = len(parameter_chunks)

        first_fields = [
            entity["entity_type"],
            next_parameter_sequence,
            0,
            0,
            0,
            0,
            entity["transform"],
            0,
            "00000000",
        ]
        second_fields = [
            entity["entity_type"],
            0,
            0,
            parameter_count,
            entity["form"],
            0,
            0,
            entity["label"],
            0,
        ]
        directory_records.append(_directory_record(first_fields, de_sequence))
        directory_records.append(
            _directory_record(second_fields, de_sequence + 1)
        )
        for offset, chunk in enumerate(parameter_chunks):
            p_sequence = next_parameter_sequence + offset
            parameter_records.append(
                chunk.ljust(64)
                + f"{de_sequence:8d}".encode("ascii")
                + b"P"
                + f"{p_sequence:7d}".encode("ascii")
            )
        next_parameter_sequence += parameter_count

    counts = (
        ("S", len(start_records)),
        ("G", len(global_records)),
        ("D", len(directory_records)),
        ("P", len(parameter_records)),
    )
    terminate_data = b"".join(
        letter.encode("ascii") + f"{count:7d}".encode("ascii")
        for letter, count in counts
    )
    terminate = terminate_data.ljust(72) + b"T" + f"{1:7d}".encode("ascii")
    records = (
        start_records
        + global_records
        + directory_records
        + parameter_records
        + [terminate]
    )
    newline = b"\r\n" if crlf else b"\n"
    return newline.join(records) + newline


def _line_entities(*, transform=0, start=(0, 0, 0), end=(4, 0, 0)):
    line = _entity(
        110,
        [*start, *end, 0, 0],
        transform=transform,
    )
    return [line]


def _type112_parameters():
    # Native type 112 sample: CTYPE/H/NDIM/N, breakpoints, XYZ polynomials,
    # redundant terminal evaluations, and two zero reference pointers.
    return [
        3, 0, 3, 1, 0, 1,
        2, 0, 0, 0, 0, 0, 0, 0, 5, -5, 0, 0,
        2, 0, 0, 0, 0, 0, 0, 0, 0, -5, -2.5, "-.83333333333333", 0, 0,
    ]


def _matrix_entity(values):
    return _entity(124, [*values, 0, 0])


def _arc_entities(*, transform=0, start=(3, 0), end=(-3, 0), z=0):
    arc = _entity(100, [z, 0, 0, *start, *end, 0, 0], transform=transform)
    return [arc]


def _wrapped_curve_entities(curve):
    curve_sequence = 1
    definition_sequence = 3
    name = "Identity wrapper name"
    definition = _entity(
        308,
        [0, f"{len(name)}H{name}", 1, curve_sequence, 0, 0],
    )
    instance = _entity(408, [definition_sequence, 0, 0, 0, 1, 0, 0])
    return [curve, definition, instance]


class TestGeometryFeaturesIges(unittest.TestCase):
    def test_digest_ignores_start_and_global_metadata_and_line_endings(self):
        entities = _line_entities()
        lf = _build_iges(
            entities, start="model-a 2026", metadata="same-length/date-a"
        )
        crlf = _build_iges(
            entities, start="model-b 2027", metadata="same-length/date-b", crlf=True
        )
        self.assertEqual(canonical_digest(lf), canonical_digest(crlf))

    def test_digest_changes_when_local_geometry_changes(self):
        first = canonical_digest(_build_iges(_line_entities(end=(4, 0, 0))))
        second = canonical_digest(_build_iges(_line_entities(end=(5, 0, 0))))
        self.assertNotEqual(first, second)

    def test_digest_includes_directory_transform_reference(self):
        matrix = [1, 0, 0, 20, 0, 1, 0, 0, 0, 0, 1, 0]
        without_reference = _build_iges(
            _line_entities(transform=0) + [_matrix_entity(matrix)]
        )
        with_reference = _build_iges(
            _line_entities(transform=3) + [_matrix_entity(matrix)]
        )
        self.assertNotEqual(
            canonical_digest(without_reference),
            canonical_digest(with_reference),
        )

    def test_digest_rejects_empty_truncated_and_wrong_width_records(self):
        valid = _build_iges(_line_entities())
        with self.assertRaises(ValueError):
            canonical_digest(b"")
        with self.assertRaises(ValueError):
            canonical_digest(valid[:-83])
        records = valid.splitlines()
        records[0] = records[0][:-1]
        with self.assertRaises(ValueError):
            canonical_digest(b"\n".join(records) + b"\n")

    def test_digest_rejects_unbound_parameter_record(self):
        records = _build_iges(_line_entities()).splitlines()
        p_index = next(i for i, record in enumerate(records) if record[72:73] == b"P")
        record = records[p_index]
        records[p_index] = record[:64] + b"       3" + record[72:]
        with self.assertRaises(ValueError):
            canonical_digest(b"\n".join(records) + b"\n")

    def test_digest_rejects_malformed_hollerith_length(self):
        metadata = "text,with;delimiters"
        entities = [_entity(406, [1, f"99H{metadata}", 0, 0])]
        with self.assertRaises(ValueError):
            canonical_digest(_build_iges(entities))

    def test_digest_rejects_input_over_8_mib(self):
        with self.assertRaises(ValueError):
            canonical_digest(b"x" * (8 * 1024 * 1024 + 1))

    def test_line_returns_length_and_endpoints(self):
        result = analytic_curve(_build_iges(_line_entities()))
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["kind"], "line")
        self.assertAlmostEqual(result["length"], 4.0)
        self.assertEqual(result["start"], [0.0, 0.0, 0.0])
        self.assertEqual(result["end"], [4.0, 0.0, 0.0])

    def test_native_type112_polynomial_and_redundant_terminal_data(self):
        result = analytic_curve(
            _build_iges([_entity(112, _type112_parameters())])
        )
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["kind"], "line")
        self.assertEqual(result["start"], [2.0, 0.0, 5.0])
        self.assertEqual(result["end"], [2.0, 0.0, 0.0])
        self.assertEqual(result["length"], 5.0)
        self.assertTrue(result["polynomial_domain_verified"])
        self.assertEqual(
            result["redundant_terminal_evaluations_status"], "inconsistent"
        )
        self.assertIn("polynomial coefficients", result["annotation"])

    def test_type112_consistent_terminal_derivatives_and_tolerance(self):
        cases = ((0.0, "consistent"), (5e-13, "consistent"), (2e-12, "inconsistent"))
        for terminal_second_derivative, expected_status in cases:
            with self.subTest(terminal_second_derivative=terminal_second_derivative):
                parameters = _type112_parameters()
                parameters[28] = terminal_second_derivative
                parameters[29] = 0.0
                result = analytic_curve(
                    _build_iges([_entity(112, parameters)])
                )
                self.assertEqual(result["status"], "supported")
                self.assertTrue(result["polynomial_domain_verified"])
                self.assertEqual(
                    result["redundant_terminal_evaluations_status"],
                    expected_status,
                )

    def test_type112_nonlinear_and_reversing_polynomials_are_unsupported(self):
        nonlinear = _type112_parameters()
        nonlinear[12] = 0.25  # Y has a non-zero quadratic coefficient.
        reversing = _type112_parameters()
        reversing[15:18] = [1, -1, 0]  # Z(s)=5+s-s^2 reverses direction.
        for parameters in (nonlinear, reversing):
            with self.subTest(parameters=parameters[7:18]):
                result = analytic_curve(_build_iges([_entity(112, parameters)]))
                self.assertEqual(result["status"], "unsupported")
                self.assertIn("not strictly linear", result["reason"])

    def test_type112_discontinuous_multisegment_curve_is_unsupported(self):
        first_segment = [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        second_segment = [10, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        terminal = [11, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        parameters = [
            3, 0, 3, 2, 0, 1, 2,
            *first_segment,
            *second_segment,
            *terminal,
            0, 0,
        ]
        result = analytic_curve(_build_iges([_entity(112, parameters)]))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("exactly one polynomial segment", result["reason"])

    def test_type112_nonfinite_polynomial_value_is_unsupported(self):
        parameters = _type112_parameters()
        parameters[15] = "1e309"
        result = analytic_curve(_build_iges([_entity(112, parameters)]))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("not finite", result["reason"])

    def test_type112_hollerith_polynomial_value_is_unsupported(self):
        parameters = _type112_parameters()
        parameters[15] = "2H,;"
        result = analytic_curve(_build_iges([_entity(112, parameters)]))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("Hollerith", result["reason"])

    def test_type112_transform_mixed_curves_and_type126_remain_unsupported(self):
        identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
        transformed = [
            _entity(112, _type112_parameters(), transform=3),
            _matrix_entity(identity),
        ]
        mixed = [_entity(112, _type112_parameters()), *_line_entities()]
        rational = [_entity(126, [0])]
        for entities in (transformed, mixed, rational):
            with self.subTest(entity_types=[item["entity_type"] for item in entities]):
                self.assertEqual(
                    analytic_curve(_build_iges(entities))["status"], "unsupported"
                )

    def test_line_applies_rigid_rotation_and_translation(self):
        matrix = [0, -1, 0, 10, 1, 0, 0, -2, 0, 0, 1, 5]
        entities = _line_entities(
            transform=3, start=(1, 2, 3), end=(1, 6, 3)
        ) + [_matrix_entity(matrix)]
        result = analytic_curve(_build_iges(entities))
        self.assertEqual(result["status"], "supported")
        self.assertAlmostEqual(result["length"], 4.0)
        self.assertEqual(result["start"], [8.0, -1.0, 8.0])
        self.assertEqual(result["end"], [4.0, -1.0, 8.0])

    def test_arc_applies_translation_and_d_exponents(self):
        matrix = [
            "1D0", "0", "0", "20D0",
            "0", "1D0", "0", "0",
            "0", "0", "1D0", "0",
        ]
        entities = _arc_entities(transform=3) + [_matrix_entity(matrix)]
        result = analytic_curve(_build_iges(entities))
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["kind"], "circular_arc")
        self.assertAlmostEqual(result["radius"], 3.0)
        self.assertAlmostEqual(result["length"], 3.0 * math.pi)
        self.assertEqual(result["center"], [20.0, 0.0, 0.0])
        self.assertEqual(result["start"], [23.0, 0.0, 0.0])
        self.assertEqual(result["end"], [17.0, 0.0, 0.0])
        self.assertEqual(result["axis"], [0.0, 0.0, 1.0])

    def test_coincident_arc_endpoints_mean_full_circle(self):
        result = analytic_curve(_build_iges(_arc_entities(start=(2, 0), end=(2, 0))))
        self.assertEqual(result["status"], "supported")
        self.assertAlmostEqual(result["sweep_angle_radians"], 2.0 * math.pi)
        self.assertAlmostEqual(result["length"], 4.0 * math.pi)

    def test_identity_instance_and_metadata_hollerith_are_supported(self):
        curve = _entity(110, [0, 0, 0, 4, 0, 0, 0, 0])
        entities = _wrapped_curve_entities(curve)
        entities.extend(
            [
                _entity(314, [20, 30, 40, 0, 0]),
                _entity(
                    406,
                    [
                        1,
                        f"{len('property,with;delimiters')}Hproperty,with;delimiters",
                        0,
                        0,
                    ],
                ),
            ]
        )
        result = analytic_curve(_build_iges(entities))
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["kind"], "line")
        self.assertAlmostEqual(result["length"], 4.0)

    def test_multiple_basic_curves_are_unsupported(self):
        entities = _line_entities() + _line_entities(start=(0, 1, 0), end=(4, 1, 0))
        result = analytic_curve(_build_iges(entities))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("multiple", result["reason"])

    def test_other_geometry_is_unsupported(self):
        result = analytic_curve(
            _build_iges(_line_entities() + [_entity(128, [0, 0])])
        )
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("128", result["reason"])

    def test_reflection_transform_is_conservatively_unsupported(self):
        reflection = [-1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
        result = analytic_curve(
            _build_iges(_line_entities(transform=3) + [_matrix_entity(reflection)])
        )
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("reflection", result["reason"])

    def test_non_rigid_scale_transform_is_unsupported(self):
        scale = [2, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
        result = analytic_curve(
            _build_iges(_line_entities(transform=3) + [_matrix_entity(scale)])
        )
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("non-rigid", result["reason"])

    def test_non_rigid_shear_transform_is_unsupported(self):
        shear = [1, 0, 0, 0, 0.6, 0.8, 0, 0, 0, 0, 1, 0]
        result = analytic_curve(
            _build_iges(_line_entities(transform=3) + [_matrix_entity(shear)])
        )
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("shear", result["reason"])

    def test_non_identity_instance_is_unsupported(self):
        curve = _entity(110, [0, 0, 0, 4, 0, 0, 0, 0])
        entities = _wrapped_curve_entities(curve)
        entities[2] = _entity(408, [3, 1, 0, 0, 1, 0, 0])
        result = analytic_curve(_build_iges(entities))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("translation", result["reason"])

    def test_zero_radius_arc_is_unsupported(self):
        result = analytic_curve(
            _build_iges(_arc_entities(start=(0, 0), end=(0, 0)))
        )
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("radius", result["reason"])

    def test_wrong_input_type_is_rejected(self):
        with self.assertRaises(TypeError):
            canonical_digest("not bytes")

    def test_near_coincident_endpoints_keep_short_arc_sweep(self):
        result = analytic_curve(_build_iges(_arc_entities(start=(1e6, 0), end=(1e6, .005))))
        self.assertEqual(result["status"], "supported")
        self.assertAlmostEqual(result["length"], .005, places=12)
        self.assertLess(result["sweep_angle_radians"], 1e-8)

    def test_small_scale_inconsistent_radii_are_rejected(self):
        result = analytic_curve(_build_iges(_arc_entities(start=(1e-9, 0), end=(0, 2e-9))))
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("one radius", result["reason"])

    def test_numeric_looking_hollerith_is_not_a_numeric_field(self):
        coordinates = [_entity(110, [0, 0, 0, "1H3", 0, 0])]
        self.assertEqual(analytic_curve(_build_iges(coordinates))["status"], "unsupported")
        malformed_type = _entity(110, [0, 0, 0, 3, 0, 0])
        malformed_type["parameters"][0] = "3H110"
        with self.assertRaisesRegex(ValueError, "Hollerith"):
            canonical_digest(_build_iges([malformed_type]))
        tail = [_entity(110, [0, 0, 0, 3, 0, 0, "1H0", "1H0"])]
        self.assertEqual(analytic_curve(_build_iges(tail))["status"], "unsupported")

    def test_large_representable_line_length_does_not_square_overflow(self):
        result = analytic_curve(_build_iges(_line_entities(end=(1e155, 0, 0))))
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["length"], 1e155)


if __name__ == "__main__":
    unittest.main()
