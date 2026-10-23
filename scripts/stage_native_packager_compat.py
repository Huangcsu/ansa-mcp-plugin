"""基于原生 release staging 生成官方 Packager 的 GBK 兼容副本。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "build" / "native-0.1.3"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "build" / "native-0.1.3-packager-gbk"
MANIFEST_NAME = "PACKAGER_COMPAT_MANIFEST.json"
CODING_COOKIE = "# -*- coding: gbk -*-"
REPLACEMENTS = {"\u2022": "*"}
TEXT_SUFFIXES = frozenset({".py", ".ppl"})
_CODING_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-_.a-zA-Z0-9]+")


class PackagerCompatError(RuntimeError):
    """Packager 兼容副本无法在不改变语义的情况下生成。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def _newline_for(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _with_gbk_cookie(text: str) -> str:
    """把 coding cookie 放到首行；存在 shebang 时保留在第二行。"""
    lines = text.splitlines(keepends=True)
    newline = _newline_for(text)
    if lines and lines[0].startswith("#!"):
        if not _line_ending(lines[0]):
            lines[0] += newline
        if len(lines) > 1 and _CODING_COOKIE.match(lines[1]):
            lines[1] = CODING_COOKIE + (_line_ending(lines[1]) or newline)
        else:
            lines.insert(1, CODING_COOKIE + newline)
    elif lines and _CODING_COOKIE.match(lines[0]):
        lines[0] = CODING_COOKIE + (_line_ending(lines[0]) or newline)
    else:
        if len(lines) > 1 and _CODING_COOKIE.match(lines[1]):
            del lines[1]
        lines.insert(0, CODING_COOKIE + newline)
    return "".join(lines)


def _convert_text_file(source: Path, target: Path, relative: str) -> tuple[str, int]:
    try:
        text = source.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagerCompatError(
            "无法按 UTF-8 读取 %s: 字节偏移 %d" % (relative, exc.start)
        ) from exc

    replacement_count = text.count("\u2022")
    for source_character, target_character in REPLACEMENTS.items():
        text = text.replace(source_character, target_character)
    text = _with_gbk_cookie(text)
    try:
        payload = text.encode("gbk")
    except UnicodeEncodeError as exc:
        character = text[exc.start]
        raise PackagerCompatError(
            "无法将 %s 编码为 GBK: U+%04X %r 位于偏移 %d"
            % (relative, ord(character), character, exc.start)
        ) from exc
    target.write_bytes(payload)
    return "gbk", replacement_count


def _copy_tree(source: Path, output: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for current, directory_names, file_names in os.walk(source, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        current_target = output / current_path.relative_to(source)
        for directory_name in directory_names:
            path = current_path / directory_name
            if path.is_symlink():
                raise PackagerCompatError("拒绝复制符号链接目录: %s" % path)
            (current_target / directory_name).mkdir(parents=True, exist_ok=True)

        for file_name in file_names:
            path = current_path / file_name
            relative_path = path.relative_to(source)
            relative = relative_path.as_posix()
            if path.is_symlink() or not path.is_file():
                raise PackagerCompatError("拒绝复制符号链接或非普通文件: %s" % relative)
            target = output / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix.casefold() in TEXT_SUFFIXES:
                encoding, replacement_count = _convert_text_file(path, target, relative)
            else:
                shutil.copyfile(path, target)
                encoding, replacement_count = "unchanged", 0
            entries.append(
                {
                    "source": relative,
                    "target": relative,
                    "encoding": encoding,
                    "replacements": dict(REPLACEMENTS) if replacement_count else {},
                    "replacement_count": replacement_count,
                    "source_sha256": _sha256(path),
                    "sha256": _sha256(target),
                }
            )
    return sorted(entries, key=lambda item: str(item["target"]))


def stage_packager_compat(
    source_dir: Path = DEFAULT_SOURCE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    """复制原生 staging，并仅将 .py/.ppl 转为 GBK。"""
    source = Path(source_dir)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    if source.is_symlink() or not source.is_dir():
        raise PackagerCompatError("源 staging 目录不存在或为符号链接: %s" % source)
    source = source.resolve(strict=True)

    output = Path(output_dir)
    if not output.is_absolute():
        output = PROJECT_ROOT / output
    if output.exists() or output.is_symlink():
        raise FileExistsError("拒绝覆盖已有 Packager 兼容目录: %s" % output)
    output = output.resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError("拒绝覆盖已有 Packager 兼容目录: %s" % output)
    if (
        output == source
        or output.is_relative_to(source)
        or source.is_relative_to(output)
    ):
        raise PackagerCompatError("源目录与输出目录不能互相包含: %s -> %s" % (source, output))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=False, exist_ok=False)
    try:
        files = _copy_tree(source, output)
        manifest = {
            "format": 1,
            "source": source.as_posix(),
            "output": output.as_posix(),
            "source_encoding": "utf-8",
            "target_encoding": "gbk",
            "coding_cookie": CODING_COOKIE,
            "replacements": dict(REPLACEMENTS),
            "files": files,
        }
        (output / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except BaseException:
        shutil.rmtree(output)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR,
                        help="stage_native_release.py 已生成的目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="新的 GBK 兼容目录；拒绝覆盖已有目录")
    args = parser.parse_args()
    try:
        result = stage_packager_compat(args.source_dir, args.output_dir)
    except (OSError, PackagerCompatError) as exc:
        print("Packager GBK 兼容 staging 失败: %s" % exc, file=sys.stderr)
        return 1

    print("packager compatibility staging: %s" % result)
    print("manifest: %s" % (result / MANIFEST_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
