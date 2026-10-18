"""Run a bounded NASTRAN model-file inspection batch on ANSA's main thread."""

import csv
import hashlib
import io
import json
import os
import stat
import uuid
from pathlib import Path, PureWindowsPath

from ansa_mcp_shared.security import verify_private_path

from . import (
    _component_has_reparse_point,
    _model_save,
    _params,
    _save_path,
    entity_catalog,
    geometry,
    model_files,
)


_ALLOWED_SUFFIXES = frozenset({".ansa", ".step", ".stp"})
_COUNT_FIELDS = ("parts", "faces", "nodes", "shells", "solids", "elements")
_CSV_FIELDS = (
    "input_path", "ok", "import_ok", *_COUNT_FIELDS,
    "largest_face_id", "largest_face_area", "top_face_id", "top_face_max_z",
    "sha256", "size_bytes", "import_status", "model_epoch", "error",
)
_MAX_SCAN_ITEMS = 1000
_MAX_CASES = 10
_MAX_PATH_LENGTH = 4096
_MAX_ERROR_LENGTH = 512
_REQUEST_BUDGET = 128 * 1024
_REPORT_BUDGET = 512 * 1024


def _error(exc):
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LENGTH]


def _append_error(current, addition):
    if not current:
        return addition[:_MAX_ERROR_LENGTH]
    separator = "; "
    addition = addition[:_MAX_ERROR_LENGTH - len(separator)]
    available = _MAX_ERROR_LENGTH - len(separator) - len(addition)
    return current[:max(0, available)] + separator + addition


def _limit_path(value):
    if type(value) is not str or len(value) <= _MAX_PATH_LENGTH:
        return value, False
    return value[:_MAX_PATH_LENGTH], True


def _limit_path_field(record, key):
    value, truncated = _limit_path(record.get(key))
    if type(value) is str:
        record[key] = value
    if truncated:
        record[key + "_truncated"] = True


def _limit_probe_paths(probe):
    if type(probe) is not dict or type(probe.get("model")) is not dict:
        return
    _limit_path_field(probe["model"], "database")


def _limit_result_paths(result):
    for key in ("root_path", "original_model_path", "current_model_path"):
        _limit_path_field(result, key)
    for row in result.get("cases", ()):
        if type(row) is dict:
            _limit_path_field(row, "path")
            _limit_path_field(row, "input_path")
    for key in ("backup", "restore"):
        record = result.get(key)
        if type(record) is dict:
            _limit_path_field(record, "path")
            if key == "restore":
                _limit_path_field(record, "current_model_path")
                _limit_probe_paths(record.get("probe"))
    for artifact in result.get("artifacts", ()):
        if type(artifact) is dict:
            _limit_path_field(artifact, "path")
    _limit_probe_paths(result.get("current_model_probe"))


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def _validate_path_text(value, label):
    if type(value) is not str or not value or len(value) > _MAX_PATH_LENGTH:
        raise TypeError(f"{label} must be a non-empty path string of at most 4096 characters")
    parts = (*PureWindowsPath(value).parts, *Path(value).parts)
    if any(part in (".", "..") for part in parts):
        raise ValueError(f"{label} must not contain traversal components")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if _component_has_reparse_point(path):
        raise ValueError(f"{label} must not traverse symlinks or reparse points")
    return path


