"""MCP tool registration for explicitly scoped mesh penetrations."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
ShellId = Annotated[int, Field(strict=True, ge=1)]
ShellIds = Annotated[list[ShellId], Field(min_length=1, max_length=200)]
Factor = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def register(server, submit):
    @server.tool(name="ansa_mesh_penetrations", structured_output=True)
    def ansa_mesh_penetrations(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        first_ids: ShellIds,
        second_ids: ShellIds,
        thickness_mode: Literal["property", "user"],
        factor: Factor,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查两组显式 NASTRAN CQUAD4/CTRIA3；每组 1–200 项。

        thickness_mode=property 时 factor 是 PSHELL 厚度倍率；user 时是用户厚度值，
        使用模型长度单位，必须为正有限数。两种模式均要求有效的正厚度 PSHELL；
        两组不能共享属性，因为本机原生厚度模式会忽略同属性壳对。
        仅检查组间，隐藏单元仍参与；分别返回相交、穿透和并集实体，原生计数不代表壳对数。
        不修复或改变显示、卡片和模型文件。
        """
        return submit(
            "mesh_penetrations",
            {
                "deck": deck,
                "first_ids": first_ids,
                "second_ids": second_ids,
                "thickness_mode": thickness_mode,
                "factor": factor,
            },
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    tool_manager = getattr(server, "_tool_manager", None)
    get_tool = getattr(tool_manager, "get_tool", None)
    tool = get_tool("ansa_mesh_penetrations") if callable(get_tool) else None
    if tool is None:
        raise RuntimeError("FastMCP strict argument validation is unavailable")
    argument_model = getattr(getattr(tool, "fn_metadata", None), "arg_model", None)
    if argument_model is None or not hasattr(argument_model, "model_config"):
        raise RuntimeError("FastMCP strict argument validation is unavailable")
    # FastMCP 默认忽略未知字段；拒绝额外的 fix 等写入参数。
    argument_model.model_config["extra"] = "forbid"
    argument_model.model_rebuild(force=True)
    tool.parameters = argument_model.model_json_schema(by_alias=True)


__all__ = ["register"]
