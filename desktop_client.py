"""Local Codex desktop app-tools client, using its bundled pipe protocol.

No second App Server is started. The desktop supplies the pipe endpoint to
its tools; a local descriptor is refreshed by notify when that endpoint changes.
This is a version-dependent desktop integration, not a public HTTP service.
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import subprocess
import sys
import uuid
from contextlib import closing
from pathlib import Path

DESCRIPTOR = (
    Path.home() / ".astrbot/data/plugin_data/astrbot_plugin_codex_notify/desktop_connection.json"
)
READ_TOOLS = {"list_threads", "read_thread", "wait_threads", "list_projects"}
WRITE_TOOLS = {"send_message_to_thread", "create_thread"}


def refresh_from_payload(payload):
    thread = payload.get("thread-id") or payload.get("thread_id")
    if not isinstance(thread, str):
        return False
    path = Path.home() / ".codex/state_5.sqlite"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        row = db.execute("SELECT thread_source FROM threads WHERE id=?", (thread,)).fetchone()
    return bool(row and row[0] == "user" and refresh_connection(thread))


def refresh_connection(thread_id: str) -> bool:
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
    if not pipe.startswith("\\\\.\\pipe\\") or not thread_id:
        return False
    DESCRIPTOR.parent.mkdir(parents=True, exist_ok=True)
    temp = DESCRIPTOR.with_name("desktop_connection." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps({"pipe": pipe, "caller_thread_id": thread_id}), encoding="utf-8")
        os.replace(temp, DESCRIPTOR)
    finally:
        temp.unlink(missing_ok=True)
    return True


class DesktopClient:
    def __init__(self, descriptor=DESCRIPTOR):
        self.descriptor = Path(descriptor)

    def call(self, tool: str, arguments: dict, *, allow_write: bool = False):
        if tool not in READ_TOOLS | WRITE_TOOLS:
            raise ValueError("Unsupported desktop operation")
        if tool in WRITE_TOOLS and not allow_write:
            raise PermissionError("Explicit write authorization is required")
        connection = json.loads(self.descriptor.read_text(encoding="utf-8-sig"))
        if not str(connection.get("pipe", "")).startswith("\\\\.\\pipe\\"):
            raise ValueError("Expected a local Windows named pipe")
        job = {"connection": connection, "tool": tool, "arguments": arguments}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise ConnectionError(
                "Codex desktop connection unavailable; refresh with a new Codex notification"
            )
        response = json.loads(result.stdout)
        if "error" in response:
            raise RuntimeError(str(response["error"].get("message", "Desktop request failed")))
        result = response.get("result", {})
        if not result.get("success"):
            raise RuntimeError("Codex desktop rejected the operation")
        return result


def _read_exact(handle, size):
    data = bytearray()
    while len(data) < size:
        chunk = handle.read(size - len(data))
        if not chunk:
            raise ConnectionError("Desktop pipe closed")
        data.extend(chunk)
    return bytes(data)


def _worker(job):
    connection = job["connection"]
    params = {
        "namespace": "codex_app",
        "tool": job["tool"],
        "arguments": job["arguments"],
        "callerSource": "codex",
        "threadId": connection["caller_thread_id"],
        "callId": "mcp-call-" + str(uuid.uuid4()),
        "turnId": "mcp-turn-" + str(uuid.uuid4()),
    }
    raw = json.dumps({"id": 1, "jsonrpc": "2.0", "method": "tools/call", "params": params}).encode(
        "utf-8"
    )
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Request too large")
    with open(connection["pipe"], "r+b", buffering=0) as handle:
        handle.write(struct.pack("<I", len(raw)) + raw)
        size = struct.unpack("<I", _read_exact(handle, 4))[0]
        if size > 8 * 1024 * 1024:
            raise ValueError("Response too large")
        return json.loads(_read_exact(handle, size))


if __name__ == "__main__":
    try:
        print(json.dumps(_worker(json.load(sys.stdin)), ensure_ascii=True))
    except Exception:
        # Never print endpoint paths, prompts, or private replies to stderr.
        raise SystemExit(1)
