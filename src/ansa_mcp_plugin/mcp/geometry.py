"""Bounded, read-only FACE ranking and deletion previews."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
TTL = Annotated[int, Field(ge=0, le=86400)]
FaceId = Annotated[int, Field(strict=True, ge=1)]
FaceIds = Annotated[list[FaceId], Field(min_length=1, max_length=10000)]
Coordinate = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Point = Annotated[list[Coordinate], Field(min_length=3, max_length=3)]
Limit = Annotated[int, Field(strict=True, ge=1, le=200)]
Deck = Annotated[int, Field(strict=True, ge=0)]
Query = Literal[
    "largest_area",
    "smallest_area",
    "highest_z",
    "lowest_z",
    "nearest_center",
    "normal_pos_z",
    "normal_neg_z",
]


def register(server, submit):
    @server.tool(structured_output=True)
    def ansa_geometry_select(operation_id: OperationId, expected_target: ExpectedTarget,
                             expected_model: ExpectedModel, deck: Deck, query: Query,
                             face_ids: FaceIds | None = None, point: Point | None = None,
                             limit: Limit = 5, ttl_seconds: TTL = 300
                             ) -> Annotated[CallToolResult, ToolResult]:
        """Rank FACE candidates by area, centroid Z, centroid distance, or normalized face-normal Z. normal_pos_z sorts the unit normal's Z component descending; normal_neg_z sorts it ascending. Both include all signs, return the unit normal on each candidate, and use ascending FACE ID for ties. Read-only: no ANSA selection or visibility changes. Omitted face_ids scans the whole model, rejecting more than 10,000 candidates; an explicit empty list is invalid. nearest_center requires point; other queries reject it. Values use current model units."""
        return submit("geometry_select", {"deck": deck, "query": query, "face_ids": face_ids,
                                           "point": point, "limit": limit},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_geometry_preview(operation_id: OperationId, expected_target: ExpectedTarget,
                              expected_model: ExpectedModel, deck: Deck, query: Query,
                              face_ids: FaceIds | None = None, point: Point | None = None,
                              limit: Limit = 5,
                              include_delete_conditions: Annotated[bool, Field(strict=True)] = False,
                              ttl_seconds: TTL = 300
                              ) -> Annotated[CallToolResult, ToolResult]:
        """Return ranked FACE candidates, proposed deletion IDs, area and bounds without model or UI changes. normal_pos_z sorts normalized face-normal Z descending; normal_neg_z sorts it ascending, returning each unit normal with all signs included and FACE ID as the tie-breaker. include_delete_conditions adds prepared deletion conditions and limits limit to 100. Preview grants no write authority: an actual deletion is a separate operation requiring freshly read preconditions. nearest_center means distance to the FACE centroid, not to its surface."""
        if include_delete_conditions and limit > 100:
            raise ValueError("limit must be at most 100 when include_delete_conditions is true")
        return submit("geometry_preview", {"deck": deck, "query": query, "face_ids": face_ids,
                                            "point": point, "limit": limit,
                                            "include_delete_conditions": include_delete_conditions},
                      operation_id, expected_target, expected_model, ttl_seconds)
