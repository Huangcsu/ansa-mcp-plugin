"""Versioned, private-session ledger for bounded structured-mesh stages.

This module uses only the Python standard library and never imports ANSA. The
caller must pass the already-private ``state.session_dir`` Path. The module
creates only its fixed ``structured-mesh`` child and uses a SHA-256-derived
filename for each validated ledger ID; callers cannot supply a file path.
"""

from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


SCHEMA_VERSION = 1
STAGES = ("planned", "source_checked", "applying", "verified", "checkpointed", "recovery_required")
_STAGE_SET = frozenset(STAGES)
_MAX_LEDGER_BYTES = 8 * 1024 * 1024
_MAX_FE_REFERENCES = 10000
_MAX_ID_REUSE_EVENTS = 10000
_LEDGER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WRITE_LOCK = threading.RLock()
_DEFAULT_NATIVE_STATE = {
    "ownership": "none",
    "owner": None,
    "regeneration": "not_required",
}
_OWNERSHIP_STATES = frozenset({"none", "owned", "released", "unknown"})
_REGENERATION_STATES = frozenset({"not_required", "required", "running", "complete", "unknown"})
_TRANSITIONS = {
    "planned": frozenset({"source_checked", "recovery_required"}),
    "source_checked": frozenset({"applying", "recovery_required"}),
    "applying": frozenset({"verified", "recovery_required"}),
    "verified": frozenset({"checkpointed", "recovery_required"}),
    "checkpointed": frozenset(),
    # Recovery may close an operation only after an independent verification;
    # it can never restart the applying stage under this ledger ID.
    "recovery_required": frozenset({"verified", "checkpointed"}),
}


