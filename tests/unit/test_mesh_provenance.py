import hashlib
from pathlib import Path
import tempfile
import unittest

from ansa_mcp_bridge.operations.mesh_provenance import (
    create_ledger,
    read_ledger,
    record_stage,
    validate_resume,
)


def _fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _binding():
    return {
        "target": {"type": "target", "id": "target-a", "fingerprint": _fingerprint("target"), "epoch": 2},
        "model": {"type": "model", "id": "model-a", "fingerprint": _fingerprint("model"), "epoch": 7},
        "source": {"type": "SHELL_SET", "id": "source-a", "fingerprint": _fingerprint("source"), "epoch": 7},
    }


def _payload(stage, **extra):
    return {**_binding(), "stage": stage, **extra}


def _fe_ref(entity_id, content):
    return {"type": "CQUAD4", "id": entity_id, "fingerprint": _fingerprint(content), "epoch": 7}


def _fe_mapping(new_content="new-a"):
    old = _fe_ref(1, "original-a")
    new = _fe_ref(101, new_content)
    return {
        "original_fe": [old],
        "new_fe": [new],
        "pairs": [{"original": old, "created": [new]}],
    }


class MeshProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.session_dir = Path(self.temp.name).resolve()
        (self.session_dir / "keep.txt").write_text("untouched", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ledger_id="mesh-op-1", payload=None):
        return create_ledger(self.session_dir, ledger_id, payload or _binding())

    def test_creates_versioned_ledger_in_fixed_private_child_and_reads_hash(self):
        created = self._create()
        folder = self.session_dir / "structured-mesh"
        self.assertTrue(folder.is_dir())
        entries = list(folder.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].name.startswith("ledger-"))
        self.assertTrue(entries[0].name.endswith(".json"))
        self.assertEqual((self.session_dir / "keep.txt").read_text(encoding="utf-8"), "untouched")
        read = read_ledger(self.session_dir, "mesh-op-1")
        self.assertEqual(created, read)
        self.assertEqual(read["ledger"]["schema_version"], 1)
        self.assertEqual(read["ledger"]["stage"], "planned")

    def test_rejects_recreation_and_path_like_ledger_ids(self):
        self._create()
        with self.assertRaises(FileExistsError):
            self._create()
        with self.assertRaisesRegex(ValueError, "ledger_id"):
            self._create("../escape")

    def test_expected_hash_prevents_stale_update(self):
        created = self._create()
        with self.assertRaisesRegex(RuntimeError, "changed since it was read"):
            record_stage(self.session_dir, "mesh-op-1", _payload("source_checked"), "0" * 64)
        current = read_ledger(self.session_dir, "mesh-op-1")
        self.assertEqual(current["ledger_hash"], created["ledger_hash"])
        self.assertEqual(current["ledger"]["stage"], "planned")

    def test_resume_requires_exact_target_model_and_source_fingerprints(self):
        created = self._create()
        checked = record_stage(self.session_dir, "mesh-op-1", _payload("source_checked"),
                               created["ledger_hash"])
        valid = validate_resume(self.session_dir, "mesh-op-1", _binding(), checked["ledger_hash"])
        self.assertTrue(valid["valid"])
        self.assertTrue(valid["can_apply"])
        changed = _binding()
        changed["source"]["fingerprint"] = _fingerprint("changed source")
        with self.assertRaisesRegex(RuntimeError, "fingerprint/epoch mismatch"):
            validate_resume(self.session_dir, "mesh-op-1", changed)

    def test_bounded_stage_lifecycle_and_recovery_never_reenters_applying(self):
        created = self._create()
        checked = record_stage(self.session_dir, "mesh-op-1", _payload("source_checked"),
                               created["ledger_hash"])
        applying = record_stage(self.session_dir, "mesh-op-1",
                                _payload("applying", elapsed_seconds=1.25), checked["ledger_hash"])
        self.assertEqual(applying["ledger"]["phase_timings"], [
            {"stage": "applying", "elapsed_seconds": 1.25}
        ])
        with self.assertRaisesRegex(ValueError, "explicit native_state"):
            record_stage(self.session_dir, "mesh-op-1",
                         _payload("verified"), applying["ledger_hash"])
        with self.assertRaisesRegex(RuntimeError, "cannot re-enter applying"):
            validate_resume(self.session_dir, "mesh-op-1", _binding())
        recovery = validate_resume(self.session_dir, "mesh-op-1",
                                   {**_binding(), "purpose": "recovery"}, applying["ledger_hash"])
        self.assertFalse(recovery["can_apply"])
        interrupted = record_stage(
            self.session_dir, "mesh-op-1",
            _payload("recovery_required", native_state={
                "ownership": "unknown", "owner": None, "regeneration": "unknown",
            }),
            applying["ledger_hash"],
        )
        self.assertEqual(interrupted["ledger"]["stage"], "recovery_required")
        recovery = validate_resume(self.session_dir, "mesh-op-1",
                                   {**_binding(), "purpose": "recovery"}, interrupted["ledger_hash"])
        self.assertTrue(recovery["valid"])
        self.assertFalse(recovery["can_apply"])
        verified = record_stage(
            self.session_dir, "mesh-op-1",
            _payload("verified", native_state={
                "ownership": "unknown", "owner": None, "regeneration": "unknown",
            }),
            interrupted["ledger_hash"],
        )
        checkpointed = record_stage(self.session_dir, "mesh-op-1",
                                    _payload("checkpointed"), verified["ledger_hash"])
        self.assertEqual(checkpointed["ledger"]["stage"], "checkpointed")
        with self.assertRaisesRegex(RuntimeError, "cannot re-enter applying"):
            validate_resume(self.session_dir, "mesh-op-1", _binding())
        self.assertEqual(checkpointed["ledger"]["native_state"]["regeneration"], "unknown")

    def test_unknown_native_state_blocks_apply(self):
        created = self._create()
        checked = record_stage(
            self.session_dir, "mesh-op-1",
            _payload("source_checked", native_state={
                "ownership": "unknown", "owner": None, "regeneration": "unknown",
            }),
            created["ledger_hash"],
        )
        with self.assertRaisesRegex(RuntimeError, "unknown native ownership"):
            validate_resume(self.session_dir, "mesh-op-1", _binding(), checked["ledger_hash"])
        with self.assertRaisesRegex(RuntimeError, "unknown native ownership"):
            record_stage(self.session_dir, "mesh-op-1", _payload("applying"), checked["ledger_hash"])

    def test_fe_mapping_retains_original_to_new_refs_and_detects_id_reuse(self):
        initial_map = _fe_mapping("new-before")
        created = self._create(payload={**_binding(), "fe_mapping": initial_map})
        prior = initial_map["new_fe"][0]
        replacement = _fe_ref(prior["id"], "new-after")
        reused_map = {
            "original_fe": initial_map["original_fe"],
            "new_fe": [replacement],
            "pairs": [{"original": initial_map["original_fe"][0], "created": [replacement]}],
        }
        checked = record_stage(self.session_dir, "mesh-op-1",
                               _payload("source_checked", fe_mapping=reused_map),
                               created["ledger_hash"])
        ledger = checked["ledger"]
        self.assertEqual(ledger["fe_mapping"]["pairs"][0]["created"][0], replacement)
        self.assertEqual(len(ledger["id_reuse_events"]), 1)
        self.assertEqual(ledger["id_reuse_events"][0]["id"], prior["id"])
        with self.assertRaisesRegex(RuntimeError, "ID reuse blocks applying"):
            validate_resume(self.session_dir, "mesh-op-1", _binding(), checked["ledger_hash"])

    def test_rejects_untyped_or_non_sha_references(self):
        bad = _binding()
        bad["source"]["fingerprint"] = "not-a-sha256"
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self._create(payload=bad)


if __name__ == "__main__":
    unittest.main()
