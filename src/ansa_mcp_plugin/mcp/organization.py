"""Model files and entity organization through the durable task entrance."""

from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from .schemas import ExpectedModel, ExpectedTarget, ToolResult

OperationId = Annotated[str, Field(min_length=1, max_length=128)]
TTL = Annotated[int, Field(ge=0, le=86400)]
EntityId = Annotated[int, Field(ge=1, strict=True)]
Nonempty = Annotated[str, Field(min_length=1)]


class MemberReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deck: Annotated[int, Field(strict=True)]
    type: Nonempty
    id: EntityId


class SetMemberCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Nonempty
    id: EntityId
    expected_fields: Annotated[dict[str, Any], Field(min_length=1)]


def register(server, original_submit):
    def submit(tool, params, operation_id, target, model, ttl):
        params = {key: value for key, value in params.items()
                  if not (key == "deck" and value is None)}
        return original_submit(tool, params, operation_id, target, model, ttl)

    @server.tool(structured_output=True)
    def ansa_model_export(operation_id: OperationId, expected_target: ExpectedTarget,
                          expected_model: ExpectedModel, path: Nonempty,
                          format: Literal["NASTRAN", "ABAQUS", "LSDYNA", "STEP"],
                          overwrite: bool = False, scope: Literal["all", "visible"] = "all",
                          ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Export one file with explicit overwrite intent. STEP requires visible scope and confirmation in the owning ANSA Output Parameters dialog; query the same task after any timeout."""
        return submit("model_export", {"path": path, "format": format, "overwrite": overwrite,
                                       "scope": scope}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_model_import(operation_id: OperationId, expected_target: ExpectedTarget,
                          expected_model: ExpectedModel, path: Nonempty,
                          format: Literal["NASTRAN", "ABAQUS", "LSDYNA", "STEP"],
                          mode: Literal["merge", "replace"] = "merge",
                          preservation: Literal["backup", "discard"] = "backup",
                          backup_path: str | None = None,
                          ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Merge a solver file with safe ID offsets, or explicitly replace the model with STEP using mode='replace'. Backup is the default; reacquire model_after even on failure and query the original task after any timeout."""
        params = {"path": path, "format": format, "preservation": preservation, "backup_path": backup_path}
        # Keep default solver requests identical to their persisted pre-mode form.
        if format == "STEP" or mode != "merge":
            params["mode"] = mode
        return submit("model_import", params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_model_autosave(operation_id: OperationId, expected_target: ExpectedTarget,
                            expected_model: ExpectedModel, directory: Nonempty,
                            ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Save a uniquely named .ansa copy in an existing directory, preserving the current database path."""
        return submit("model_autosave", {"directory": directory}, operation_id,
                      expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_model_save_current(operation_id: OperationId, expected_target: ExpectedTarget,
                                expected_model: ExpectedModel, expected_path: Nonempty,
                                overwrite: bool = False,
                                ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Save the current .ansa file only when its path matches expected_path and overwrite is explicitly true."""
        return submit("model_save_current", {"expected_path": expected_path, "overwrite": overwrite},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_part_create(operation_id: OperationId, expected_target: ExpectedTarget,
                         expected_model: ExpectedModel, name: Nonempty,
                         module_id: str = "", deck: int | None = None,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Create a named ANSA part and independently read back its identity and name."""
        return submit("part_create", {"name": name, "module_id": module_id, "deck": deck},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_set_members(operation_id: OperationId, expected_target: ExpectedTarget,
                         expected_model: ExpectedModel, set_id: EntityId,
                         deck: int | None = None,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read direct SET membership; bounded results cannot serve as complete write preconditions."""
        return submit("set_members", {"set_id": set_id, "deck": deck}, operation_id,
                      expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_set_add(operation_id: OperationId, expected_target: ExpectedTarget,
                     expected_model: ExpectedModel, set_id: EntityId,
                     expected_fields: Annotated[dict[str, Any], Field(min_length=1)],
                     expected_members: Annotated[list[MemberReference], Field(max_length=200)],
                     entities: Annotated[list[SetMemberCondition], Field(min_length=1, max_length=200)],
                     deck: int | None = None,
                     ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Add ordinary members only after the SET, complete membership and every entity still match their readback."""
        return submit("set_add", {"set_id": set_id, "expected_fields": expected_fields,
                                  "expected_members": [item.model_dump() for item in expected_members],
                                  "entities": [item.model_dump() for item in entities], "deck": deck},
                      operation_id, expected_target, expected_model, ttl_seconds)