def _object(value, required, optional=(), label="object"):
    if type(value) is not dict:
        raise TypeError(f"{label} must be an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing:
        raise ValueError(f"{label} is missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"{label} has unsupported field(s): {', '.join(sorted(extra))}")
    return value


def _ledger_id(value):
    if type(value) is not str or not _LEDGER_ID.fullmatch(value):
        raise ValueError("ledger_id must be 1 to 128 ASCII letters, digits, dot, underscore, or hyphen")
    return value


def _identifier(value, label):
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value and value == value.strip() and len(value) <= 128:
        return value
    raise ValueError(f"{label} must be a positive integer or non-empty string")


def _reference(value, label):
    _object(value, ("type", "id", "fingerprint", "epoch"), label=label)
    kind = value["type"]
    if type(kind) is not str or not kind or kind != kind.strip() or len(kind) > 64:
        raise ValueError(f"{label}.type must be a non-empty exact string of at most 64 characters")
    fingerprint = value["fingerprint"]
    if type(fingerprint) is not str or not _SHA256.fullmatch(fingerprint):
        raise ValueError(f"{label}.fingerprint must be a lowercase SHA-256 hex digest")
    epoch = value["epoch"]
    if type(epoch) is not int or epoch < 0:
        raise ValueError(f"{label}.epoch must be a non-negative integer")
    return {"type": kind, "id": _identifier(value["id"], f"{label}.id"),
            "fingerprint": fingerprint, "epoch": epoch}


def _binding(payload, label="binding"):
    _object(payload, ("target", "model", "source"), label=label)
    target = _reference(payload["target"], f"{label}.target")
    model = _reference(payload["model"], f"{label}.model")
    source = _reference(payload["source"], f"{label}.source")
    if target["type"] != "target":
        raise ValueError(f"{label}.target.type must be 'target'")
    if model["type"] != "model":
        raise ValueError(f"{label}.model.type must be 'model'")
    return {"target": target, "model": model, "source": source}


def _reference_key(reference):
    return reference["type"], str(reference["id"])


def _same_observation(left, right):
    return (left["fingerprint"], left["epoch"]) == (right["fingerprint"], right["epoch"])


def _reference_list(value, label):
    if type(value) is not list or len(value) > _MAX_FE_REFERENCES:
        raise ValueError(f"{label} must be a list of at most {_MAX_FE_REFERENCES} typed references")
    result = [_reference(item, f"{label}[{index}]") for index, item in enumerate(value)]
    seen_exact = set()
    for item in result:
        encoded = _canonical(item, label)
        if encoded in seen_exact:
            raise ValueError(f"{label} contains a duplicate typed reference")
        seen_exact.add(encoded)
    return result


def _fe_mapping(value):
    if value is None:
        return {"original_fe": [], "new_fe": [], "pairs": []}
    _object(value, ("original_fe", "new_fe", "pairs"), label="fe_mapping")
    original = _reference_list(value["original_fe"], "fe_mapping.original_fe")
    new = _reference_list(value["new_fe"], "fe_mapping.new_fe")
    pairs = value["pairs"]
    if type(pairs) is not list or len(pairs) > _MAX_FE_REFERENCES:
        raise ValueError(f"fe_mapping.pairs must contain at most {_MAX_FE_REFERENCES} items")
    original_exact = {_canonical(item, "original FE reference") for item in original}
    new_exact = {_canonical(item, "new FE reference") for item in new}
    normalized_pairs = []
    seen_originals = set()
    for index, pair in enumerate(pairs):
        _object(pair, ("original", "created"), label=f"fe_mapping.pairs[{index}]")
        old = _reference(pair["original"], f"fe_mapping.pairs[{index}].original")
        created = _reference_list(pair["created"], f"fe_mapping.pairs[{index}].created")
        old_key = _canonical(old, "fe_mapping.pairs.original")
        if old_key not in original_exact:
            raise ValueError(f"fe_mapping.pairs[{index}].original is absent from original_fe")
        if old_key in seen_originals:
            raise ValueError("fe_mapping.pairs repeats an original FE reference")
        seen_originals.add(old_key)
        for item in created:
            if _canonical(item, "fe_mapping.pairs.created") not in new_exact:
                raise ValueError(f"fe_mapping.pairs[{index}].created is absent from new_fe")
        normalized_pairs.append({"original": old, "created": created})
    normalized_pairs.sort(key=lambda item: (item["original"]["type"], str(item["original"]["id"])))
    return {"original_fe": original, "new_fe": new, "pairs": normalized_pairs}


def _native_state(value):
    if value is None:
        return dict(_DEFAULT_NATIVE_STATE)
    _object(value, ("ownership", "owner", "regeneration"), label="native_state")
    ownership = value["ownership"]
    regeneration = value["regeneration"]
    if ownership not in _OWNERSHIP_STATES:
        raise ValueError(f"native_state.ownership must be one of {sorted(_OWNERSHIP_STATES)}")
    if regeneration not in _REGENERATION_STATES:
        raise ValueError(f"native_state.regeneration must be one of {sorted(_REGENERATION_STATES)}")
    owner = None if value["owner"] is None else _reference(value["owner"], "native_state.owner")
    if ownership == "owned" and owner is None:
        raise ValueError("native_state.owner is required when ownership is 'owned'")
    return {"ownership": ownership, "owner": owner, "regeneration": regeneration}


def _finite_elapsed(value):
    if type(value) not in (int, float):
        raise ValueError("elapsed_seconds must be a finite non-negative number")
    elapsed = float(value)
    if not math.isfinite(elapsed) or elapsed < 0 or elapsed > 86400 * 30:
        raise ValueError("elapsed_seconds must be finite and between 0 and 30 days")
    return elapsed


def _canonical(value, label="ledger"):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be strict JSON: {exc}") from exc


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_is_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check_no_reparse(path):
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if _path_is_reparse(info):
            raise PermissionError(f"ledger path contains a symlink or reparse point: {entry}")


def _storage_dir(session_dir, *, create=False):
    """Resolve only the fixed child under a caller-verified private session dir."""
    if not isinstance(session_dir, Path):
        raise TypeError("session_dir must be the private state.session_dir Path")
    if not session_dir.is_absolute():
        raise ValueError("session_dir must be absolute")
    _check_no_reparse(session_dir)
    if not session_dir.exists() or not session_dir.is_dir():
        raise PermissionError("session_dir must be an existing private directory")
    directory = session_dir / "structured-mesh"
    if directory.exists():
        _check_no_reparse(directory)
        if not directory.is_dir():
            raise PermissionError("structured-mesh ledger location is not a directory")
    else:
        if not create:
            raise FileNotFoundError("structured-mesh ledger directory does not exist")
        directory.mkdir(mode=0o700)
        _check_no_reparse(directory)
    return directory


def _ledger_path(directory, ledger_id):
    digest = hashlib.sha256(ledger_id.encode("ascii")).hexdigest()
    return directory / f"ledger-{digest}.json"


def _check_regular_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if _path_is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise PermissionError("ledger file must be a regular file without reparse points")
    if info.st_nlink != 1:
        raise PermissionError("ledger file must not have additional hard links")
    return info


def _validate_ledger(value, expected_id=None):
    _object(value, ("schema_version", "ledger_id", "revision", "stage", "binding",
                    "fe_mapping", "native_state", "phase_timings", "observed_fe",
                    "id_reuse_events"), label="ledger")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported mesh provenance ledger schema_version")
    ledger_id = _ledger_id(value["ledger_id"])
    if expected_id is not None and ledger_id != expected_id:
        raise ValueError("ledger_id does not match the requested ledger")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValueError("ledger.revision must be a positive integer")
    stage = value["stage"]
    if stage not in _STAGE_SET:
        raise ValueError("ledger.stage is not a supported bounded stage")
    binding = _binding(value["binding"], "ledger.binding")
    fe_map = _fe_mapping(value["fe_mapping"])
    native = _native_state(value["native_state"])
    timings = value["phase_timings"]
    if type(timings) is not list or len(timings) > len(STAGES) * 2:
        raise ValueError("ledger.phase_timings exceeds the bounded phase history")
    normalized_timings = []
    for index, timing in enumerate(timings):
        _object(timing, ("stage", "elapsed_seconds"), label=f"ledger.phase_timings[{index}]")
        if timing["stage"] not in _STAGE_SET:
            raise ValueError(f"ledger.phase_timings[{index}].stage is unsupported")
        normalized_timings.append({"stage": timing["stage"],
                                   "elapsed_seconds": _finite_elapsed(timing["elapsed_seconds"])})
    observed = _reference_list(value["observed_fe"], "ledger.observed_fe")
    events = value["id_reuse_events"]
    if type(events) is not list or len(events) > _MAX_ID_REUSE_EVENTS:
        raise ValueError("ledger.id_reuse_events exceeds its limit")
    normalized_events = []
    for index, event in enumerate(events):
        _object(event, ("type", "id", "prior_fingerprint", "prior_epoch",
                        "new_fingerprint", "new_epoch"), label=f"ledger.id_reuse_events[{index}]")
        kind = event["type"]
        if type(kind) is not str or not kind or len(kind) > 64:
            raise ValueError(f"ledger.id_reuse_events[{index}].type is invalid")
        entity_id = _identifier(event["id"], f"ledger.id_reuse_events[{index}].id")
        for name in ("prior_fingerprint", "new_fingerprint"):
            if type(event[name]) is not str or not _SHA256.fullmatch(event[name]):
                raise ValueError(f"ledger.id_reuse_events[{index}].{name} is invalid")
        for name in ("prior_epoch", "new_epoch"):
            if type(event[name]) is not int or event[name] < 0:
                raise ValueError(f"ledger.id_reuse_events[{index}].{name} is invalid")
        normalized_events.append({"type": kind, "id": entity_id,
                                  "prior_fingerprint": event["prior_fingerprint"],
                                  "prior_epoch": event["prior_epoch"],
                                  "new_fingerprint": event["new_fingerprint"],
                                  "new_epoch": event["new_epoch"]})
    return {
        "schema_version": SCHEMA_VERSION,
        "ledger_id": ledger_id,
        "revision": value["revision"],
        "stage": stage,
        "binding": binding,
        "fe_mapping": fe_map,
        "native_state": native,
        "phase_timings": normalized_timings,
        "observed_fe": observed,
        "id_reuse_events": normalized_events,
    }


def _read_file(path, ledger_id):
    info = _check_regular_file(path)
    if info is None:
        raise FileNotFoundError("mesh provenance ledger does not exist")
    if info.st_size > _MAX_LEDGER_BYTES:
        raise ValueError("mesh provenance ledger exceeds its size limit")
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read mesh provenance ledger: {exc}") from exc
    ledger = _validate_ledger(value, ledger_id)
    canonical = _canonical(ledger)
    if canonical != raw:
        raise RuntimeError("mesh provenance ledger is non-canonical or malformed")
    return ledger, _hash_text(canonical)


def _write_atomic(path, ledger):
    encoded = _canonical(ledger).encode("utf-8")
    if len(encoded) > _MAX_LEDGER_BYTES:
        raise ValueError("mesh provenance ledger exceeds its size limit")
    _check_regular_file(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".mesh-ledger-", suffix=".tmp",
                                         dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _check_regular_file(path)
        os.replace(temp_path, path)
        temp_path = None
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _ledger_hash(ledger):
    return _hash_text(_canonical(ledger))


def _check_expected_hash(expected_hash, actual_hash, *, required):
    if expected_hash is None:
        if required:
            raise ValueError("expected_hash is required for ledger updates")
        return
    if type(expected_hash) is not str or not _SHA256.fullmatch(expected_hash):
        raise ValueError("expected_hash must be a lowercase SHA-256 hex digest")
    if expected_hash != actual_hash:
        raise RuntimeError("ledger changed since it was read; reread before updating")


def _all_fe_references(fe_map):
    return fe_map["original_fe"] + fe_map["new_fe"]


def _observe_fe(ledger, fe_map):
    observed = list(ledger["observed_fe"])
    by_id = defaultdict(list)
    for ref in observed:
        by_id[_reference_key(ref)].append(ref)
    events = list(ledger["id_reuse_events"])
    event_keys = {_canonical(event, "id reuse event") for event in events}
    for current in _all_fe_references(fe_map):
        prior_refs = by_id[_reference_key(current)]
        for prior in prior_refs:
            if not _same_observation(prior, current):
                event = {
                    "type": current["type"], "id": current["id"],
                    "prior_fingerprint": prior["fingerprint"], "prior_epoch": prior["epoch"],
                    "new_fingerprint": current["fingerprint"], "new_epoch": current["epoch"],
                }
                encoded = _canonical(event, "id reuse event")
                if encoded not in event_keys:
                    if len(events) >= _MAX_ID_REUSE_EVENTS:
                        raise ValueError("mesh provenance ID-reuse history exceeds its limit")
                    events.append(event)
                    event_keys.add(encoded)
        encoded_ref = _canonical(current, "observed FE reference")
        if all(_canonical(prior, "observed FE reference") != encoded_ref for prior in observed):
            if len(observed) >= _MAX_FE_REFERENCES:
                raise ValueError("mesh provenance observed FE history exceeds its limit")
            observed.append(current)
            by_id[_reference_key(current)].append(current)
    observed.sort(key=lambda ref: (ref["type"], str(ref["id"]), ref["epoch"], ref["fingerprint"]))
    events.sort(key=lambda event: (event["type"], str(event["id"]), event["new_epoch"], event["new_fingerprint"]))
    ledger["observed_fe"] = observed
    ledger["id_reuse_events"] = events


def _read_checked(directory, path, ledger_id, expected_hash=None):
    ledger, actual_hash = _read_file(path, ledger_id)
    _check_expected_hash(expected_hash, actual_hash, required=False)
    return ledger, actual_hash


def create_ledger(session_dir, ledger_id, payload):
    """Create a ``planned`` ledger under ``state.session_dir/structured-mesh``.

    ``payload`` requires typed ``target``, ``model`` and ``source`` references;
    it may also contain ``fe_mapping`` and ``native_state``. The derived ledger
    filename is fixed by the validated ledger ID. Existing ledgers are never
    overwritten. Returns ``{"ledger", "ledger_hash"}``.
    """
    ledger_id = _ledger_id(ledger_id)
    _object(payload, ("target", "model", "source"), ("fe_mapping", "native_state"), "payload")
    binding = _binding({key: payload[key] for key in ("target", "model", "source")})
    fe_map = _fe_mapping(payload.get("fe_mapping"))
    native = _native_state(payload.get("native_state"))
    directory = _storage_dir(session_dir, create=True)
    path = _ledger_path(directory, ledger_id)
    with _WRITE_LOCK:
        if _check_regular_file(path) is not None:
            raise FileExistsError("ledger_id already exists; create a new ledger_id")
        ledger = {"schema_version": SCHEMA_VERSION, "ledger_id": ledger_id, "revision": 1,
                  "stage": "planned", "binding": binding, "fe_mapping": fe_map,
                  "native_state": native, "phase_timings": [], "observed_fe": [],
                  "id_reuse_events": []}
        _observe_fe(ledger, fe_map)
        _write_atomic(path, ledger)
        return {"ledger": ledger, "ledger_hash": _ledger_hash(ledger)}


def read_ledger(session_dir, ledger_id, expected_hash=None):
    """Read and validate a versioned ledger without modifying it.

    ``expected_hash`` is optional; when supplied, the read fails if the ledger
    no longer has that canonical content hash.
    """
    ledger_id = _ledger_id(ledger_id)
    directory = _storage_dir(session_dir)
    path = _ledger_path(directory, ledger_id)
    with _WRITE_LOCK:
        ledger, digest = _read_checked(directory, path, ledger_id, expected_hash)
    return {"ledger": ledger, "ledger_hash": digest}


def record_stage(session_dir, ledger_id, payload, expected_hash):
    """Atomically advance one bounded ledger stage using optimistic hash checks.

    ``payload`` requires ``stage``, ``target``, ``model`` and ``source`` and
    may replace ``fe_mapping``/``native_state`` or add ``elapsed_seconds``.
    Legal progress is planned -> source_checked -> applying -> verified ->
    checkpointed. ``recovery_required`` can only close through verified or
    checkpointed. A ledger already at ``applying`` can never enter applying
    again; an unknown ownership/regeneration state and detected FE ID reuse also
    block entry to applying. Advancing to verified requires explicit known
    ownership and completed or unnecessary regeneration readback. No credentials
    or free-form metadata are accepted.
    """
    ledger_id = _ledger_id(ledger_id)
    if expected_hash is None:
        raise ValueError("expected_hash is required for ledger updates")
    _object(payload, ("stage", "target", "model", "source"),
            ("fe_mapping", "native_state", "elapsed_seconds"), "payload")
    desired = payload["stage"]
    if desired not in _STAGE_SET:
        raise ValueError(f"stage must be one of {list(STAGES)}")
    binding = _binding({key: payload[key] for key in ("target", "model", "source")})
    supplied_fe_map = _fe_mapping(payload["fe_mapping"]) if "fe_mapping" in payload else None
    supplied_native = _native_state(payload["native_state"]) if "native_state" in payload else None
    elapsed = _finite_elapsed(payload["elapsed_seconds"]) if "elapsed_seconds" in payload else None
    directory = _storage_dir(session_dir)
    path = _ledger_path(directory, ledger_id)
    with _WRITE_LOCK:
        ledger, current_hash = _read_checked(directory, path, ledger_id, expected_hash)
        if binding != ledger["binding"]:
            raise RuntimeError("target, model, or source fingerprint/epoch does not match this ledger")
        if desired not in _TRANSITIONS[ledger["stage"]]:
            raise RuntimeError(f"illegal mesh provenance transition: {ledger['stage']} -> {desired}")
        if desired == "applying":
            if ledger["stage"] != "source_checked":
                raise RuntimeError("applying is allowed only immediately after source_checked")
            if ledger["id_reuse_events"]:
                raise RuntimeError("detected FE ID reuse blocks applying under this ledger")
            native = supplied_native if supplied_native is not None else ledger["native_state"]
            if native["ownership"] == "unknown" or native["regeneration"] in {"running", "unknown"}:
                raise RuntimeError("unknown native ownership/regeneration state blocks applying")
        if desired == "verified":
            if supplied_native is None:
                raise ValueError("verified requires an explicit native_state readback")
            # verified records the FE operation's independently checked result.
            # Native ownership/regeneration remain separate observations;
            # unknown is preserved and never grants subsequent write authority.
            if supplied_native["regeneration"] == "running":
                raise RuntimeError("verified cannot be recorded while native regeneration is running")
        next_ledger = dict(ledger)
        next_ledger["revision"] += 1
        next_ledger["stage"] = desired
        if supplied_fe_map is not None:
            next_ledger["fe_mapping"] = supplied_fe_map
            _observe_fe(next_ledger, supplied_fe_map)
        if supplied_native is not None:
            next_ledger["native_state"] = supplied_native
        if desired == "applying" and next_ledger["id_reuse_events"]:
            raise RuntimeError("detected FE ID reuse blocks applying under this ledger")
        if elapsed is not None:
            next_ledger["phase_timings"] = list(next_ledger["phase_timings"])
            next_ledger["phase_timings"].append({"stage": desired, "elapsed_seconds": elapsed})
        next_ledger = _validate_ledger(next_ledger, ledger_id)
        # Recheck the content immediately before replacement. The bridge calls
        # this on its owning main thread; the process lock also serializes local callers.
        _current, rechecked_hash = _read_file(path, ledger_id)
        if rechecked_hash != current_hash:
            raise RuntimeError("ledger changed during update; reread before updating")
        _write_atomic(path, next_ledger)
        return {"ledger": next_ledger, "ledger_hash": _ledger_hash(next_ledger)}


def validate_resume(session_dir, ledger_id, payload, expected_hash=None):
    """Verify target/model/source identity before applying or recovering.

    ``payload`` requires the same three typed references and may specify
    ``purpose`` as ``apply`` (the ledger must be source_checked and unambiguous)
    or ``recovery`` (the ledger must be applying/recovery_required). Recovery
    validation never authorizes re-entry into applying.
    """
    ledger_id = _ledger_id(ledger_id)
    _object(payload, ("target", "model", "source"), ("purpose",), "payload")
    binding = _binding({key: payload[key] for key in ("target", "model", "source")})
    purpose = payload.get("purpose", "apply")
    if purpose not in {"apply", "recovery"}:
        raise ValueError("purpose must be 'apply' or 'recovery'")
    directory = _storage_dir(session_dir)
    path = _ledger_path(directory, ledger_id)
    with _WRITE_LOCK:
        ledger, digest = _read_checked(directory, path, ledger_id, expected_hash)
    if binding != ledger["binding"]:
        raise RuntimeError("resume target, model, or source fingerprint/epoch mismatch")
    if purpose == "apply":
        if ledger["stage"] != "source_checked":
            raise RuntimeError(f"cannot re-enter applying from ledger stage {ledger['stage']!r}")
        if ledger["id_reuse_events"]:
            raise RuntimeError("detected FE ID reuse blocks applying under this ledger")
        native = ledger["native_state"]
        if native["ownership"] == "unknown" or native["regeneration"] in {"running", "unknown"}:
            raise RuntimeError("unknown native ownership/regeneration state blocks applying")
        can_apply = True
    else:
        if ledger["stage"] not in {"applying", "recovery_required"}:
            raise RuntimeError("recovery validation requires applying or recovery_required stage")
        can_apply = False
    return {"valid": True, "purpose": purpose, "stage": ledger["stage"],
            "can_apply": can_apply, "id_reuse_detected": bool(ledger["id_reuse_events"]),
            "ledger_hash": digest}
