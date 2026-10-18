"""MCP schema for the controlled NASTRAN CBAR-to-CROD operation."""

from pathlib import Path
import json
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments
from ansa_mcp_shared.framing import MAX_FRAME_BYTES


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
ExpectedFields = Annotated[dict[str, Any], Field(min_length=3, max_length=100)]
ExpectedPropertyFields = Annotated[dict[str, Any], Field(min_length=5, max_length=100)]


def _absolute_ansa_path(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.casefold() != ".ansa":
        raise ValueError("backup_path must be an absolute .ansa file path")
    return value


BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_absolute_ansa_path),
]


class ElementCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def require_source_fields(self):
        required = {"PID", "GA", "GB"}
        if not required.issubset(self.expected_fields):
            raise ValueError("expected_fields must include PID, GA, and GB")
        return self


def _unique_elements(value):
    ids = [item.entity_id for item in value]
    if len(ids) != len(set(ids)):
        raise ValueError("elements must not contain duplicate entity IDs")
    return value


Elements = Annotated[
    list[ElementCondition],
    Field(min_length=1, max_length=100),
    AfterValidator(_unique_elements),
]


def register(server, submit):
    @server.tool(name="ansa_element_change_type", structured_output=True)
    def ansa_element_change_type(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        source_type: Literal["CBAR"],
        target_type: Literal["CROD"],
        elements: Elements,
        target_property_id: EntityId,
        expected_property_fields: ExpectedPropertyFields,
        backup_path: BackupPath,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Convert explicitly conditioned NASTRAN CBAR elements to CROD using an existing PROD and a verified model backup."""
        required = {"MID", "A", "J", "C", "NSM"}
        if not required.issubset(expected_property_fields):
            raise ValueError(
                "expected_property_fields must include MID, A, J, C, and NSM"
            )
        params = {
            "deck": deck,
            "source_type": source_type,
            "target_type": target_type,
            "elements": [item.model_dump(mode="json") for item in elements],
            "target_property_id": target_property_id,
            "expected_property_fields": expected_property_fields,
            "backup_path": backup_path,
        }
        encoded = json.dumps(params, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES // 8:
            raise ValueError("element conversion request exceeds the reply budget")
        return submit(
            "element_change_type",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_element_change_type")


__all__ = ["register"]
