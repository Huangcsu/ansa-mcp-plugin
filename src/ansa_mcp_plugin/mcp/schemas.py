"""Explicit input/output schemas, including structured tool errors."""

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from mcp.server.fastmcp.utilities.func_metadata import FuncMetadata
from pydantic import BaseModel, ConfigDict, Field


class _RawArguments(FuncMetadata):
    """Strict tool arguments must reach Pydantic without SDK JSON-string parsing."""

    def pre_parse_json(self, data):
        return data


def _forbid_extra_arguments(server, tool_name):
    """Preserve raw scalar types and reject undeclared fields for one tool."""
    manager = getattr(server, "_tool_manager", None)
    get_tool = getattr(manager, "get_tool", None)
    tool = get_tool(tool_name) if callable(get_tool) else None
    metadata = getattr(tool, "fn_metadata", None)
    argument_model = getattr(metadata, "arg_model", None)
    if argument_model is None or not callable(getattr(argument_model, "model_rebuild", None)):
        raise RuntimeError("FastMCP does not expose the argument model needed for strict input validation")
    argument_model.model_config = {**argument_model.model_config, "extra": "forbid"}
    if argument_model.model_rebuild(force=True) is not True:
        raise RuntimeError("FastMCP could not rebuild the strict argument schema")
    tool.parameters = argument_model.model_json_schema(by_alias=True)
    if not isinstance(metadata, FuncMetadata):
        raise RuntimeError("FastMCP metadata cannot preserve raw argument types")
    tool.fn_metadata = _RawArguments(**metadata.model_dump())


class ExpectedTarget(BaseModel):
    model_config = ConfigDict(extra="ignore")
    backend_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    generation: int = Field(ge=1, strict=True)


class ToolError(BaseModel):
    code: str
    message: str


class ExpectedModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    epoch: int = Field(ge=1, strict=True)


class ToolResult(BaseModel):
    ok: bool
    status: str
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    operation_id: str | None = None
    task_id: str | None = None
    target: ExpectedTarget | None = None
    model: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)

    def as_mcp(self):
        value = self.model_dump(mode="json")
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
            structuredContent=value, isError=not self.ok,
        )
