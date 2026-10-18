"""Read-only, explicitly scoped mesh-check tools."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult


OperationId = Annotated[str, Field(min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
ShellId = Annotated[int, Field(strict=True, ge=1)]
ShellIds = Annotated[list[ShellId], Field(min_length=1, max_length=200)]
Angle = Annotated[float, Field(strict=True, gt=0, lt=180, allow_inf_nan=False)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]


def register(server, submit):
    @server.tool(structured_output=True)
    def ansa_mesh_check(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        check: Literal["sharp_edges"],
        deck: Deck,
        entity_ids: ShellIds,
        angle: Angle = 120.0,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读检查 1–200 个显式 NASTRAN SHELL ID；目前仅支持 CQUAD4 和 CTRIA3。

        angle 默认 120.0，必须是有限数值且满足 0 < angle < 180。
        已验收的两壳共边夹具按法向夹角大于阈值报告：平面为 0°、直角折壳为 90°。
        桥接固定设置 sharp_edges_type=0、exclude_edges=0，只配置这三个选项；
        parameters() 中的 sharp_edges_check_triple 及三重边行为尚未验收。
        检查使用 EXEC_ON_SELECTED、REPORT_NONE、KEEP_OLD，不调用修复，不改变显示状态。
        原生报告计数不代表节点数；连接上下文最多 10000 个实体，报告树最多深度 8、节点 1000。
        """
        return submit(
            "mesh_check",
            {"check": check, "deck": deck, "entity_ids": entity_ids, "angle": angle},
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )


__all__ = ["register"]
