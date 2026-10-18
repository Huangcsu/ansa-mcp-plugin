"""MCP schemas for controlled ABAQUS kinematics operations."""

from __future__ import annotations

import json
import math
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

from mcp.types import CallToolResult
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from .schemas import ExpectedModel, ExpectedTarget, ToolResult, _forbid_extra_arguments


OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
TTL = Annotated[int, Field(strict=True, ge=0, le=86400)]
Deck = Annotated[int, Field(strict=True, ge=1)]
EntityId = Annotated[int, Field(strict=True, ge=1)]
ActuatorId = Annotated[int, Field(strict=True, ge=1)]
StrictBool = Annotated[bool, Field(strict=True)]


def _nonempty_name(value):
    if value != value.strip():
        raise ValueError("name must not have leading or trailing whitespace")
    return value


Name = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=128),
    AfterValidator(_nonempty_name),
]


def _absolute_ansa_path(value):
    path = Path(value)
    parts = (*PureWindowsPath(value).parts, *path.parts)
    if any(part in (".", "..") for part in parts):
        raise ValueError("backup_path must not contain traversal components")
    if not path.is_absolute() or path.suffix.casefold() != ".ansa":
        raise ValueError("backup_path must be an absolute .ansa file path")
    return value


BackupPath = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=4096),
    AfterValidator(_absolute_ansa_path),
]


def _finite_vector(value, label):
    if len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    return value


def _anchor(value):
    return _finite_vector(value, "anchor")


def _axis(value):
    _finite_vector(value, "axis")
    scale = max(abs(component) for component in value)
    if scale <= 1.0e-12:
        raise ValueError("axis must be a non-zero vector")
    return value


def _normalized_axis(value):
    scale = max(abs(component) for component in value)
    scaled = [component / scale for component in value]
    length = math.hypot(*scaled)
    if not math.isfinite(length) or length <= 1.0e-12:
        raise ValueError("axis could not be normalized safely")
    return [component / length for component in scaled]


FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Vector3 = Annotated[list[FiniteFloat], Field(min_length=3, max_length=3)]
Anchor = Annotated[Vector3, AfterValidator(_anchor)]
NonzeroAxis = Annotated[Vector3, AfterValidator(_axis)]


class MemberReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deck: Deck
    type: Literal[
        "NODE", "SHELL", "SOLID", "SECTION", "SHELL_SECTION", "SOLID_SECTION",
        "ANSAPART",
    ]
    id: EntityId


def _unique_members(value):
    keys = [(item.type, item.id) for item in value]
    if len(keys) != len(set(keys)):
        raise ValueError("members must not contain duplicate references")
    return value


Members = Annotated[
    list[MemberReference],
    Field(max_length=200),
    AfterValidator(_unique_members),
]


def _unique_joint_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("joint_ids must not contain duplicates")
    return value


JointIds = Annotated[
    list[EntityId],
    Field(min_length=1, max_length=100),
    AfterValidator(_unique_joint_ids),
]


def _unique_body_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("body_ids must not contain duplicates")
    return value


def _unique_inspect_joint_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("joint_ids must not contain duplicates")
    return value


def _unique_config_ids(value):
    if len(value) != len(set(value)):
        raise ValueError("config_ids must not contain duplicates")
    return value


BodyIds = Annotated[
    list[EntityId],
    Field(max_length=200),
    AfterValidator(_unique_body_ids),
]
InspectJointIds = Annotated[
    list[EntityId],
    Field(max_length=200),
    AfterValidator(_unique_inspect_joint_ids),
]
ConfigIds = Annotated[
    list[EntityId],
    Field(max_length=100),
    AfterValidator(_unique_config_ids),
]


