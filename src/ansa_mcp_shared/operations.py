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
    "mesh_workflow",
    "kinematics_body_create", "kinematics_joint_create", "kinematics_config_create",
})


def writes_model(tool):
    if tool not in TASK_TOOLS:
        raise ValueError("Unsupported operation")
    return tool not in READ_ONLY_TOOLS
