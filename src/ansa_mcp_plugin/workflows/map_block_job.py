"""Recoverable client orchestration for explicit MapBlock fill groups.

The client never performs native meshing itself. It persists each public MCP
request before sending it and resumes uncertain writes only by querying their
original task or operation ID.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping

from ..mcp.quality_workflow import NativeQualityProfile, QualityProfile


_MANIFEST_NAME = "manifest.json"
_MANIFEST_VERSION = 1
_MAX_GROUPS = 20
_MAX_VOLUMES = 20
_MAX_SOLIDS = 5000
_MAX_PROFILE_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_CALLS = 1024
_MAX_POLLS_PER_WAIT = 256
_MAX_REPORT_MEGABYTES = 16
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
_JOB_STATES = {
    "planned", "applying", "verified", "checkpointed", "awaiting_original_result",
    "needs_attention", "complete",
}
_GROUP_STATES = {"unplanned", "plan_descriptor", "planned", "applying", "verified", "checkpointed"}


class MapBlockJobError(RuntimeError):
    """The durable MapBlock job requires inspection or explicit user action."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _json_copy(value: Any, label: str) -> Any:
    try:
        return json.loads(_canonical_bytes(value).decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"{label} must be finite JSON data") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _reject_reparse_chain(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if _is_reparse(info):
            raise ValueError(f"path must not traverse symlinks or reparse points: {current}")


def _absolute_path(value: str | os.PathLike[str], label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{label} must be an absolute path")
    text = os.fspath(value)
    if type(text) is not str or not text:
        raise TypeError(f"{label} must be a non-empty path string")
    windows_parts = PureWindowsPath(text).parts
    if any(part in (".", "..") for part in windows_parts):
        raise ValueError(f"{label} must not contain traversal components")
    path = Path(text)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    _reject_reparse_chain(path)
    return path


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _normalize_binding(expected_target: Mapping[str, Any], expected_model: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(expected_target, Mapping) or not isinstance(expected_model, Mapping):
        raise TypeError("expected_target and expected_model must be mappings")
    target = _json_copy(dict(expected_target), "expected_target")
    model = _json_copy(dict(expected_model), "expected_model")
    required_target = {"backend_id", "instance_id", "session_id", "generation"}
    if (set(target) != required_target
            or any(type(target.get(key)) is not str or not target[key] for key in
                   ("backend_id", "instance_id", "session_id"))):
        raise ValueError("expected_target must contain only backend_id, instance_id, session_id and generation")
    _positive_int(target["generation"], "expected_target.generation")
    if set(model) != {"epoch"}:
        raise ValueError("expected_model must contain only epoch")
    _positive_int(model.get("epoch"), "expected_model.epoch")
    return {"expected_target": target, "expected_model": model}


def _normalize_groups(fill_groups: Any) -> list[list[int]]:
    if type(fill_groups) is not list or not 1 <= len(fill_groups) <= _MAX_GROUPS:
        raise ValueError("fill_groups must contain 1..20 explicit volume groups")
    result: list[list[int]] = []
    seen: set[int] = set()
    total = 0
    for index, group in enumerate(fill_groups):
        if type(group) not in (list, tuple) or not 1 <= len(group) <= _MAX_VOLUMES:
            raise ValueError(f"fill_groups[{index}] must contain 1..20 volume IDs")
        ids = [_positive_int(value, f"fill_groups[{index}] volume_id") for value in group]
        if len(ids) != len(set(ids)):
            raise ValueError(f"fill_groups[{index}] contains duplicate volume IDs")
        overlap = seen.intersection(ids)
        if overlap:
            raise ValueError(f"fill_groups repeat volume IDs across groups: {sorted(overlap)}")
        seen.update(ids)
        total += len(ids)
        result.append(ids)
    if total > _MAX_VOLUMES:
        raise ValueError("a MapBlock job may contain at most 20 unique volumes")
    return result


def _normalize_profile(profile: Any) -> dict[str, Any]:
    if hasattr(profile, "model_dump"):
        profile = profile.model_dump(exclude_none=True)
    if type(profile) is not dict or not profile:
        raise ValueError("an explicit, non-empty quality profile is required")
    profile = _json_copy(profile, "quality_profile")
    if len(_canonical_bytes(profile)) > _MAX_PROFILE_BYTES:
        raise ValueError("quality_profile exceeds the 64 KiB job limit")
    valid = []
    for schema in (QualityProfile, NativeQualityProfile):
        try:
            valid.append(schema.model_validate(profile).model_dump(mode="json", exclude_none=True))
        except Exception:
            continue
    if len(valid) != 1:
        raise ValueError("quality_profile must match exactly one public MCP quality profile schema")
    normalized = valid[0]
    if len(_canonical_bytes(normalized)) > _MAX_PROFILE_BYTES:
        raise ValueError("normalized quality_profile exceeds the 64 KiB job limit")
    return normalized


def _safe_regular_file(path: Path, label: str) -> os.stat_result:
    _reject_reparse_chain(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {exc}") from exc
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular non-reparse file")
    return info


def _write_all(fd: int, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting the MapBlock manifest")
        view = view[written:]
    os.fsync(fd)


def _write_manifest(path: Path, manifest: dict[str, Any], *, create: bool = False) -> None:
    body = _canonical_bytes(manifest)
    if len(body) > _MAX_MANIFEST_BYTES:
        raise ValueError("MapBlock job manifest exceeds the 64 MiB resource limit")
    _reject_reparse_chain(path)
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if create:
        flags |= os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            _write_all(fd, body)
        finally:
            os.close(fd)
        return

    _safe_regular_file(path, "job manifest")
    temporary = path.with_name(f".manifest-{uuid.uuid4().hex}.tmp")
    flags |= os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        _write_all(fd, body)
    finally:
        os.close(fd)
    try:
        _reject_reparse_chain(path)
        _safe_regular_file(path, "job manifest")
        os.replace(temporary, path)
        _safe_regular_file(path, "job manifest")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_manifest(path: Path) -> dict[str, Any]:
    info = _safe_regular_file(path, "job manifest")
    if info.st_size <= 0 or info.st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("job manifest is empty or exceeds its 64 MiB resource limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (type(value) is not dict or value.get("schema") != "ansa-map-block-job"
            or value.get("version") != _MANIFEST_VERSION):
        raise ValueError("unsupported or malformed MapBlock job manifest")
    if value.get("status") not in _JOB_STATES or type(value.get("groups")) is not list:
        raise ValueError("MapBlock job manifest has invalid state or groups")
    return value


@contextmanager
def _job_lock(manifest_path: Path):
    lock_path = manifest_path.with_name(".workflow.lock")
    _reject_reparse_chain(lock_path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("workflow lock must be a regular non-reparse file")
        if os.name == "nt":
            import msvcrt
            if info.st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MapBlockJobError("this MapBlock job is already active in another client") from exc
            unlock = lambda: msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MapBlockJobError("this MapBlock job is already active in another client") from exc
            unlock = lambda: fcntl.flock(fd, fcntl.LOCK_UN)
        try:
            yield
        finally:
            unlock()
    finally:
        os.close(fd)


def _normalized_status_result(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    return {
        "job_id": manifest["job_id"],
        "status": manifest["status"],
        "manifest_path": str(manifest_path),
        "completed_groups": sum(row["state"] == "checkpointed" for row in manifest["groups"]),
        "group_count": len(manifest["groups"]),
        "solid_ids": list(manifest.get("solid_ids", [])),
        "pending_call": None if manifest.get("pending_call_id") is None else {
            "call_id": manifest["pending_call_id"],
            "tool": _call_by_id(manifest, manifest["pending_call_id"]).get("tool"),
            "task_id": _call_by_id(manifest, manifest["pending_call_id"]).get("task_id"),
        },
        "error": manifest.get("attention"),
        "quality_summary": manifest.get("quality_summary"),
    }


def _call_by_id(manifest: dict[str, Any], call_id: str) -> dict[str, Any]:
    for record in reversed(manifest["calls"]):
        if record["call_id"] == call_id:
            return record
    raise RuntimeError("manifest pending call record is missing")


def _group(manifest: dict[str, Any], index: int) -> dict[str, Any]:
    group = manifest["groups"][index]
    if group.get("index") != index or group.get("state") not in _GROUP_STATES:
        raise ValueError("manifest group state is invalid")
    return group


def _result_payload(reply: dict[str, Any]) -> dict[str, Any]:
    data = reply.get("data")
    result = data.get("result") if type(data) is dict else None
    if type(result) is not dict:
        raise MapBlockJobError("terminal MCP task has no structured operation result")
    return result


def _has_effect_marker(value: Any) -> bool:
    if type(value) is dict:
        return any(
            (key in {"possible_effects", "recovery_required"} and child is True)
            or _has_effect_marker(child)
            for key, child in value.items()
        )
    if type(value) is list:
        return any(_has_effect_marker(child) for child in value)
    return False


class MapBlockJobClient:
    """Run/resume sequential MapBlock fill groups through injected public MCP calls.

    The callback receives ``(tool_name, strict_args)`` and must return the
    standard MCP ``structuredContent`` dictionary. The caller owns and supplies
    the immutable target/model binding.
    """

    def __init__(
        self,
        call_tool: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        poll_interval: float = 0.25,
        wait_timeout_seconds: float = 120.0,
    ) -> None:
        if not callable(call_tool):
            raise TypeError("call_tool must be an async callable")
        if (type(poll_interval) not in (int, float) or poll_interval < 0
                or type(wait_timeout_seconds) not in (int, float) or wait_timeout_seconds < 0):
            raise ValueError("poll_interval and wait_timeout_seconds must be finite nonnegative numbers")
        if not all(map(lambda value: value < float("inf"), (poll_interval, wait_timeout_seconds))):
            raise ValueError("poll_interval and wait_timeout_seconds must be finite")
        self.call_tool = call_tool
        self.poll_interval = float(poll_interval)
        self.wait_timeout_seconds = float(wait_timeout_seconds)

    async def run(
        self,
        *,
        deck: int,
        fill_groups: list[list[int]],
        quality_profile: Any,
        job_dir: str | os.PathLike[str],
        expected_target: Mapping[str, Any],
        expected_model: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create a new job directory/manifest and execute the sequential workflow."""
        if type(deck) is not int or deck <= 0:
            raise ValueError("deck must be a positive integer NASTRAN deck code")
        groups = _normalize_groups(fill_groups)
        profile = _normalize_profile(quality_profile)
        binding = _normalize_binding(expected_target, expected_model)
        directory = _absolute_path(job_dir, "job_dir")
        if directory.exists():
            raise FileExistsError("job_dir must be a new directory; existing job data is never overwritten")
        if not directory.parent.is_dir():
            raise FileNotFoundError("job_dir parent directory must already exist")
        directory.mkdir(mode=0o700)
        _reject_reparse_chain(directory)
        (directory / "backups").mkdir(mode=0o700)
        (directory / "checkpoints").mkdir(mode=0o700)
        manifest_path = directory / _MANIFEST_NAME
        manifest = {
            "schema": "ansa-map-block-job",
            "version": _MANIFEST_VERSION,
            "job_id": uuid.uuid4().hex,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "job_dir": str(directory),
            "manifest_path": str(manifest_path),
            "deck": deck,
            "binding": binding,
            "fill_groups": groups,
            "quality_profile": profile,
            "status": "planned",
            "current_group_index": 0,
            "groups": [
                {"index": index, "volume_ids": ids, "state": "unplanned", "solid_ids": []}
                for index, ids in enumerate(groups)
            ],
            "solid_ids": [],
            "quality_summary": None,
            "pending_call_id": None,
            "calls": [],
            "events": [{"at": _utc_now(), "state": "planned", "detail": "job created"}],
            "attention": None,
        }
        _write_manifest(manifest_path, manifest, create=True)
        with _job_lock(manifest_path):
            return await self._drive(manifest, manifest_path, resuming=False)

    async def resume(
        self,
        manifest_path: str | os.PathLike[str],
        *,
        expected_target: Mapping[str, Any],
        expected_model: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resume a job under its original target/model binding without replaying writes."""
        path = _absolute_path(manifest_path, "manifest_path")
        if path.name != _MANIFEST_NAME:
            raise ValueError(f"manifest_path must name {_MANIFEST_NAME}")
        binding = _normalize_binding(expected_target, expected_model)
        with _job_lock(path):
            manifest = _read_manifest(path)
            if manifest.get("manifest_path") != str(path) or manifest.get("job_dir") != str(path.parent):
                raise ValueError("manifest path binding differs from its containing job directory")
            if manifest.get("binding") != binding:
                raise ValueError("target/model differs from the immutable job binding; revalidate before resuming")
            if manifest["status"] in {"needs_attention", "complete"}:
                return _normalized_status_result(manifest, path)
            return await self._drive(manifest, path, resuming=True)

    def _persist(self, manifest: dict[str, Any], path: Path) -> None:
        manifest["updated_at"] = _utc_now()
        _write_manifest(path, manifest)

    def _event(self, manifest: dict[str, Any], state: str, detail: str, **fields: Any) -> None:
        if state not in _JOB_STATES:
            raise ValueError("invalid workflow state")
        manifest["status"] = state
        manifest["events"].append({"at": _utc_now(), "state": state, "detail": detail, **fields})
        if len(manifest["events"]) > 512:
            raise MapBlockJobError("job event history exceeded its 512-record bound")

    def _attention(self, manifest: dict[str, Any], path: Path, detail: str) -> dict[str, Any]:
        manifest["attention"] = {"at": _utc_now(), "reason": detail[:2048]}
        if manifest.get("pending_call_id") is not None:
            _call_by_id(manifest, manifest["pending_call_id"])["state"] = "needs_attention"
        self._event(manifest, "needs_attention", detail[:2048])
        self._persist(manifest, path)
        return _normalized_status_result(manifest, path)

    def _new_call_record(
        self,
        manifest: dict[str, Any],
        path: Path,
        *,
        tool: str,
        args: dict[str, Any],
        operation_id: str | None,
        phase: str,
        group_index: int | None,
        query_for: str | None = None,
    ) -> dict[str, Any]:
        if len(manifest["calls"]) >= _MAX_CALLS:
            raise MapBlockJobError("job exceeded its 1024-call resource limit")
        safe_args = _json_copy(args, "MCP request arguments")
        request = {"tool": tool, "args": safe_args}
        record = {
            "call_id": uuid.uuid4().hex,
            "operation_id": operation_id,
            "tool": tool,
            "phase": phase,
            "group_index": group_index,
            "query_for_call_id": query_for,
            "binding": copy.deepcopy(manifest["binding"]),
            "request": request,
            "request_sha256": _sha256(request),
            "created_at": _utc_now(),
            "state": "prepared",
            "task_id": None,
            "submission_reply": None,
            "query_replies": [],
            "errors": [],
        }
        manifest["calls"].append(record)
        if phase != "task_query":
            manifest["pending_call_id"] = record["call_id"]
        self._persist(manifest, path)
        return record

    def _operation_args(self, manifest: dict[str, Any], operation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "expected_target": copy.deepcopy(manifest["binding"]["expected_target"]),
            "expected_model": copy.deepcopy(manifest["binding"]["expected_model"]),
            **params,
        }

    async def _send_operation(
        self,
        manifest: dict[str, Any],
        path: Path,
        *,
        tool: str,
        params: dict[str, Any],
        phase: str,
        group_index: int | None,
    ) -> dict[str, Any]:
        operation_id = uuid.uuid4().hex
        args = self._operation_args(manifest, operation_id, params)
        record = self._new_call_record(
            manifest, path, tool=tool, args=args, operation_id=operation_id,
            phase=phase, group_index=group_index,
        )
        record["state"] = "sending"
        if phase == "fill_apply":
            _group(manifest, group_index)["state"] = "applying"
            self._event(manifest, "applying", f"apply group {group_index+1}", operation_id=operation_id)
        elif phase == "model_save":
            self._event(manifest, "verified", f"checkpoint group {group_index+1}", operation_id=operation_id)
        elif phase == "quality_report":
            self._event(manifest, "checkpointed", "running final explicit quality report", operation_id=operation_id)
        self._persist(manifest, path)
        try:
            reply = await self.call_tool(tool, args)
        except asyncio.CancelledError:
            # The durable request remains pending. Cancellation is never a write rollback.
            raise
        except Exception as exc:
            record["state"] = "submission_result_unknown"
            record["errors"].append({"at": _utc_now(), "phase": "submit", "error": f"{type(exc).__name__}: {exc}"[:2048]})
            self._event(manifest, "awaiting_original_result", f"submit result unknown for {tool}", operation_id=operation_id)
            self._persist(manifest, path)
            return await self._resolve_original(manifest, path, record, query_first=True)
        if type(reply) is not dict:
            record["state"] = "malformed_reply"
            record["submission_reply"] = {"malformed_type": type(reply).__name__, "repr": repr(reply)[:2048]}
            self._persist(manifest, path)
            return self._attention(manifest, path, f"{tool} returned no structuredContent dictionary")
        record["task_id"] = reply.get("task_id")
        try:
            record["submission_reply"] = _json_copy(reply, f"{tool} structuredContent")
        except ValueError as exc:
            record["state"] = "malformed_reply"
            record["submission_reply"] = {"malformed_type": type(reply).__name__, "repr": repr(reply)[:2048]}
            self._persist(manifest, path)
            return self._attention(manifest, path, f"{tool} returned non-JSON structuredContent: {exc}")
        record["state"] = "submitted"
        self._persist(manifest, path)  # Persist task_id as the first step after receiving it.
        status = reply.get("status")
        if status == "unknown":
            return await self._resolve_original(manifest, path, record, query_first=True)
        if status in {"queued", "running", "succeeded"} and type(record["task_id"]) is not str:
            return self._attention(manifest, path, f"{tool} returned {status} without task_id")
        if status in {"queued", "running"} and not self._verify_envelope_binding(manifest, reply, record):
            return self._attention(manifest, path, f"{tool} returned a task bound to a different operation/target/model")
        if status == "succeeded":
            return self._finish_terminal(manifest, path, reply, record)
        if status in {"queued", "running"}:
            return await self._wait_pending(manifest, path, record, first_reply=reply)
        return self._attention(manifest, path, f"{tool} returned terminal status {status!r}")

    async def _send_query(
        self,
        manifest: dict[str, Any],
        path: Path,
        original: dict[str, Any],
    ) -> dict[str, Any] | None:
        args = ({"task_id": original["task_id"]} if original.get("task_id") is not None
                else {"operation_id": original["operation_id"]})
        query_record = self._new_call_record(
            manifest, path, tool="ansa_task_query", args=args,
            operation_id=original["operation_id"], phase="task_query",
            group_index=original.get("group_index"), query_for=original["call_id"],
        )
        query_record["state"] = "sending"
        self._persist(manifest, path)
        try:
            reply = await self.call_tool("ansa_task_query", args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            query_record["state"] = "query_failed"
            query_record["errors"].append({"at": _utc_now(), "phase": "query", "error": f"{type(exc).__name__}: {exc}"[:2048]})
            self._persist(manifest, path)
            self._attention(manifest, path, f"query of original task failed: {type(exc).__name__}: {exc}")
            return None
        if type(reply) is not dict:
            query_record["state"] = "malformed_reply"
            query_record["submission_reply"] = {"malformed_type": type(reply).__name__, "repr": repr(reply)[:2048]}
            self._persist(manifest, path)
            self._attention(manifest, path, "ansa_task_query returned no structuredContent dictionary")
            return None
        try:
            safe_reply = _json_copy(reply, "ansa_task_query structuredContent")
        except ValueError as exc:
            query_record["state"] = "malformed_reply"
            query_record["submission_reply"] = {"malformed_type": type(reply).__name__, "repr": repr(reply)[:2048]}
            self._persist(manifest, path)
            self._attention(manifest, path, f"ansa_task_query returned non-JSON structuredContent: {exc}")
            return None
        query_record["submission_reply"] = safe_reply
        query_record["task_id"] = reply.get("task_id")
        query_record["state"] = "queried"
        original["query_replies"].append(safe_reply)
        if type(reply.get("task_id")) is str:
            if original.get("task_id") not in (None, reply["task_id"]):
                self._persist(manifest, path)
                self._attention(manifest, path, "task query returned a different task_id for the original operation")
                return None
            original["task_id"] = reply["task_id"]
        self._persist(manifest, path)
        if reply.get("status") in {"queued", "running", "succeeded"} and (
                type(reply.get("task_id")) is not str
                or reply.get("operation_id") != original["operation_id"]
                or not self._verify_envelope_binding(manifest, reply, original)):
            self._attention(manifest, path, "task query identity/target/model differs from the persisted original operation")
            return None
        if reply.get("operation_id") not in (None, original["operation_id"]):
            self._attention(manifest, path, "task query returned a different operation_id")
            return None
        return safe_reply

    async def _resolve_original(
        self,
        manifest: dict[str, Any],
        path: Path,
        original: dict[str, Any],
        *,
        query_first: bool,
    ) -> dict[str, Any]:
        if query_first:
            reply = await self._send_query(manifest, path, original)
            if reply is None:
                return _normalized_status_result(manifest, path)
        else:
            reply = original.get("submission_reply")
        if type(reply) is not dict:
            return self._attention(manifest, path, "original task has no queryable structured result")
        if reply.get("status") == "succeeded":
            return self._finish_terminal(manifest, path, reply, original)
        if reply.get("status") in {"queued", "running"}:
            if type(original.get("task_id")) is not str:
                return self._attention(manifest, path, "queued/running original task has no task_id")
            return await self._wait_pending(manifest, path, original, first_reply=reply)
        return self._attention(
            manifest, path,
            f"original task query returned {reply.get('status')!r}; automatic replay is forbidden",
        )

    async def _wait_pending(
        self,
        manifest: dict[str, Any],
        path: Path,
        original: dict[str, Any],
        *,
        first_reply: dict[str, Any],
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.wait_timeout_seconds
        reply = first_reply
        polls = 0
        while reply.get("status") in {"queued", "running"}:
            if polls >= _MAX_POLLS_PER_WAIT or time.monotonic() >= deadline:
                self._event(manifest, "awaiting_original_result", "original task remains queued/running",
                            operation_id=original["operation_id"], task_id=original.get("task_id"))
                original["state"] = "awaiting_original_result"
                self._persist(manifest, path)
                return _normalized_status_result(manifest, path)
            delay = min(self.poll_interval, max(0.0, deadline-time.monotonic()))
            if delay:
                await asyncio.sleep(delay)
            reply = await self._send_query(manifest, path, original)
            polls += 1
            if reply is None:
                return _normalized_status_result(manifest, path)
        if reply.get("status") == "succeeded":
            return self._finish_terminal(manifest, path, reply, original)
        return self._attention(manifest, path, f"original task query returned {reply.get('status')!r}")

    def _verify_reply_binding(
        self, manifest: dict[str, Any], reply: dict[str, Any], operation_result: dict[str, Any],
    ) -> bool:
        binding = manifest["binding"]
        target = reply.get("target")
        model = reply.get("model")
        if type(target) is not dict or target != binding["expected_target"]:
            return False
        if type(model) is not dict or model != binding["expected_model"]:
            return False
        model_after = operation_result.get("model_after")
        if (type(model_after) is not dict or type(model_after.get("epoch")) is not int
                or model_after["epoch"] != binding["expected_model"]["epoch"]):
            return False
        return True

    def _verify_envelope_binding(
        self, manifest: dict[str, Any], reply: dict[str, Any], call: dict[str, Any],
    ) -> bool:
        target = reply.get("target")
        model = reply.get("model")
        return (
            reply.get("operation_id") == call.get("operation_id")
            and call.get("task_id") in (None, reply.get("task_id"))
            and type(target) is dict
            and target == manifest["binding"]["expected_target"]
            and type(model) is dict
            and model == manifest["binding"]["expected_model"]
        )

    def _finish_terminal(
        self,
        manifest: dict[str, Any],
        path: Path,
        reply: dict[str, Any],
        call: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            payload = self._terminal_payload(manifest, path, reply, call)
        except (KeyError, TypeError, ValueError, OSError, MapBlockJobError) as exc:
            return self._attention(manifest, path, f"{type(exc).__name__}: {exc}")
        return self._complete_phase(manifest, path, call, payload)

    def _terminal_payload(
        self,
        manifest: dict[str, Any],
        path: Path,
        reply: dict[str, Any],
        call: dict[str, Any],
    ) -> dict[str, Any]:
        if reply.get("status") != "succeeded" or reply.get("ok") is not True:
            raise MapBlockJobError("task is not authoritatively succeeded")
        if type(call.get("operation_id")) is not str or reply.get("operation_id") != call.get("operation_id"):
            raise MapBlockJobError("task result operation_id differs from the persisted request")
        if type(call.get("task_id")) is not str or reply.get("task_id") != call.get("task_id"):
            raise MapBlockJobError("task result task_id differs from the persisted original task")
        payload = _result_payload(reply)
        if _has_effect_marker(reply) or _has_effect_marker(payload):
            raise MapBlockJobError("task result reports possible effects or recovery_required")
        if not self._verify_reply_binding(manifest, reply, payload):
            raise MapBlockJobError("task result target/model/model_after differs from the immutable job binding")
        return payload

    def _complete_phase(
        self,
        manifest: dict[str, Any],
        path: Path,
        call: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            phase = call["phase"]
            group_index = call.get("group_index")
            if phase == "fill_plan":
                group = _group(manifest, group_index)
                self._validate_plan_descriptor(manifest, path, group, payload)
                group["plan_descriptor"] = _json_copy(payload, "fill plan descriptor")
                group["plan_sha256"] = payload["plan_sha256"]
                group["plan_report_id"] = payload.get("report_id")
                group["plan_report_sha256"] = payload.get("sha256")
                if type(payload.get("scope_sha256")) is str:
                    group["scope_sha256"] = payload["scope_sha256"]
                if type(payload.get("predicted_solids")) is int:
                    group["predicted_solids"] = payload["predicted_solids"]
                group["state"] = "plan_descriptor"
                call["state"] = "succeeded"
                manifest["pending_call_id"] = None
                self._event(manifest, "planned", f"fill plan group {group_index+1} received",
                            operation_id=call["operation_id"], task_id=call.get("task_id"))
                self._persist(manifest, path)
            elif phase in {"plan_scope", "plan_predicted_solids"}:
                group = _group(manifest, group_index)
                self._validate_plan_evidence_page(manifest, group, payload, phase)
                call["state"] = "succeeded"
                manifest["pending_call_id"] = None
                self._persist(manifest, path)
            elif phase == "fill_apply":
                group = _group(manifest, group_index)
                self._validate_apply_result(group, manifest, payload)
                group["state"] = "verified"
                group["apply_call_id"] = call["call_id"]
                group["apply_operation_id"] = call["operation_id"]
                group["apply_task_id"] = call["task_id"]
                group["apply_result"] = _json_copy(payload, "MapBlock fill result")
                group["apply_reconfirmed"] = False
                group["provenance"] = {
                    "ledger_id": payload["ledger_id"],
                    "ledger_hash": payload["ledger_hash"],
                    "provenance_stage": payload["provenance_stage"],
                }
                ids = payload["new_solid_ids"]
                if set(ids) & set(manifest["solid_ids"]):
                    raise MapBlockJobError("new_solid_ids overlap a previously completed group")
                group["solid_ids"] = list(ids)
                manifest["solid_ids"] = sorted(manifest["solid_ids"] + ids)
                call["state"] = "succeeded"
                manifest["pending_call_id"] = None
                self._event(manifest, "verified", f"fill group {group_index+1} verified",
                            operation_id=call["operation_id"], task_id=call["task_id"],
                            ledger_id=payload["ledger_id"], ledger_hash=payload["ledger_hash"])
                self._persist(manifest, path)
            elif phase == "model_save":
                group = _group(manifest, group_index)
                self._validate_checkpoint_result(group, manifest, payload)
                checkpoint = self._fingerprint_checkpoint(group["checkpoint_path"], manifest["job_dir"])
                group["checkpoint"] = checkpoint
                group["state"] = "checkpointed"
                group["checkpoint_call_id"] = call["call_id"]
                group["checkpoint_task_id"] = call["task_id"]
                group["checkpoint_operation_id"] = call["operation_id"]
                call["state"] = "succeeded"
                manifest["pending_call_id"] = None
                manifest["current_group_index"] = group_index + 1
                self._event(manifest, "checkpointed", f"checkpoint group {group_index+1} verified",
                            path=checkpoint["path"], sha256=checkpoint["sha256"])
                self._persist(manifest, path)
            elif phase == "quality_report":
                summary = self._quality_summary(payload, len(manifest["solid_ids"]))
                call["state"] = "succeeded"
                manifest["pending_call_id"] = None
                manifest["quality_summary"] = summary
                manifest["quality_report"] = _json_copy(payload, "quality report result")
                manifest["quality_call_id"] = call["call_id"]
                manifest["quality_task_id"] = call["task_id"]
                manifest["quality_operation_id"] = call["operation_id"]
                self._event(manifest, "complete", "all new SOLIDs passed the explicit quality profile",
                            task_id=call["task_id"], summary=summary)
                self._persist(manifest, path)
            else:
                return self._attention(manifest, path, f"unsupported completion phase {phase!r}")
        except (KeyError, TypeError, ValueError, OSError, MapBlockJobError) as exc:
            return self._attention(manifest, path, f"{type(exc).__name__}: {exc}")
        return _normalized_status_result(manifest, path)

    def _validate_plan_descriptor(
        self, manifest: dict[str, Any], path: Path, group: dict[str, Any], payload: dict[str, Any],
    ) -> None:
        if not _SHA256_RE.fullmatch(str(payload.get("plan_sha256", ""))):
            raise MapBlockJobError("fill plan descriptor lacks plan_sha256")
        if payload.get("usable_for_mutation") is True:
            raise MapBlockJobError("fill plan incorrectly claims mutation authority")
        context = payload.get("context")
        if type(context) is not dict:
            raise MapBlockJobError("fill plan descriptor lacks its bound model/session/deck context")
        expected = manifest["binding"]["expected_model"]["epoch"]
        if context.get("epoch") != expected:
            raise MapBlockJobError("fill plan report context differs from the original model epoch")
        if context.get("instance_id") != manifest["binding"]["expected_target"]["instance_id"]:
            raise MapBlockJobError("fill plan report belongs to another ANSA instance")
        if context.get("session_id") != manifest["binding"]["expected_target"]["session_id"]:
            raise MapBlockJobError("fill plan report belongs to another ANSA session")
        if context.get("deck") != manifest["deck"]:
            raise MapBlockJobError("fill plan report deck differs from the job")
        if "scope_sha256" in payload:
            if type(payload["scope_sha256"]) is not str or not _SHA256_RE.fullmatch(payload["scope_sha256"]):
                raise MapBlockJobError("fill plan scope_sha256 is invalid")
        if "predicted_solids" in payload:
            self._set_predicted_solids(manifest, group, payload["predicted_solids"])
        if "scope_sha256" not in payload or "predicted_solids" not in payload:
            report_id = payload.get("report_id")
            report_sha = payload.get("sha256")
            if (type(report_id) is not str or not _UUID_RE.fullmatch(report_id)
                    or type(report_sha) is not str or not _SHA256_RE.fullmatch(report_sha)):
                raise MapBlockJobError("fill plan descriptor hides scope fields and lacks evidence report binding")

    def _set_predicted_solids(self, manifest: dict[str, Any], group: dict[str, Any], value: Any) -> None:
        if type(value) is not int or value <= 0:
            raise MapBlockJobError("fill plan predicted_solids must be a positive integer")
        total = sum(row.get("predicted_solids", 0) for row in manifest["groups"] if row is not group) + value
        if total > _MAX_SOLIDS:
            raise MapBlockJobError("planned job exceeds the 5000-SOLID resource limit")
        group["predicted_solids"] = value

    def _validate_plan_evidence_page(
        self, manifest: dict[str, Any], group: dict[str, Any], payload: dict[str, Any], phase: str,
    ) -> None:
        if (payload.get("report_id") != group.get("plan_report_id")
                or payload.get("sha256") != group.get("plan_report_sha256")
                or payload.get("section") != ("scope_sha256" if phase == "plan_scope" else "predicted_solids")
                or payload.get("complete") is not True):
            raise MapBlockJobError("plan evidence page does not match its original report/section")
        rows = payload.get("rows")
        if type(rows) is not list or len(rows) != 1:
            raise MapBlockJobError("plan evidence page did not return exactly one scalar")
        value = rows[0]
        if phase == "plan_scope":
            if type(value) is not str or not _SHA256_RE.fullmatch(value):
                raise MapBlockJobError("native plan scope_sha256 is invalid")
            group["scope_sha256"] = value
        else:
            self._set_predicted_solids(manifest, group, value)

    def _validate_apply_result(self, group: dict[str, Any], manifest: dict[str, Any], payload: dict[str, Any]) -> None:
        if (payload.get("ok") is not True or payload.get("scope_verified") is not True
                or payload.get("provenance_stage") != "verified"):
            raise MapBlockJobError("MapBlock apply lacks ok/scope_verified/verified provenance evidence")
        ids = payload.get("new_solid_ids")
        if (type(ids) is not list or not ids
                or any(type(ident) is not int or ident <= 0 for ident in ids)
                or len(ids) != len(set(ids))):
            raise MapBlockJobError("MapBlock apply returned no valid new_solid_ids inventory")
        if len(ids) != group.get("predicted_solids"):
            raise MapBlockJobError("MapBlock apply new_solid_ids count differs from its read-only plan")
        ledger_id, ledger_hash = payload.get("ledger_id"), payload.get("ledger_hash")
        if (type(ledger_id) is not str or not _UUID_RE.fullmatch(ledger_id)
                or type(ledger_hash) is not str or not _SHA256_RE.fullmatch(ledger_hash)):
            raise MapBlockJobError("MapBlock apply lacks a valid native provenance ledger_id/hash")
        if payload.get("plan_sha256") not in (None, group.get("plan_sha256")):
            raise MapBlockJobError("MapBlock apply result is bound to a different plan")
        if len(manifest["solid_ids"])+len(ids) > _MAX_SOLIDS:
            raise MapBlockJobError("completed job exceeds the 5000-SOLID resource limit")

    def _validate_checkpoint_result(self, group: dict[str, Any], manifest: dict[str, Any], payload: dict[str, Any]) -> None:
        expected = group.get("checkpoint_path")
        returned = payload.get("path")
        if payload.get("saved") is not True or type(returned) is not str or not self._same_path(returned, expected):
            raise MapBlockJobError("model_save did not confirm the requested checkpoint path")

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return os.path.normcase(os.path.normpath(os.path.abspath(left))) == os.path.normcase(
            os.path.normpath(os.path.abspath(right))
        )

    def _fingerprint_checkpoint(self, value: str, job_dir: str) -> dict[str, Any]:
        path = _absolute_path(value, "checkpoint path")
        root = Path(job_dir)
        if not self._same_path(str(path.parent), str(root / "checkpoints")):
            raise MapBlockJobError("checkpoint path escaped the job checkpoints directory")
        info = _safe_regular_file(path, "checkpoint")
        if info.st_size <= 0:
            raise MapBlockJobError("model_save checkpoint is empty")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                digest.update(chunk)
        after = _safe_regular_file(path, "checkpoint")
        before_fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        after_fingerprint = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_fingerprint != after_fingerprint:
            raise MapBlockJobError("checkpoint changed while hashing")
        return {"path": str(path), "size_bytes": after.st_size,
                "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()}

    @staticmethod
    def _quality_summary(payload: dict[str, Any], expected_count: int) -> dict[str, Any]:
        report = payload
        summary = report.get("summary")
        if type(summary) is not dict:
            nested = report.get("report")
            summary = nested.get("summary") if type(nested) is dict else None
            if type(nested) is dict:
                report = nested
        if type(summary) is not dict:
            raise MapBlockJobError("quality report has no structured summary")
        required = ("checked_count", "failed_count", "unknown_count", "passed_count")
        if any(type(summary.get(key)) is not int or summary[key] < 0 for key in required):
            raise MapBlockJobError("quality report summary has missing/invalid decision counts")
        if (report.get("complete") is not True or report.get("passed") is not True
                or report.get("snapshot_verified") is not True
                or summary.get("status") != "passed"
                or summary["checked_count"] != expected_count
                or summary["failed_count"] != 0
                or summary["unknown_count"] != 0
                or summary["passed_count"] != expected_count):
            raise MapBlockJobError("quality report is incomplete or has failed/unknown entities")
        return {key: summary[key] for key in (*required, "status")}

    async def _drive(self, manifest: dict[str, Any], path: Path, *, resuming: bool) -> dict[str, Any]:
        if manifest["status"] in {"complete", "needs_attention"}:
            return _normalized_status_result(manifest, path)
        if resuming and manifest.get("pending_call_id") is None:
            confirmed = await self._reconfirm_verified_group(manifest, path)
            if confirmed is None:
                return _normalized_status_result(manifest, path)

        if manifest.get("pending_call_id") is not None:
            original = _call_by_id(manifest, manifest["pending_call_id"])
            terminal = await self._resolve_original(manifest, path, original, query_first=True)
            if terminal["status"] in {"awaiting_original_result", "needs_attention"}:
                return terminal

        while manifest["status"] not in {"needs_attention", "complete"}:
            pending_id = manifest.get("pending_call_id")
            if pending_id is not None:
                original = _call_by_id(manifest, pending_id)
                terminal = await self._resolve_original(manifest, path, original, query_first=True)
                if terminal["status"] in {"awaiting_original_result", "needs_attention"}:
                    return terminal
                continue

            current = next((row for row in manifest["groups"] if row["state"] != "checkpointed"), None)
            if current is None:
                if manifest.get("quality_call_id"):
                    return _normalized_status_result(manifest, path)
                result = await self._start_quality_report(manifest, path)
                if result["status"] in {"awaiting_original_result", "needs_attention"}:
                    return result
                continue
            index = current["index"]
            if current["state"] == "unplanned":
                result = await self._start_plan(manifest, path, index)
            elif current["state"] == "plan_descriptor":
                result = await self._fetch_plan_field(manifest, path, index)
            elif current["state"] == "planned":
                result = await self._start_apply(manifest, path, index)
            elif current["state"] == "verified":
                result = await self._start_checkpoint(manifest, path, index)
            else:
                return self._attention(manifest, path, f"unsupported group state {current['state']!r}")
            if result["status"] in {"awaiting_original_result", "needs_attention"}:
                return result
        return _normalized_status_result(manifest, path)

    async def _reconfirm_verified_group(self, manifest: dict[str, Any], path: Path) -> dict[str, Any] | None:
        group = next((row for row in manifest["groups"]
                      if row["state"] == "verified" and not row.get("apply_reconfirmed")), None)
        if group is None:
            return {}
        apply_call_id = group.get("apply_call_id")
        if type(apply_call_id) is not str:
            self._attention(manifest, path, "verified group has no durable original apply call")
            return None
        call = _call_by_id(manifest, apply_call_id)
        reply = await self._send_query(manifest, path, call)
        if reply is None:
            return None
        if reply.get("status") in {"queued", "running"}:
            self._event(manifest, "awaiting_original_result", "verified apply is still running",
                        operation_id=call["operation_id"], task_id=call.get("task_id"))
            manifest["status"] = "awaiting_original_result"
            self._persist(manifest, path)
            # _drive will query the same original task on the next resume.
            return None
        if reply.get("status") != "succeeded":
            self._attention(manifest, path, "original verified apply is no longer authoritatively succeeded")
            return None
        try:
            payload = self._terminal_payload(manifest, path, reply, call)
            self._validate_apply_result(group, manifest, payload)
            if (payload["new_solid_ids"] != group["solid_ids"]
                    or payload["ledger_id"] != group["provenance"]["ledger_id"]
                    or payload["ledger_hash"] != group["provenance"]["ledger_hash"]):
                raise MapBlockJobError("original apply result differs from persisted verified group evidence")
            group["apply_reconfirmed"] = True
            self._event(manifest, "verified", f"reconfirmed original apply for group {group['index']+1}",
                        task_id=call["task_id"])
            self._persist(manifest, path)
            return {}
        except (KeyError, TypeError, ValueError, MapBlockJobError) as exc:
            self._attention(manifest, path, f"{type(exc).__name__}: {exc}")
            return None

    async def _start_plan(self, manifest: dict[str, Any], path: Path, index: int) -> dict[str, Any]:
        group = _group(manifest, index)
        return await self._send_operation(
            manifest, path, tool="ansa_mesh_map_block_fill",
            params={"action": "plan", "deck": manifest["deck"], "volume_ids": list(group["volume_ids"])},
            phase="fill_plan", group_index=index,
        )

    async def _fetch_plan_field(self, manifest: dict[str, Any], path: Path, index: int) -> dict[str, Any]:
        group = _group(manifest, index)
        if "scope_sha256" not in group:
            field, phase = "scope_sha256", "plan_scope"
        elif "predicted_solids" not in group:
            field, phase = "predicted_solids", "plan_predicted_solids"
        else:
            if not _SHA256_RE.fullmatch(group["scope_sha256"]):
                return self._attention(manifest, path, "plan scope_sha256 is invalid")
            group["state"] = "planned"
            self._event(manifest, "planned", f"fill plan group {index+1} fully bound",
                        plan_sha256=group["plan_sha256"], scope_sha256=group["scope_sha256"])
            self._persist(manifest, path)
            return _normalized_status_result(manifest, path)
        descriptor = group["plan_descriptor"]
        args = {
            "deck": manifest["deck"],
            "report_id": group["plan_report_id"],
            "section": field,
            "offset": 0,
            "limit": 1,
        }
        return await self._send_operation(
            manifest, path, tool="ansa_mesh_evidence_page", params=args,
            phase=phase, group_index=index,
        )

    async def _start_apply(self, manifest: dict[str, Any], path: Path, index: int) -> dict[str, Any]:
        group = _group(manifest, index)
        job_dir = Path(manifest["job_dir"])
        backup_path = job_dir / "backups" / f"group-{index+1:02d}.ansa"
        if backup_path.exists():
            return self._attention(manifest, path, "new per-group backup path already exists")
        _reject_reparse_chain(backup_path)
        group["backup_path"] = str(backup_path)
        self._persist(manifest, path)
        return await self._send_operation(
            manifest, path, tool="ansa_mesh_map_block_fill",
            params={
                "action": "apply",
                "deck": manifest["deck"],
                "volume_ids": list(group["volume_ids"]),
                "expected_plan_sha256": group["plan_sha256"],
                "backup_path": str(backup_path),
            },
            phase="fill_apply", group_index=index,
        )

    async def _start_checkpoint(self, manifest: dict[str, Any], path: Path, index: int) -> dict[str, Any]:
        group = _group(manifest, index)
        job_dir = Path(manifest["job_dir"])
        checkpoint = job_dir / "checkpoints" / f"group-{index+1:02d}.ansa"
        if checkpoint.exists():
            return self._attention(manifest, path, "new per-group checkpoint path already exists")
        _reject_reparse_chain(checkpoint)
        group["checkpoint_path"] = str(checkpoint)
        self._persist(manifest, path)
        return await self._send_operation(
            manifest, path, tool="ansa_model_save",
            params={"path": str(checkpoint), "overwrite": False},
            phase="model_save", group_index=index,
        )

    async def _start_quality_report(self, manifest: dict[str, Any], path: Path) -> dict[str, Any]:
        if not manifest["solid_ids"]:
            return self._attention(manifest, path, "job has no new SOLIDs to quality-check")
        return await self._send_operation(
            manifest, path, tool="ansa_mesh_quality_report",
            params={
                "deck": manifest["deck"],
                "profile": copy.deepcopy(manifest["quality_profile"]),
                "entity_ids": list(manifest["solid_ids"]),
                "paged": True,
                "batch_size": 1000,
                "max_seconds": 120,
                "max_report_megabytes": _MAX_REPORT_MEGABYTES,
            },
            phase="quality_report", group_index=None,
        )
