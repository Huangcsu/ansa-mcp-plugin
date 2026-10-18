"""Context-checked, explicitly scoped model checks."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult

OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
NodeId = Annotated[int, Field(strict=True, ge=1)]
NodeIds = Annotated[list[NodeId], Field(min_length=1, max_length=200)]
MassFree = Annotated[bool, Field(strict=True)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def register(server, submit):
    @server.tool(structured_output=True)
    def ansa_check_run(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        check: Literal["free_nodes"],
        deck: Deck,
        entity_ids: NodeIds,
        consider_nodes_with_mass_free: MassFree = True,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Check 1-200 explicit NASTRAN GRID IDs using the modern FreeNodes API.

        Whole-model and implicit visible scopes are not accepted. A connection to a hidden element
        still counts as a connection. NodesToElements supplies attached elements/BCs and their node
        context (at most 10,000 entities); reports retain requested nodes and their related entities.
        consider_nodes_with_mass_free is a strict bool (default True);
        mass-only nodes are reported when True. Scalar nodes are excluded; other native policies are
        included in the result options. Returns bounded report trees, leaf issue counts and sorted
        failed_entity_ids for this scope. Model-read-only: no fixes, deletion, selection, visibility
        or model-file writes; existing check history is kept. Missing APIs or invalid report data
        fail explicitly. Inspect issue_counts and failed_entity_ids; task success means the check ran.
        """
        return submit(
            "check_run",
            {"check": check, "deck": deck, "entity_ids": entity_ids,
             "consider_nodes_with_mass_free": consider_nodes_with_mass_free},
            operation_id, expected_target, expected_model, ttl_seconds,
        )
