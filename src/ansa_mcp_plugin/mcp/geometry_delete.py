"""Explicit FACE deletion using freshly read geometry and card conditions."""

from typing import Annotated, Any

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from .geometry import Coordinate, Deck, FaceId, OperationId, TTL
from .schemas import ExpectedModel, ExpectedTarget, ToolResult


class FaceGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    center: Annotated[list[Coordinate], Field(min_length=3, max_length=3)]
    bounds: Annotated[list[Coordinate], Field(min_length=6, max_length=6)]
    normal: Annotated[list[Coordinate], Field(min_length=3, max_length=3)]


class FaceDeleteCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: FaceId
    expected_fields: dict[str, Any] = Field(min_length=4, max_length=4)
    expected_geometry: FaceGeometry
    expected_cons_ids: Annotated[list[FaceId], Field(min_length=1, max_length=1000)]


FaceConditions = Annotated[list[FaceDeleteCondition], Field(min_length=1, max_length=100)]


def register(server, submit):
    @server.tool(structured_output=True)
    def ansa_geometry_delete(operation_id: OperationId, expected_target: ExpectedTarget,
                             expected_model: ExpectedModel, deck: Deck, faces: FaceConditions,
                             ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Delete 1-100 explicitly supplied isolated, unfrozen and unmeshed NASTRAN FACEs after checking every fresh condition. Obtain conditions with ansa_geometry_preview(include_delete_conditions=True). Never execute a query again to choose a replacement FACE. Uses force=False/compress=False and verifies outside FACEs, boundary CONS and protected entity identities. Failure preserves scope evidence and may have effects; inspect the original task before retrying. Save a recoverable model copy before deletion."""
        return submit("geometry_delete", {"deck": deck, "faces": [face.model_dump(mode="json") for face in faces]},
                      operation_id, expected_target, expected_model, ttl_seconds)
