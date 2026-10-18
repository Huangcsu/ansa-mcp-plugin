"""Windows command output remains readable with Python UTF-8 mode enabled."""

import subprocess
import unittest
from unittest.mock import patch

from ansa_mcp_shared import security


class RuntimeSecurityOutputTests(unittest.TestCase):
    def test_localized_command_bytes_do_not_prevent_ascii_sid_readback(self):
        output = b"\xd3\xc3\xbb\xa7\xd0\xc5\xcf\xa2\r\naccount S-1-5-21-123-456-789-1001\r\n"
        with patch.object(security.subprocess, "run", return_value=subprocess.CompletedProcess(
            ["whoami", "/user"], 0, output, b"")) as run:
            self.assertEqual(security._current_sid(), "S-1-5-21-123-456-789-1001")
        self.assertNotIn("text", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
