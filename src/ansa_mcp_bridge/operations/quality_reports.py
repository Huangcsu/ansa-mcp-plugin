"""Immutable paged quality evidence, stored inside the owning private session."""

from collections import Counter, deque
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid


def _context(state, deck):
    return {"deck": deck, "model_epoch": state.model_epoch,
            "instance_id": state.instance_id, "session_id": state.session_id}


def _path(state, report_id):
    if not isinstance(report_id, str) or re.fullmatch(r"[0-9a-f]{32}", report_id) is None:
        raise ValueError("invalid quality report ID")
    root = Path(state.session_dir) / "quality-reports"
    root.mkdir(exist_ok=True)
    path = root / (report_id + ".sqlite3")
    if root.is_symlink() or path.is_symlink():
        raise ValueError("quality report storage must not be a symlink")
    return path


def _limits(params):
    result = {}
    for name, default, lower, upper in (("batch_size", 1000, 1, 2000),
                                       ("max_seconds", 120, 1, 86400),
                                       ("max_report_megabytes", 256, 1, 4096)):
        value = params.get(name, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in {lower}..{upper}")
        result[name] = value
    return result


def build(state, base, constants, deck, profile, params, reporter):
    from . import quality_workflow as common
    from .native_quality import _dependencies
    limits = _limits(params)
    scope = params.get("scope")
    ids = params.get("entity_ids")
    if scope is not None and ids is not None:
        raise ValueError("provide entity_ids or scope, not both")
    kind = common._element_kind(constants, deck, profile["element_family"])
    if scope in ("all", "visible"):
        entities = base.CollectEntities(deck, None, kind, filter_visible=scope == "visible")
        if type(entities) not in (list, tuple):
            raise RuntimeError("quality scope enumeration was incomplete")
        ids = sorted(e._id for e in entities)
    elif scope is None and ids is not None:
        ids = common.inspection._id_list(ids, "entity_ids", maximum=len(ids))
        scope = "explicit"
    else:
        raise ValueError("scope must be all or visible, or provide explicit entity_ids")
    if len(ids) != len(set(ids)):
        raise ValueError("quality scope contains duplicate entities")
    report_id = uuid.uuid4().hex
    path = _path(state, report_id)
    context = _context(state, deck)
    original = common._criteria(base)
    dependencies = _dependencies(base, deck) if "template" in profile else None
    started = time.monotonic()
    counts = Counter(checked_count=0, failed_count=0, unknown_count=0, passed_count=0)
    violations = Counter()
    hashes = []
    complete = bool(ids)
    reason = None if ids else "empty_scope"
    criteria = None
    failure = None
    def budget():
        if time.monotonic() - started > limits["max_seconds"]:
            return "time_budget"
        stopped = getattr(state, "stopped", None)
        if stopped is not None and stopped.is_set():
            return "stopped_between_batches"
        if path.stat().st_size > limits["max_report_megabytes"] * 1024 * 1024:
            return "report_storage_budget"
        return None
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE meta(body TEXT NOT NULL);"
                         "CREATE TABLE rows(seq INTEGER PRIMARY KEY,id INTEGER UNIQUE,failed INTEGER,unknown INTEGER,body TEXT);"
                         "CREATE TABLE violations(seq INTEGER,criterion TEXT,PRIMARY KEY(seq,criterion));"
                         "CREATE INDEX status_idx ON rows(failed,unknown);"
                         "CREATE INDEX criterion_idx ON violations(criterion,seq);")
        chunks = deque(ids[offset:offset + limits["batch_size"]] for offset in range(0, len(ids), limits["batch_size"]))
        while chunks:
            reason = budget()
            if reason:
                complete = False
                break
            chunk = chunks.popleft()
            try:
                report = reporter(base, constants, deck, profile, chunk)[0]
                if common._criteria(base) != original or _context(state, deck) != context:
                    raise RuntimeError("quality model or criteria changed between batches")
                criteria = report.get("criteria_snapshot", report.get("definitions"))
                complete = complete and report["complete"]
                failed, unknown = set(report["failed_entity_ids"]), set(report["unknown_entity_ids"])
                for row in report["rows"]:
                    seq = counts["checked_count"]
                    bad, missing = row["id"] in failed, row["id"] in unknown
                    db.execute("INSERT INTO rows VALUES (?,?,?,?,?)", (seq, row["id"], int(bad), int(missing), common._encoded(row).decode("utf-8")))
                    for name in row["violations"]:
                        db.execute("INSERT INTO violations VALUES (?,?)", (seq, name))
                        violations[name] += 1
                    counts.update(checked_count=1, failed_count=int(bad), unknown_count=int(missing), passed_count=int(not bad and not missing))
                hashes.append((chunk, report["geometry_sha256"], report["scope_sha256"]))
                db.commit()
            except Exception as exc:
                if len(chunk) > 1 and "CheckReport tree exceeds" in str(exc):
                    middle = len(chunk) // 2
                    chunks.appendleft(chunk[middle:])
                    chunks.appendleft(chunk[:middle])
                    continue
                complete = False
                reason = "batch_failed"
                failure = f"{type(exc).__name__}: {exc}"[:2048]
                break
        snapshot_verified = False
        try:
            for chunk, expected, _decision in hashes:
                interrupted = budget()
                if interrupted:
                    reason, complete = interrupted, False
                    break
                if common._scope(base, constants, deck, profile["element_family"], chunk)[3] != expected:
                    raise RuntimeError("previously checked mesh changed during the quality job")
            else:
                if (common._criteria(base) != original or _context(state, deck) != context
                        or (dependencies is not None and _dependencies(base, deck) != dependencies)):
                    raise RuntimeError("quality model, materials or criteria changed during the quality job")
                snapshot_verified = True
        except Exception as exc:
            reason, complete = "snapshot_changed", False
            failure = f"{type(exc).__name__}: {exc}"[:2048]
        complete = complete and snapshot_verified and counts["checked_count"] == len(ids)
        passed = complete and not counts["failed_count"] and not counts["unknown_count"]
        digest = hashlib.sha256()
        for _chunk, _geometry, decision in hashes:
            digest.update(decision.encode("ascii"))
        value = {"report_id": report_id, "deck": deck, "entity_type": kind,
                 "profile": {key: item for key, item in profile.items() if key != "quality_criteria"},
                 "analysis_type": profile.get("analysis_type"),
                 "scope": scope, "read_only": True, "requested_count": len(ids),
                 "complete": complete, "passed": passed, "snapshot_verified": snapshot_verified,
                 "scope_sha256": digest.hexdigest(), "usable_for_mutation": False,
                 "summary": {"status": "passed" if passed else "failed" if complete else "incomplete", **counts},
                 "violation_summary": [{"criterion": name, "failed_count": count} for name, count in sorted(violations.items())],
                 "criteria_snapshot": criteria, "rows_total": counts["checked_count"],
                 "rows_returned": 0, "rows_truncated": counts["checked_count"] > 0,
                 "rows": [], "ids_paged": True, "page_tool": "ansa_mesh_quality_page",
                 "reason": reason, "error": failure, "batch_count": len(hashes), "batch_size": limits["batch_size"],
                 "elapsed_seconds": time.monotonic() - started, "solver_verified": False}
        db.execute("INSERT INTO meta VALUES (?)", (json.dumps({"context": context, "report": value}, ensure_ascii=False, allow_nan=False),))
        db.commit()
    return value


