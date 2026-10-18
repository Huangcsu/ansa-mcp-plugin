"""Third-stage grouped tools; all model requests use the durable task entrance."""
import json
from typing import Annotated, Any, Literal
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field
from .schemas import ExpectedTarget, ExpectedModel, ToolResult, ToolError, _forbid_extra_arguments
from ansa_mcp_shared.framing import MAX_FRAME_BYTES

OperationId = Annotated[str, Field(min_length=1, max_length=128)]
TTL = Annotated[int, Field(ge=0, le=86400)]
PageSize = Annotated[int, Field(ge=1, le=200)]
EntityId = Annotated[int, Field(ge=1)]


class DeletePrecondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: EntityId
    expected_fields: dict[str, Any] = Field(min_length=1)


def register(server, service, submit):
    original_submit = submit
    def submit(tool, params, operation_id, target, model, ttl):
        params = {key: value for key, value in params.items() if not (key == "deck" and value is None)}
        return original_submit(tool, params, operation_id, target, model, ttl)
    @server.tool(structured_output=True)
    def ansa_entity_count(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                          entity_type: str, deck: int | None = None, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue an entity count for the current deck and model."""
        return submit("entity_count", {"entity_type": entity_type, "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_list(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                         entity_type: str, deck: int | None = None, fields: list[str] | None = None,
                         offset: Annotated[int, Field(ge=0)] = 0, limit: PageSize = 100,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue a bounded page of entities; API entities are returned as deck/type/id references."""
        return submit("entity_list", {"entity_type": entity_type, "deck": deck, "fields": fields, "offset": offset, "limit": limit}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_get(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                        entity_type: str, entity_id: EntityId, fields: list[str] | None = None,
                        deck: int | None = None, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Re-resolve an entity by deck/type/id and read only verified card fields."""
        return submit("entity_get", {"entity_type": entity_type, "entity_id": entity_id, "fields": fields, "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_create(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           entity_type: str, fields: dict[str, Any], deck: int | None = None,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Create one entity using current-deck fields, with independent readback."""
        return submit("entity_create", {"entity_type": entity_type, "fields": fields, "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_update(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           entity_type: str, entity_id: EntityId, expected_fields: dict[str, Any], fields: dict[str, Any],
                           deck: int | None = None, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Update only after every expected field still matches the freshly resolved entity."""
        return submit("entity_update", {"entity_type": entity_type, "entity_id": entity_id, "expected_fields": expected_fields, "fields": fields, "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_delete(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           entity_type: str, entities: list[DeletePrecondition], deck: int | None = None,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Validate all per-entity preconditions before deletion; never compress IDs. FACE entities require ansa_geometry_delete with fresh geometry and boundary conditions."""
        return submit("entity_delete", {"entity_type": entity_type, "entities": [x.model_dump() for x in entities], "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_find(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                         pattern: str, match: Literal["regex", "exact", "substring", "substring_ignorecase"] = "substring_ignorecase",
                         deck: int | None = None, offset: Annotated[int, Field(ge=0)] = 0, limit: PageSize = 100,
                         ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Find entities by name within a bounded result page."""
        return submit("entity_find", {"pattern": pattern, "match": match, "deck": deck, "offset": offset, "limit": limit}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_model_summary(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           deck: Annotated[int, Field(strict=True)] | None = None,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read database path, deck name and total/eight category counts, including hidden entities. Pair with the same snapshot model context. Unavailable category counts are null with errors; an unavailable total fails. Totals are native counts, not category sums or a model-loaded flag."""
        return submit("model_summary", {"deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)
    _forbid_extra_arguments(server, "ansa_model_summary")

    @server.tool(structured_output=True)
    def ansa_model_includes(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                            deck: int | None = None, offset: Annotated[int, Field(ge=0)] = 0, limit: PageSize = 100,
                            ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read model includes by ascending entity ID, with Name when its card exposes it."""
        return submit("model_includes", {"deck": deck, "offset": offset, "limit": limit}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_entity_bounds(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           entity_type: str, entity_ids: list[EntityId], deck: int | None = None,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Compute bounds of explicitly resolved entities."""
        return submit("entity_bounds", {"entity_type": entity_type, "entity_ids": entity_ids, "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_node_coordinates(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                              node_ids: list[EntityId], deck: int | None = None,
                              ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read NASTRAN GRID coordinates by ID."""
        return submit("node_coordinates", {"node_ids": node_ids, "deck": deck}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_model_new(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                       preservation: Literal["backup", "discard"] = "backup", backup_path: str | None = None,
                       ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Replace the model. Default requires a new backup path; discarding requires explicit intent."""
        return submit("model_new", {"preservation": preservation, "backup_path": backup_path}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_model_open(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                        path: str, preservation: Literal["backup", "discard"] = "backup", backup_path: str | None = None,
                        ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Open an existing .ansa file after preserving the current model; always reacquire model_after."""
        return submit("model_open", {"path": path, "preservation": preservation, "backup_path": backup_path}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_script_validate(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                             script: str, ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Parse Python and check statically visible ANSA APIs; never execute it during preflight."""
        return submit("script_validate", {"script": script}, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_script_execute(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                            script: Annotated[str, Field(strict=True, min_length=1, max_length=65536)],
                            validate: Annotated[bool, Field(strict=True)] = True,
                            function_name: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None,
                            allow_model_change: Annotated[bool, Field(strict=True)] = False,
                            ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Execute Python as a write task, optionally calling one synchronous no-argument global entrypoint. RESULT overrides its JSON return; top-level effects can precede call failures."""
        return submit("script_execute", {"script": script, "validate": validate, "function_name": function_name,
                                          "allow_model_change": allow_model_change}, operation_id, expected_target, expected_model, ttl_seconds)
    _forbid_extra_arguments(server, "ansa_script_execute")

    @server.tool(structured_output=True)
    def ansa_api_reference(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           action: Literal["lookup", "search", "modules", "categories"] = "lookup", query: str = "",
                           module: str | None = None, limit: Annotated[int, Field(strict=True, ge=1, le=20)] = 10,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Read version-matched installed API help, with explicit runtime-docstring fallback, or module/function counts. Catalog actions require an empty query."""
        return submit("api_lookup", {"action": action, "query": query, "module": module, "limit": limit}, operation_id, expected_target, expected_model, ttl_seconds)
    _forbid_extra_arguments(server, "ansa_api_reference")

    @server.tool(structured_output=True)
    def ansa_model_inspect(operation_id: OperationId, expected_target: ExpectedTarget, expected_model: ExpectedModel,
                           action: Literal["geometry_inventory", "face_properties", "entity_metrics", "mass_properties", "surface_members", "mesh_quality", "geometry_check", "entity_visibility", "visibility_snapshot"],
                           deck: Annotated[int, Field(strict=True)], options: dict[str, Any] | None = None,
                           ttl_seconds: TTL = 300) -> Annotated[CallToolResult, ToolResult]:
        """Queue bounded inspection or a display change; options are checked for each action and no auto_fix is accepted.

        face_properties requires face_ids (1-100). Optional include_geometry is a strict bool (default False);
        True adds NASTRAN FACE orientation, unit_normal, six-coordinate bounds and completeness flags.
        orientation is the ANSA face direction vector; unit_normal normalizes that vector, not a local curved-surface normal.
        Bounds and centers use model length units. Missing or invalid fields remain null with field errors;
        inspect complete before using an enriched result. The query is read-only and does not change selection or visibility.

        entity_metrics requires action (mass/area/volume), entity_type and one positive entity_id.
        Area/volume return the API's finite numeric value in model length units squared/cubed.
        NASTRAN/OPTISTRUCT mass uses GPWGInfo custom mode and returns [Mx, My, Mz].
        ABAQUS mass returns a scalar via CalcElementMass on the root's deduplicated SHELL/SOLID scope.
        GPWG may ignore unsupported or unmeshed entities; a zero result does not prove a complete mass calculation.
        These queries do not repair entities or change the current view.

        mass_properties is ABAQUS only: entities is 1-200 explicit deck/type/id references to MATERIAL,
        SHELL_SECTION, SOLID_SECTION, ANSAPART, ANSAGROUP, SHELL or SOLID. Optional element_types
        contains SHELL and/or SOLID (default both); no_nsm is a strict bool (default False).
        Up to 500,000 deduplicated elements and 1,000,000 scanned references; empty or invalid mass fails.
        Returns scalar mass, center, inertia and an explicit hashed scope. Other element types are excluded.

        surface_members reads one ABAQUS surface SET via set_id; offset/limit provide bounded paging.
        It preserves SOLIDFACET identity and mixed SPOS/SNEG membership. Check completeness/errors;
        expand_nested is an optional strict bool (default False), expanding nested SETs with explicit
        inherited orientation, cycle detection, maximum depth 16 and 200,000 references.
        This is membership inspection, not a contact-gap or TIE-coverage test.

        geometry_check supports NASTRAN Basic on 1-100 explicit FACE entity_ids only.
        Options require entity_type="FACE" and entity_ids, with check="basic" and strict boolean single_cons=False
        as defaults. It verifies boundary CONS ownership and returns complete native report trees, leaf issue_counts,
        failed_cons_ids and affected_face_ids. CONS and extensive selections are unavailable.
        Native findings do not make the task fail when the complete check result is valid. It never calls try_fix.

        visibility_snapshot reads NASTRAN FACE/SHELL IDs and visible_ids, rejecting more than 10,000 monitored entities.
        entity_visibility without IDs retains type-level control. Explicit FACE/SHELL show_only/show_also/hide requires
        entity_ids (1-200), expected_visible from a fresh snapshot, and optional apply_on_contents (bool, default True).
        near requires a finite positive radius and optional dense_search (bool); neighb requires integer steps (1-20);
        both require expected_visible and expand the current visible view without reseeding it. Near/Neighb are display writes,
        not read-only spatial queries. Readback covers FACE/SHELL; related types may also change visibility.
        Inspect the original task after any failure before retrying.
        """
        params = {**(options or {}), "deck": deck}
        if action == "geometry_check":
            encoded = json.dumps(params, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_FRAME_BYTES // 8:
                raise ValueError("geometry check request exceeds the reply budget")
        return submit(action, params, operation_id, expected_target, expected_model, ttl_seconds)

    @server.tool(structured_output=True)
    def ansa_task_list(limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 30,
                       state: Literal["queued", "running", "succeeded", "failed", "cancelled", "expired", "unknown"] | None = None,
                       before_sequence: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)] | None = None
                       ) -> Annotated[CallToolResult, ToolResult]:
        """Read durable task history across target switches without exposing script/request bodies."""
        try:
            page = service.tasks.history_page(limit, state, before_sequence)
            rows = [{"task_id": r["task_id"], "operation_id": r["operation_id"], "state": r["state"],
                     "tool": r["task"]["tool"], "dispatch": r["dispatch"], "cancel_pending": r["cancel_pending"],
                     "target": r["task"]["target"], "model": r["task"]["model"]} for r in page["tasks"]]
            return ToolResult(ok=True, status="succeeded", data={
                "tasks": rows,
                "limit": limit,
                "state": state,
                "before_sequence": before_sequence,
                "has_more": page["has_more"],
                "next_before_sequence": page["next_before_sequence"],
            }).as_mcp()
        except Exception as exc:
            return ToolResult(ok=False, status="failed", error=ToolError(code="HISTORY_UNAVAILABLE", message=str(exc))).as_mcp()
    _forbid_extra_arguments(server, "ansa_task_list")
