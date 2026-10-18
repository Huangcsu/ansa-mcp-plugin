"""Bounded length-prefixed UTF-8 JSON frames."""

import json
import struct

MAX_FRAME_BYTES = 1024 * 1024


def recv_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("TCP connection closed inside a frame")
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(sock):
    length = struct.unpack("!I", recv_exact(sock, 4))[0]
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ValueError("Invalid TCP frame length")
    def reject_constant(value):
        raise ValueError("Non-finite JSON number: " + value)
    value = json.loads(recv_exact(sock, length).decode("utf-8"), parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("TCP frame must contain a JSON object")
    return value


def send_frame(sock, value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not data or len(data) > MAX_FRAME_BYTES:
        raise ValueError("TCP frame exceeds limit")
    sock.sendall(struct.pack("!I", len(data)) + data)
