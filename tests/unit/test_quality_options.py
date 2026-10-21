from __future__ import annotations

import sys
from pathlib import Path

import unittest


_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from ansa_mcp_bridge.operations.quality_options import (  # noqa: E402
    QualityOptionsTransactionError,
    run_with_options,
)


_ORIGINAL = """# Fix Quality
    fix_elements_general_flag = true
    split_remain_viol_quads_flag = true
    zones_around_violating = 3
    maximum_distance_from_surface = 0.1*L
    maximum_distance_from_single_perimeters = 0.2*L
    maximum_distortion_at_double_perimeters = 0.2*L
    maximum_distance_from_double_perimeters = 0.5*L
    maximum_distance_from_triple_perimeters = 0.
    maximum_distance_from_perimeter_for_2nd_order_nodes = 0.5*L
    maximum_distance_from_surface_for_2nd_order_nodes = 0.1*L
    freeze_single_perimeters = false
    freeze_triple_perimeters = false
    freeze_double_perimeters = false
    freeze_line_elements = false
    freeze_named_grids = false
    freeze_washers = true
    frozen_entities_set_id = 0
    fix_solid_elements_general_flag = false
    freeze_non_visible_shells = false
    unconstrained_2nd_order_nodes = false
    fix_solid_freeze_line_elements = true
    fix_solid_constrained_layer_nodes = true
    number_of_affected_solid_zones = 3
    fix_volume_external_bounds = constrained
    maximum_distance_from_external_bounds = 0.1*local
    fix_volume_pid_bounds = unconstrained
    maximum_distance_from_pid_bounds = 0.1*local
    unrelated_option = KeepCase
"""


class FakeMeshAPI:
    def __init__(self, text: str = _ORIGINAL) -> None:
        self.text = text
        self.read_count = 0
        self.fail_read_count: int | None = None

    def SaveMeshParams(self, filename: str) -> int:
        Path(filename).write_text(self.text, encoding="utf-8")
        return 1

    def ReadMeshParams(self, filename: str) -> int:
        self.read_count += 1
        if self.read_count == self.fail_read_count:
            return 0
        self.text = Path(filename).read_text(encoding="utf-8")
        return 1


def _values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip()
    return values


class QualityOptionsTests(unittest.TestCase):
    def test_applies_and_reads_back_shell_options_then_restores(self) -> None:
        mesh = FakeMeshAPI()
        result: dict[str, object] = {}
        callback_values: dict[str, str] = {}

        def callback() -> str:
            callback_values.update(_values(mesh.text))
            return "fixed"

        returned = run_with_options(mesh, "shell", 0.25, callback, result)

        assert returned == "fixed"
        assert callback_values["split_remain_viol_quads_flag"] == "false"
        assert callback_values["fix_elements_general_flag"] == "true"
        assert callback_values["zones_around_violating"] == "3"
        assert callback_values["maximum_distance_from_surface"] == "0.1*L"
        assert callback_values["maximum_distance_from_triple_perimeters"] == "0."
        assert callback_values["maximum_distortion_at_double_perimeters"] == "0.2*L"
        assert callback_values["freeze_named_grids"] == "false"
        assert callback_values["frozen_entities_set_id"] == "0"
        assert result["options_synchronized"] is True
        assert result["options_restored"] is True
        assert mesh.text == _ORIGINAL


    def test_callback_failure_is_preserved_and_options_are_restored(self) -> None:
        mesh = FakeMeshAPI()
        result: dict[str, object] = {}
        callback_error = RuntimeError("repair failed")
        callback_values: dict[str, str] = {}

        def callback() -> None:
            callback_values.update(_values(mesh.text))
            raise callback_error

        with self.assertRaises(RuntimeError) as raised:
            run_with_options(mesh, "solid", 0.5, callback, result)

        assert raised.exception is callback_error
        assert callback_values["split_remain_viol_quads_flag"] == "false"
        assert callback_values["fix_solid_elements_general_flag"] == "true"
        assert callback_values["freeze_non_visible_shells"] == "false"
        assert callback_values["unconstrained_2nd_order_nodes"] == "false"
        assert callback_values["fix_solid_freeze_line_elements"] == "true"
        assert callback_values["fix_solid_constrained_layer_nodes"] == "true"
        assert callback_values["number_of_affected_solid_zones"] == "3"
        assert callback_values["fix_volume_external_bounds"] == "constrained"
        assert callback_values["maximum_distance_from_external_bounds"] == "0.1*local"
        assert callback_values["fix_volume_pid_bounds"] == "unconstrained"
        assert callback_values["maximum_distance_from_pid_bounds"] == "0.1*local"
        assert result["operation_error"] == {
            "type": "RuntimeError",
            "message": "repair failed",
        }
        assert result["restore_error"] is None
        assert result["options_synchronized"] is True
        assert result["options_restored"] is True
        assert mesh.text == _ORIGINAL


    def test_restore_failure_retains_original_parameter_file(self) -> None:
        mesh = FakeMeshAPI()
        mesh.fail_read_count = 2
        result: dict[str, object] = {}
        operation_error = RuntimeError("repair failed")

        def callback() -> None:
            raise operation_error

        with self.assertRaises(QualityOptionsTransactionError) as raised:
            run_with_options(mesh, "solid", 0.5, callback, result)

        error = raised.exception
        recovery_file = result["recovery_file"]
        assert error.operation_error is operation_error
        assert error.restore_error is not None
        assert error.recovery_file == recovery_file
        assert recovery_file is not None
        assert Path(recovery_file).read_text(encoding="utf-8") == _ORIGINAL
        assert result["options_synchronized"] is True
        assert result["options_restored"] is False
        assert result["operation_error"] == {
            "type": "RuntimeError",
            "message": "repair failed",
        }
        assert result["restore_error"] == {
            "type": "QualityOptionsError",
            "message": f"ansa.mesh.ReadMeshParams failed for {recovery_file!r}: 0",
        }
        Path(recovery_file).unlink()

if __name__ == "__main__":
    unittest.main()