def _root_directory(value):
    path = _validate_path_text(value, "root_path")
    try:
        info = os.lstat(path)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("root_path is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x0400:
        raise ValueError("root_path must not be a symlink or reparse point")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("root_path must be a directory")
    if os.path.normcase(os.path.normpath(str(path))) != os.path.normcase(
        os.path.normpath(str(resolved))
    ):
        raise ValueError("root_path must name its real directory directly")
    return resolved


def _discover_files(root, max_cases):
    scanned = 0
    pending = [root]
    candidates = []
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                ordered = []
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_SCAN_ITEMS:
                        raise ValueError(
                            f"benchmark root exceeds the {_MAX_SCAN_ITEMS}-item scan limit"
                        )
                    ordered.append(entry)
        except OSError as exc:
            raise ValueError("could not scan benchmark directory") from exc
        ordered.sort(key=lambda item: item.name.casefold())
        for entry in ordered:
            if entry.name in (".", ".."):
                raise ValueError("benchmark root contains a traversal entry")
            path = Path(entry.path)
            try:
                info = os.lstat(path)
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise ValueError("benchmark entry is unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x0400:
                raise ValueError("benchmark root contains a symlink or reparse point")
            try:
                common = os.path.commonpath((str(root), str(resolved)))
            except ValueError as exc:
                raise ValueError("benchmark entry escapes root_path") from exc
            if os.path.normcase(os.path.normpath(common)) != os.path.normcase(
                os.path.normpath(str(root))
            ):
                raise ValueError("benchmark entry escapes root_path")
            if stat.S_ISDIR(info.st_mode):
                pending.append(resolved)
            elif stat.S_ISREG(info.st_mode):
                if path.suffix.casefold() in _ALLOWED_SUFFIXES:
                    if info.st_size <= 0:
                        raise ValueError("supported benchmark source is empty")
                    candidates.append((info.st_size, str(path), path))
            else:
                raise ValueError("benchmark root contains a non-regular entry")

    candidates.sort(key=lambda item: (item[0], item[1].casefold()))
    selected = []
    for size, _sort_path, path in candidates[:max_cases]:
        if len(str(path)) > _MAX_PATH_LENGTH:
            raise ValueError("benchmark source path exceeds 4096 characters")
        try:
            source = model_files._input_path(str(path))
        except Exception as exc:
            raise ValueError("benchmark source path is unavailable") from exc
        if len(str(source)) > _MAX_PATH_LENGTH:
            raise ValueError("benchmark source path exceeds 4096 characters")
        try:
            fingerprint = model_files._fingerprint(source)
        except Exception as exc:
            raise ValueError("benchmark source could not be fingerprinted") from exc
        if fingerprint["size_bytes"] != size:
            raise ValueError("benchmark source changed during discovery")
        selected.append({"path": source, "fingerprint": fingerprint})
    if not selected:
        raise ValueError("no non-empty .ansa, .step, or .stp benchmark files were found")
    return selected


def _fingerprints_match(left, right):
    return (left.get("size_bytes"), left.get("sha256")) == (
        right.get("size_bytes"), right.get("sha256")
    )


def _event_set(value):
    return bool(value is not None and callable(getattr(value, "is_set", None)) and value.is_set())


def _assert_grant(state, batch):
    state.assert_main()
    state.check_conflict()
    if state.identity() != batch["identity"]:
        raise RuntimeError("benchmark process or session identity changed")
    if not _event_set(getattr(state, "connected", None)) or _event_set(
        getattr(state, "stopped", None)
    ):
        raise RuntimeError("benchmark backend authorization is no longer active")
    grant = getattr(state, "authorization", None)
    if type(grant) is not dict or grant != batch["grant"] or grant.get("active") is not True:
        raise RuntimeError("benchmark backend grant changed")
    if getattr(state, "connected_backend_id", None) != batch["grant"].get("backend_id"):
        raise RuntimeError("benchmark backend identity changed")
    read_authorization = getattr(state, "_read_authorization", None)
    if not callable(read_authorization) or read_authorization() != batch["grant"]:
        raise RuntimeError("benchmark persisted authorization changed")


def _capture_context(state, deck, batch):
    _assert_grant(state, batch)
    probe = state.probe()
    model = probe.get("model")
    if (type(model) is not dict or type(model.get("epoch")) is not int
            or model["epoch"] <= 0 or model.get("write_enabled") is not True
            or probe.get("deck") != deck or probe.get("connected") is not True
            or probe.get("stopped") is not False
            or probe.get("persistence_blocked") is not False
            or probe.get("authorization") != batch["grant"]):
        raise RuntimeError("benchmark model context or write authorization is unavailable")
    identity = {key: probe.get(key) for key in batch["identity"]}
    if identity != batch["identity"]:
        raise RuntimeError("benchmark process or session probe changed")
    database = model.get("database")
    if type(database) is not str:
        raise RuntimeError("benchmark model database path is unavailable")
    context = {
        **identity,
        "backend_id": batch["grant"]["backend_id"],
        "generation": batch["grant"]["generation"],
        "deck": deck,
        "epoch": model["epoch"],
        "database": database,
    }
    return context, probe


class _ContextVerificationError(RuntimeError):
    pass


def _assert_context(state, expected, deck, batch):
    try:
        actual, probe = _capture_context(state, deck, batch)
    except Exception as exc:
        raise _ContextVerificationError(
            "benchmark model context could not be verified: " + _error(exc)
        ) from exc
    if actual != expected:
        raise _ContextVerificationError("benchmark model context changed during a case")
    return probe


def _public_probe(probe):
    if type(probe) is not dict:
        return None
    model = probe.get("model")
    if type(model) is not dict:
        model = {}
    result = {
        key: probe.get(key)
        for key in ("pid", "process_start", "instance_id", "session_id", "thread_id",
                    "main_thread", "deck", "version", "connected", "stopped")
        if probe.get(key) is not None
    }
    result["model"] = {
        key: model.get(key)
        for key in ("epoch", "identity_hint", "detection", "database", "write_enabled")
        if model.get(key) is not None
    }
    if type(result["model"].get("identity_hint")) is str:
        result["model"]["identity_hint"] = result["model"]["identity_hint"][:256]
    _limit_path_field(result["model"], "database")
    return result


def _safe_probe(state):
    try:
        return _public_probe(state.probe())
    except Exception as exc:
        return {"error": _error(exc)}


def _open_model(state, base, constants, path, batch):
    _assert_grant(state, batch)
    previous_epoch = getattr(state, "model_epoch", None)
    state.invalidate_model()
    status = base.Open(str(path))
    if type(status) is not int or status != 0:
        raise RuntimeError(f"base.Open returned {status!r}; model effects may be partial")
    database = base.DataBaseName()
    if type(database) is not str or not model_files._same_path(database, path):
        raise RuntimeError("base.Open succeeded but DataBaseName does not match the requested file")
    deck = base.CurrentDeck()
    nastran = getattr(constants, "NASTRAN", None)
    if type(nastran) is not int or type(deck) is not int or deck != nastran:
        raise RuntimeError("base.Open did not leave the requested file in the NASTRAN deck")
    context, probe = _capture_context(state, nastran, batch)
    if type(previous_epoch) is not int or context["epoch"] <= previous_epoch:
        raise RuntimeError("base.Open did not produce a newly probed model epoch")
    return context, probe


def _summary_counts(base, constants, deck):
    summary = entity_catalog._model_summary(base, constants, deck)
    if type(summary) is not dict or summary.get("counts_complete") is not True:
        raise RuntimeError("model summary did not verify all benchmark counts")
    counts = summary.get("entity_counts")
    if type(counts) is not dict:
        raise RuntimeError("model summary returned no entity counts")
    selected = {name: counts.get(name) for name in _COUNT_FIELDS}
    if any(type(value) is not int or value < 0 for value in selected.values()):
        raise RuntimeError("model summary contains missing or invalid benchmark counts")
    return selected


def _rank_faces(base, deck):
    pairs = geometry._source_faces(base, deck, None, "all")
    largest_rows = geometry._face_rows(base, deck, "largest_area", None, pairs)
    top_rows = geometry._face_rows(base, deck, "highest_z", None, pairs)
    result = {
        "largest_face_id": None,
        "largest_face_area": None,
        "top_face_id": None,
        "top_face_max_z": None,
    }
    if largest_rows:
        # geometry._face_rows implements geometry_select ordering and stable ID ties.
        largest = largest_rows[0]
        result["largest_face_id"] = largest["_id"]
        result["largest_face_area"] = largest["area"]
    if top_rows:
        top = top_rows[0]
        bounds = geometry._bounds(base, [top["_entity"]])
        if bounds is None:
            raise RuntimeError("top FACE bounds could not be read")
        result["top_face_id"] = top["_id"]
        result["top_face_max_z"] = bounds[5]
    return result


def _initial_rows(files):
    rows = []
    for item in files:
        fingerprint = item["fingerprint"]
        row = {
            "input_path": fingerprint["path"],
            "sha256": fingerprint["sha256"],
            "size_bytes": fingerprint["size_bytes"],
            "ok": False,
            "import_ok": False,
            "import_status": "not_started",
            "model_epoch": None,
            "error": None,
        }
        row.update({name: None for name in _COUNT_FIELDS})
        row.update({
            "largest_face_id": None,
            "largest_face_area": None,
            "top_face_id": None,
            "top_face_max_z": None,
        })
        rows.append(row)
    return rows


def _case_source_unchanged(item):
    current = model_files._fingerprint(item["path"])
    return _fingerprints_match(item["fingerprint"], current)


def _run_cases(state, base, constants, deck, batch, files, rows, current_context):
    attempted = 0
    stop = False
    for item, row in zip(files, rows):
        if stop:
            break
        attempted += 1
        case_context = None
        open_started = False
        try:
            _assert_context(state, current_context, deck, batch)
            if not _case_source_unchanged(item):
                raise RuntimeError("benchmark source hash or size changed before Open")
            open_started = True
            case_context, _probe = _open_model(state, base, constants, item["path"], batch)
            current_context = case_context
            row["import_ok"] = True
            row["import_status"] = "opened"
            row["model_epoch"] = case_context["epoch"]
            row.update(_summary_counts(base, constants, deck))
            row.update(_rank_faces(base, deck))
            _assert_context(state, case_context, deck, batch)
            if not _case_source_unchanged(item):
                raise RuntimeError("benchmark source hash or size changed during inspection")
            row["ok"] = True
            row["import_status"] = "inspected"
        except Exception as exc:
            row["error"] = _error(exc)
            if row["import_status"] == "not_started":
                row["import_status"] = "failed"
            if case_context is None:
                if open_started:
                    current_context = None
                stop = True
                continue
            row["import_status"] = "partial"
            if isinstance(exc, _ContextVerificationError):
                stop = True
                continue
            try:
                _assert_context(state, case_context, deck, batch)
                if not _case_source_unchanged(item):
                    stop = True
                else:
                    current_context = case_context
            except Exception:
                stop = True
    return attempted, current_context


def _restore(
    state, base, constants, backup, backup_fingerprint, baseline_counts, batch,
    expected_context,
):
    receipt = {
        "attempted": False,
        "confirmed": False,
        "possible_effects": False,
        "path": backup["path"],
        "error": None,
        "probe": None,
        "counts": None,
    }
    try:
        _assert_grant(state, batch)
        if expected_context is None:
            raise RuntimeError("benchmark current model context is unknown; restore Open is forbidden")
        _assert_context(state, expected_context, constants.NASTRAN, batch)
        if not _fingerprints_match(backup_fingerprint, model_files._fingerprint(Path(backup["path"]))):
            raise RuntimeError("verified backup changed before restore")
        receipt["attempted"] = True
        receipt["possible_effects"] = True
        context, probe = _open_model(state, base, constants, Path(backup["path"]), batch)
        counts = _summary_counts(base, constants, constants.NASTRAN)
        receipt["counts"] = counts
        _assert_context(state, context, constants.NASTRAN, batch)
        after_fingerprint = model_files._fingerprint(Path(backup["path"]))
        if not _fingerprints_match(backup_fingerprint, after_fingerprint):
            raise RuntimeError("verified backup changed during restore")
        if counts != baseline_counts:
            raise RuntimeError("restored backup counts do not match the original model readback")
        receipt.update({
            "confirmed": True,
            "possible_effects": False,
            "model_epoch": context["epoch"],
            "current_model_path": context["database"],
            "counts": counts,
            "probe": _public_probe(probe),
        })
    except Exception as exc:
        receipt["error"] = _error(exc)
        if expected_context is None or not receipt["confirmed"]:
            receipt["possible_effects"] = True
        receipt["probe"] = _safe_probe(state)
        try:
            database = base.DataBaseName()
            receipt["current_model_path"] = database if type(database) is str else None
        except Exception:
            receipt["current_model_path"] = None
    return receipt


def _current_source_hashes_unchanged(files):
    try:
        return all(_case_source_unchanged(item) for item in files)
    except Exception:
        return False


def _csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(_CSV_FIELDS)
    for row in rows:
        writer.writerow([row.get(name) if row.get(name) is not None else "" for name in _CSV_FIELDS])
    return stream.getvalue().encode("utf-8")


def _artifact_record(path, expected_payload=None):
    if len(str(path)) > _MAX_PATH_LENGTH:
        raise ValueError("benchmark report path exceeds 4096 characters")
    fingerprint = model_files._fingerprint(path, allow_empty=True)
    record = {key: fingerprint[key] for key in ("path", "size_bytes", "sha256")}
    if expected_payload is not None and (
        record["size_bytes"] != len(expected_payload)
        or record["sha256"] != hashlib.sha256(expected_payload).hexdigest()
    ):
        raise RuntimeError("benchmark report fingerprint does not match the complete payload")
    return record


class _ReportWriteError(RuntimeError):
    def __init__(self, cause, artifacts):
        super().__init__(str(cause))
        self.artifacts = artifacts


def _write_reports(session_dir, report, rows):
    directory = Path(session_dir)
    if not directory.is_absolute() or not directory.is_dir() or _component_has_reparse_point(directory):
        raise ValueError("state.session_dir is not a verified real directory")
    resolved = directory.resolve(strict=True)
    if os.path.normcase(os.path.normpath(str(directory))) != os.path.normcase(
        os.path.normpath(str(resolved))
    ):
        raise ValueError("state.session_dir does not resolve to itself")
    verify_private_path(directory)

    json_payload = _json_bytes(report)
    csv_payload = _csv_bytes(rows)
    if len(json_payload) + len(csv_payload) > _REPORT_BUDGET:
        raise ValueError("benchmark result exceeds the 512 KiB report budget")
    stem = "benchmark-" + uuid.uuid4().hex
    paths = {
        "json": directory / (stem + ".json"),
        "csv": directory / (stem + ".csv"),
    }
    payloads = {"csv": csv_payload, "json": json_payload}
    projected_artifacts = [
        {
            "kind": kind,
            "path": str(paths[kind]),
            "size_bytes": len(payloads[kind]),
            "sha256": "0" * 64,
            "complete": True,
            **({"valid_json": True} if kind == "json" else {}),
        }
        for kind in ("csv", "json")
    ]
    projected_result = {**report, "artifacts": projected_artifacts}
    if len(_json_bytes(projected_result)) > _REPORT_BUDGET:
        raise ValueError("benchmark response exceeds the 512 KiB report budget")
    artifacts = []
    created = set()
    completed = set()
    try:
        if any(path.exists() or path.is_symlink() for path in paths.values()):
            raise FileExistsError("benchmark report path already exists")
        if any(len(str(path)) > _MAX_PATH_LENGTH for path in paths.values()):
            raise ValueError("benchmark report path exceeds 4096 characters")
        for kind in ("csv", "json"):
            payload_text = payloads[kind].decode("utf-8")
            with paths[kind].open("x", encoding="utf-8", newline="") as stream:
                created.add(kind)
                written = stream.write(payload_text)
                if type(written) is not int or written != len(payload_text):
                    raise OSError("benchmark report write was incomplete")
            artifact = {
                "kind": kind,
                **_artifact_record(paths[kind], payloads[kind]),
                "complete": True,
            }
            completed.add(kind)
            if kind == "json":
                artifact["valid_json"] = True
            artifacts.append(artifact)
    except Exception as exc:
        for kind, path in paths.items():
            if kind not in created or any(item["path"] == str(path) for item in artifacts):
                continue
            try:
                if path.is_file() and not _component_has_reparse_point(path):
                    artifact = {
                        "kind": kind,
                        **_artifact_record(path),
                        "complete": kind in completed,
                    }
                    if kind == "json":
                        artifact["valid_json"] = kind in completed
                    artifacts.append(artifact)
                else:
                    raise RuntimeError("partial benchmark report artifact could not be verified")
            except Exception as artifact_exc:
                artifact_path, truncated = _limit_path(str(path))
                artifact = {
                    "kind": kind,
                    "path": artifact_path,
                    "complete": kind in completed,
                    "error": _error(artifact_exc),
                }
                if truncated:
                    artifact["path_truncated"] = True
                if kind == "json":
                    artifact["valid_json"] = False
                artifacts.append(artifact)
        artifacts.sort(key=lambda item: ("csv", "json").index(item["kind"]))
        raise _ReportWriteError(exc, artifacts) from exc
    return artifacts


def _bounded_failure_summary(result):
    def path_record(source, keys):
        record = {}
        if type(source) is not dict:
            return record
        for key in keys:
            value = source.get(key)
            if key in ("path", "input_path", "current_model_path"):
                value, truncated = _limit_path(value)
                if value is not None:
                    record[key] = value
                if truncated or source.get(key + "_truncated") is True:
                    record[key + "_truncated"] = True
            elif key == "error":
                if type(value) is str:
                    record[key] = value[:_MAX_ERROR_LENGTH]
            elif key == "sha256":
                if type(value) is str:
                    record[key] = value[:64]
            elif key in ("readback_counts", "counts"):
                if type(value) is dict:
                    record[key] = {
                        name: count for name, count in value.items()
                        if name in _COUNT_FIELDS and type(count) is int
                    }
            elif type(value) is str:
                record[key] = value[:64]
            elif type(value) in (bool, int):
                record[key] = value
        return record

    cases = []
    for row in result.get("cases", ()):
        if type(row) is not dict:
            continue
        case = path_record(
            row,
            ("input_path", "ok", "import_ok", "import_status", "model_epoch", "size_bytes", "sha256", "error"),
        )
        case["truncated"] = True
        cases.append(case)

    artifacts = []
    for item in result.get("artifacts", ()):
        if type(item) is not dict:
            continue
        artifact = path_record(
            item, ("path", "size_bytes", "sha256", "complete", "valid_json", "error")
        )
        if type(item.get("kind")) is str:
            artifact["kind"] = item["kind"][:16]
        artifacts.append(artifact)

    error = result.get("error")
    error = error if type(error) is str else "benchmark response exceeded the 512 KiB budget"
    root_path, root_truncated = _limit_path(result.get("root_path"))
    original_path, original_truncated = _limit_path(result.get("original_model_path"))
    current_path, current_truncated = _limit_path(result.get("current_model_path"))
    summary = {
        "ok": False,
        "truncated": True,
        "error": _append_error(error, "benchmark response exceeded the 512 KiB budget"),
        "root_path": root_path,
        "case_count": result.get("case_count", 0),
        "attempted_case_count": result.get("attempted_case_count", 0),
        "cases": cases,
        "backup": path_record(
            result.get("backup"),
            ("path", "saved", "verified", "size_bytes", "sha256", "readback_counts"),
        ),
        "restore": path_record(
            result.get("restore"),
            ("path", "attempted", "confirmed", "possible_effects", "current_model_path",
             "model_epoch", "counts", "error"),
        ),
        "original_model_path": original_path,
        "current_model_path": current_path,
        "source_hash_unchanged": result.get("source_hash_unchanged") is True,
        "all_cases_verified": False,
        "possible_effects": result.get("possible_effects") is True,
        "artifacts": artifacts,
    }
    if root_truncated or result.get("root_path_truncated") is True:
        summary["root_path_truncated"] = True
    if original_truncated or result.get("original_model_path_truncated") is True:
        summary["original_model_path_truncated"] = True
    if current_truncated or result.get("current_model_path_truncated") is True:
        summary["current_model_path_truncated"] = True
    json_artifact = next((item for item in artifacts if item.get("kind") == "json"), None)
    summary["report_location"] = json_artifact.get("path") if json_artifact else None
    if len(_json_bytes(summary)) > _REPORT_BUDGET:
        for row in summary["cases"]:
            value = row.get("input_path")
            if type(value) is str and len(value) > 1024:
                row["input_path"] = value[:1024]
                row["input_path_truncated"] = True
            if type(row.get("error")) is str:
                row["error"] = row["error"][:128]
        summary["error"] = summary["error"][:256]
        for key in ("root_path", "original_model_path", "current_model_path"):
            if type(summary.get(key)) is str and len(summary[key]) > 1024:
                summary[key] = summary[key][:1024]
                summary[key + "_truncated"] = True
        for key in ("backup", "restore"):
            record = summary.get(key)
            if type(record) is dict:
                for path_key in ("path", "current_model_path"):
                    if type(record.get(path_key)) is str and len(record[path_key]) > 1024:
                        record[path_key] = record[path_key][:1024]
                        record[path_key + "_truncated"] = True
        if len(_json_bytes(summary)) > _REPORT_BUDGET:
            summary = {
                "ok": False,
                "truncated": True,
                "error": summary["error"][:_MAX_ERROR_LENGTH],
                "case_count": summary["case_count"],
                "attempted_case_count": summary["attempted_case_count"],
                "backup": summary["backup"],
                "restore": summary["restore"],
                "current_model_path": summary["current_model_path"],
                "possible_effects": summary["possible_effects"],
                "artifacts": summary["artifacts"],
                "report_location": summary["report_location"],
            }
    return summary


def _validate_params(params):
    params = _params(params, ("root_path", "backup_path"), ("max_cases",))
    max_cases = params.get("max_cases", _MAX_CASES)
    if type(max_cases) is not int or not 1 <= max_cases <= _MAX_CASES:
        raise ValueError("max_cases must be an integer from 1 to 10")
    _validate_path_text(params["root_path"], "root_path")
    _validate_path_text(params["backup_path"], "backup_path")
    encoded = _json_bytes({"root_path": params["root_path"],
                           "backup_path": params["backup_path"], "max_cases": max_cases})
    if len(encoded) > _REQUEST_BUDGET:
        raise ValueError("benchmark request exceeds the 128 KiB budget")
    return max_cases


def execute(state, tool, params):
    """Inspect an explicit file batch and report the actual backup restoration state."""
    state.assert_main()
    if type(tool) is not str or tool != "benchmark_run":
        raise ValueError(f"unsupported benchmark operation: {tool!r}")
    max_cases = _validate_params(params)
    root = _root_directory(params["root_path"])
    backup_path, _prior = _save_path(params["backup_path"], False)

    from ansa import base, constants

    if not callable(getattr(base, "Open", None)):
        raise NotImplementedError("documented ANSA base.Open is unavailable")
    nastran = getattr(constants, "NASTRAN", None)
    if type(nastran) is not int:
        raise RuntimeError("documented NASTRAN deck constant is unavailable")
    state.check_conflict()
    initial_probe = state.probe()
    grant = initial_probe.get("authorization")
    if type(grant) is not dict:
        raise RuntimeError("benchmark requires an active backend grant")
    batch = {"identity": state.identity(), "grant": dict(grant)}
    initial_context, _probe = _capture_context(state, nastran, batch)
    baseline_counts = _summary_counts(base, constants, nastran)
    files = _discover_files(root, max_cases)
    for item in files:
        if len(str(item["path"])) > _MAX_PATH_LENGTH:
            raise ValueError("benchmark source path exceeds 4096 characters; no backup was saved")
        if model_files._same_path(item["path"], backup_path):
            raise ValueError("backup_path must differ from every benchmark source")
    if initial_context["database"] and model_files._same_path(
        initial_context["database"], backup_path
    ):
        raise ValueError("backup_path must differ from the current model path")
    _assert_context(state, initial_context, nastran, batch)

    rows = _initial_rows(files)
    backup = {"path": str(backup_path), "saved": False, "verified": False}
    backup_fingerprint = None
    backup_recoverable = False
    last_trusted_context = initial_context
    workflow_error = None
    restore = {
        "attempted": False, "confirmed": False, "possible_effects": False,
        "path": str(backup_path),
        "error": "backup was not verified", "probe": None, "counts": None,
    }
    attempted = 0
    current_probe = _public_probe(initial_probe)

    try:
        saved = _model_save(base, constants, {"path": str(backup_path), "overwrite": False})
        if type(saved) is not dict or saved.get("saved") is not True or not model_files._same_path(
            saved.get("path"), backup_path
        ):
            raise RuntimeError("silent SaveAs did not confirm the requested backup path")
        backup["saved"] = True
        backup_fingerprint = model_files._fingerprint(backup_path)
        backup.update({key: backup_fingerprint[key] for key in ("size_bytes", "sha256")})
        backup_recoverable = True
        after_save_context, _after_save_probe = _capture_context(state, nastran, batch)
        after_save_counts = _summary_counts(base, constants, nastran)
        if after_save_context != initial_context or after_save_counts != baseline_counts:
            raise RuntimeError("silent SaveAs changed the live model context or count readback")

        # Read the saved database back before replacing it with any batch input.
        last_trusted_context = None
        backup_context, backup_probe = _open_model(
            state, base, constants, backup_path, batch
        )
        last_trusted_context = backup_context
        backup_counts = _summary_counts(base, constants, nastran)
        backup["readback_counts"] = backup_counts
        _assert_context(state, backup_context, nastran, batch)
        after_open_fingerprint = model_files._fingerprint(backup_path)
        if not _fingerprints_match(backup_fingerprint, after_open_fingerprint):
            raise RuntimeError("verified backup changed during backup readback")
        if backup_counts != baseline_counts:
            raise RuntimeError("backup readback counts do not match the original model")
        backup["readback_counts"] = backup_counts
        backup["verified"] = True
        current_probe = _public_probe(backup_probe)
        attempted, last_trusted_context = _run_cases(
            state, base, constants, nastran, batch, files, rows, backup_context
        )
    except Exception as exc:
        workflow_error = _error(exc)
        current_probe = _safe_probe(state)

    if backup_recoverable:
        restore = _restore(
            state, base, constants, backup, backup_fingerprint, baseline_counts, batch,
            last_trusted_context,
        )
        current_probe = restore.get("probe") or _safe_probe(state)
    else:
        restore["probe"] = current_probe
    possible_effects = bool(backup.get("saved") and not restore.get("confirmed"))
    restore["possible_effects"] = bool(
        restore.get("possible_effects") or possible_effects
    )

    source_hash_unchanged = _current_source_hashes_unchanged(files)
    cases_ok = bool(rows) and attempted == len(rows) and all(row["ok"] is True for row in rows)
    current_model_path = restore.get("current_model_path")
    if current_model_path is None and type(current_probe) is dict:
        current_model_path = (current_probe.get("model") or {}).get("database")
    result = {
        "ok": False,
        "root_path": str(root),
        "case_count": len(rows),
        "attempted_case_count": attempted,
        "cases": rows,
        "original_counts": baseline_counts,
        "backup": backup,
        "restore": restore,
        "original_model_path": initial_context["database"],
        "current_model_path": current_model_path,
        "current_model_probe": current_probe,
        "source_hash_unchanged": source_hash_unchanged,
        "all_cases_verified": cases_ok,
        "possible_effects": possible_effects,
        "error": workflow_error,
        "artifacts": [],
    }
    result["ok"] = bool(
        cases_ok and backup.get("verified") is True
        and restore.get("confirmed") is True and source_hash_unchanged
    )
    _limit_result_paths(result)

    report = {key: value for key, value in result.items() if key != "artifacts"}
    try:
        artifacts = _write_reports(state.session_dir, report, rows)
        result["artifacts"] = artifacts
    except Exception as exc:
        result["artifacts"] = list(getattr(exc, "artifacts", ()))
        result["error"] = _append_error(
            result["error"], "report write: " + _error(exc)
        )
        result["ok"] = False
    try:
        if len(_json_bytes(result)) > _REPORT_BUDGET:
            result["ok"] = False
            result["error"] = _append_error(
                result["error"], "benchmark response exceeds the 512 KiB budget"
            )
    except (TypeError, ValueError) as exc:
        result["ok"] = False
        result["error"] = _append_error(result["error"], _error(exc))
    try:
        if len(_json_bytes(result)) > _REPORT_BUDGET:
            result = _bounded_failure_summary(result)
    except (TypeError, ValueError) as exc:
        result["ok"] = False
        result["error"] = _append_error(result.get("error"), _error(exc))
        result = _bounded_failure_summary(result)
    return result


__all__ = ["execute"]
