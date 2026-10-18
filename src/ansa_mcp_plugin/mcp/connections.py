"""Bounded connection inventory and independently checked point creation."""

from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments

OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Position = Annotated[list[Number], Field(strict=True, min_length=3, max_length=3)]
PointType = Literal["SpotweldPoint_Type", "Bolt_Type", "GumDrop_Type", "Rivet_Type", "Screw_Type", "Clip_Type"]


class PartReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck: Deck
    type: Literal["ANSAPART"]
    id: Annotated[int, Field(strict=True, ge=1)]


def register(server, submit):
    @server.tool(structured_output=True)
    def ansa_connection_list(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck | None = None,
        kind: Literal["all", "connector", "point"] = "all",
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """只读列出 NASTRAN CONNECTOR_ENTITY 和六类连接点，按类型和 ID 稳定分页。

        kind 可筛选 connector 或 point；不含线连接、面连接或有限元连接单元。
        收集范围最多 10000 项；未能读取的名称、坐标、尺寸或连接归属保留错误和完整性，
        不把缺失信息当作零或空集合，不改变选择与显示。
        """
        params = {"kind": kind, "offset": offset, "limit": limit}
        if deck is not None:
            params["deck"] = deck
        return submit("connection_list", params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_connection_create(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        position: Position,
        parts: Annotated[list[PartReference], Field(strict=True, min_length=2, max_length=2)],
        diameter: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)],
        connection_type: PointType = "Bolt_Type",
        entity_id: Annotated[int, Field(strict=True, ge=0, le=2147483647)] = 0,
        name: Annotated[str, Field(strict=True, max_length=128)] | None = None,
        deck: Deck | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """创建一个连接两个不同 ANSAPART 的 NASTRAN 连接点，长度采用当前模型单位。

        六种类型见 connection_type；默认 Bolt_Type，entity_id=0 由 ANSA 分配。
        创建前核对 Part 身份和连接字符串，创建后独立回读身份、坐标、直径、名称及全部归属。
        仅创建连接定义；有限元实现须另行执行。创建后失败会保留已知实体和变化证据，
        不自动删除或重试。超时后查询原 operation_id，重复请求必须使用相同参数。
        """
        params = {"position": position, "parts": [part.model_dump() for part in parts],
                  "diameter": diameter, "connection_type": connection_type,
                  "entity_id": entity_id, "name": name}
        if deck is not None:
            params["deck"] = deck
        return submit("connection_create", params, operation_id, expected_target, expected_model, ttl_seconds)

    _forbid_extra_arguments(server, "ansa_connection_list")
    _forbid_extra_arguments(server, "ansa_connection_create")


__all__ = ["register"]
