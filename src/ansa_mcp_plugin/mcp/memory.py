"""Explicit local memory tools; preferences and notes share the private runtime."""

from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from ..backend.memory import MemoryConflictError
from .schemas import ToolError, ToolResult, _forbid_extra_arguments


Key = Annotated[str, Field(strict=True, min_length=1, max_length=120)]
Text = Annotated[str, Field(strict=True, max_length=4000)]
OperationId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]


class PreferenceChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["preference"]
    key: Key
    value: Any
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    note: Text = ""


class NoteChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["note"]
    text: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]
    tag: Annotated[str, Field(strict=True, max_length=120)] = ""


Change = Annotated[PreferenceChange | NoteChange, Field(discriminator="kind")]


def register(server, service):
    @server.tool(structured_output=True)
    def ansa_memory_read(key: Key | None = None,
                         limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 30,
                         after_key: Key | None = None,
                         before_note_sequence: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)] | None = None
                         ) -> Annotated[CallToolResult, ToolResult]:
        """Read explicit preferences or notes in this runtime. Without key, page preferences by ascending key and notes by descending sequence. Key lookup rejects snapshot cursors; inspect found for a missing key."""
        try:
            data = service.memory.read(key, limit, after_key, before_note_sequence)
            return ToolResult(ok=True, status="succeeded", data=data).as_mcp()
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, status="failed", error=ToolError(
                code="MEMORY_REJECTED", message=str(exc))).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", error=ToolError(
                code="MEMORY_UNAVAILABLE", message=str(exc))).as_mcp()

    _forbid_extra_arguments(server, "ansa_memory_read")

    @server.tool(structured_output=True)
    def ansa_memory_write(operation_id: OperationId, change: Change
                          ) -> Annotated[CallToolResult, ToolResult]:
        """Persist an explicit preference with expected_revision (0 creates), or append a note. Repeat the same operation_id and change to retrieve the original receipt. This is a local transaction without an ANSA task; data is shared only within this runtime."""
        try:
            data = service.memory.write(operation_id, change.model_dump())
            return ToolResult(ok=True, status="succeeded", data=data, operation_id=operation_id).as_mcp()
        except MemoryConflictError as exc:
            return ToolResult(ok=False, status="failed", operation_id=operation_id,
                              error=ToolError(code="MEMORY_CONFLICT", message=str(exc))).as_mcp()
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, status="failed", operation_id=operation_id,
                              error=ToolError(code="MEMORY_REJECTED", message=str(exc))).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", operation_id=operation_id,
                              error=ToolError(code="MEMORY_UNAVAILABLE", message=str(exc))).as_mcp()

    _forbid_extra_arguments(server, "ansa_memory_write")
