import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import install_external_release as installer


class ExternalReleaseInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wheel = self.root / "ansa_mcp_plugin-0.1.2-py3-none-any.whl"
        self.lock = self.root / "requirements-runtime.lock"
        self.lock.write_text("mcp==1.0\npydantic==2.0\n", encoding="utf-8")
        with zipfile.ZipFile(self.wheel, "w") as archive:
            archive.writestr(
                "ansa_mcp_plugin-0.1.2.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: ansa-mcp-plugin\nVersion: 0.1.2\n\n",
            )
        self.install_dir = self.root / "installed-plugin"
        self.runtime_home = self.root / "private-runtime"

    def tearDown(self):
        self.temp.cleanup()

    def _fake_env_builder(self, *, with_pip):
        self.assertTrue(with_pip)

        class FakeBuilder:
            def create(inner_self, destination):
                scripts = Path(destination) / "Scripts"
                scripts.mkdir(parents=True)
                (scripts / "python.exe").write_bytes(b"fake interpreter")

        return FakeBuilder()

    @staticmethod
    def _metadata_json():
        return json.dumps({
            "ansa-mcp-plugin": "0.1.2",
            "mcp": "1.0",
            "pydantic": "2.0",
        })

    def test_install_uses_clean_venv_pip_and_writes_relative_launcher_and_absolute_config(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if "-c" in command:
                return subprocess.CompletedProcess(command, 0, self._metadata_json(), "")
            return completed

        with patch.object(installer, "_host_details", return_value=("Windows", (3, 12), 22631)), \
                patch.object(installer.venv, "EnvBuilder", side_effect=self._fake_env_builder), \
                patch.object(installer.subprocess, "run", side_effect=fake_run), \
                patch.dict("os.environ", {"PYTHONPATH": str(self.root / "source-tree"),
                                         "PIP_TARGET": str(self.root / "existing-packages"),
                                         "PIP_UPGRADE": "1", "PIP_CONFIG_FILE": "polluted.ini"}):
            manifest = installer.install_release(
                self.wheel, self.lock, self.install_dir, self.runtime_home
            )

        self.assertEqual(calls[0][0][1:7], ["-m", "pip", "--isolated", "--no-cache-dir", "install", "-r"])
        self.assertEqual(calls[0][0][-2:], [str(self.lock.resolve()), str(self.wheel.resolve())])
        self.assertEqual(calls[1][0][1:], ["-m", "pip", "--isolated", "check"])
        for command, kwargs in calls:
            self.assertEqual(kwargs["cwd"], self.install_dir.resolve())
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertNotIn("PIP_TARGET", kwargs["env"])
            self.assertNotIn("PIP_UPGRADE", kwargs["env"])
            self.assertEqual(kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
            self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(calls[2][0][1], "-c")
        self.assertIn("Service(runtime_home)", calls[2][0][2])
        self.assertIn("outside the installed distribution", calls[2][0][2])
        self.assertEqual(manifest["installed_metadata_versions"]["ansa-mcp-plugin"], "0.1.2")
        self.assertTrue((self.install_dir / "venv" / "Scripts" / "python.exe").is_file())

        launcher = (self.install_dir / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('Path(__file__).resolve().parent', launcher)
        self.assertIn('"-m", "ansa_mcp_plugin", "--runtime-home", runtime_home', launcher)
        self.assertIn('sys.executable', launcher)

        config = json.loads((self.install_dir / "mcp-config.json").read_text(encoding="utf-8"))
        entry = config["mcpServers"][installer.DIST_NAME]
        self.assertTrue(Path(entry["command"]).is_absolute())
        self.assertTrue(Path(entry["args"][0]).is_absolute())
        self.assertEqual(entry["cwd"], str(self.install_dir.resolve()))
        self.assertEqual(manifest["runtime_home"], str(self.runtime_home.resolve()))

    def test_existing_install_directory_is_never_modified(self):
        self.install_dir.mkdir()
        sentinel = self.install_dir / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        with patch.object(installer, "_host_details", return_value=("Windows", (3, 12), 22631)), \
                patch.object(installer.venv, "EnvBuilder") as builder:
            with self.assertRaisesRegex(installer.InstallError, "must not already exist"):
                installer.install_release(self.wheel, self.lock, self.install_dir, self.runtime_home)

        builder.assert_not_called()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.install_dir / "failure-receipt.json").exists())

    def test_standalone_installer_does_not_treat_its_parent_as_source_repository(self):
        with patch.object(installer, "PROJECT_ROOT", self.root):
            self.assertIsNone(installer._source_root())
            (self.root / "pyproject.toml").write_text("[project]\nname='ansa-mcp-plugin'\n", encoding="utf-8")
            main_file = self.root / "src/ansa_mcp_plugin/__main__.py"
            main_file.parent.mkdir(parents=True)
            main_file.write_text("", encoding="utf-8")
            self.assertEqual(installer._source_root(), self.root)

    def test_failure_keeps_created_install_dir_and_writes_failure_receipt(self):
        failed = subprocess.CompletedProcess(["pip"], 1, stdout="out", stderr="pip failed")

        def fake_run(command, **kwargs):
            return failed

        with patch.object(installer, "_host_details", return_value=("Windows", (3, 12), 22631)), \
                patch.object(installer.venv, "EnvBuilder", side_effect=self._fake_env_builder), \
                patch.object(installer.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(installer.InstallError, "install_wheel_and_lock"):
                installer.install_release(self.wheel, self.lock, self.install_dir, self.runtime_home)

        receipt = json.loads((self.install_dir / "failure-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["stage"], "install_wheel_and_lock")
        self.assertEqual(receipt["command_failure"]["stderr"], "pip failed")
        self.assertTrue(self.install_dir.is_dir())

    def test_requires_absolute_runtime_home_and_supported_host(self):
        with patch.object(installer, "_host_details", return_value=("Windows", (3, 12), 22631)):
            with self.assertRaisesRegex(installer.InstallError, "absolute path"):
                installer.install_release(self.wheel, self.lock, self.install_dir, "relative-runtime")

        for details in (("Windows", (3, 11), 22631), ("Windows", (3, 12), 19045), ("Linux", (3, 12), None)):
            with self.subTest(details=details), self.assertRaises(installer.InstallError):
                installer._validate_host(*details)

    def test_rejects_non_plugin_wheel_metadata(self):
        other_wheel = self.root / "other.whl"
        with zipfile.ZipFile(other_wheel, "w") as archive:
            archive.writestr(
                "other-1.0.dist-info/METADATA",
                "Metadata-Version: 2.1\nName: other\nVersion: 1.0\n\n",
            )
        with self.assertRaisesRegex(installer.InstallError, "must identify ansa-mcp-plugin"):
            installer._read_wheel_metadata(other_wheel)


if __name__ == "__main__":
    unittest.main()
