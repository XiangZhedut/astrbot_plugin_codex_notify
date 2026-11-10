"""Read only the input and final answer of an explicitly identified user turn."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def turn_details(thread, turn, codex_home=None):
    home = Path(codex_home or Path.home() / ".codex")
    database = (home / "state_5.sqlite").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database, uri=True, timeout=2)) as db:
        row = db.execute(
            "SELECT rollout_path FROM threads WHERE id=? AND thread_source='user'", (thread,)
        ).fetchone()
    if not row:
        return {}
    path = Path(row[0])
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 32 * 1024 * 1024))
        lines = handle.read().splitlines()
    active = False
    inputs = []
    result = {}
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            continue
        pay = event["payload"]
        if event.get("type") == "turn_context" or (
            event.get("type") == "event_msg" and pay.get("type") == "task_started"
        ):
            active = pay.get("turn_id") == turn
        if active and event.get("type") == "response_item" and pay.get("role") == "user":
            for part in pay.get("content", []):
                if not isinstance(part, dict) or part.get("type") != "input_text":
                    continue
                text = part.get("text")
                if (
                    isinstance(text, str)
                    and text.strip()
                    and not text.lstrip().startswith(("<environment_context>", "<codex_apps_"))
                ):
                    if not inputs or inputs[-1] != text:
                        inputs.append(text)
        if (
            event.get("type") == "event_msg"
            and pay.get("type") == "task_complete"
            and pay.get("turn_id") == turn
        ):
            result = {
                "user_instruction": "\n\n".join(inputs),
                "full_result": str(pay.get("last_agent_message") or ""),
            }
            break
    return result


def context_record(target, text, payload=None, codex_home=None):
    record = {"target": target, "time": time.time(), "text": text}
    if not payload:
        return record
    thread, turn = payload.get("thread-id"), payload.get("turn-id")
    record.update(thread_id=thread, turn_id=turn, task_name=payload.get("task-name", ""))
    if payload.get("type") == "agent-turn-complete":
        record["full_result"] = str(payload.get("last-assistant-message") or "")
        try:
            record.update(turn_details(thread, turn, codex_home))
        except (OSError, sqlite3.Error, ValueError):
            pass
    return record


def enrich_legacy(record, monitor_database, codex_home=None):
    if "full_result" in record:
        return record
    # Match only an already stored notification by its local delivery timestamp.
    # Never expose unrelated historic chats or replay notifications to QQ.
    try:
        stamp = float(record["time"])
        with closing(
            sqlite3.connect(Path(monitor_database).resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            rows = db.execute(
                "SELECT thread_id,turn_id FROM deliveries WHERE delivered_at BETWEEN ? AND ?",
                (stamp - 2, stamp + 2),
            ).fetchall()
        if len(rows) == 1:
            details = turn_details(*rows[0], codex_home=codex_home)
            if details:
                return {**record, "thread_id": rows[0][0], "turn_id": rows[0][1], **details}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        pass
    return record
