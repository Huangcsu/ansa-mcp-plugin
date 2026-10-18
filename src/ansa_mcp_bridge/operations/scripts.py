"""Script preflight and API reference inspection; execution remains a write task."""
import ast
import contextlib
import inspect
import io
import json
import keyword
import re

from . import api_catalog, api_documentation

MODULES = ("base", "mesh", "session", "constants", "connections", "cad", "morph", "guitk", "utils", "batchmesh", "kinetics")


def resolve_api(path):
    if not isinstance(path, str) or not re.fullmatch(r"ansa(?:\.[A-Za-z][A-Za-z0-9_]*)+", path):
        raise ValueError("Use an ANSA public dotted API name")
    parts = path.split(".")
    if parts[1] not in MODULES:
        raise ValueError("Unsupported ANSA API module")
    value = api_catalog.load_namespace(parts[1])
    for part in parts[2:]:
        value = getattr(value, part)
    return value


def validate(script):
    if type(script) is not str or not 1 <= len(script) <= 65536:
        raise ValueError("script must contain 1 to 65536 characters")
    tree = ast.parse(script, filename="<ansa-mcp-task>", mode="exec")
    aliases = {"base": "ansa.base", "constants": "ansa.constants", "ansa": "ansa"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.startswith("ansa"):
                    aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else "ansa"
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ansa"):
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = node.module + "." + item.name
    checked, missing = set(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        current, names = node.func, []
        while isinstance(current, ast.Attribute):
            names.insert(0, current.attr)
            current = current.value
        if not isinstance(current, ast.Name) or current.id not in aliases:
            continue
        path = aliases[current.id] + ("." + ".".join(names) if names else "")
        if path == "ansa":
            continue
        if path in checked:
            continue
        checked.add(path)
        try:
            resolve_api(path)
        except (AttributeError, ImportError, ValueError) as exc:
            missing.append({"api": path, "line": node.lineno, "error": str(exc)})
    return {"valid": not missing, "checked_apis": sorted(checked), "missing": missing,
            "warnings": ["Static API preflight does not prove parameter compatibility or absence of writes."]}


class CapturedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.truncated = False
    def write(self, value):
        remaining = 65536 - self.tell()
        if len(value) > remaining:
            self.truncated = True
        super().write(value[:max(0, remaining)])
        return len(value)


def execute_script(params):
    if type(params) is not dict:
        raise TypeError("params must be an object")
    allowed = {"script", "validate", "function_name", "allow_model_change"}
    extra = set(params) - allowed
    if extra:
        raise ValueError("unsupported parameter(s): " + ", ".join(sorted(map(str, extra))))
    if "script" not in params:
        raise ValueError("missing parameter(s): script")
    for key in ("validate", "allow_model_change"):
        if key in params and type(params[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    name = params.get("function_name")
    if name is not None and (
        type(name) is not str
        or not 1 <= len(name) <= 128
        or not name.isidentifier()
        or keyword.iskeyword(name)
    ):
        raise ValueError("function_name must be a non-keyword Python identifier of 1 to 128 characters")

    script = params["script"]
    preflight = validate(script)
    if params.get("validate", True) and not preflight["valid"]:
        raise ValueError("Missing ANSA API(s): " + ", ".join(x["api"] for x in preflight["missing"]))
    import ansa
    from ansa import base, constants
    namespace = {"__name__": "__ansa_mcp_task__", "ansa": ansa, "base": base, "constants": constants}
    output = CapturedOutput()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            exec(compile(script, "<ansa-mcp-task>", "exec"), namespace)
            returned = None
            if name is not None:
                function = namespace.get(name)
                if not callable(function):
                    raise ValueError("The requested script entrypoint is not callable")
                if (
                    inspect.iscoroutinefunction(function)
                    or inspect.isasyncgenfunction(function)
                    or inspect.isgeneratorfunction(function)
                ):
                    raise ValueError("The requested script entrypoint must complete synchronously")
                returned = function()
                if (
                    inspect.isawaitable(returned)
                    or inspect.isgenerator(returned)
                    or inspect.isasyncgen(returned)
                ):
                    if inspect.iscoroutine(returned):
                        returned.close()
                    raise ValueError("The requested script entrypoint must complete synchronously")
        if "RESULT" not in namespace and name is None:
            raise ValueError("Script must set JSON-serializable RESULT")
        result = namespace.get("RESULT", returned)
        from ..journal import Journal
        result = json.loads(Journal._canonical(result, "script RESULT"))
    except BaseException as exc:
        raise RuntimeError(f"Script failed; effects may exist. {type(exc).__name__}: {exc}") from exc
    return {"result": result, "stdout": output.getvalue(), "stdout_truncated": output.truncated,
            "preflight": preflight}


def api_reference(params):
    if type(params) is not dict:
        raise TypeError("params must be an object")
    allowed = {"action", "module", "query", "limit"}
    extra = set(params) - allowed
    if extra:
        raise ValueError("unsupported parameter(s): " + ", ".join(sorted(map(str, extra))))

    action = params.get("action", "lookup")
    if type(action) is not str:
        raise ValueError("action must be a string")
    if action not in {"lookup", "search", "modules", "categories"}:
        raise ValueError("Unsupported API reference action")

    module = params.get("module")
    if module is not None and (type(module) is not str or module not in MODULES):
        raise ValueError("Unsupported ANSA API module")

    query = params.get("query", "")
    limit = params.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")

    if type(query) is not str:
        raise ValueError("query must be a string")
    if action in {"modules", "categories"}:
        if query:
            raise ValueError("query must be empty for module and category catalogs")
    elif not query.strip():
        raise ValueError("query is required")

    from ansa import constants
    version = constants.app_version
    if type(version) is not str or not version.strip():
        raise ValueError("ANSA constants.app_version must be a non-empty string")

    if action in {"modules", "categories"}:
        return api_catalog.build_catalog(
            MODULES, version, module=module, include_categories=action == "categories"
        )

    if action == "lookup":
        path = query if query.startswith("ansa.") else "ansa." + (module + "." if module else "") + query
        documentation = api_documentation.lookup(path, version, MODULES)
        if documentation is not None:
            return {"version": version, "api": path, **documentation}
        obj = resolve_api(path)
        return {"version": version, "api": path, **_runtime_documentation(path, obj, version)}

    terms = query.casefold().split()
    selected_modules = (module,) if module else MODULES
    documented = api_documentation.search(query, selected_modules, version, limit)
    matches = list(documented["matches"]) if documented is not None else []
    documented_ids = {
        item for item in documented.get("_api_ids", ())
        if type(item) is str
    } if documented is not None else set()
    if documented is not None and not documented_ids:
        documented_ids = {item["api"] for item in matches}
    source_truncated = documented["source_truncated"] if documented is not None else False
    matches_truncated = documented["matches_truncated"] if documented is not None else False

    # Runtime introspection is an explicitly marked fallback for APIs omitted
    # from local help or for installations whose local help is incomplete.
    use_runtime_fallback = documented is None or not documented["complete"] or not matches
    runtime_matches = []
    if use_runtime_fallback:
        for name in selected_modules:
            try:
                source = api_catalog.load_namespace(name)
                if name == "constants" and not inspect.ismodule(source):
                    namespace = vars(type(source))
                else:
                    namespace = vars(source)
            except Exception:
                continue
            for member, obj in namespace.items():
                if type(member) is not str or member.startswith("_"):
                    continue
                path = "ansa." + name + "." + member
                doc = inspect.getdoc(obj) or ""
                if all(term in (path + " " + doc).casefold() for term in terms):
                    runtime_matches.append({"api": path, **_runtime_documentation(path, obj, version)})

    runtime_ids = set()
    for item in runtime_matches:
        api_id = item["api"]
        runtime_ids.add(api_id)
        if api_id not in documented_ids:
            matches.append(item)
    matches.sort(key=lambda item: (not all(t in item["api"].casefold() for t in terms), item["api"]))
    total = len(documented_ids | runtime_ids) if documented is not None else len(runtime_ids)
    matches_truncated |= len(matches) > limit or (documented is not None and documented["matches_truncated"])
    if documented is not None and documented["matches"]:
        source = documented["source"]
        source_type = documented.get("source_type", "official_help")
        complete = documented["complete"] and not use_runtime_fallback
        if runtime_matches:
            source_type = "official_help_with_runtime_docstring_fallback"
        source_truncated |= documented["source_truncated"]
    else:
        source = "installed ANSA runtime docstrings"
        source_type = "runtime_docstring"
        complete = False
    return {"version": version, "total": total, "matches": matches[:limit], "source": source,
            "source_type": source_type,
            "complete": complete, "source_truncated": source_truncated,
            "matches_truncated": matches_truncated}


def _runtime_documentation(path, obj, version):
    documentation = inspect.getdoc(obj) or ""
    documentation = documentation[:16384]
    try:
        signature = inspect.signature(obj)
        signature_text = path.rsplit(".", 1)[-1] + str(signature)
        parameters = ", ".join(str(item) for item in signature.parameters.values())
        returns = "" if signature.return_annotation is inspect.Signature.empty else str(signature.return_annotation)
    except (TypeError, ValueError):
        signature_text, parameters, returns = "", "", ""
    examples = []
    for match in re.finditer(r"(?ms)^\s*Examples?\s*:?\s*\n(.*?)(?=\n\s*[A-Z][A-Za-z ]+\s*:?\s*\n|\Z)", documentation):
        example = match.group(1).strip()
        if example:
            examples.append(example[:4096])
        if len(examples) == 4:
            break
    return {
        "signature": signature_text,
        "parameters": parameters,
        "returns": returns,
        "examples": examples,
        "documentation": documentation,
        "source": "ANSA runtime docstring fallback",
        "source_type": "runtime_docstring",
        "source_file": None,
        "source_version": version,
        "complete": False,
        "source_truncated": len(inspect.getdoc(obj) or "") > 16384,
    }


def execute(state, tool, params):
    state.assert_main()
    if tool == "script_validate":
        return validate(params["script"])
    if tool == "api_lookup":
        return api_reference(params)
    if tool == "script_execute":
        return execute_script(params)
    raise ValueError("Unsupported script/reference operation")
