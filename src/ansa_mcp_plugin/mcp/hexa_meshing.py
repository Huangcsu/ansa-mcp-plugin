"""MCP schema for guarded ABAQUS Map-volume HEXA8 meshing."""

from pathlib import Path
import json
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
ExpectedFields = Annotated[dict[str, Any], Field(min_length=1, max_length=100)]
Steps = Annotated[int, Field(strict=True, ge=1, le=10000)]
def _raw_number(value):
    if type(value) not in (int, float) or type(value) is bool:
        raise ValueError("target_length must be a raw number")
    return value


TargetLength = Annotated[
    float,
    BeforeValidator(_raw_number),
    Field(gt=0, allow_inf_nan=False),
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


class RegionCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["SHELL"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required_fields(self):
        required = {"PID", "G1", "G2", "G3", "G4"}
        missing = sorted(required - self.expected_fields.keys())
        if missing:
            raise ValueError(
                "expected_fields must include: " + ", ".join(missing)
            )
        return self


class PartCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deck: Deck
    type: Literal["ANSAPART"]
    id: EntityId
    expected_fields: ExpectedFields


class PropertyCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deck: Deck
    type: Literal["SOLID_SECTION"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required_mid(self):
        if "MID" not in self.expected_fields:
            raise ValueError("expected_fields must include MID")
        return self


Regions = Annotated[list[RegionCondition], Field(min_length=1, max_length=200)]


def _check_region_scope(master, slave, round):
    keys = set()
    kinds = set()
    total = 0
    for name, values in (("master", master), ("slave", slave), ("round", round)):
        for item in values:
            key = (item.type, item.id)
            if key in keys:
                raise ValueError(
                    "master, slave and round must not contain duplicate entity references"
                )
            keys.add(key)
            kinds.add(item.type)
        total += len(values)
    if len(kinds) != 1:
        raise ValueError(
            "master, slave and round must use SHELL entities only in this batch"
        )
    if total > 400:
        raise ValueError("master, slave and round may contain at most 400 entities")


def register(server, submit):
    @server.tool(name="ansa_mesh_hex_map", structured_output=True)
    def ansa_mesh_hex_map(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        master: Regions,
        slave: Regions,
        round: Regions,
        property: PropertyCondition,
        backup_path: BackupPath,
        method: Literal["Normal parts", "Thin parts"] = "Normal parts",
        steps: Steps | None = None,
        target_length: TargetLength | None = None,
        part: PartCondition | None = None,
        light_volume_representation: Annotated[bool, Field(strict=True)] = False,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """以显式 SHELL master、slave、round 区域创建并网格化 ABAQUS Map volume。

        本批仅接受 SHELL 区域，FACE 会在写前被拒绝。`steps` 与 `target_length`
        必须且只能提供一个；target_length 只支持 master/slave 平面平行且法向
        指向直扫掠轴的情形，其他几何必须使用明确 steps。核心成功条件为独立回读
        新 VOLUME 的 Type/Status/Method/Map steps/PID/Hexas/Grids/Volume，并逐项
        核对新增 HEXA8、节点使用、SOLID_SECTION 属性以及 VOLUME 边界/实体成员。
        执行前必须保存到新的绝对 .ansa 备份；失败结果保留已取得的模型快照和
        部分效果标记，调用方须先核对原任务再决定后续写入。
        """
        if (steps is None) == (target_length is None):
            raise ValueError("provide exactly one of steps or target_length")
        if part is not None and part.deck != deck:
            raise ValueError("part.deck must match deck")
        if property.deck != deck:
            raise ValueError("property.deck must match deck")
        _check_region_scope(master, slave, round)

        params = {
            "deck": deck,
            "master": [item.model_dump(mode="json") for item in master],
            "slave": [item.model_dump(mode="json") for item in slave],
            "round": [item.model_dump(mode="json") for item in round],
            "method": method,
            "part": part.model_dump(mode="json") if part is not None else None,
            "property": property.model_dump(mode="json"),
            "light_volume_representation": light_volume_representation,
            "backup_path": backup_path,
        }
        if steps is not None:
            params["steps"] = steps
        if target_length is not None:
            params["target_length"] = target_length
        encoded = json.dumps(
            params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES // 8:
            raise ValueError("mesh_hex_map request exceeds the 128 KiB budget")
        return submit(
            "mesh_hex_map",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_mesh_hex_map")


__all__ = ["register"]
