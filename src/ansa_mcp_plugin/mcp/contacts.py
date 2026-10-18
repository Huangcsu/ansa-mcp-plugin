"""MCP schema for read-only ABAQUS contact inspection."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import AfterValidator, Field

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
Tolerance = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


def _unique_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("ids must not contain duplicates")
    return value


ContactIds = Annotated[
    list[EntityId],
    Field(strict=True, min_length=1, max_length=20),
    AfterValidator(_unique_ids),
]


def register(server, submit):
    @server.tool(name="ansa_contact_inspect", structured_output=True)
    def ansa_contact_inspect(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        relation_type: Literal["TIE", "CONTACT_PAIR"],
        ids: ContactIds,
        tolerance: Tolerance,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Read complete ABAQUS TIE or CONTACT_PAIR cards and bounded surface geometry."""
        params = {
            "deck": deck,
            "relation_type": relation_type,
            "ids": ids,
            "tolerance": tolerance,
        }
        encoded = json.dumps(
            params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES // 8:
            raise ValueError("contact inspection request exceeds the reply budget")
        return submit(
            "abaqus_contact_inspect",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_contact_inspect")


__all__ = ["register"]
