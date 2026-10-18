"""Read durable task results and export private, content-addressed log files."""

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from .security import ensure_private_directory, ensure_safe_parent, verify_private_path


_MAX_CHARS = 65536
_HISTORY_LIMIT = 200
_STORE_PAGE_LIMIT = 100
_TARGET_FIELDS = ("backend_id", "instance_id", "session_id", "generation")


class TaskLogReader:
    """Read the current task store without contacting or executing ANSA."""

    def __init__(self, runtime_home, tasks):
        if not callable(getattr(tasks, "history_page", None)):
            raise TypeError("tasks must provide history_page")

        home = Path(runtime_home).absolute()
        ensure_safe_parent(home)
        verify_private_path(home)
        if not home.is_dir():
            raise PermissionError("runtime_home must be a verified private directory")

        self.runtime_home = home.resolve(strict=True)
        self.tasks = tasks
        self._logs_dir = self.runtime_home / "logs"

    def read(self, max_chars=65536):
        if type(max_chars) is not int or not 1 <= max_chars <= _MAX_CHARS:
            raise ValueError("max_chars must be an integer between 1 and 65536")

        records, has_more = self._recent_records()
        history = self._history_projection(records)
        if not records:
            return {
                "found": False,
                "available": False,
                "audit_scope": "task_history",
                "has_more": False,
                "history": history,
                "artifact": None,
                "content_excerpt": "",
                "truncated": False,
                "total_chars": 0,
            }

        latest = records[0]
        latest_projection = history["tasks"][0]
        result = latest.get("result")
        error = latest.get("error")
        available = result is not None or error is not None
        document = {
            "audit_scope": "task_history",
            "available": available,
            "has_more": has_more,
            "history": history,
            "latest": latest_projection,
            "result": result,
            "error": error,
        }
        text = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        content = text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        artifact_path = self._store_artifact(digest, content)
        body = json.dumps({"result": result, "error": error}, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
        truncated = len(body) > max_chars

        return {
            "found": True,
            "available": available,
            "audit_scope": "task_history",
            "has_more": has_more,
            "history": history,
            "latest": latest_projection,
            "result": None if truncated else result,
            "error": None if truncated else error,
            "body_inline": not truncated,
            "artifact": {
                "path": str(artifact_path.absolute()),
                "mime_type": "application/json",
                "sha256": digest,
                "bytes": len(content),
            },
            "preview_scope": "latest_result_and_error",
            "content_excerpt": body[:max_chars],
            "truncated": truncated,
            "total_chars": len(body),
        }

    def _recent_records(self):
        # TaskStore currently caps an individual history_page at 100 rows.
        first = self._read_page()
        records = list(first["tasks"])
        has_more = first["has_more"]
        cursor = first.get("next_before_sequence")

        if has_more:
            if type(cursor) is not int or cursor < 1:
                raise RuntimeError("TaskStore returned an invalid history cursor")
            second = self._read_page(before_sequence=cursor)
            records.extend(second["tasks"])
            has_more = second["has_more"]

        if len(records) > _HISTORY_LIMIT:
            raise RuntimeError("TaskStore returned more than the requested history bound")
        return records, has_more

    def _read_page(self, before_sequence=None):
        arguments = {"limit": _STORE_PAGE_LIMIT}
        if before_sequence is not None:
            arguments["before_sequence"] = before_sequence
        page = self.tasks.history_page(**arguments)
        if type(page) is not dict or type(page.get("tasks")) is not list:
            raise RuntimeError("TaskStore returned an invalid history page")
        if len(page["tasks"]) > _STORE_PAGE_LIMIT or type(page.get("has_more")) is not bool:
            raise RuntimeError("TaskStore returned an invalid history page")
        return page

    @classmethod
    def _project_record(cls, record):
        if type(record) is not dict or type(record.get("task")) is not dict:
            raise RuntimeError("TaskStore returned an invalid task record")
        task = record["task"]
        projection = {}
        for key in ("task_id", "operation_id", "state"):
            value = record.get(key)
            if type(value) is not str:
                raise RuntimeError("TaskStore returned incomplete task metadata")
            projection[key] = value

        sequence_key = "seq" if "seq" in record else "sequence"
        sequence = record.get(sequence_key)
        if type(sequence) is int and sequence > 0:
            projection[sequence_key] = sequence

        for key in ("created", "updated"):
            value = record.get(key)
            if (type(value) is str or type(value) is int
                    or (type(value) is float and math.isfinite(value))):
                projection[key] = value

        tool = task.get("tool")
        if type(tool) is str and tool:
            projection["tool"] = tool

        target = task.get("target")
        if type(target) is dict:
            safe_target = {
                key: target[key]
                for key in _TARGET_FIELDS
                if (key in target and (type(target[key]) is str or
                    (key == "generation" and type(target[key]) is int)))
            }
            if safe_target:
                projection["target"] = safe_target

        model = task.get("model")
        if type(model) is dict and type(model.get("epoch")) is int:
            projection["model"] = {"epoch": model["epoch"]}
        return projection

    @classmethod
    def _history_projection(cls, records):
        projected = [cls._project_record(record) for record in records]
        states = Counter(item["state"] for item in projected)
        tools = Counter(item["tool"] for item in projected if "tool" in item)
        return {
            "tasks": projected,
            "task_count": len(projected),
            "state_counts": dict(sorted(states.items())),
            "tool_counts": dict(sorted(tools.items())),
        }

    def _ensure_logs_directory(self):
        ensure_safe_parent(self._logs_dir)
        if self._logs_dir.exists() or self._logs_dir.is_symlink():
            verify_private_path(self._logs_dir)
        else:
            ensure_private_directory(self._logs_dir)
        ensure_safe_parent(self._logs_dir)
        verify_private_path(self._logs_dir)
        if not self._logs_dir.is_dir():
            raise PermissionError("runtime logs path is not a private directory")

    def _store_artifact(self, digest, content):
        self._ensure_logs_directory()
        target = self._logs_dir / (digest + ".json")
        existing = self._existing_content(target)
        if existing is not None:
            if existing != content:
                raise RuntimeError("Content-addressed task log already contains different data")
            return target

        temporary_path = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="." + digest + ".", suffix=".tmp", dir=self._logs_dir
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                ensure_safe_parent(temporary_path)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())

            ensure_safe_parent(temporary_path)
            verify_private_path(temporary_path)
            ensure_safe_parent(target)
            try:
                # Windows rename is atomic on the same volume and refuses an
                # existing destination; POSIX uses link for the same no-clobber
                # property because rename would replace an existing file there.
                if os.name == "nt":
                    os.rename(temporary_path, target)
                else:
                    os.link(temporary_path, target)
            except FileExistsError:
                existing = self._existing_content(target)
                if existing == content:
                    return target
                raise RuntimeError("Content-addressed task log already contains different data")

            ensure_safe_parent(target)
            verify_private_path(target)
            if not target.is_file() or target.read_bytes() != content:
                raise OSError("Persisted task log does not match its content hash")
            ensure_safe_parent(target)
            verify_private_path(target)
            return target
        except (OSError, RuntimeError) as exc:
            raise OSError("Could not persist task log artifact: " + str(exc)) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def _existing_content(self, target):
        ensure_safe_parent(target)
        if not target.exists() and not target.is_symlink():
            return None
        verify_private_path(target)
        if not target.is_file():
            raise PermissionError("Content-addressed task log target is not a regular file")
        content = target.read_bytes()
        ensure_safe_parent(target)
        verify_private_path(target)
        return content
