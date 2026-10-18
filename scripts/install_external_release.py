#!/usr/bin/env python3
"""Install an externally built ANSA MCP Plugin wheel into an isolated venv."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import venv
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path


DIST_NAME = "ansa-mcp-plugin"
RUNTIME_METADATA_NAMES = (DIST_NAME, "mcp", "pydantic")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FAILURE_OUTPUT = 12000


class InstallError(RuntimeError):
    """An installation failure with optional subprocess diagnostics."""

    def __init__(self, message: str, command_details: dict | None = None):
        super().__init__(message)
        self.command_details = command_details


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _host_details() -> tuple[str, tuple[int, int], int | None]:
    build = sys.getwindowsversion().build if sys.platform == "win32" else None
    return platform.system(), tuple(sys.version_info[:2]), build


def _validate_host(system: str, python_version: tuple[int, int], windows_build: int | None) -> None:
    if system != "Windows" or windows_build is None or windows_build < 22000:
        raise InstallError("Only Windows 11 is supported (Windows build 22000 or newer).")
    if python_version != (3, 12):
        raise InstallError("Only Python 3.12 is supported for this release installer.")


def _read_wheel_metadata(wheel_path: Path) -> tuple[str, str]:
    if wheel_path.suffix.lower() != ".whl":
        raise InstallError("--wheel must point to a .whl file.")
    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            metadata_paths = [
                name for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA") and len(name.split("/")) == 2
            ]
            if len(metadata_paths) != 1:
                raise InstallError("Wheel must contain exactly one top-level .dist-info/METADATA file.")
            metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise InstallError(f"Unable to read wheel metadata: {exc}") from exc

    name = metadata.get("Name", "").strip()
    version = metadata.get("Version", "").strip()
    if _normalize_name(name) != DIST_NAME or not version:
        raise InstallError(f"Wheel metadata must identify {DIST_NAME} and include a version.")
    return name, version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_file(path: Path, contents: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(contents)


def _json_text(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _clean_subprocess_env() -> dict[str, str]:
    child_env = {key: value for key, value in os.environ.items()
                 if not key.upper().startswith("PIP_")}
    child_env.pop("PYTHONPATH", None)
    child_env.pop("PYTHONHOME", None)
    child_env["PYTHONNOUSERSITE"] = "1"
    child_env["PYTHONUTF8"] = "1"
    child_env["PIP_CONFIG_FILE"] = os.devnull
    return child_env


def _source_root() -> Path | None:
    if ((PROJECT_ROOT / "pyproject.toml").is_file()
            and (PROJECT_ROOT / "src/ansa_mcp_plugin/__main__.py").is_file()):
        return PROJECT_ROOT
    return None


def _run(command: list[str], *, stage: str, install_dir: Path, child_env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=install_dir,
        env=child_env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        details = {
            "stage": stage,
            "command": command,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "")[-MAX_FAILURE_OUTPUT:],
            "stderr": (completed.stderr or "")[-MAX_FAILURE_OUTPUT:],
        }
        raise InstallError(f"Command failed during {stage} (exit {completed.returncode}).", details)
    return completed.stdout or ""


RUNTIME_VALIDATION_CODE = r'''import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import sys

distribution_name = sys.argv[1]
runtime_home = sys.argv[2]
distribution = metadata.distribution(distribution_name)
module = importlib.import_module("ansa_mcp_plugin")
installed_module = Path(distribution.locate_file("ansa_mcp_plugin/__init__.py")).resolve()
if Path(module.__file__).resolve() != installed_module:
    raise RuntimeError("ansa_mcp_plugin imported from outside the installed distribution")

from ansa_mcp_plugin.backend.service import Service
service = Service(runtime_home)
try:
    pass
finally:
    service.close()

versions = {}
for dist in metadata.distributions():
    name = dist.metadata.get("Name")
    if name:
        versions[name.lower().replace("_", "-")] = dist.version
print(json.dumps(versions, sort_keys=True))
'''


LAUNCHER_SOURCE = '''"""Start the installed ANSA MCP Plugin using this installation's runtime home."""

import json
import os
from pathlib import Path
import subprocess
import sys


INSTALL_DIR = Path(__file__).resolve().parent
MANIFEST = INSTALL_DIR / "install-manifest.json"


