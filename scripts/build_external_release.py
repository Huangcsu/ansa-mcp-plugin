"""Build and audit the external ANSA MCP Plugin wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "ansa-mcp-plugin"
EXPECTED_VERSION = "0.1.2"
DEFAULT_OUTPUT_RELATIVE = Path("dist") / f"v{EXPECTED_VERSION}"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / DEFAULT_OUTPUT_RELATIVE
PACKAGE_ROOTS = frozenset({"ansa_mcp_plugin", "ansa_mcp_shared"})
EXPECTED_DIST_INFO = "ansa_mcp_plugin-0.1.2.dist-info"
EXPECTED_RUNTIME_DEPENDENCIES = frozenset({"mcp", "pydantic"})
FORBIDDEN_PATH_PARTS = frozenset(
    {
        "ansa",
        "ansa_mcp_bridge",
        "bridge",
        "grpc",
        "grpcio",
        "grpc_tools",
        "runtime",
        "tests",
        "__pycache__",
    }
)
FORBIDDEN_FILE_SUFFIXES = frozenset(
    {".db", ".db3", ".db-journal", ".pem", ".p12", ".pfx", ".key", ".sqlite", ".sqlite3"}
)
_ABSOLUTE_USER_PATH = re.compile(
    rb"(?i)(?:[a-z]:[\\/]+(?:users|documents and settings)[\\/]+|/(?:home|users)/)[^\r\n\"'<> ]*"
)
_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)


class ReleaseBuildError(RuntimeError):
    """Raised when a release wheel does not meet the package boundary."""


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _metadata_requirement_name(value: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", value)
    if match is None:
        raise ReleaseBuildError(f"无法解析 wheel 依赖元数据: {value!r}")
    return _normalized_name(match.group(1))


def _validate_member_name(member: zipfile.ZipInfo) -> tuple[str, ...]:
    raw_name = member.filename
    if not raw_name or "\\" in raw_name:
        raise ReleaseBuildError(f"wheel 含非法路径: {raw_name!r}")
    name = raw_name.rstrip("/")
    parts = tuple(name.split("/"))
    if (
        not name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or re.match(r"^[A-Za-z]:", parts[0])
    ):
        raise ReleaseBuildError(f"wheel 含绝对路径或路径穿越: {raw_name!r}")
    return parts


def _check_member_content(name: str, content: bytes) -> None:
    if _ABSOLUTE_USER_PATH.search(content):
        raise ReleaseBuildError(f"wheel 文件含绝对用户路径: {name}")
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        raise ReleaseBuildError(f"wheel 文件疑似包含密钥材料: {name}")


def audit_wheel(wheel_path: Path) -> dict[str, object]:
    """Validate wheel paths, payloads, and distribution metadata."""
    wheel_path = Path(wheel_path)
    if not wheel_path.name.startswith("ansa_mcp_plugin-0.1.2-") or not wheel_path.name.endswith(".whl"):
        raise ReleaseBuildError(f"wheel 文件名与 {EXPECTED_VERSION} 不符: {wheel_path.name}")

    try:
        with zipfile.ZipFile(wheel_path) as archive:
            members = archive.infolist()
            seen_names: set[str] = set()
            package_python_roots: set[str] = set()
            metadata_paths: list[str] = []
            file_names: list[str] = []

            for member in members:
                parts = _validate_member_name(member)
                name = member.filename.rstrip("/")
                folded_name = name.casefold()
                if folded_name in seen_names:
                    raise ReleaseBuildError(f"wheel 含重复路径: {member.filename}")
                seen_names.add(folded_name)

                root = parts[0]
                if root in PACKAGE_ROOTS:
                    if len(parts) == 1 and not member.is_dir():
                        raise ReleaseBuildError(f"wheel 包根不是目录: {member.filename}")
                    if any(part.casefold() in FORBIDDEN_PATH_PARTS for part in parts[1:]):
                        raise ReleaseBuildError(f"wheel 含禁止目录: {member.filename}")
                    if name.lower().endswith((".pyc", ".pyo")):
                        raise ReleaseBuildError(f"wheel 含 Python 缓存文件: {member.filename}")
                    leaf_name = parts[-1].casefold()
                    if (
                        leaf_name == ".env"
                        or leaf_name.startswith(".env.")
                        or leaf_name in {
                            "auth.json",
                            "conftest.py",
                            "credential",
                            "credentials",
                            "secret",
                            "secrets",
                            "test.py",
                            "tests.py",
                            "token",
                            "tokens",
                        }
                        or leaf_name.startswith("test_")
                        or leaf_name.endswith("_test.py")
                    ):
                        raise ReleaseBuildError(f"wheel 含禁止文件: {member.filename}")
                    if parts[-1].lower().endswith(tuple(FORBIDDEN_FILE_SUFFIXES)):
                        raise ReleaseBuildError(f"wheel 含机密或运行时数据文件: {member.filename}")
                    if not member.is_dir():
                        file_names.append(name)
                        if parts[-1].endswith(".py"):
                            package_python_roots.add(root)
                elif root == EXPECTED_DIST_INFO:
                    if len(parts) == 1 and not member.is_dir():
                        raise ReleaseBuildError(f"wheel 元数据根不是目录: {member.filename}")
                    if not member.is_dir():
                        file_names.append(name)
                        if parts[-1] == "METADATA":
                            metadata_paths.append(name)
                else:
                    raise ReleaseBuildError(f"wheel 含范围外文件: {member.filename}")

                if not member.is_dir():
                    _check_member_content(member.filename, archive.read(member))

            if package_python_roots != PACKAGE_ROOTS:
                raise ReleaseBuildError(
                    "wheel 必须同时包含 ansa_mcp_plugin 和 ansa_mcp_shared Python 包"
                )
            if metadata_paths != [f"{EXPECTED_DIST_INFO}/METADATA"]:
                raise ReleaseBuildError("wheel 必须且只能包含根目录 dist-info/METADATA")
            for required in ("WHEEL", "RECORD"):
                if f"{EXPECTED_DIST_INFO}/{required}" not in file_names:
                    raise ReleaseBuildError(f"wheel 缺少 {required} 元数据")

            metadata = BytesParser(policy=compat32).parsebytes(archive.read(metadata_paths[0]))
            if _normalized_name(metadata.get("Name", "")) != _normalized_name(PROJECT_NAME):
                raise ReleaseBuildError(f"wheel 项目名不符: {metadata.get('Name')!r}")
            if metadata.get("Version") != EXPECTED_VERSION:
                raise ReleaseBuildError(
                    f"wheel metadata 版本应为 {EXPECTED_VERSION}，实际为 {metadata.get('Version')!r}"
                )

            dependency_names = [
                _metadata_requirement_name(value)
                for value in metadata.get_all("Requires-Dist", [])
            ]
            if (
                len(dependency_names) != len(EXPECTED_RUNTIME_DEPENDENCIES)
                or set(dependency_names) != EXPECTED_RUNTIME_DEPENDENCIES
            ):
                raise ReleaseBuildError(
                    "wheel 运行依赖必须且只能是 mcp 与 pydantic；不得捆入 SDK、gRPC 或开发依赖"
                )

            return {
                "name": PROJECT_NAME,
                "version": EXPECTED_VERSION,
                "files": sorted(file_names),
            }
    except zipfile.BadZipFile as exc:
        raise ReleaseBuildError(f"不是有效的 wheel ZIP 文件: {wheel_path.name}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(wheel_path: Path, audit: dict[str, object], output_dir: Path) -> Path:
    manifest = {
        "distribution": PROJECT_NAME,
        "version": EXPECTED_VERSION,
        "wheel": Path(wheel_path).name,
        "sha256": _sha256(Path(wheel_path)),
        "files": audit["files"],
    }
    manifest_path = Path(output_dir) / f"{Path(wheel_path).stem}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def build_release(output_dir: Path) -> tuple[Path, Path, str]:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".wheel-build-", dir=output_dir) as temp_dir:
        staging_dir = Path(temp_dir)
        command = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--wheel-dir",
            str(staging_dir),
            str(PROJECT_ROOT),
        ]
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
        wheels = sorted(staging_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise ReleaseBuildError(f"预期生成一个 wheel，实际发现 {len(wheels)} 个")

        built_wheel = wheels[0]
        audit_wheel(built_wheel)
        final_wheel = output_dir / built_wheel.name
        shutil.copy2(built_wheel, final_wheel)

    audit = audit_wheel(final_wheel)
    manifest_path = write_manifest(final_wheel, audit, output_dir)
    return final_wheel, manifest_path, _sha256(final_wheel)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并审计外部 MCP wheel")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=f"产物目录（默认 {DEFAULT_OUTPUT_RELATIVE.as_posix()}，相对于仓库根目录）",
    )
    args = parser.parse_args()
    output_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir

    try:
        wheel_path, manifest_path, digest = build_release(output_dir)
    except (OSError, ReleaseBuildError, subprocess.CalledProcessError) as exc:
        print(f"外部 wheel 构建失败: {exc}", file=sys.stderr)
        return 1

    print(f"wheel: {wheel_path}")
    print(f"manifest: {manifest_path}")
    print(f"sha256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