def _submit(submit, tool, params, operation_id, target, model, ttl):
    encoded = json.dumps(
        params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES // 8:
        raise ValueError("kinematics request exceeds the reply budget")
    return submit(tool, params, operation_id, target, model, ttl)


def register(server, submit):
    @server.tool(name="ansa_kinematics_body_create", structured_output=True)
    def ansa_kinematics_body_create(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        name: Name,
        ground: StrictBool,
        members: Members,
        backup_path: BackupPath,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Create one ABAQUS KIN_BODY with explicit NODE/SHELL/SOLID/SECTION/SHELL_SECTION/SOLID_SECTION/ANSAPART members and a verified new backup."""
        if type(ground) is not bool:
            raise ValueError("ground must be a boolean")
        if not ground and not members:
            raise ValueError("non-ground bodies require at least one member")
        if ground and members:
            raise ValueError("ground bodies must have an empty members list")
        if any(item.deck != deck for item in members):
            raise ValueError("every member deck must match the requested deck")
        params = {
            "deck": deck,
            "name": name,
            "ground": ground,
            "members": [item.model_dump(mode="json") for item in members],
            "backup_path": backup_path,
        }
        return _submit(
            submit, "kinematics_body_create", params, operation_id,
            expected_target, expected_model, ttl_seconds,
        )

    @server.tool(name="ansa_kinematics_joint_create", structured_output=True)
    def ansa_kinematics_joint_create(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        name: Name,
        joint_type: Literal["REVOLUTE", "SLIDER"],
        body1_id: EntityId,
        body2_id: EntityId,
        anchor: Anchor,
        axis: NonzeroAxis,
        backup_path: BackupPath,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Create one ABAQUS KIN_JOINT between two distinct KIN_BODY entities; the axis is normalized before creation."""
        if body1_id == body2_id:
            raise ValueError("body1_id and body2_id must be different")
        normalized = _normalized_axis(axis)
        params = {
            "deck": deck,
            "name": name,
            "joint_type": joint_type,
            "body1_id": body1_id,
            "body2_id": body2_id,
            "anchor": anchor,
            "axis": normalized,
            "backup_path": backup_path,
        }
        return _submit(
            submit, "kinematics_joint_create", params, operation_id,
            expected_target, expected_model, ttl_seconds,
        )

    @server.tool(name="ansa_kinematics_config_create", structured_output=True)
    def ansa_kinematics_config_create(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        name: Name,
        joint_ids: JointIds,
        backup_path: BackupPath,
        actuator_joint_id: ActuatorId | None = None,
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Create one ABAQUS KIN_CONFIG with fixed mm/tonne/newton/sec/deg units and an optional zero-pose actuator joint."""
        if actuator_joint_id is not None and actuator_joint_id not in joint_ids:
            raise ValueError("actuator_joint_id must belong to joint_ids")
        params = {
            "deck": deck,
            "name": name,
            "joint_ids": joint_ids,
            "actuator_joint_id": actuator_joint_id,
            "backup_path": backup_path,
        }
        return _submit(
            submit, "kinematics_config_create", params, operation_id,
            expected_target, expected_model, ttl_seconds,
        )

    @server.tool(name="ansa_kinematics_inspect", structured_output=True)
    def ansa_kinematics_inspect(
        operation_id: OperationId,
        expected_target: ExpectedTarget,
        expected_model: ExpectedModel,
        deck: Deck,
        config_id: EntityId | None = None,
        body_ids: BodyIds = [],
        joint_ids: InspectJointIds = [],
        config_ids: ConfigIds = [],
        ttl_seconds: TTL = 300,
    ) -> Annotated[CallToolResult, ToolResult]:
        """Read one complete KIN_CONFIG closure or bounded KIN_BODY/KIN_JOINT/KIN_CONFIG IDs using every current card_fields label."""
        if config_id is not None:
            if body_ids or joint_ids or config_ids:
                raise ValueError(
                    "config_id cannot be combined with body_ids, joint_ids, or config_ids"
                )
        elif not (body_ids or joint_ids or config_ids):
            raise ValueError("provide config_id or at least one bounded ID list")
        params = {
            "deck": deck,
            "config_id": config_id,
            "body_ids": body_ids,
            "joint_ids": joint_ids,
            "config_ids": config_ids,
        }
        return _submit(
            submit, "kinematics_inspect", params, operation_id,
            expected_target, expected_model, ttl_seconds,
        )

    for tool_name in (
        "ansa_kinematics_body_create",
        "ansa_kinematics_joint_create",
        "ansa_kinematics_config_create",
        "ansa_kinematics_inspect",
    ):
        _forbid_extra_arguments(server, tool_name)


__all__ = ["register"]
