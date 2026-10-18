"""Boundary checks for the custom transport, using real stream sockets."""

import socket
import struct
import threading
import unittest

from ansa_mcp_shared.framing import MAX_FRAME_BYTES, recv_frame, send_frame


class FramingChecks(unittest.TestCase):
    def test_fragmentation_and_consecutive_frames(self):
        reader, writer = socket.socketpair()
        with reader, writer:
            payload = b'{"message":"split"}'
            frame = struct.pack("!I", len(payload)) + payload
            def write():
                for byte in frame:
                    writer.sendall(bytes([byte]))
                send_frame(writer, {"message": "next"})
            thread = threading.Thread(target=write)
            thread.start()
            self.assertEqual(recv_frame(reader), {"message": "split"})
            self.assertEqual(recv_frame(reader), {"message": "next"})
            thread.join()

    def test_oversized_frame_rejected_before_payload(self):
        reader, writer = socket.socketpair()
        with reader, writer:
            writer.sendall(struct.pack("!I", MAX_FRAME_BYTES + 1))
            with self.assertRaises(ValueError):
                recv_frame(reader)

    def test_disconnection_mid_frame(self):
        reader, writer = socket.socketpair()
        with reader:
            writer.sendall(struct.pack("!I", 20) + b"{")
            writer.close()
            with self.assertRaises(EOFError):
                recv_frame(reader)


if __name__ == "__main__":
    unittest.main()
