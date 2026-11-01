"""Immutable, context-bound structured-mesh evidence in the private session."""

import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from ansa_mcp_shared.framing import MAX_FRAME_BYTES

from . import _component_has_reparse_point


_MAX_FILE_BYTES = 64 * 1024 * 1024
_PAGE_BYTES = 192 * 1024


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_result(value, depth=0):
    """Normalize supported Python mappings before hashing persisted JSON.

    Integer keys are used by native FACE/volume maps. Their JSON names must be
    sorted as strings on both sides of persistence; collisions are rejected.
    """
    if depth > 64:raise ValueError('structured evidence exceeds the JSON depth budget')
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if type(key) not in (str, int):
                raise ValueError('structured evidence mapping keys must be strings or integer IDs')
            name = str(key)
            if name in result:raise ValueError('ambiguous structured evidence JSON mapping key')
            result[name] = _json_result(item, depth+1)
        return result
    if type(value) in (list, tuple):return [_json_result(item, depth+1) for item in value]
    return value


def _context(state, deck):
    return {"deck": deck, "epoch": state.model_epoch,
            "instance_id": state.instance_id, "session_id": state.session_id}


def _path(state, report_id):
    if type(report_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", report_id):
        raise ValueError("invalid structured mesh report ID")
    root = Path(state.session_dir) / "structured-mesh-evidence"
    if _component_has_reparse_point(root):
        raise ValueError("structured mesh evidence cannot traverse reparse points")
    root.mkdir(exist_ok=True, mode=0o700)
    target = root / (report_id + ".json")
    if _component_has_reparse_point(target):
        raise ValueError("structured mesh evidence cannot traverse reparse points")
    return target


def store_report(state, deck, kind, result):
    """Persist complete evidence before returning a bounded descriptor."""
    if type(kind) is not str or type(result) is not dict:
        raise TypeError("structured mesh evidence requires a kind and object result")
    result = _json_result(result)
    body = _encoded(result)
    if len(body) > _MAX_FILE_BYTES:
        raise ValueError("structured mesh evidence exceeds the storage budget")
    report_id = uuid.uuid4().hex
    context = _context(state, deck)
    digest = hashlib.sha256(body).hexdigest()
    envelope = {"version": 1, "kind": kind, "context": context,
                "sha256": digest, "usable_for_mutation": False, "result": result}
    target = _path(state, report_id)
    raw = _encoded(envelope)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("structured mesh evidence envelope exceeds the storage budget")
    with target.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = {"ok": True, "report_id": report_id, "sha256": digest,
                  "kind": kind, "context": context, "stored_complete": True,
                  "usable_for_mutation": False,
                  "sections": {key: len(value) if isinstance(value, (dict, list)) else 1
                               for key, value in result.items()}}
    for key in ("status", "topology_status", "geometry_status", "quality_status",
                "regeneration_status", "persistence_status", "summary", "plan_sha256", "snapshot_sha256",
                "seed_snapshot_sha256", "ledger_id", "ledger_hash", "provenance_stage",
                "source_snapshot_sha256"):
        if key in result and len(_encoded(result[key])) <= 32 * 1024:
            descriptor[key] = result[key]
    return descriptor


def read_page(state, deck, report_id, section, offset=0, limit=100, *, freshness_validator=None):
    """Page one evidence section; historical evidence never authorizes writes."""
    if (type(section) is not str or not section
            or len(section) > (MAX_FRAME_BYTES // 16 if section.startswith("/") else 128)):
        raise ValueError("section must be a nonempty field name")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("invalid structured mesh page offset/limit")
    target = _path(state, report_id)
    if target.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("structured mesh evidence exceeds the read budget")
    envelope = json.loads(target.read_text(encoding="utf-8"))
    if envelope.get("version") != 1 or envelope.get("context") != _context(state, deck):
        raise ValueError("structured mesh evidence belongs to a different model context")
    result = envelope["result"]
    if hashlib.sha256(_encoded(result)).hexdigest() != envelope.get("sha256"):
        raise ValueError("structured mesh evidence checksum mismatch")
    if envelope.get("kind") in {"mesh_seed_read", "mesh_cad_map_plan", "geometry_sweep_candidates", "mesh_map_block_inspect",
                                "mesh_map_block_plan", "mesh_map_block_fill_plan", "mesh_map_blocks_plan", "mesh_scope_read"}:
        if freshness_validator is None:
            raise ValueError("structured evidence requires a native freshness check")
        freshness_validator(envelope["kind"], result)
    elif envelope.get("kind") == "geometry_features" or "feature_context" in result:
        if envelope.get("kind") != "geometry_features":
            raise ValueError("geometry feature evidence kind mismatch")
        if freshness_validator is None:
            raise ValueError("geometry feature evidence requires a native freshness check")
        freshness_validator(envelope["kind"], result)
    # A JSON pointer permits paging nested measurement arrays without returning
    # or truncating their (possibly very large) parent object. Plain top-level
    # names remain supported for existing clients.
    value = result
    parts = section[1:].split("/") if section.startswith("/") else [section]
    for part in parts:
        if re.search(r"~(?![01])", part):
            raise ValueError("invalid evidence JSON pointer escape")
        part = part.replace("~1", "/").replace("~0", "~")
        if type(value) is dict and part in value:
            value = value[part]
        elif type(value) is list and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError("unknown evidence section; inspect the report sections first")
    chunked_string = (envelope.get("kind") == "script_execute_result"
                      and type(value) is str and len(_encoded([value])) > _PAGE_BYTES)
    rows = ([{"start": index, "end": min(index + 16384, len(value)), "text": value[index:index + 16384]}
             for index in range(0, len(value), 16384)] if chunked_string else
            [{"key": key, "value": item} for key, item in value.items()]
            if type(value) is dict else value if type(value) is list else [value])
    selected = []
    for row in rows[offset:offset + limit]:
        if len(_encoded(selected + [row])) > _PAGE_BYTES:
            break
        selected.append(row)
    if offset < len(rows) and not selected:
        raise ValueError("one evidence row exceeds the page budget; page its nested JSON pointer")
    next_offset = offset + len(selected)
    return {"ok": True, "report_id": report_id, "sha256": envelope["sha256"],
            "context": envelope["context"], "usable_for_mutation": False,
            "section": section, "offset": offset, "total": len(rows),
            "paging_unit": "string_chunks" if chunked_string else "rows",
            "rows": selected, "next_offset": next_offset if next_offset < len(rows) else None,
            "complete": next_offset >= len(rows)}
