"""Focused checks for the external wheel boundary and runtime lock."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "build_external_release.py"
SPEC = importlib.util.spec_from_file_location("build_external_release", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
release_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_build)


def _write_test_wheel(directory: Path, *, extra_files=(), version="0.1.0", requires_dist=None) -> Path:
    wheel_path = directory / "ansa_mcp_plugin-0.1.0-py3-none-any.whl"
    dist_info = "ansa_mcp_plugin-0.1.0.dist-info"
    dependencies = requires_dist or ("mcp>=1.30,<2", "pydantic>=2.11,<3")
    metadata = "\n".join(
        [
            "Metadata-Version: 2.1",
            "Name: ansa-mcp-plugin",
            f"Version: {version}",
            *(f"Requires-Dist: {item}" for item in dependencies),
            "",
            "",
        ]
    )
    files = {
        "ansa_mcp_plugin/__init__.py": b"",
        "ansa_mcp_shared/__init__.py": b"",
        f"{dist_info}/METADATA": metadata.encode("utf-8"),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/RECORD": b"",
        f"{dist_info}/top_level.txt": b"ansa_mcp_plugin\nansa_mcp_shared\n",
    }
    files.update(dict(extra_files))
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel_path


class ReleaseBuildTests(unittest.TestCase):
    def test_default_output_directory_uses_versioned_release_path(self):
        self.assertEqual(
            release_build.DEFAULT_OUTPUT_DIR,
            PROJECT_ROOT / "dist" / "v0.1.0",
        )

    def test_project_declares_pep639_license_and_author_metadata(self):
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["build-system"]["requires"], ["setuptools>=77"])
        project = config["project"]
        self.assertEqual(project["readme"], "README.md")
        self.assertEqual(project["authors"], [{"name": "Huangcsu"}])
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE"])

    def test_build_command_disables_pip_cache(self):
        def fake_run(command, *, cwd, check):
            staging_dir = Path(command[command.index("--wheel-dir") + 1])
            _write_test_wheel(staging_dir)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(release_build.subprocess, "run", side_effect=fake_run) as run:
                release_build.build_release(Path(temp_dir) / "dist")

        command = run.call_args.args[0]
        self.assertIn("--no-cache-dir", command)
        self.assertIn("--no-deps", command)
        self.assertIn("--no-build-isolation", command)

    def test_audit_accepts_only_the_external_and_shared_packages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wheel = _write_test_wheel(Path(temp_dir))
            audit = release_build.audit_wheel(wheel)

        self.assertEqual(audit["version"], "0.1.0")
        self.assertEqual(
            {name.split("/", 1)[0] for name in audit["files"] if "/" in name},
            {"ansa_mcp_plugin", "ansa_mcp_shared", "ansa_mcp_plugin-0.1.0.dist-info"},
        )

    def test_audit_accepts_dist_info_license_file(self):
        relative_license = "ansa_mcp_plugin-0.1.0.dist-info/licenses/LICENSE"
        with tempfile.TemporaryDirectory() as temp_dir:
            wheel = _write_test_wheel(
                Path(temp_dir),
                extra_files=((relative_license, b"MIT License\n"),),
            )
            audit = release_build.audit_wheel(wheel)

        self.assertIn(relative_license, audit["files"])

    def test_audit_rejects_forbidden_payloads_and_absolute_user_paths(self):
        forbidden_paths = (
            "ansa_mcp_bridge/bridge.py",
            "ansa/__init__.py",
            "grpc/__init__.py",
            "tests/test_private.py",
            "ansa_mcp_plugin/test_fixtures.py",
            "ansa_mcp_plugin/runtime/state.json",
            "ansa_mcp_plugin/.env",
            "ansa_mcp_plugin/.env.local",
        )
        for member in forbidden_paths:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as temp_dir:
                wheel = _write_test_wheel(Path(temp_dir), extra_files=((member, b"data"),))
                with self.assertRaises(release_build.ReleaseBuildError):
                    release_build.audit_wheel(wheel)

        with tempfile.TemporaryDirectory() as temp_dir:
            wheel = _write_test_wheel(
                Path(temp_dir),
                extra_files=(("ansa_mcp_plugin/config.py", b'HOME = "C:\\\\Users\\\\alice\\\\config"'),),
            )
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.audit_wheel(wheel)

        with tempfile.TemporaryDirectory() as temp_dir:
            wheel = _write_test_wheel(
                Path(temp_dir),
                extra_files=(("ansa_mcp_plugin/credentials.py", b'KEY = "AKIAABCDEFGHIJKLMNOP"'),),
            )
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.audit_wheel(wheel)

    def test_audit_rejects_wrong_metadata_version_and_unapproved_dependencies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            wheel = _write_test_wheel(Path(temp_dir), version="0.0.1")
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.audit_wheel(wheel)

        with tempfile.TemporaryDirectory() as temp_dir:
            wheel = _write_test_wheel(
                Path(temp_dir),
                requires_dist=("mcp>=1.30,<2", "pydantic>=2.11,<3", "grpcio>=1"),
            )
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.audit_wheel(wheel)

    def test_manifest_records_wheel_sha256(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            wheel = _write_test_wheel(directory)
            audit = release_build.audit_wheel(wheel)
            manifest_path = release_build.write_manifest(wheel, audit, directory)

            self.assertIn(hashlib.sha256(wheel.read_bytes()).hexdigest(), manifest_path.read_text("utf-8"))

    def test_runtime_lock_is_the_active_mcp_and_pydantic_dependency_closure(self):
        environment = default_environment()
        lock_path = PROJECT_ROOT / "packaging" / "requirements-runtime.lock"
        locked: dict[str, Requirement] = {}
        for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.marker is None or requirement.marker.evaluate(
                {**environment, "extra": ""}
            ):
                name = canonicalize_name(requirement.name)
                self.assertNotIn(name, locked, f"duplicate lock entry: {name}")
                locked[name] = requirement

        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        roots = [Requirement(item) for item in project["project"]["dependencies"]]
        distributions = {
            canonicalize_name(dist.metadata["Name"]): dist
            for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name")
        }
        pending = [(req.name, set(req.extras)) for req in roots]
        visited: set[tuple[str, frozenset[str]]] = set()
        closure: dict[str, importlib.metadata.Distribution] = {}

        while pending:
            raw_name, extras = pending.pop()
            name = canonicalize_name(raw_name)
            state = (name, frozenset(extras))
            if state in visited:
                continue
            visited.add(state)
            distribution = distributions.get(name)
            self.assertIsNotNone(distribution, f"runtime dependency is missing: {name}")
            assert distribution is not None
            closure[name] = distribution

            for raw_dependency in distribution.requires or ():
                dependency = Requirement(raw_dependency)
                active_extras = ("", *extras)
                active = dependency.marker is None or any(
                    dependency.marker.evaluate({**environment, "extra": extra})
                    for extra in active_extras
                )
                if active:
                    pending.append((dependency.name, set(dependency.extras)))

        self.assertEqual(set(locked), set(closure))
        for name, distribution in closure.items():
            requirement = locked[name]
            self.assertEqual(str(requirement.specifier), f"=={distribution.version}", name)
        self.assertEqual(locked["pyjwt"].extras, {"crypto"})
        self.assertIsNotNone(locked["pywin32"].marker)
        self.assertTrue(locked["pywin32"].marker.evaluate({**environment, "extra": ""}))
        self.assertFalse(
            locked["pywin32"].marker.evaluate(
                {**environment, "sys_platform": "linux", "extra": ""}
            )
        )
        self.assertIsNotNone(locked["cffi"].marker)
        self.assertFalse(
            locked["cffi"].marker.evaluate(
                {**environment, "platform_python_implementation": "PyPy", "extra": ""}
            )
        )


if __name__ == "__main__":
    unittest.main()
