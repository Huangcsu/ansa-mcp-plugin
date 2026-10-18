"""原生入口导入隔离与 Packager staging 清单检查。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "stage_native_release.py"
SPEC = importlib.util.spec_from_file_location("stage_native_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
stage_native_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stage_native_release)


class NativeReleaseTests(unittest.TestCase):
    def _official_nested_archive(self, staged: Path, root: Path) -> Path:
        """把实际 staging 重排为官方 Packager 的 root/root/app.py 布局。"""
        archive_path = root / "native-official.bpkgo.zip"
        package = staged / "AnsaMcpPlugin_root"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("AnsaMcpPlugin_root/__init__.py", "")
            archive.writestr("AnsaMcpPlugin_root/AnsaMcpPlugin_root/__init__.py", "")
            archive.write(staged / "AnsaMcpPlugin.ppl", "AnsaMcpPlugin_root/AnsaMcpPlugin.ppl")
            for path in sorted(package.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(package).as_posix()
                if relative == "__init__.py":
                    continue
                target = "AnsaMcpPlugin_root/AnsaMcpPlugin_root/"
                target += "app.py" if relative == "AnsaMcpPlugin.py" else relative
                archive.write(path, target)
        return archive_path

    def test_stage_contains_only_allowlisted_files_with_sha256_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = stage_native_release.stage_release(Path(temp_dir) / "native-0.1.0")
            manifest_path = output / "SHA256SUMS.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["version"], "0.1.0")
            self.assertEqual(manifest["module_root"], "AnsaMcpPlugin_root")
            self.assertEqual(manifest["ppl"], "AnsaMcpPlugin.ppl")
            expected = {
                "AnsaMcpPlugin.ppl",
                "LICENSE",
                "AnsaMcpPlugin_root/AnsaMcpPlugin.py",
                "AnsaMcpPlugin_root/__init__.py",
                *(
                    "AnsaMcpPlugin_root/" + relative_path
                    for relative_path in stage_native_release.SOURCE_FILES
                ),
            }
            actual = {item["path"] for item in manifest["files"]}
            self.assertEqual(actual, expected)
            self.assertEqual((output / "LICENSE").read_bytes(), (PROJECT_ROOT / "LICENSE").read_bytes())
            for item in manifest["files"]:
                path = output / Path(item["path"])
                self.assertEqual(
                    item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
                )
            self.assertFalse(any(path.suffix.casefold() == ".bpkg" for path in output.rglob("*")))

    def test_stage_refuses_to_overwrite_existing_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "native-0.1.0"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                stage_native_release.stage_release(output)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_directory_and_zip_import_use_bundled_aliases_without_source_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staged = stage_native_release.stage_release(root / "native-0.1.0")
            archive_path = root / "native-package.bpkgo.zip"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted((staged / "AnsaMcpPlugin_root").rglob("*")):
                    if path.is_file():
                        archive.write(path, path.relative_to(staged).as_posix())

            program = r'''
import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

import_root = Path(sys.argv[1]).resolve()
source_root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(import_root))
entry = importlib.import_module("AnsaMcpPlugin_root.AnsaMcpPlugin")
assert entry._persistent_entry() is entry

if import_root.is_file():
    foreign = types.ModuleType("ansa_mcp_bridge")
    foreign.__file__ = str(import_root.parent / "other-release" / "ansa_mcp_bridge" / "__init__.py")
    sys.modules["ansa_mcp_bridge"] = foreign
    try:
        entry._import_bridge_packages()
    except RuntimeError as exc:
        assert "其他位置加载" in str(exc)
    else:
        raise AssertionError("foreign same-name bridge module was accepted")
    del sys.modules["ansa_mcp_bridge"]

entry._import_bridge_packages()
foreign_bridge = types.SimpleNamespace(closed=False, runtime_home=import_root.parent / "other-runtime", close=Mock())
with patch.object(entry, "_bridge", return_value=foreign_bridge), patch.object(
    entry, "_read_config", return_value={"runtime_home": import_root.parent / "this-runtime"}
):
    try:
        entry._stop_bridge()
    except RuntimeError as exc:
        assert "其他 runtime_home" in str(exc)
    else:
        raise AssertionError("Stop accepted a bridge owned by a different runtime")
foreign_bridge.close.assert_not_called()
if import_root.is_file():
    package_root = Path(str(import_root) + os.sep + "AnsaMcpPlugin_root")
else:
    package_root = import_root / "AnsaMcpPlugin_root"
for name in ("ansa_mcp_bridge", "ansa_mcp_shared"):
    module = sys.modules[name]
    assert Path(module.__file__).resolve().is_relative_to(package_root), module.__file__
    assert not Path(module.__file__).resolve().is_relative_to(source_root), module.__file__
assert all(
    not item or not Path(item).is_absolute() or Path(item).resolve() != source_root
    for item in sys.path
)
'''
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            for import_root in (staged, archive_path):
                with self.subTest(import_root=import_root.name):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            program,
                            str(import_root),
                            str(PROJECT_ROOT / "src"),
                        ],
                        cwd=root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_official_nested_app_import_and_foreign_rejection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            staged = stage_native_release.stage_release(root / "native-0.1.0")
            archive_path = self._official_nested_archive(staged, root)

            program = r'''
import importlib
import os
import sys
import types
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
source_root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(archive))
entry = importlib.import_module("AnsaMcpPlugin_root.AnsaMcpPlugin_root.app")

assert entry._persistent_entry() is entry
package_root, import_parent = entry._package_locations()
expected_root = Path(
    str(archive) + os.sep + "AnsaMcpPlugin_root" + os.sep + "AnsaMcpPlugin_root"
)
assert Path(package_root) == expected_root, (package_root, expected_root)
assert Path(import_parent) == archive
sys.path.remove(str(archive))
assert entry._package_locations() == (package_root, import_parent)
entry._reject_foreign_modules(package_root)

foreign = types.ModuleType("AnsaMcpPlugin_root.AnsaMcpPlugin_root.foreign")
foreign.__file__ = str(archive.parent / "other-release" / "AnsaMcpPlugin_root" / "foreign.py")
sys.modules[foreign.__name__] = foreign
try:
    entry._reject_foreign_modules(package_root)
except RuntimeError as exc:
    assert "其他位置加载" in str(exc)
else:
    raise AssertionError("foreign same-name root module was accepted")
del sys.modules[foreign.__name__]

foreign_bridge = types.ModuleType("ansa_mcp_bridge")
foreign_bridge.__file__ = str(archive.parent / "other-release" / "ansa_mcp_bridge" / "__init__.py")
sys.modules["ansa_mcp_bridge"] = foreign_bridge
try:
    entry._import_bridge_packages()
except RuntimeError as exc:
    assert "其他位置加载" in str(exc)
else:
    raise AssertionError("foreign same-name bridge module was accepted")
del sys.modules["ansa_mcp_bridge"]

entry._import_bridge_packages()
for name in ("ansa_mcp_bridge", "ansa_mcp_shared"):
    module = sys.modules[name]
    module_path = Path(module.__file__).resolve()
    assert module_path.is_relative_to(expected_root), module.__file__
    assert not module_path.is_relative_to(source_root), module.__file__
'''
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    program,
                    str(archive_path),
                    str(PROJECT_ROOT / "src"),
                ],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ppl_registers_without_importing_relocated_entry(self):
        ppl_path = PROJECT_ROOT / "packaging" / "ansa" / "AnsaMcpPlugin.ppl"
        source = ppl_path.read_text(encoding="ascii")
        filepath_lines = [
            line for line in source.splitlines() if line.strip().startswith("self.filepath =")
        ]
        self.assertEqual(len(filepath_lines), 1)
        self.assertIn("AnsaMcpPlugin_root", filepath_lines[0])

        program = r'''
import runpy
import sys
import types

captured = {}
session = types.ModuleType("ansa.session")
def set_plugin_infos(info):
    captured["info"] = info
session.setPluginInfos = set_plugin_infos
ansa = types.ModuleType("ansa")
ansa.session = session
sys.modules["ansa"] = ansa
sys.modules["ansa.session"] = session

runpy.run_path(sys.argv[1], run_name="__main__")

assert captured["info"].title == "ANSA MCP Plugin"
assert captured["info"].author == "Huangcsu"
assert "AnsaMcpPlugin_root" not in sys.modules
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-B", "-c", program, str(ppl_path)],
                cwd=temp_dir,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
