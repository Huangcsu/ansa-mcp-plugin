"""Read-only, explicitly scoped NASTRAN RBE2 dependency tool."""

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import AfterValidator, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
RigidId = Annotated[int, Field(strict=True, ge=1)]


def _unique_rigid_ids(value):
    if len(set(value)) != len(value):
        raise ValueError("rigid_ids must not contain duplicate IDs")
    return value


RigidIds = Annotated[
    list[RigidId], Field(min_length=1, max_length=200), AfterValidator(_unique_rigid_ids)
]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def _forbid_extra_arguments(server):
    manager = getattr(server, "_tool_manager", None)
    get_tool = getattr(manager, "get_tool", None)
    tool = get_tool("ansa_rigid_dependencies") if callable(get_tool) else None
    metadata = getattr(tool, "fn_metadata", None)
    argument_model = getattr(metadata, "arg_model", None)
    if argument_model is None or not callable(getattr(argument_model, "model_rebuild", None)):
        raise RuntimeError("FastMCP does not expose the argument model needed for strict input validation")
    argument_model.model_config = {**argument_model.model_config, "extra": "forbid"}
    if argument_model.model_rebuild(force=True) is not True:
        raise RuntimeError("FastMCP could not rebuild the strict argument schema")
    tool.parameters = argument_model.model_json_schema(by_alias=True)


def register(server, submit):
    @server.tool(name="ansa_rigid_dependencies", structured_output=True)
    def ansa_rigid_dependencies(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        rigid_ids: RigidIds,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查显式选择的 1-200 个 NASTRAN RBE2 刚性依赖，不自动修复。"""
        return submit(
            "rigid_dependencies",
            {"deck": deck, "rigid_ids": rigid_ids},
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server)


__all__ = ["register"]
