import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ansa_mcp_bridge.operations.cad_domain_audit import load_manifest


class CadManifestTests(unittest.TestCase):
    def test_complete_source_inventory_and_hash_are_required_before_any_CAD_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cad=root/"face.brep";cad.write_bytes(b"test CAD bytes")
            entry={"path":cad.name,"sha256":hashlib.sha256(cad.read_bytes()).hexdigest()}
            good={"schema_version":1,"units":"mm","expected_source_face_ids":[10,20],"groups":[{
                "label":"merged support","source_face_ids":[10,20],"candidate_face_ids":[31],
                "reference_files":[entry],"candidate_files":[entry]}]}
            path=root/"manifest.json"
            path.write_text(json.dumps(good),encoding="utf-8")
            self.assertEqual(load_manifest(path)["expected_source_face_ids"],[10,20])
            changed=json.loads(json.dumps(good));changed["expected_source_face_ids"].append(30)
            path.write_text(json.dumps(changed),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"exactly cover"):load_manifest(path)
            path.write_text(json.dumps(good),encoding="utf-8");cad.write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError,"stale CAD"):load_manifest(path)

    def test_duplicate_ownership_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cad=root/"a.brep";cad.write_bytes(b"a")
            entry={"path":cad.name,"sha256":hashlib.sha256(b"a").hexdigest()}
            group={"label":"a","source_face_ids":[1],"candidate_face_ids":[2],"reference_files":[entry],"candidate_files":[entry]}
            path=root/"m.json";path.write_text(json.dumps({"schema_version":1,"units":"mm",
                "expected_source_face_ids":[1],"groups":[group,group]}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"multiple groups"):load_manifest(path)
