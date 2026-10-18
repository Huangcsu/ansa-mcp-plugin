"""MCP schema for explicitly scoped, read-only NASTRAN connection checks."""

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import AfterValidator, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
PointId = Annotated[int, Field(strict=True, ge=1)]
Distance = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def _unique_point_ids(value):
    if len(set(value)) != len(value):
        raise ValueError("point_ids must not contain duplicate IDs")
    return value


PointIds = Annotated[
    list[PointId], Field(strict=True, min_length=1, max_length=200),
    AfterValidator(_unique_point_ids),
]


def register(server, submit):
    @server.tool(name="ansa_connection_check", structured_output=True)
    def ansa_connection_check(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        point_ids: PointIds,
        distance: Distance = 0.1,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查显式选择的 1–200 个 NASTRAN `SpotweldPoint_Type`。

        仅启用原生重复连接检查，按 `distance`（当前模型长度单位）设定绝对间距；
        不接受全模型、可见集或修复选项。原生检查仍可能报告点焊点投影问题；
        任务成功只表示检查报告已生成，不代表问题数为零，也不会自动修复。
        """
        return submit(
            "connection_check",
            {"deck": deck, "point_ids": point_ids, "distance": distance},
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_connection_check")


__all__ = ["register"]
