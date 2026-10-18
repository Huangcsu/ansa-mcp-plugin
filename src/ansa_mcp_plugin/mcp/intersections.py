"""Read-only, explicitly scoped mesh intersection tools."""

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
ShellId = Annotated[int, Field(strict=True, ge=1)]
ShellIds = Annotated[list[ShellId], Field(min_length=1, max_length=200)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def register(server, submit):
    @server.tool(name="ansa_mesh_intersections", structured_output=True)
    def ansa_mesh_intersections(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        first_ids: ShellIds,
        second_ids: ShellIds,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查两组显式 NASTRAN SHELL ID 的交叉相交；每组 1–200 个 CQUAD4/CTRIA3。"""
        return submit(
            "mesh_intersections",
            {"deck": deck, "first_ids": first_ids, "second_ids": second_ids},
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    tool_manager = getattr(server, "_tool_manager", None)
    get_tool = getattr(tool_manager, "get_tool", None)
    tool = get_tool("ansa_mesh_intersections") if callable(get_tool) else None
    if tool is None:
        raise RuntimeError("FastMCP strict argument validation is unavailable")
    argument_model = getattr(getattr(tool, "fn_metadata", None), "arg_model", None)
    if argument_model is None or not hasattr(argument_model, "model_config"):
        raise RuntimeError("FastMCP strict argument validation is unavailable")
    # FastMCP's generated argument model otherwise ignores unknown input fields.
    argument_model.model_config["extra"] = "forbid"
    argument_model.model_rebuild(force=True)
    tool.parameters = argument_model.model_json_schema(by_alias=True)


__all__ = ["register"]
