"""Run an MCP stdio frontend connected to the runtime's shared backend."""

import argparse
import os
import sys
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from .backend.recoverable import RecoverableService
from .backend.lock import BackendBusyError
from .backend.shared_client import SharedService, BackendUnavailableError, BackendOutcomeUnknownError
from .backend.scheduler import TaskNotFoundError
from pydantic import Field, FiniteFloat
from .mcp.schemas import ExpectedTarget, ExpectedModel, ToolError, ToolResult, _forbid_extra_arguments

OperationId = Annotated[str, Field(min_length=1, max_length=128)]
TaskQueryId = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Vector = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
TTL = Annotated[int, Field(ge=0, le=86400)]


def create_server(service):
    server = FastMCP("ANSA MCP Plugin")

    @server.tool(name="ansa_connection_status", structured_output=True)
    def ansa_connection_status() -> Annotated[CallToolResult, ToolResult]:
        """List registered ANSA instances and the panel-bound target context."""
        try:
            return ToolResult(ok=True, status="ready", data=service.status()).as_mcp()
        except BackendBusyError as exc:
            return ToolResult(ok=False, status="blocked", data={"retryable": True},
                              error=ToolError(code="BACKEND_BUSY", message=str(exc))).as_mcp()
        except BackendUnavailableError as exc:
            return ToolResult(ok=False, status="blocked", data={"retryable": exc.code != "AUTH", "connection_stage": "backend"},
                              error=ToolError(code="BACKEND_" + exc.code, message=str(exc))).as_mcp()
        except (RuntimeError, OSError, ValueError) as exc:
            return ToolResult(ok=False, status="failed", error=ToolError(
                code="STATUS_UNAVAILABLE", message=str(exc))).as_mcp()

    @server.tool(name="ansa_model_snapshot", structured_output=True)
    def ansa_model_snapshot(expected_target: ExpectedTarget) -> Annotated[CallToolResult, ToolResult]:
        """Read deck, model context and execution thread from the bound ANSA."""
        try:
            value = service.model_snapshot(expected_target.model_dump())
            model = value["model"]
            warnings = ([] if model["write_enabled"] else
                        ["仅开放网格质量定位／受控修复；其他写入未开放"] if model.get("write_tools") else
                        ["模型更替检测能力受限；写入未开放"])
            return ToolResult(ok=True, status="succeeded", data=value,
                              target=expected_target, model=value["model"],
                              warnings=warnings).as_mcp()
        except (RuntimeError, OSError, ValueError, LookupError) as exc:
            return ToolResult(ok=False, status="failed", target=expected_target,
                              error=ToolError(code="SNAPSHOT_REJECTED", message=str(exc))).as_mcp()

    @server.tool(name="ansa_panel_show", structured_output=True)
    def ansa_panel_show(expected_target: ExpectedTarget) -> Annotated[CallToolResult, ToolResult]:
        """Queue the panel in the bound ANSA instance; success does not confirm that it is visible."""
        try:
            value = service.panel_show(expected_target.model_dump())
            return ToolResult(ok=True, status="queued",
                              data={"queued": True, "visible": None, "result": value},
                              target=expected_target,
                              warnings=["已提交面板显示请求；可见状态需通过后续探测确认"]).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", target=expected_target,
                              error=ToolError(code="PANEL_SHOW_REJECTED", message=str(exc))).as_mcp()

    def task_result(record):
        task = record["task"]
        state = record["state"]
        message = record.get("error")
        return ToolResult(ok=state not in {"failed", "expired", "unknown", "cancelled"}, status=state,
                          data={"result": record["result"], "dispatch": record["dispatch"],
                                "cancel_pending": record["cancel_pending"], "acked": record["acked"]},
                          operation_id=record["operation_id"], task_id=record["task_id"],
                          target=task["target"], model=task["model"],
                          error=ToolError(code="TASK_" + state.upper(), message=str(message or state)) if state in {"failed", "expired", "unknown", "cancelled"} else None,
                          warnings=["结果未知；请查询原任务并回读，不能用新 operation_id 重试"] if state == "unknown" else []).as_mcp()

    def submit(tool, params, operation_id, target, model, ttl):
        try:
            return task_result(service.submit_task(tool, params, operation_id, target.model_dump(), model.model_dump(), ttl))
        except BackendOutcomeUnknownError as exc:
            return ToolResult(ok=False, status="unknown", operation_id=operation_id, target=target,
                              model=model.model_dump(), error=ToolError(code="TASK_UNKNOWN", message=str(exc)),
                              warnings=["提交结果未知；请用原 operation_id 查询任务并独立回读，不能用新 operation_id 重试"]).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", operation_id=operation_id, target=target, model=model.model_dump(),
                              error=ToolError(code="TASK_REJECTED", message=str(exc))).as_mcp()

    @server.tool(name="ansa_grid_create", structured_output=True)
    def ansa_grid_create(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                         position: Vector, deck: int = 1, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue one NASTRAN GRID creation. Retrying requires the same operation_id and request; query the returned task."""
        return submit("grid_create", {"deck": deck, "position": list(position)}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_grid_read", structured_output=True)
    def ansa_grid_read(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                       entity_id: int, deck: int = 1, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue a context-checked GRID read; inspect its durable result with ansa_task_query."""
        return submit("grid_read", {"deck": deck, "id": entity_id}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_grid_update", structured_output=True)
    def ansa_grid_update(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                         entity_id: int, expected_position: Vector, position: Vector, deck: int = 1,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue a GRID edit only if its current coordinates still match expected_position."""
        return submit("grid_update", {"deck": deck, "id": entity_id, "expected_position": list(expected_position),
                                      "position": list(position)}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_grid_delete", structured_output=True)
    def ansa_grid_delete(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                         entity_id: int, expected_position: Vector, deck: int = 1,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue one GRID deletion with coordinate preconditions and independent absence readback."""
        return submit("grid_delete", {"deck": deck, "id": entity_id, "expected_position": list(expected_position)},
                      operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_model_save", structured_output=True)
    def ansa_model_save(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                        path: str, overwrite: bool = False, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue SaveAs to an absolute .ansa path; overwrite requires explicit intent."""
        return submit("model_save", {"path": path, "overwrite": overwrite}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(name="ansa_task_query", structured_output=True)
    def ansa_task_query(task_id: TaskQueryId | None = None,
                        operation_id: TaskQueryId | None = None) -> Annotated[CallToolResult, ToolResult]:
        """Query the original task across disconnects, model changes and target switches. A client timeout does not cancel it."""
        if task_id is None and operation_id is None:
            return ToolResult(ok=False, status="failed", task_id=task_id, operation_id=operation_id,
                              error=ToolError(code="QUERY_REJECTED", message="Provide task_id or operation_id")).as_mcp()
        if any(value is not None and (type(value) is not str or not 1 <= len(value) <= 128)
               for value in (task_id, operation_id)):
            return ToolResult(ok=False, status="failed", task_id=task_id, operation_id=operation_id,
                              error=ToolError(code="QUERY_REJECTED", message="Task identifiers must be strings of length 1 to 128")).as_mcp()
        try:
            return task_result(service.query_task(task_id, operation_id))
        except TaskNotFoundError as exc:
            return ToolResult(ok=False, status="failed", task_id=task_id, operation_id=operation_id,
                              error=ToolError(code="TASK_NOT_FOUND", message=str(exc))).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", task_id=task_id, operation_id=operation_id,
                              error=ToolError(code="QUERY_UNAVAILABLE", message=str(exc))).as_mcp()
    _forbid_extra_arguments(server, "ansa_task_query")

    @server.tool(name="ansa_task_cancel", structured_output=True)
    def ansa_task_cancel(task_id: str) -> Annotated[CallToolResult, ToolResult]:
        """Request business cancellation; already claimed ANSA calls cannot be cancelled. Pending confirmation is not cancellation."""
        try:
            return task_result(service.cancel_task(task_id))
        except Exception as exc:
            return ToolResult(ok=False, status="failed", task_id=task_id,
                              error=ToolError(code="CANCEL_REJECTED", message=str(exc))).as_mcp()

    from .mcp.extensions import register
    register(server, service, submit)
    from .mcp.organization import register as register_organization
    register_organization(server, submit)
    from .mcp.meshing import register as register_meshing
    register_meshing(server, submit)
    from .mcp.geometry import register as register_geometry
    register_geometry(server, submit)
    from .mcp.geometry_delete import register as register_geometry_delete
    register_geometry_delete(server, submit)
    from .mcp.checks import register as register_checks
    register_checks(server, submit)
    from .mcp.mesh_checks import register as register_mesh_checks
    register_mesh_checks(server, submit)
    from .mcp.rigid_dependencies import register as register_rigid_dependencies
    register_rigid_dependencies(server, submit)
    from .mcp.failed_elements import register as register_failed_elements
    register_failed_elements(server, submit)
    from .mcp.intersections import register as register_intersections
    register_intersections(server, submit)
    from .mcp.penetrations import register as register_penetrations
    register_penetrations(server, submit)
    from .mcp.memory import register as register_memory
    register_memory(server, service)
    from .mcp.connections import register as register_connections
    register_connections(server, submit)
    from .mcp.connection_checks import register as register_connection_checks
    register_connection_checks(server, submit)
    from .mcp.element_types import register as register_element_types
    register_element_types(server, submit)
    from .mcp.volume_meshing import register as register_volume_meshing
    register_volume_meshing(server, submit)
    from .mcp.hexa_meshing import register as register_hexa_meshing
    register_hexa_meshing(server, submit)
    from .mcp.kinematics import register as register_kinematics
    register_kinematics(server, submit)
    from .mcp.contacts import register as register_contacts
    register_contacts(server, submit)
    from .mcp.mesh_workflow import register as register_mesh_workflow
    register_mesh_workflow(server, submit)
    from .mcp.benchmark import register as register_benchmark
    register_benchmark(server, submit)
    from .mcp.connection_apply import register as register_connection_apply
    register_connection_apply(server, submit)
    from .mcp.quality_workflow import register as register_quality_workflow
    register_quality_workflow(server, submit)
    from .mcp.logs import register as register_logs
    register_logs(server, service)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-home", default=os.environ.get("ANSA_MCP_PLUGIN_HOME"))
    parser.add_argument("--direct-backend", action="store_true",
                        help="Use the legacy process-owned backend for isolated tests")
    args = parser.parse_args(argv)
    if not args.runtime_home:
        parser.error("--runtime-home or ANSA_MCP_PLUGIN_HOME is required")
    service = None
    try:
        service = RecoverableService(args.runtime_home) if args.direct_backend else SharedService(args.runtime_home)
        create_server(service).run(transport="stdio")
        return 0
    except Exception as exc:
        print("ANSA MCP backend: " + str(exc), file=sys.stderr)
        return 1
    finally:
        if service:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
