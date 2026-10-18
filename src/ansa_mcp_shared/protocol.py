"""Versioned restricted controls and the sole read-only model operation."""

from . import PROTOCOL_VERSION

REQUEST_KINDS = frozenset({"hello", "probe", "grant", "revoke", "authorization", "model_snapshot",
                           "panel_show", "task_submit", "task_query", "task_cancel", "result_ack", "task_status", "backend_status"})


def request(kind, request_id, **fields):
    if kind not in REQUEST_KINDS:
        raise ValueError("Unsupported request")
    return {"version": PROTOCOL_VERSION, "kind": kind, "request_id": request_id, **fields}


def validate(message):
    if not isinstance(message, dict) or type(message.get("version")) is not int or message["version"] != PROTOCOL_VERSION:
        raise ValueError("Unsupported protocol version")
    kind = message.get("kind")
    if kind not in REQUEST_KINDS | {"reply", "bind_requested"}:
        raise ValueError("Unsupported message kind")
    if not isinstance(message.get("request_id"), str) or not message["request_id"]:
        raise ValueError("Missing request ID")
    return message