def page(state, deck, params):
    from . import quality_workflow as common
    report_id = params["report_id"]
    path = _path(state, report_id)
    if not path.is_file():
        raise ValueError("quality report is unavailable in this ANSA session")
    offset, limit = params.get("offset", 0), params.get("limit", 100)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("quality page requires offset >= 0 and limit in 1..200")
    status = params.get("status", "failed")
    clauses, values = [], []
    if status == "failed":
        clauses.append("r.failed=1")
    elif status == "unknown":
        clauses.append("r.unknown=1")
    elif status != "all":
        raise ValueError("quality page status must be all, failed or unknown")
    criterion = params.get("criterion")
    if criterion is not None:
        if not isinstance(criterion, str) or not criterion.strip() or len(criterion) > 128:
            raise ValueError("invalid quality page criterion")
        clauses.append("EXISTS (SELECT 1 FROM violations v WHERE v.seq=r.seq AND v.criterion=?)")
        values.append(criterion)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with closing(sqlite3.connect(path)) as db:
        meta = json.loads(db.execute("SELECT body FROM meta").fetchone()[0])
        if meta["context"] != _context(state, deck):
            raise ValueError("quality report belongs to a different model context")
        total = db.execute("SELECT COUNT(*) FROM rows r" + where, values).fetchone()[0]
        rows, used = [], 0
        for body, in db.execute("SELECT r.body FROM rows r" + where + " ORDER BY r.seq LIMIT ? OFFSET ?", [*values, limit, offset]):
            size = len(body.encode("utf-8"))
            if rows and used + size > 128 * 1024:
                break
            if size > 128 * 1024:
                raise ValueError("one quality detail exceeds the page budget")
            rows.append(json.loads(body))
            used += size
    return {"report_id": report_id, "read_only": True, "historical_snapshot": True,
            "source_complete": meta["report"]["complete"], "scope_sha256": meta["report"]["scope_sha256"],
            "status": status, "criterion": criterion, "total": total, "offset": offset,
            "rows": rows, "next_offset": offset + len(rows) if offset + len(rows) < total else None}
