"""MCP schema for four narrowly scoped ABAQUS mesh operations."""

from pathlib import Path
import json
import re
from typing import Annotated, Any, Literal, Union

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
Steps = Annotated[int, Field(strict=True, ge=1, le=1000)]


def _absolute_ansa_path(value):
    path = Path(value)
    if not path.is_absolute() or path.suffix.casefold() != ".ansa":
        raise ValueError("backup_path must be an absolute .ansa file path")
    return value


def _raw_number(value):
    if type(value) not in (int, float):
        raise ValueError("coordinates must be raw finite numbers")
    return value


def _unique_conditions(value):
    keys = [(item.type, item.id) for item in value]
    if len(keys) != len(set(keys)):
        raise ValueError("entity conditions must not contain duplicate IDs")
    return value


def _required_fields(fields, required, label):
    missing = sorted(set(required) - fields.keys())
    if missing:
        raise ValueError(f"{label} expected_fields must include: " + ", ".join(missing))
    return fields


BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_absolute_ansa_path),
]
Coordinate = Annotated[
    float,
    BeforeValidator(_raw_number),
    Field(allow_inf_nan=False),
]
Point = tuple[Coordinate, Coordinate, Coordinate]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShellCondition(_StrictModel):
    type: Literal["SHELL"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(
            self.expected_fields, ("type", "PID", "G1", "G2", "G3", "G4"), "SHELL"
        )
        return self


class SolidCondition(_StrictModel):
    type: Literal["SOLID"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(
            self.expected_fields,
            ("type", "EID", "PID", *(f"G{i}" for i in range(1, 9))),
            "SOLID",
        )
        return self


class NodeCondition(_StrictModel):
    type: Literal["NODE"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(self.expected_fields, ("X", "Y", "Z"), "NODE")
        return self


class PartCondition(_StrictModel):
    deck: Deck
    type: Literal["ANSAPART"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(self.expected_fields, ("Name",), "ANSAPART")
        return self


class PropertyCondition(_StrictModel):
    deck: Deck
    type: Literal["SOLID_SECTION"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(self.expected_fields, ("MID",), "SOLID_SECTION")
        return self


MeshElement = Annotated[Union[ShellCondition, SolidCondition], Field(discriminator="type")]
MeshElements = Annotated[
    list[MeshElement],
    Field(min_length=1, max_length=200),
    AfterValidator(_unique_conditions),
]


class ExtrudeQuadRequest(_StrictModel):
    action: Literal["extrude_quad"]
    deck: Deck
    backup_path: BackupPath
    shells: Annotated[list[ShellCondition], Field(min_length=1, max_length=200)]
    vector: Point
    steps: Steps
    part: PartCondition
    property: PropertyCondition

    @model_validator(mode="after")
    def _scope(self):
        _unique_conditions(self.shells)
        if self.part.deck != self.deck or self.property.deck != self.deck:
            raise ValueError("part.deck and property.deck must match deck")
        if len(self.shells) * self.steps > 10000:
            raise ValueError("extrusion is limited to 10000 new SOLIDs")
        if sum(value * value for value in self.vector) <= 0:
            raise ValueError("vector must have positive finite length")
        return self


class Plane(_StrictModel):
    point1: Point
    point2: Point
    point3: Point


class MirrorCopyRequest(_StrictModel):
    action: Literal["mirror_copy"]
    deck: Deck
    backup_path: BackupPath
    sources: MeshElements
    plane: Plane
    part: PartCondition

    @model_validator(mode="after")
    def _deck_matches(self):
        if self.part.deck != self.deck:
            raise ValueError("part.deck must match deck")
        return self


class NodePair(_StrictModel):
    source: NodeCondition
    target: NodeCondition

    @model_validator(mode="after")
    def _distinct(self):
        if self.source.id == self.target.id:
            raise ValueError("node pair source and target IDs must differ")
        return self


class ReleaseShellCondition(_StrictModel):
    type: Literal["SHELL"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(self.expected_fields, ("type", "PID"), "SHELL")
        node_fields = [
            name for name in self.expected_fields if re.fullmatch(r"(?:G|N)\d+", name, re.I)
        ]
        if len(node_fields) < 3:
            raise ValueError("SHELL release preconditions must include at least three node card fields")
        return self


class ReleaseSolidCondition(_StrictModel):
    type: Literal["SOLID"]
    id: EntityId
    expected_fields: ExpectedFields

    @model_validator(mode="after")
    def _required(self):
        _required_fields(
            self.expected_fields, ("type", "EID", "PID"), "SOLID"
        )
        node_fields = [
            name for name in self.expected_fields if re.fullmatch(r"(?:G|N)\d+", name, re.I)
        ]
        if len(node_fields) < 4:
            raise ValueError("SOLID release preconditions must include at least four node card fields")
        return self


ReleaseElement = Annotated[
    Union[ReleaseShellCondition, ReleaseSolidCondition], Field(discriminator="type")
]
ReleaseElements = Annotated[
    list[ReleaseElement],
    Field(min_length=1, max_length=200),
    AfterValidator(_unique_conditions),
]


class MergeNodesRequest(_StrictModel):
    action: Literal["merge_nodes"]
    deck: Deck
    backup_path: BackupPath
    pairs: Annotated[list[NodePair], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _disjoint(self):
        sources = [pair.source.id for pair in self.pairs]
        targets = [pair.target.id for pair in self.pairs]
        if len(sources) != len(set(sources)) or len(targets) != len(set(targets)):
            raise ValueError("merge source and target node IDs must each be unique")
        if set(sources) & set(targets):
            raise ValueError("merge source and target node IDs must be disjoint")
        return self


class ReleaseMeshRequest(_StrictModel):
    action: Literal["release_mesh"]
    deck: Deck
    backup_path: BackupPath
    entities: ReleaseElements


ActionRequest = Annotated[
    Union[ExtrudeQuadRequest, MirrorCopyRequest, MergeNodesRequest, ReleaseMeshRequest],
    Field(discriminator="action"),
]


def register(server, submit):
    @server.tool(name="ansa_mesh_workflow", structured_output=True)
    def ansa_mesh_workflow(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        request: ActionRequest,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """执行一个ABAQUS显式范围网格操作；原生写入失败后不自动重放。"""
        params = request.model_dump(mode="json")
        encoded = json.dumps(
            params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES // 8:
            raise ValueError("mesh workflow request exceeds the 128 KiB budget")
        return submit(
            "mesh_workflow",
            params,
            operation_id,
            expected_target,
            expected_model,
            ttl_seconds,
        )

    _forbid_extra_arguments(server, "ansa_mesh_workflow")


__all__ = ["register"]
