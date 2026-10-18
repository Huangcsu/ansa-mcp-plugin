"""MCP schema for a controlled, backup-protected benchmark file batch."""

import json
from pathlib import Path, PureWindowsPath
from typing import Annotated

from mcp.types import CallToolResult
from pydantic import AfterValidator, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
MaxCases = Annotated[int, Field(strict=True, ge=1, le=10)]


def _absolute_path(value, label, suffix=None):
    path = Path(value)
    parts = (*PureWindowsPath(value).parts, *path.parts)
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"{label} must not contain traversal components")
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if suffix is not None and path.suffix.casefold() != suffix:
        raise ValueError(f"{label} must end in {suffix}")
    return value


def _root_path(value):
    return _absolute_path(value, "root_path")


def _backup_path(value):
    return _absolute_path(value, "backup_path", ".ansa")


RootPath = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=4096),
    AfterValidator(_root_path),
]
BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=4096),
    AfterValidator(_backup_path),
]


def register(server, submit):
    @server.tool(name="ansa_benchmark_run", structured_output=True)
    def ansa_benchmark_run(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        root_path: RootPath,
        backup_path: BackupPath,
        max_cases: MaxCases = 10,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Inspect at most ten NASTRAN .ansa/.step/.stp files using a new backup. The response reports the actual current database path and whether reopening the backup was confirmed."""
        params = {"root_path": root_path, "backup_path": backup_path, "max_cases": max_cases}
        if len(json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 128 * 1024:
            raise ValueError("benchmark request exceeds the 128 KiB budget")
        return submit(
            "benchmark_run", params, operation_id, expected_target, expected_model, ttl_seconds
        )

    _forbid_extra_arguments(server, "ansa_benchmark_run")


__all__ = ["register"]
