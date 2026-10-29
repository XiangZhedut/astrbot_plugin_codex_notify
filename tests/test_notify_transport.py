"""Exercise the real local HTTP transport without sending anything to QQ."""

import importlib.util
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

SCRIPTS = [
    Path(__file__).resolve().parents[1] / "tools/codex_astrbot_notify.py",
]


def load_script(path):
    spec = importlib.util.spec_from_file_location("notify_transport_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotifyTransportTests(unittest.TestCase):
    def test_accepted_queue_is_not_logged_as_delivered(self):
        for script in SCRIPTS:
            with self.subTest(script=str(script)), tempfile.TemporaryDirectory() as temporary:
                module = load_script(script)
                module.LOG_PATH = Path(temporary) / "notify.log"
                response = io.BytesIO(b'{"ok":true,"status":"ignored_internal"}')
                response.status = 202
                with (
                    patch.object(
                        module, "_read_bridge_settings", return_value=("127.0.0.1", 6198, "secret")
                    ),
                    patch.object(module.urllib.request, "urlopen", return_value=response),
                ):
                    module._send_to_astrbot(
                        {"type": "agent-turn-complete", "thread-id": "internal"}
                    )
                logs = module.LOG_PATH.read_text(encoding="utf-8")
                self.assertIn("astrbot_ignored_internal", logs)
                self.assertNotIn("astrbot_delivered", logs)

    def test_bom_settings_and_different_threads_reach_the_same_bridge(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(
                    (
                        self.path,
                        self.headers.get("X-Codex-Notify-Token"),
                        json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                    )
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for script in SCRIPTS:
                    with (
                        self.subTest(script=str(script)),
                        tempfile.TemporaryDirectory() as temporary,
                    ):
                        module = load_script(script)
                        root = Path(temporary)
                        module.STATE_PATH = root / "state.json"
                        module.ASTRBOT_CONFIG_PATH = root / "config.json"
                        module.LOG_PATH = root / "notify.log"
                        # Windows editors may add a BOM to either settings file.
                        module.STATE_PATH.write_text(
                            json.dumps({"token": "test-token"}), encoding="utf-8-sig"
                        )
                        module.ASTRBOT_CONFIG_PATH.write_text(
                            json.dumps(
                                {
                                    "listen_host": "127.0.0.1",
                                    "listen_port": server.server_port,
                                }
                            ),
                            encoding="utf-8-sig",
                        )
                        handler_name = (
                            "_forward_codex_desktop_handler"
                            if hasattr(module, "_forward_codex_desktop_handler")
                            else "_forward_existing_handler"
                        )
                        start = len(received)
                        for thread_id in ("current-thread", "other-background-thread"):
                            payload = {
                                "type": "agent-turn-complete",
                                "thread-id": thread_id,
                                "turn-id": "turn-" + thread_id,
                                "input-messages": ["private-input-not-for-logs"],
                                "last-assistant-message": "private-result-not-for-logs",
                            }
                            raw = json.dumps(payload)
                            with (
                                patch.object(module.sys, "argv", [str(script), raw]),
                                patch.object(module, handler_name) as existing_handler,
                            ):
                                self.assertEqual(module.main(), 0)
                                existing_handler.assert_called_once_with(raw)
                        sent = received[start:]
                        self.assertEqual(
                            [x[2]["thread-id"] for x in sent],
                            ["current-thread", "other-background-thread"],
                        )
                        self.assertTrue(
                            all(x[0] == "/notify" and x[1] == "test-token" for x in sent)
                        )
                        logs = module.LOG_PATH.read_text(encoding="utf-8")
                        self.assertEqual(logs.count("astrbot_delivered"), 2)
                        self.assertIn("other-background-thread", logs)
                        for secret in (
                            "test-token",
                            "private-input-not-for-logs",
                            "private-result-not-for-logs",
                        ):
                            self.assertNotIn(secret, logs)
            finally:
                server.shutdown()
                worker.join(timeout=3)

    def test_plain_utf8_still_works(self):
        for script in SCRIPTS:
            with self.subTest(script=str(script)), tempfile.TemporaryDirectory() as temporary:
                module = load_script(script)
                root = Path(temporary)
                module.STATE_PATH = root / "state.json"
                module.ASTRBOT_CONFIG_PATH = root / "config.json"
                module.STATE_PATH.write_text('{"token":"test-token"}', encoding="utf-8")
                module.ASTRBOT_CONFIG_PATH.write_text('{"listen_port":6198}', encoding="utf-8")
                self.assertEqual(module._read_bridge_settings(), ("127.0.0.1", 6198, "test-token"))


if __name__ == "__main__":
    unittest.main()
