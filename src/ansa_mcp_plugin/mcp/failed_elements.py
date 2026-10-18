"""Read-only, explicitly scoped MeshQuality failure statistics."""

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
    @server.tool(structured_output=True)
    def ansa_mesh_failed_elements(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        entity_ids: ShellIds,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """统计 1–200 个显式 NASTRAN CQUAD4/CTRIA3 单元的 MeshQuality 失败数。

        仅使用当前 ANSA 会话中的 MeshQuality 准则；结果为 0 仅说明本次显式范围
        未报告失败单元，不代表全工程质量检查通过；准则全部关闭时也可能为零。
        结果包含实际全局准则文本和 SHA-256。操作静默、只读且不执行修复。
        """
        return submit(
            "mesh_failed_elements",
            {"deck": deck, "entity_ids": entity_ids},
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )


__all__ = ["register"]
