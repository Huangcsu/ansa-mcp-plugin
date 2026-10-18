"""官方 Packager GBK 兼容 staging 的编码与目录边界检查。"""

from __future__ import annotations

import hashlib
import ast
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "stage_native_packager_compat.py"
SPEC = importlib.util.spec_from_file_location("stage_native_packager_compat", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
packager_compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager_compat)


class NativePackagerCompatTests(unittest.TestCase):
    def _source_tree(self, root: Path) -> Path:
        source = root / "native-0.1.0"
        package = source / "AnsaMcpPlugin_root"
        package.mkdir(parents=True)
        (source / "AnsaMcpPlugin.ppl").write_text(
            "# -*- coding: utf-8 -*-\nTITLE = '中文 •'\n", encoding="utf-8"
        )
        (package / "panel.py").write_text(
            "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\nTEXT = '中文 •'\n",
            encoding="utf-8",
        )
        (package / "plain.py").write_text("VALUE = '中文'\n", encoding="utf-8")
        (source / "icon.svg").write_bytes(b"<svg>\xe2\x80\xa2</svg>")
        (source / "SHA256SUMS.json").write_text(
            '{"files": ["utf-8", "•"]}\n', encoding="utf-8"
        )
        return source

    def test_transcodes_only_python_and_ppl_and_records_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source_tree(root)
            source_before = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }

            output = packager_compat.stage_packager_compat(source, root / "compat")

            ppl = (output / "AnsaMcpPlugin.ppl").read_bytes()
            panel = (output / "AnsaMcpPlugin_root" / "panel.py").read_bytes()
            plain = (output / "AnsaMcpPlugin_root" / "plain.py").read_bytes()
            self.assertIn("中文", ppl.decode("gbk"))
            self.assertIn("中文", panel.decode("gbk"))
            self.assertIn("中文", plain.decode("gbk"))
            self.assertNotIn("\u2022", ppl.decode("gbk"))
            self.assertNotIn("\u2022", panel.decode("gbk"))

            self.assertEqual((output / "icon.svg").read_bytes(), b"<svg>\xe2\x80\xa2</svg>")
            source_manifest = (source / "SHA256SUMS.json").read_bytes()
            self.assertEqual((output / "SHA256SUMS.json").read_bytes(), source_manifest)

            manifest = json.loads(
                (output / packager_compat.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["source_encoding"], "utf-8")
            self.assertEqual(manifest["target_encoding"], "gbk")
            self.assertEqual(manifest["replacements"], {"\u2022": "*"})
            entries = {item["target"]: item for item in manifest["files"]}
            self.assertEqual(entries["AnsaMcpPlugin.ppl"]["encoding"], "gbk")
            self.assertEqual(entries["AnsaMcpPlugin.ppl"]["replacement_count"], 1)
            self.assertEqual(entries["icon.svg"]["encoding"], "unchanged")
            self.assertEqual(entries["icon.svg"]["replacements"], {})
            for item in manifest["files"]:
                path = output / item["target"]
                self.assertEqual(item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

            self.assertEqual(
                source_before,
                {
                    path.relative_to(source).as_posix(): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
            )

    def test_coding_cookie_is_first_line_and_shebang_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "native"
            package = source / "AnsaMcpPlugin_root"
            package.mkdir(parents=True)
            (source / "AnsaMcpPlugin.ppl").write_text(
                "# -*- coding: utf-8 -*-\nVALUE = 1\n", encoding="utf-8"
            )
            (package / "with_shebang.py").write_text(
                "#!/usr/bin/env python\n# coding: latin-1\nVALUE = 1\n",
                encoding="utf-8",
            )
            (package / "without_cookie.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )

            output = packager_compat.stage_packager_compat(source, root / "compat")

            ppl_lines = (output / "AnsaMcpPlugin.ppl").read_text(encoding="gbk").splitlines()
            self.assertEqual(ppl_lines[0], "# -*- coding: gbk -*-")
            shebang_lines = (
                (output / "AnsaMcpPlugin_root" / "with_shebang.py")
                .read_text(encoding="gbk")
                .splitlines()
            )
            self.assertEqual(shebang_lines[0], "#!/usr/bin/env python")
            self.assertEqual(shebang_lines[1], "# -*- coding: gbk -*-")
            plain_lines = (
                (output / "AnsaMcpPlugin_root" / "without_cookie.py")
                .read_text(encoding="gbk")
                .splitlines()
            )
            self.assertEqual(plain_lines[0], "# -*- coding: gbk -*-")

    def test_all_transcoded_python_payloads_parse_as_declared_gbk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source_tree(root)
            output = packager_compat.stage_packager_compat(source, root / "compat")

            for path in sorted(output.rglob("*")):
                if path.suffix.casefold() not in {".py", ".ppl"}:
                    continue
                source_text = path.read_bytes().decode("gbk")
                ast.parse(source_text, filename=str(path))

    def test_rejects_unknown_non_gbk_character(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "native"
            package = source / "AnsaMcpPlugin_root"
            package.mkdir(parents=True)
            (package / "bad.py").write_text("VALUE = '\U0001F600'\n", encoding="utf-8")
            output = root / "compat"

            with self.assertRaises(packager_compat.PackagerCompatError) as caught:
                packager_compat.stage_packager_compat(source, output)

            self.assertIn("U+1F600", str(caught.exception))
            self.assertFalse(output.exists())

    def test_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source_tree(root)
            output = root / "compat"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                packager_compat.stage_packager_compat(source, output)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")


if __name__ == "__main__":
    unittest.main()
