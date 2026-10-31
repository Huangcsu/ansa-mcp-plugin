"""Task classification shared by admission and the owning-thread claim gate."""

READ_ONLY_TOOLS = frozenset({
    "grid_read", "entity_count", "entity_list", "entity_get", "entity_find",
    "model_summary", "model_includes", "entity_bounds", "node_coordinates",
    "script_validate", "api_lookup",
    "geometry_inventory", "face_properties", "entity_metrics", "mass_properties", "surface_members", "mesh_quality", "geometry_check", "set_members", "mesh_faces",
    "mesh_settings_read",
    "geometry_select", "geometry_preview",
    "visibility_snapshot",
    "check_run",
    "mesh_check",
    "mesh_failed_elements",
    "mesh_intersections",
    "mesh_penetrations",
    "quality_report",
    "quality_page",
    "mesh_interface_audit", "mesh_block_plan", "mesh_source_audit",
    "mesh_seed_plan", "mesh_sweep_prepare",
    "geometry_partition_plan", "mesh_provenance", "mesh_evidence_page",
    "geometry_features", "geometry_sweep_candidates", "mesh_seed_read",
    "mesh_cad_map_plan", "mesh_map_block_inspect", "mesh_map_block_plan", "mesh_map_block_fill_plan",
    "mesh_map_block_caps_plan",
    "mesh_map_blocks_plan", "mesh_annular_source_plan",
    "rigid_dependencies",
    "connection_list",
    "connection_check",
    "kinematics_inspect",
    "abaqus_contact_inspect",
})
MODEL_CHANGE_TOOLS = frozenset({"model_new", "model_open", "model_import", "benchmark_run"})
TASK_TOOLS = READ_ONLY_TOOLS | MODEL_CHANGE_TOOLS | frozenset({
    "grid_create", "grid_update", "grid_delete", "model_save",
    "entity_create", "entity_update", "entity_delete", "script_execute", "entity_visibility",
    "model_export", "model_autosave", "model_save_current", "part_create", "set_add",
    "mesh_surface", "mesh_delete", "mesh_settings_update",
    "geometry_delete",
    "connection_create",
    "connection_apply",
    "element_change_type",
    "mesh_volume",
    "mesh_hex_map",
    "geometry_partition_apply", "mesh_sweep",
    "geometry_boundary_prepare", "mesh_seed_apply",
    "mesh_cad_map_apply", "mesh_map_block_apply", "mesh_map_block_fill_apply",
    "mesh_map_block_caps_apply",
    "mesh_map_blocks_apply", "mesh_annular_source_apply",
    "mesh_workflow",
    "quality_locate", "quality_repair",
    "kinematics_body_create", "kinematics_joint_create", "kinematics_config_create",
})


def writes_model(tool):
    if tool not in TASK_TOOLS:
        raise ValueError("Unsupported operation")
    return tool not in READ_ONLY_TOOLS


# A verified model lifetime may permit only a small subset of write operations.
LIMITED_WRITE_TOOLS = frozenset({"quality_locate", "quality_repair"})


def model_write_allowed(model, tool):
    """Apply the same fail-closed per-operation gate at admission and claim."""
    if tool not in TASK_TOOLS:
        raise ValueError("Unsupported operation")
    if model.get("write_enabled") is True:
        return True
    allowed = model.get("write_tools", [])
    return (type(allowed) is list and tool in LIMITED_WRITE_TOOLS and tool in allowed)
