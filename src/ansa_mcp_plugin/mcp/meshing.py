"""Mesh faces and remove explicitly selected shell entities."""

from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import ExpectedModel, ExpectedTarget, ToolError, ToolResult

OperationId = Annotated[str, Field(min_length=1, max_length=128)]
TTL = Annotated[int, Field(ge=0, le=86400)]
EntityId = Annotated[int, Field(ge=1, strict=True)]
FaceIds = Annotated[list[EntityId], Field(min_length=1, max_length=100)]
ExpectedFields = Annotated[dict[str, Any], Field(min_length=1)]
TargetLength = Annotated[float, Field(gt=0, allow_inf_nan=False)]
SettingValue = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
AbsoluteLength = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


class FaceMeshCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: EntityId
    expected_fields: ExpectedFields
    expected_macro_id: EntityId
    expected_shell_ids: Annotated[list[EntityId], Field(max_length=2000)]


class ShellCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: EntityId
    expected_fields: ExpectedFields


class MeshSettingsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mesh_change_option: SettingValue
    element_type: SettingValue
    element_order: SettingValue
    target_element_length: SettingValue


class MeshSettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_length: AbsoluteLength | None = None
    element_type: Literal["tria", "quad", "mixed"] | None = None
    element_order: Literal["1st Order", "2nd Order"] | None = None

    @model_validator(mode="after")
    def require_change(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("explicit settings values must not be null")
        if not self.model_dump(exclude_none=True):
            raise ValueError("settings must contain at least one supported change")
        return self


FaceConditions = Annotated[list[FaceMeshCondition], Field(min_length=1, max_length=100)]
ShellConditions = Annotated[list[ShellCondition], Field(min_length=1, max_length=200)]


class SurfaceSeedScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    face_ids: Annotated[list[EntityId], Field(min_length=1, max_length=20)]
    cons_ids: Annotated[list[EntityId], Field(min_length=1, max_length=32)]
    expected_seed_snapshot_sha256: Annotated[
        str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    ]
    backup_path: Annotated[str, Field(strict=True, min_length=1)]
    max_boundary_restore_mm: Annotated[float, Field(strict=True, ge=1e-12, le=1e-5, allow_inf_nan=False)] | None = None
    allow_unmeshed_tessellation_updates: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def require_distinct_ids(self):
        if len(self.face_ids) != len(set(self.face_ids)):
            raise ValueError("seed_scope face_ids must be distinct")
        if len(self.cons_ids) != len(set(self.cons_ids)):
            raise ValueError("seed_scope cons_ids must be distinct")
        return self


def register(server, original_submit):
    def submit(tool, params, operation_id, target, model, ttl):
        params = {key: value for key, value in params.items()
                  if not (key == "deck" and value is None)}
        return original_submit(tool, params, operation_id, target, model, ttl)

    def reject_surface(message, operation_id, target, model):
        return ToolResult(
            ok=False,
            status="failed",
            target=target,
            model=model.model_dump(mode="json"),
            operation_id=operation_id,
            error=ToolError(code="MESH_SURFACE_REJECTED", message=message),
        ).as_mcp()

    @server.tool(structured_output=True)
    def ansa_mesh_faces(operation_id: OperationId, expected_target: ExpectedTarget,
                        expected_model: ExpectedModel, face_ids: FaceIds,
                        deck: int | None = None,
                        ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read FACE macro membership, attached SHELL IDs and current meshing preconditions without modifying the model."""
        return submit("mesh_faces", {"face_ids": face_ids, "deck": deck}, operation_id,
                      expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_surface(operation_id: OperationId, expected_target: ExpectedTarget,
                          expected_model: ExpectedModel, faces: FaceConditions,
                          target_length: TargetLength | None = None,
                          element_type: Literal["tria", "quad"] = "tria",
                          method: Literal["length", "mapped_existing_seeds"] = "length",
                          seed_scope: SurfaceSeedScope | None = None,
                          deck: int | None = None,
                          ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Mesh only unmeshed FACE entities across their complete macro scope, restoring temporary parameters afterward.

        NASTRAN keeps the triangular default. ABAQUS quad applies target_length
        to the explicitly scoped CONS and verifies FACE derived counts, real
        four-node QUAD topology, node coordinates, positive finite edge lengths
        capped at 1.5x target, and bounds.

        mapped_existing_seeds is first-order NASTRAN only. It requires quad output
        and an explicit four-sided FACE macro scope, reuses existing integer CONS
        seeds, and invokes native Create4SidedMesh with backup and CAD/boundary
        protection. It does not create a volume mesh or cover arbitrary complex
        faces.
        """
        if method == "length":
            if target_length is None:
                return reject_surface(
                    "length method requires target_length",
                    operation_id,
                    expected_target,
                    expected_model,
                )
            if seed_scope is not None:
                return reject_surface(
                    "length method does not accept seed_scope",
                    operation_id,
                    expected_target,
                    expected_model,
                )
        else:
            if element_type != "quad":
                return reject_surface(
                    "mapped_existing_seeds requires element_type='quad'",
                    operation_id,
                    expected_target,
                    expected_model,
                )
            if seed_scope is None:
                return reject_surface(
                    "mapped_existing_seeds requires seed_scope",
                    operation_id,
                    expected_target,
                    expected_model,
                )
            if target_length is not None:
                return reject_surface(
                    "mapped_existing_seeds does not accept target_length",
                    operation_id,
                    expected_target,
                    expected_model,
                )
            return submit("mesh_surface", {
                "faces": [face.model_dump(mode="json") for face in faces],
                "element_type": element_type,
                "method": method,
                "seed_scope": seed_scope.model_dump(mode="json", exclude_none=True),
                "deck": deck,
            }, operation_id, expected_target, expected_model, ttl_seconds)

        return submit("mesh_surface", {
            "faces": [face.model_dump(mode="json") for face in faces],
            "target_length": target_length,
            "element_type": element_type,
            "deck": deck,
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_delete(operation_id: OperationId, expected_target: ExpectedTarget,
                         expected_model: ExpectedModel, entities: ShellConditions,
                         deck: int | None = None,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Delete only the supplied SHELL entities; do not clean up nodes or geometry."""
        return submit("mesh_delete", {
            "entities": [entity.model_dump(mode="json") for entity in entities],
            "deck": deck,
        }, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_mesh_settings(operation_id: OperationId, expected_target: ExpectedTarget,
                           expected_model: ExpectedModel,
                           action: Literal["read", "update"] = "read",
                           expected_settings: MeshSettingsSnapshot | None = None,
                           settings: MeshSettingsChange | None = None,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read shell mesh settings or update explicit values for this ANSA session with complete current-value preconditions; do not write defaults files or generate mesh."""
        if action == "read":
            if expected_settings is None and settings is None:
                return submit("mesh_settings_read", {}, operation_id,
                              expected_target, expected_model, ttl_seconds)
            error = "read does not accept expected_settings or settings"
        elif expected_settings is not None and settings is not None:
            return submit("mesh_settings_update", {
                "expected_settings": expected_settings.model_dump(mode="json"),
                "settings": settings.model_dump(mode="json", exclude_none=True),
            }, operation_id, expected_target, expected_model, ttl_seconds)
        else:
            error = "update requires expected_settings and settings"
        return ToolResult(ok=False, status="failed", target=expected_target, model=expected_model.model_dump(mode="json"),
                          operation_id=operation_id,
                          error=ToolError(code="MESH_SETTINGS_REJECTED", message=error)).as_mcp()
