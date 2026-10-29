import io
import unittest

from astrbot_plugin_codex_notify.desktop_client import DesktopClient, _read_exact


class DesktopClientTests(unittest.TestCase):
    def test_unapproved_mutation_and_unknown_operation_never_connect(self):
        client = DesktopClient("descriptor-does-not-exist.json")
        for name in ("send_message_to_thread", "create_thread"):
            with self.assertRaises(PermissionError):
                client.call(name, {})
        with self.assertRaises(ValueError):
            client.call("command/exec", {}, allow_write=True)

    def test_read_frame_handles_eof(self):
        self.assertEqual(_read_exact(io.BytesIO(b"abc"), 3), b"abc")
        with self.assertRaises(ConnectionError):
            _read_exact(io.BytesIO(b"a"), 2)
