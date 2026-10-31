"""Risk-focused checks for the configurable whole-model evidence budget."""

import os
import unittest
from unittest.mock import patch

from ansa_mcp_bridge.operations import structured_native, volume_meshing
from test_structured_native import FakeBase


class ScopeEvidenceBudgetTests(unittest.TestCase):
    def _base_with_payload(self):
        base = FakeBase()
        base.add("GRID", 1, NID=1, Payload="中" * 90000)
        return base

    def test_default_scope_budget_accepts_more_than_old_256_kib_without_truncation(self):
        base = self._base_with_payload()

        with patch.dict(os.environ, {}, clear=True):
            snapshot = structured_native._capture_scope(base, 1)

        encoded = volume_meshing._encoded(snapshot)
        self.assertGreater(len(encoded), 256 * 1024)
        self.assertEqual(snapshot["entities"][0]["fields"]["Payload"], "中" * 90000)
        self.assertTrue(snapshot["complete"])

    def test_environment_threshold_uses_utf8_bytes_and_reports_effective_limit(self):
        base = self._base_with_payload()
        snapshot = structured_native._capture_scope(
            base, 1, budget=16 * 1024 * 1024
        )
        encoded_size = len(volume_meshing._encoded(snapshot))
        self.assertGreater(encoded_size, 256 * 1024)

        with patch.dict(
            os.environ,
            {structured_native._SCOPE_EVIDENCE_MAX_BYTES_ENV: str(encoded_size)},
            clear=True,
        ):
            self.assertEqual(structured_native._capture_scope(base, 1), snapshot)

        with patch.dict(
            os.environ,
            {structured_native._SCOPE_EVIDENCE_MAX_BYTES_ENV: str(encoded_size - 1)},
            clear=True,
        ), self.assertRaises(ValueError) as raised:
            structured_native._capture_scope(base, 1)
        self.assertIn(f"{encoded_size - 1} bytes", str(raised.exception))

    def test_invalid_environment_is_rejected_before_model_collection(self):
        values = (
            str(structured_native._SCOPE_EVIDENCE_MIN_BUDGET - 1),
            str(structured_native._SCOPE_EVIDENCE_MAX_BUDGET + 1),
            "262144.0",
            " 262144",
            "not-an-integer",
            "",
        )
        for value in values:
            with self.subTest(value=value):
                base = self._base_with_payload()
                with patch.object(
                    base, "CollectEntities", wraps=base.CollectEntities
                ) as collect, patch.dict(
                    os.environ,
                    {structured_native._SCOPE_EVIDENCE_MAX_BYTES_ENV: value},
                    clear=True,
                ), self.assertRaisesRegex(
                    ValueError, structured_native._SCOPE_EVIDENCE_MAX_BYTES_ENV
                ):
                    structured_native._capture_scope(base, 1)
                collect.assert_not_called()

    def test_explicit_small_budget_still_rejects_without_reading_environment(self):
        base = self._base_with_payload()
        limit = 256 * 1024

        with patch.dict(
            os.environ,
            {structured_native._SCOPE_EVIDENCE_MAX_BYTES_ENV: "invalid"},
            clear=True,
        ), self.assertRaises(ValueError) as raised:
            structured_native._capture_scope(base, 1, budget=limit)

        self.assertIn(f"{limit} bytes (256 KiB)", str(raised.exception))

    def test_model_entity_limit_still_rejects_scope(self):
        class OverflowBase:
            @staticmethod
            def CollectEntities(*_args, **_kwargs):
                return [object()] * (structured_native._MAX_MODEL_ENTITIES + 1)

        with self.assertRaisesRegex(
            ValueError,
            f"{structured_native._MAX_MODEL_ENTITIES}-entity limit",
        ):
            structured_native._capture_scope(OverflowBase(), 1)


if __name__ == "__main__":
    unittest.main()
