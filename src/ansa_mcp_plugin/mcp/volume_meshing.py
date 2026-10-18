"""MCP schema for guarded meshing of one existing NASTRAN VOLUME."""

from pathlib import Path
import json
from typing import Annotated, Any

from mcp.types import CallToolResult
from pydantic import AfterValidator, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments
from ansa_mcp_shared.framing import MAX_FRAME_BYTES


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
ExpectedFields = Annotated[dict[str, Any], Field(min_length=5, max_length=100)]


def _absolute_ansa_path(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.casefold() != ".ansa":
        raise ValueError("backup_path must be an absolute .ansa file path")
    return value


def _required_volume_fields(value):
    required = {"PID", "Type", "Order", "Status", "Frozen"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError("expected_fields must include: " + ", ".join(missing))
    return value


def _unique_shell_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("expected_shell_ids must not contain duplicate IDs")
    return value


BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_absolute_ansa_path),
]
ExpectedVolumeFields = Annotated[ExpectedFields, AfterValidator(_required_volume_fields)]
ShellIds = Annotated[
    list[EntityId],
    Field(min_length=1, max_length=200),
    AfterValidator(_unique_shell_ids),
]


def register(server, submit):
    @server.tool(name="ansa_mesh_volume", structured_output=True)
    def ansa_mesh_volume(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        volume_id: EntityId,
        expected_fields: ExpectedVolumeFields,
        expected_shell_ids: ShellIds,
        backup_path: BackupPath,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Mesh one existing unmeshed NASTRAN volume using TETRA RAPID and verify its bounded model changes."""
        params = {
            "deck": deck,
            "volume_id": volume_id,
            "expected_fields": expected_fields,
            "expected_shell_ids": expected_shell_ids,
            "backup_path": backup_path,
        }
        encoded = json.dumps(
            params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES // 8:
            raise ValueError("volume meshing request exceeds the 128 KiB budget")
        return submit(
            "mesh_volume",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_mesh_volume")


__all__ = ["register"]
