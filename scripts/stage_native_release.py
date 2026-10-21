"""按固定源码清单准备官方 ANSA Packager 的原生模块目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.2"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "build" / f"native-{VERSION}"
PACKAGE_ROOT = "AnsaMcpPlugin_root"
PPL_SOURCE = Path("packaging/ansa/AnsaMcpPlugin.ppl")
ENTRY_SOURCE = Path("packaging/ansa/AnsaMcpPlugin.py")

# 发布内容逐项列出，避免把缓存、SDK、模型、日志或其他工作区文件带入 staging。
SOURCE_FILES = (
    "ansa_mcp_bridge/__init__.py",
    "ansa_mcp_bridge/bootstrap.py",
    "ansa_mcp_bridge/executor.py",
    "ansa_mcp_bridge/journal.py",
    "ansa_mcp_bridge/state.py",
    "ansa_mcp_bridge/transport.py",
    "ansa_mcp_bridge/operations/__init__.py",
    "ansa_mcp_bridge/operations/abaqus_contact.py",
    "ansa_mcp_bridge/operations/abaqus_mass.py",
    "ansa_mcp_bridge/operations/abaqus_surfaces.py",
    "ansa_mcp_bridge/operations/api_catalog.py",
    "ansa_mcp_bridge/operations/api_documentation.py",
    "ansa_mcp_bridge/operations/benchmark.py",
    "ansa_mcp_bridge/operations/checks.py",
    "ansa_mcp_bridge/operations/connection_apply.py",
    "ansa_mcp_bridge/operations/connection_checks.py",
    "ansa_mcp_bridge/operations/connections.py",
    "ansa_mcp_bridge/operations/entities.py",
    "ansa_mcp_bridge/operations/entity_catalog.py",
    "ansa_mcp_bridge/operations/element_types.py",
    "ansa_mcp_bridge/operations/failed_elements.py",
    "ansa_mcp_bridge/operations/geometry.py",
    "ansa_mcp_bridge/operations/geometry_checks.py",
    "ansa_mcp_bridge/operations/geometry_delete.py",
    "ansa_mcp_bridge/operations/hexa_meshing.py",
    "ansa_mcp_bridge/operations/inspection.py",
    "ansa_mcp_bridge/operations/intersections.py",
    "ansa_mcp_bridge/operations/kinematics.py",
    "ansa_mcp_bridge/operations/mesh_checks.py",
    "ansa_mcp_bridge/operations/mesh_settings.py",
    "ansa_mcp_bridge/operations/mesh_workflow.py",
    "ansa_mcp_bridge/operations/meshing.py",
    "ansa_mcp_bridge/operations/model_files.py",
    "ansa_mcp_bridge/operations/models.py",
    "ansa_mcp_bridge/operations/penetrations.py",
    "ansa_mcp_bridge/operations/quality_workflow.py",
    "ansa_mcp_bridge/operations/native_quality.py",
    "ansa_mcp_bridge/operations/quality_options.py",
    "ansa_mcp_bridge/operations/quality_values.py",
    "ansa_mcp_bridge/operations/quality_reports.py",
    "ansa_mcp_bridge/operations/quality_snapshot.py",
    "ansa_mcp_bridge/operations/rigid_dependencies.py",
    "ansa_mcp_bridge/operations/scripts.py",
    "ansa_mcp_bridge/operations/structures.py",
    "ansa_mcp_bridge/operations/visibility.py",
    "ansa_mcp_bridge/operations/volume_meshing.py",
    "ansa_mcp_bridge/platform/__init__.py",
    "ansa_mcp_bridge/platform/modal.py",
    "ansa_mcp_bridge/platform/mutex.py",
    "ansa_mcp_bridge/platform/timer.py",
    "ansa_mcp_bridge/ui/__init__.py",
    "ansa_mcp_bridge/ui/native_panel.py",
    "ansa_mcp_bridge/ui/panel.py",
    "ansa_mcp_shared/__init__.py",
    "ansa_mcp_shared/framing.py",
    "ansa_mcp_shared/identity.py",
    "ansa_mcp_shared/mesh_quality.py",
    "ansa_mcp_shared/quality_templates.py",
    "ansa_mcp_shared/operations.py",
    "ansa_mcp_shared/protocol.py",
    "ansa_mcp_shared/security.py",
)


class NativeReleaseError(RuntimeError):
    """原生发布 staging 不符合明确的内容边界。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file(relative_path: str) -> Path:
    path = PROJECT_ROOT / "src" / relative_path
    if path.is_symlink() or not path.is_file():
        raise NativeReleaseError("发布清单源码缺失或为符号链接: %s" % relative_path)
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to((PROJECT_ROOT / "src").resolve(strict=True)):
        raise NativeReleaseError("发布清单源码越出 src 目录: %s" % relative_path)
    return resolved


def stage_release(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    """生成供官方 Packager 使用的 PPL 与模块根目录，不生成 .bpkg。"""
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("拒绝覆盖已有原生 staging 目录: %s" % output_dir)
    output_dir = output_dir.resolve()
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("拒绝覆盖已有原生 staging 目录: %s" % output_dir)

    ppl_source = PROJECT_ROOT / PPL_SOURCE
    entry_source = PROJECT_ROOT / ENTRY_SOURCE
    license_source = PROJECT_ROOT / "LICENSE"
    if not ppl_source.is_file() or not entry_source.is_file():
        raise NativeReleaseError("缺少正式 PPL 或原生入口源文件")
    if license_source.is_symlink() or not license_source.is_file():
        raise NativeReleaseError("缺少正式 LICENSE 文件或该文件为符号链接")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=False, exist_ok=False)
    try:
        module_root = output_dir / PACKAGE_ROOT
        module_root.mkdir()
        (module_root / "__init__.py").write_text(
            '"""ANSA MCP Plugin 官方原生模块根包。"""\n',
            encoding="utf-8",
            newline="\n",
        )

        copied = [output_dir / "AnsaMcpPlugin.ppl"]
        shutil.copyfile(ppl_source, copied[0])
        license_target = output_dir / "LICENSE"
        shutil.copyfile(license_source, license_target)
        copied.append(license_target)
        entry_target = module_root / "AnsaMcpPlugin.py"
        shutil.copyfile(entry_source, entry_target)
        copied.append(entry_target)

        for relative_path in SOURCE_FILES:
            source = _source_file(relative_path)
            target = module_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target)
        copied.append(module_root / "__init__.py")

        files = [
            {
                "path": path.relative_to(output_dir).as_posix(),
                "sha256": _sha256(path),
            }
            for path in sorted(copied, key=lambda item: item.relative_to(output_dir).as_posix())
        ]
        manifest = {
            "format": 1,
            "package": "ansa-mcp-plugin-native",
            "version": VERSION,
            "module_root": PACKAGE_ROOT,
            "ppl": "AnsaMcpPlugin.ppl",
            "files": files,
        }
        (output_dir / "SHA256SUMS.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except BaseException:
        shutil.rmtree(output_dir)
        raise
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="新的原生暂存目录；拒绝覆盖已有目录")
    args = parser.parse_args()
    try:
        result = stage_release(args.output_dir)
    except (OSError, NativeReleaseError) as exc:
        print("原生 staging 失败: %s" % exc, file=sys.stderr)
        return 1

    print("native staging: %s" % result)
    print("manifest: %s" % (result / "SHA256SUMS.json"))
    print("请由官方 ANSA Packager 构建正式 .bpkg；本脚本不生成或伪造 .bpkg。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
