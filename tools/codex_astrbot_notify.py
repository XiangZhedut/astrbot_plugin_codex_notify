"""Forward a Codex completion payload to the local CODEX通知助手 plugin."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_codex_notify"
ASTRBOT_DATA = Path.home() / ".astrbot" / "data"
STATE_PATH = ASTRBOT_DATA / "plugin_data" / PLUGIN_NAME / "bridge_state.json"
ASTRBOT_CONFIG_PATH = ASTRBOT_DATA / "config" / f"{PLUGIN_NAME}_config.json"
LOG_PATH = Path.home() / ".codex" / "astrbot-notify.log"


def _log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            handle.write(f"{timestamp} {message.rstrip()}\n")
    except OSError:
        pass


def _payload_arg() -> str:
    if len(sys.argv) > 1:
        return sys.argv[-1]
    raw = sys.stdin.read()
    return raw.strip() or "{}"


def _parse_payload(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"payload": value}
    except json.JSONDecodeError:
        return {"payload": raw}


def _forward_codex_desktop_handler(raw_payload: str) -> None:
    """Preserve the Codex desktop turn-ended handler when it is installed."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    pattern = str(
        Path(local_app_data)
        / "OpenAI"
        / "Codex"
        / "runtimes"
        / "cua_node"
        / "*"
        / "bin"
        / "node_modules"
        / "@oai"
        / "sky"
        / "bin"
        / "windows"
        / "codex-computer-use.exe"
    )
    candidates = [Path(path) for path in glob.glob(pattern)]
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        return
    handler = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        subprocess.run(
            [str(handler), "turn-ended", raw_payload],
            check=False,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"desktop_handler_error={type(exc).__name__}")


def _event_metadata(payload: dict[str, Any]) -> str:
    """Record routing metadata without logging prompts, output, or bridge tokens."""
    return json.dumps(
        {
            key: str(payload.get(key) or payload.get(key.replace("-", "_")) or "")[:128]
            for key in ("type", "thread-id", "turn-id")
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _read_bridge_settings() -> tuple[str, int, str]:
    state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
    token = str(state.get("token") or "")
    config: dict[str, Any] = {}
    if ASTRBOT_CONFIG_PATH.exists():
        config = json.loads(ASTRBOT_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    host = str(config.get("listen_host") or "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        host = "127.0.0.1"
    port = int(config.get("listen_port") or 6198)
    return host, port, token


def _send_to_astrbot(payload: dict[str, Any]) -> None:
    try:
        host, port, token = _read_bridge_settings()
        if not token:
            raise ValueError("missing_token")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}/notify",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Codex-Notify-Token": token,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status == 202:
                status = json.loads(response.read()).get("status", "queued")
                if status not in {
                    "queued",
                    "waiting_completion",
                    "ignored_event",
                    "ignored_internal",
                    "duplicate",
                }:
                    status = "queued"
                _log(f"astrbot_{status} {_event_metadata(payload)}")
            elif response.status != 200:
                _log(f"astrbot_http_status={response.status} {_event_metadata(payload)}")
            else:
                _log(f"astrbot_delivered {_event_metadata(payload)}")
    except urllib.error.HTTPError as exc:
        _log(f"astrbot_http_error={exc.code} {_event_metadata(payload)}")
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        _log(f"astrbot_notify_error={type(exc).__name__} {_event_metadata(payload)}")


def main() -> int:
    raw = _payload_arg()
    payload = _parse_payload(raw)
    try:
        import importlib.util

        client_path = Path(__file__).resolve().parent.parent / "desktop_client.py"
        if client_path.is_file():
            spec = importlib.util.spec_from_file_location("codex_desktop_connection", client_path)
            client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(client)
            client.refresh_from_payload(payload)
    except Exception as exc:
        _log(f"desktop_connection_refresh_error={type(exc).__name__}")
    _log(f"notify_received {_event_metadata(payload)}")
    _forward_codex_desktop_handler(raw)
    _send_to_astrbot(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
