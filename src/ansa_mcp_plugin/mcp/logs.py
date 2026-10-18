"""Read the latest persisted task result and its private JSON attachment."""

from typing import Annotated

from mcp.types import CallToolResult
from pydantic import Field

from .schemas import ToolError, ToolResult, _forbid_extra_arguments


def register(server, service):
    @server.tool(structured_output=True)
    def ansa_log_read(
        max_chars: Annotated[int, Field(strict=True, ge=1, le=65536)] = 65536,
    ) -> Annotated[CallToolResult, ToolResult]:
        """读取当前运行目录最新模型任务的持久结果/错误与最多200项任务审计摘要。

        不连接或执行 ANSA，不接受文件路径。最新任务没有结果时明确报告不可用，
        不退回旧结果。正文预览最多 max_chars 字符；截断时 result/error 为 null，
        完整正文与审计摘要存入私有、不可覆盖的 JSON 附件，返回路径和 SHA256。
        历史统计限于模型任务，不代表全部控制命令或外部 ANSA 日志。
        """
        try:
            value = service.log_reader.read(max_chars)
            artifact = value.get("artifact")
            return ToolResult(ok=True, status="succeeded", data=value,
                              artifacts=[artifact] if artifact else []).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", error=ToolError(
                code="LOG_UNAVAILABLE", message=str(exc))).as_mcp()

    _forbid_extra_arguments(server, "ansa_log_read")


__all__ = ["register"]
