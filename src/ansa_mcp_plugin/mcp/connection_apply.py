"""MCP schema for one guarded NASTRAN SpotweldPoint realization."""

from pathlib import Path
import json
from typing import Annotated, Any

from mcp.types import CallToolResult
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
CardFields = Annotated[dict[str, Any], Field(strict=True, min_length=1, max_length=512)]

_POINT_FIELDS = frozenset({
    "id", "numofparts", "p1", "p2", "x", "y", "z", "d", "status", "fereptype",
    "frozenid", "frozendelete",
})


def _field_key(value):
    return "".join(char.casefold() for char in value if char.isalnum())


def _required_point_fields(value):
    missing = sorted(_POINT_FIELDS - {_field_key(name) for name in value})
    if missing:
        labels = {
            "id": "ID", "numofparts": "Num of Parts", "p1": "P1", "p2": "P2", "x": "X", "y": "Y",
            "z": "Z", "d": "D", "status": "Status", "fereptype": "FE Rep Type",
            "frozenid": "FROZEN_ID", "frozendelete": "FROZEN_DELETE",
        }
        raise ValueError(
            "expected_fields must include: "
            + ", ".join(sorted(labels[name] for name in missing))
        )
    return value


PointExpectedFields = Annotated[CardFields, AfterValidator(_required_point_fields)]


class TargetPart(BaseModel):
    """Provide complete stable cards; both PSHELLs may reference the same MAT1."""

    model_config = ConfigDict(extra="forbid", strict=True)

    part_id: EntityId
    part_expected_fields: CardFields
    shell_id: EntityId
    shell_expected_fields: CardFields
    property_id: EntityId
    property_expected_fields: CardFields
    material_id: EntityId
    material_expected_fields: CardFields


def _unique_target_parts(value):
    for name in ("part_id", "shell_id", "property_id"):
        identifiers = [getattr(item, name) for item in value]
        if len(set(identifiers)) != 2:
            raise ValueError(f"target_parts must use two distinct {name} values")
    return value


TargetParts = Annotated[
    list[TargetPart],
    Field(strict=True, min_length=2, max_length=2),
    AfterValidator(_unique_target_parts),
]


def _absolute_ansa_path(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.casefold() != ".ansa":
        raise ValueError("backup_path must be an absolute .ansa file path")
    return value


BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_absolute_ansa_path),
]


def register(server, submit):
    @server.tool(name="ansa_connection_apply", structured_output=True)
    def ansa_connection_apply(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        point_id: EntityId,
        expected_fields: PointExpectedFields,
        target_parts: TargetParts,
        backup_path: BackupPath,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """受控实现一个明确选择的 NASTRAN SpotweldPoint_Type，并独立回读模型范围。

        expected_fields 必须提供当前 SpotweldPoint_Type 的完整 card_fields，至少包含
        ID、Num of Parts、P1、P2、X、Y、Z、D、Status、FE Rep Type、FROZEN_ID、FROZEN_DELETE。
        target_parts 必须恰有两个不同 ANSAPART、SHELL 和 PSHELL，
        并分别提供 part_expected_fields、shell_id/shell_expected_fields、
        property_id/property_expected_fields、material_id/material_expected_fields。每份
        Part/SHELL/MAT1 条件须完整覆盖稳定 card_fields；PSHELL 条件覆盖除派生
        Num.Elem 外的全部字段。两个目标 PSHELL 可以共享同一 MAT1，但每份 MAT1
        稳定卡条件仍须完整且匹配当前定义。两个 Part 各自必须只含一个未冻结的轴对齐
        XY 矩形 CQUAD4，且由正厚度 PSHELL 和已定义 MAT1 支持。CQUAD4 的 T1-T4
        必须是有限零值，TFLAG 必须为空白默认；其他厚度设置会在创建备份前拒绝。
        MAT1 的 RHO 必须正有限、PSHELL 的 NSM 必须为空或有限零；目标 Part 的面积、
        质量、质心及原生节点集中质量惯量须与明确的源网格和实现后扇形分别一致。
        当前目标源壳不能已有关联 FE PERIMETER；实现后完整回读八个新增单侧边界，
        其关联壳须恰好覆盖两个目标的八个三角形，已有范围外边界保持不变。

        仅接受新建的绝对 .ansa 备份路径。工具只调用一次 RealizeConnections，
        固定为已核实的九项 RBAR 设置；不接受任意 realization 参数或隐式全模型目标。
        RealizeConnections 未返回严格 int 1 时，返回有界诊断摘要并保留可用的回读证据。
        """
        params = {
            "deck": deck,
            "point_id": point_id,
            "expected_fields": expected_fields,
            "target_parts": [
                item.model_dump(mode="json") for item in target_parts
            ],
            "backup_path": backup_path,
        }
        encoded = json.dumps(
            params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES // 8:
            raise ValueError("connection apply request exceeds the 128 KiB budget")
        return submit(
            "connection_apply",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_connection_apply")


__all__ = ["register", "TargetPart", "TargetParts", "PointExpectedFields"]
