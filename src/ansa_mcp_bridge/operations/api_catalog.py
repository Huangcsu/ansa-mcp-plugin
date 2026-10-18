"""Read-only inventory of public ANSA runtime routines and plugin categories."""

import copy
import importlib
import inspect


_SOURCE = "plugin-supported module whitelist and installed ANSA runtime namespaces"
_SCOPE = "top_level_public_routines"
_CLASSIFICATION_SOURCE = "plugin-defined function-name rules; not an official API taxonomy"

_CATEGORY_RULES = {
    "base": [
        {"category": "base_query", "contains_any": ["get", "collect", "find", "filter"]},
        {"category": "base_modify", "contains_any": ["set", "create", "delete", "remove"]},
        {"category": "base_check", "contains_any": ["check", "quality"]},
        {"category": "base_transform", "contains_any": ["transform", "move", "rotate", "mirror"]},
        {"category": "base_other", "otherwise": True},
    ],
    "mesh": [
        {"category": "mesh_edit", "contains_any": ["delete", "remove", "clear"]},
        {"category": "mesh_quality", "contains_any": ["check", "quality", "length", "angle", "warpage", "aspect"]},
        {"category": "mesh_cfd", "contains_any": ["cfd", "boundary", "inflation"]},
        {"category": "mesh_other", "otherwise": True},
    ],
    "kinetics": [
        {"category": "kinetics", "otherwise": True},
    ],
    "other_modules": {"category": "module name"},
}


def _category(module, routine_name):
    name = routine_name.casefold()
    if module == "base":
        rules = _CATEGORY_RULES["base"]
    elif module == "mesh":
        rules = _CATEGORY_RULES["mesh"]
    elif module == "kinetics":
        rules = _CATEGORY_RULES["kinetics"]
    else:
        return module

    for rule in rules:
        if rule.get("otherwise") or any(word in name for word in rule["contains_any"]):
            return rule["category"]
    raise AssertionError("category rules must include a fallback")


def load_namespace(name):
    # ANSA 2026.1 exposes constants as an object, not an importable submodule.
    if name == "constants":
        from ansa import constants
        return constants
    return importlib.import_module("ansa." + name)


def build_catalog(modules, version, module=None, include_categories=False):
    """Count public routines without calling them or traversing class members."""
    if type(version) is not str or not version.strip():
        raise ValueError("ANSA constants.app_version must be a non-empty string")

    selected = (module,) if module is not None else sorted(modules)
    details = []
    category_counts = {}
    total = 0

    for name in selected:
        try:
            runtime_module = load_namespace(name)
            if name == "constants" and not inspect.ismodule(runtime_module):
                namespace = vars(type(runtime_module))
            else:
                namespace = vars(runtime_module)
            routines = [
                member_name
                for member_name, value in namespace.items()
                if type(member_name) is str
                and not member_name.startswith("_")
                and inspect.isroutine(value)
            ]
        except Exception as exc:
            details.append({
                "module": name,
                "available": False,
                "function_count": None,
                "error_type": type(exc).__name__,
            })
            continue

        details.append({"module": name, "available": True, "function_count": len(routines)})
        total += len(routines)
        if include_categories:
            for routine_name in routines:
                category = _category(name, routine_name)
                key = (category, name)
                category_counts[key] = category_counts.get(key, 0) + 1

    result = {
        "version": version,
        "modules": list(modules),
        "source": _SOURCE,
        "module_details": details,
        "function_count": total,
        "complete": all(item["available"] for item in details),
        "scope": _SCOPE,
    }
    if include_categories:
        result.update({
            "categories": [
                {"category": category, "module": name, "function_count": count}
                for (category, name), count in sorted(category_counts.items())
            ],
            "classification_source": _CLASSIFICATION_SOURCE,
            "category_rules": copy.deepcopy(_CATEGORY_RULES),
        })
    return result