def main():
    record = json.loads(MANIFEST.read_text(encoding="utf-8"))
    runtime_home = record["runtime_home"]
    if not Path(runtime_home).is_absolute():
        raise RuntimeError("Installation record has a non-absolute runtime_home")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    command = [sys.executable, "-m", "ansa_mcp_plugin", "--runtime-home", runtime_home]
    return subprocess.call(command, cwd=INSTALL_DIR, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _write_failure_receipt(install_dir: Path, stage: str, exc: BaseException) -> None:
    receipt = {
        "schema_version": 1,
        "failed_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    details = getattr(exc, "command_details", None)
    if details:
        receipt["command_failure"] = details
    try:
        _new_file(install_dir / "failure-receipt.json", _json_text(receipt))
    except OSError as receipt_error:
        print(f"Could not write failure receipt: {receipt_error}", file=sys.stderr)


def install_release(wheel: str | Path, lock: str | Path, install_dir: str | Path,
                    runtime_home: str | Path) -> dict:
    """Install a wheel without deleting or replacing any pre-existing path."""
    system, python_version, windows_build = _host_details()
    _validate_host(system, python_version, windows_build)

    wheel_path = Path(wheel).expanduser().resolve(strict=True)
    lock_path = Path(lock).expanduser().resolve(strict=True)
    if not wheel_path.is_file():
        raise InstallError("--wheel must point to a file.")
    if not lock_path.is_file():
        raise InstallError("--lock must point to a file.")
    dist_name, wheel_version = _read_wheel_metadata(wheel_path)

    runtime_path = Path(runtime_home).expanduser()
    if not runtime_path.is_absolute():
        raise InstallError("--runtime-home must be an absolute path.")
    runtime_path = runtime_path.resolve()

    target = Path(install_dir).expanduser().resolve()
    if target.exists():
        raise InstallError(f"--install-dir must not already exist: {target}")
    source_root = _source_root()
    if source_root is not None:
        try:
            target.relative_to(source_root)
        except ValueError:
            pass
        else:
            raise InstallError("--install-dir must be outside the source repository.")

    stage = "create_install_dir"
    created = False
    try:
        target.mkdir(parents=True, exist_ok=False)
        created = True

        stage = "create_venv"
        venv_dir = target / "venv"
        venv.EnvBuilder(with_pip=True).create(str(venv_dir))
        venv_python = venv_dir / "Scripts" / "python.exe"
        if not venv_python.is_file():
            raise InstallError("The created virtual environment has no Scripts/python.exe.")

        child_env = _clean_subprocess_env()
        stage = "install_wheel_and_lock"
        _run(
            [str(venv_python), "-m", "pip", "--isolated", "--no-cache-dir", "install", "-r", str(lock_path), str(wheel_path)],
            stage=stage,
            install_dir=target,
            child_env=child_env,
        )

        stage = "pip_check"
        _run([str(venv_python), "-m", "pip", "--isolated", "check"], stage=stage, install_dir=target, child_env=child_env)

        stage = "initialize_and_validate_runtime"
        output = _run(
            [str(venv_python), "-c", RUNTIME_VALIDATION_CODE, dist_name, str(runtime_path)],
            stage=stage,
            install_dir=target,
            child_env=child_env,
        )
        try:
            installed_versions = json.loads(output)
        except json.JSONDecodeError as exc:
            raise InstallError("Installed-package metadata check did not return valid JSON.") from exc
        if not isinstance(installed_versions, dict):
            raise InstallError("Installed-package metadata check returned an invalid value.")
        normalized_versions = {_normalize_name(str(name)): str(version) for name, version in installed_versions.items()}
        installed_plugin_version = normalized_versions.get(DIST_NAME)
        if installed_plugin_version != wheel_version:
            raise InstallError(
                f"Installed {DIST_NAME} metadata version {installed_plugin_version!r} "
                f"does not match wheel version {wheel_version!r}."
            )
        missing_runtime_metadata = [name for name in RUNTIME_METADATA_NAMES if name not in normalized_versions]
        if missing_runtime_metadata:
            raise InstallError("Installed runtime metadata is missing: " + ", ".join(missing_runtime_metadata))

        stage = "write_install_records"
        venv_relative = Path("venv") / "Scripts" / "python.exe"
        manifest = {
            "schema_version": 1,
            "distribution": dist_name,
            "version": wheel_version,
            "installed_at_utc": datetime.now(timezone.utc).isoformat(),
            "install_dir": str(target),
            "runtime_home": str(runtime_path),
            "python_version": f"{python_version[0]}.{python_version[1]}",
            "windows_build": windows_build,
            "venv_python": venv_relative.as_posix(),
            "launcher": "launcher.py",
            "wheel": {"filename": wheel_path.name, "sha256": _sha256(wheel_path)},
            "lock": {"filename": lock_path.name, "sha256": _sha256(lock_path)},
            "installed_metadata_versions": dict(sorted(normalized_versions.items())),
        }
        _new_file(target / "install-manifest.json", _json_text(manifest))
        _new_file(target / "launcher.py", LAUNCHER_SOURCE)

        stage = "write_mcp_config"
        mcp_config = {
            "mcpServers": {
                DIST_NAME: {
                    "command": str(venv_python),
                    "args": [str(target / "launcher.py")],
                    "cwd": str(target),
                }
            }
        }
        _new_file(target / "mcp-config.json", _json_text(mcp_config))
        return manifest
    except BaseException as exc:
        if created:
            _write_failure_receipt(target, stage, exc)
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, help="Path to the external release .whl file")
    parser.add_argument("--lock", required=True, help="Path to the exact runtime requirements lock")
    parser.add_argument("--install-dir", required=True, help="New installation directory; it must not exist")
    parser.add_argument("--runtime-home", required=True, help="Absolute path for the private plugin runtime")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifest = install_release(args.wheel, args.lock, args.install_dir, args.runtime_home)
    except (InstallError, OSError, subprocess.SubprocessError) as exc:
        print(f"External release installation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "installed": True,
        "install_dir": manifest["install_dir"],
        "version": manifest["version"],
        "manifest": str(Path(manifest["install_dir"]) / "install-manifest.json"),
        "mcp_config": str(Path(manifest["install_dir"]) / "mcp-config.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
