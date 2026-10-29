"""Read local Codex completion records; never infer completion from streamed text.

The desktop's internal title/suggestion sessions are not user threads. Reading
the session journal also covers Windows notify spawn failures (error 206).
Only this plugin's own checkpoint/outbox database is writable.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class CompletionMonitor:
    def __init__(self, codex_home: Path, state_path: Path, grace_seconds: float = 10):
        self.codex_home = codex_home
        self.state_path = state_path
        self.grace_seconds = max(0, min(float(grace_seconds), 120))
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._store() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value REAL);
                CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, offset INTEGER);
                CREATE TABLE IF NOT EXISTS deliveries(
                    thread_id TEXT, turn_id TEXT, payload TEXT, ready_at REAL,
                    delivered_at REAL, attempts INTEGER DEFAULT 0,
                    next_attempt REAL DEFAULT 0, created_at REAL,
                    PRIMARY KEY(thread_id, turn_id));
                CREATE TABLE IF NOT EXISTS alerts(
                    thread_id TEXT, event_id TEXT, turn_id TEXT, kind TEXT,
                    payload TEXT, ready_at REAL, delivered_at REAL, resolved_at REAL,
                    attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
                    PRIMARY KEY(thread_id, event_id));
                CREATE TABLE IF NOT EXISTS quota_windows(
                    bucket TEXT PRIMARY KEY, limit_name TEXT, window_minutes INTEGER,
                    reset_at REAL, snapshot_at REAL, cycle_id TEXT,
                    notified INTEGER DEFAULT 0, armed INTEGER DEFAULT 0);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(files)")}
            if "active_turn" not in columns:
                db.execute("ALTER TABLE files ADD COLUMN active_turn TEXT")
            quota_columns = {r[1] for r in db.execute("PRAGMA table_info(quota_windows)")}
            if "used_percent" not in quota_columns:
                db.execute("ALTER TABLE quota_windows ADD COLUMN used_percent REAL")
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('attention_since',?)", (int(time.time()),)
            )
        self._initialize()
        self._seed_quota()

    @contextmanager
    def _store(self):
        with closing(sqlite3.connect(self.state_path, timeout=5)) as db:
            with db:
                yield db

    def _threads(self) -> dict[str, dict[str, Any]]:
        path = self.codex_home / "state_5.sqlite"
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            db.row_factory = sqlite3.Row
            # Null thread_source is used by some older desktop sessions. Never
            # admit subagents, even if their prompts look like normal questions.
            rows = db.execute("""SELECT id, rollout_path, title, name, thread_source, source
                FROM threads WHERE thread_source='user'
                OR (thread_source IS NULL AND source IN ('vscode','cli','exec'))""")
            return {r["id"]: dict(r) for r in rows}

    def _initialize(self):
        threads = self._threads()
        with self._store() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='activated_at'").fetchone():
                return
            # First activation starts at EOF: installing the fix must not flood
            # QQ with all historic completions. Subsequent restarts resume offsets.
            for row in threads.values():
                path = Path(row["rollout_path"])
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO files(path,offset) VALUES (?,?)", (str(path), size)
                )
            db.execute("INSERT INTO meta VALUES ('activated_at',?)", (int(time.time()),))

    def _seed_quota(self):
        """Arm future deadlines from existing snapshots without replaying old resets."""
        with self._store() as db:
            if db.execute("SELECT 1 FROM meta WHERE key='quota_usage_seeded'").fetchone():
                return
            now = time.time()
            for row in self._threads().values():
                path = Path(row["rollout_path"])
                try:
                    with path.open("rb") as handle:
                        handle.seek(max(0, path.stat().st_size - 2 * 1024 * 1024))
                        lines = handle.read().splitlines()
                except OSError:
                    continue
                # Check every quota snapshot in this bounded tail: different
                # model buckets may be interleaved in the same user session.
                for line in lines:
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if (
                        isinstance(event, dict)
                        and event.get("type") == "event_msg"
                        and isinstance(event.get("payload"), dict)
                        and event["payload"].get("type") == "token_count"
                    ):
                        self._quota_update(db, event, now)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('quota_seeded',?)", (now,))
            db.execute("INSERT INTO meta VALUES ('quota_usage_seeded',?)", (now,))

    def _quota_emit(self, db, window, now, confirmed=False):
        if window["notified"] or not window["armed"]:
            return
        minutes = window["window_minutes"]
        duration = (
            f"{minutes // 1440}天"
            if minutes % 1440 == 0
            else f"{minutes // 60}小时"
            if minutes % 60 == 0
            else f"{minutes}分钟"
        )
        stamp = datetime.fromtimestamp(window["reset_at"], timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        message = (
            f"{window['limit_name']} · {duration}额度窗口\n记录的重置时间：{stamp}（北京时间）\n"
        )
        message += (
            "新额度记录显示该窗口已进入下一周期。"
            if confirmed
            else "预计重置时间已到；实际可用额度请以 Codex 面板为准。"
        )
        self._alert(
            db,
            {"id": "__codex_account__", "title": "Codex 账户额度"},
            "quota-reset",
            "quota:" + window["bucket"] + ":" + window["cycle_id"],
            "quota_reset",
            message,
            now,
            now,
        )
        db.execute("UPDATE quota_windows SET notified=1 WHERE bucket=?", (window["bucket"],))

    def _quota_update(self, db, event, now):
        rates = event.get("payload", {}).get("rate_limits")
        if not isinstance(rates, dict):
            return
        timestamp = self._timestamp(event, now)
        limit_id = str(rates.get("limit_id") or rates.get("limitId") or "codex")[:120]
        name = str(rates.get("limit_name") or rates.get("limitName") or limit_id)[:120]
        for key in ("primary", "secondary"):
            window = rates.get(key)
            if not isinstance(window, dict):
                continue
            reset = window.get("resets_at", window.get("resetsAt"))
            used = window.get("used_percent", window.get("usedPercent"))
            if (
                isinstance(used, bool)
                or not isinstance(used, (int, float))
                or not math.isfinite(used)
            ):
                used = None
            else:
                used = max(0, min(100, used))
            minutes = window.get("window_minutes", window.get("windowDurationMins"))
            if (
                isinstance(reset, bool)
                or not isinstance(reset, (int, float))
                or not math.isfinite(reset)
                or not 0 < reset < 253402200000
                or isinstance(minutes, bool)
                or not isinstance(minutes, (int, float))
                or not math.isfinite(minutes)
                or not 0 < minutes <= 1000000
            ):
                continue
            minutes = int(minutes)
            if not minutes:
                continue
            bucket = f"{limit_id}:{key}:{minutes}"
            cursor = db.execute("SELECT * FROM quota_windows WHERE bucket=?", (bucket,))
            previous = cursor.fetchone()
            old = dict(zip([c[0] for c in cursor.description], previous)) if previous else None
            if old and timestamp < old["snapshot_at"]:
                continue
            if (
                old
                and timestamp == old["snapshot_at"]
                and (old["used_percent"] is not None or used is None)
            ):
                continue
            # Small reset-time differences across simultaneous chats belong to
            # one cycle. A large advance after expiry is a new observed cycle.
            advanced = (
                old and reset - old["reset_at"] > minutes * 30 and timestamp >= old["reset_at"] - 60
            )
            if advanced:
                self._quota_emit(db, old, now, confirmed=True)
            cycle = str(int(reset)) if not old or advanced else old["cycle_id"]
            notified = old["notified"] if old and not advanced else 0
            armed = int(old["armed"] or reset > now) if old and not advanced else int(reset > now)
            db.execute(
                """INSERT OR REPLACE INTO quota_windows
                (bucket,limit_name,window_minutes,reset_at,snapshot_at,cycle_id,notified,armed,used_percent)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (bucket, name, minutes, reset, timestamp, cycle, notified, armed, used),
            )

    def quota_status(self) -> list[dict[str, Any]]:
        with self._store() as db:
            cursor = db.execute("""SELECT limit_name,window_minutes,reset_at,snapshot_at,used_percent
                FROM quota_windows ORDER BY limit_name,window_minutes""")
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _quota_due(self, db, now):
        cursor = db.execute(
            "SELECT * FROM quota_windows WHERE armed=1 AND notified=0 AND reset_at<=?", (now,)
        )
        columns = [c[0] for c in cursor.description]
        for row in cursor.fetchall():
            self._quota_emit(db, dict(zip(columns, row)), now)

    @staticmethod
    def _timestamp(event, now):
        try:
            return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, TypeError, ValueError):
            return now

    @staticmethod
    def _reply_request(text):
        # Only explicit requests near the end of a final answer. A question mark
        # in commentary, quoted examples, or an optional offer is not a pause.
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        tail = "\n".join([s.strip() for s in text.splitlines() if s.strip()][-3:])[-800:]
        return bool(
            re.search(
                r"(?:请(?:你)?(?:回复|确认|选择|提供|上传|告诉我|补充)|"
                r"需要你(?:回复|确认|选择|提供|上传|补充|登录|解锁)|"
                r"(?:是否|要不要)(?:继续|执行|允许|开始)[^。\n]*[？?])",
                tail,
            )
        )

    def _alert(self, db, row, turn, event_id, kind, text, timestamp, now):
        since = db.execute("SELECT value FROM meta WHERE key='attention_since'").fetchone()[0]
        if not turn or timestamp < since:
            return
        payload = {
            "type": "agent-needs-attention",
            "thread-id": row["id"],
            "turn-id": turn,
            "event-id": event_id,
            "notification-kind": kind,
            "task-name": row.get("name") or row.get("title") or "Codex 会话",
            "last-assistant-message": text,
        }
        db.execute(
            """INSERT OR IGNORE INTO alerts
            (thread_id,event_id,turn_id,kind,payload,ready_at) VALUES (?,?,?,?,?,?)""",
            (row["id"], event_id, turn, kind, json.dumps(payload, ensure_ascii=False), now + 2),
        )

    @staticmethod
    def _resolve_waiting(db, tid, turn=None, event_id=None):
        query = "UPDATE alerts SET resolved_at=?,payload='{}' WHERE thread_id=? AND kind IN ('input_required','approval_required') AND resolved_at IS NULL"
        values = [time.time(), tid]
        if event_id:
            query += " AND event_id=?"
            values.append(event_id)
        elif turn:
            query += " AND turn_id=?"
            values.append(turn)
        db.execute(query, values)

    def _record(self, db, row, event, now):
        pay = event.get("payload", {})
        if not isinstance(pay, dict):
            return
        event_type = event.get("type")
        kind = pay.get("type")
        if event_type == "event_msg" and kind == "token_count":
            self._quota_update(db, event, now)
            return
        timestamp = self._timestamp(event, now)
        metadata = pay.get("internal_chat_message_metadata_passthrough") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        turn = pay.get("turn_id") or metadata.get("turn_id") or row.get("_turn")
        if turn:
            row["_turn"] = turn
        if event_type == "event_msg" and kind == "task_started":
            row["_turn"] = turn
            self._resolve_waiting(db, row["id"])
            return
        if event_type == "response_item":
            call = pay.get("call_id")
            if kind == "function_call_output" and call:
                try:
                    output = json.loads(pay.get("output", "{}"))
                except (ValueError, TypeError):
                    output = None
                # Async questions return accepted=true immediately, before the
                # user has answered. That acknowledgement does not resolve it.
                if not (isinstance(output, dict) and output.get("accepted") is True):
                    self._resolve_waiting(db, row["id"], event_id="request:" + call)
                return
            name = str(pay.get("name", "")).rsplit(".", 1)[-1]
            if (
                kind == "function_call"
                and name
                in {"request_user_input", "request_user_input_async", "request_permissions"}
                and call
            ):
                try:
                    args = json.loads(pay.get("arguments", "{}"))
                except (ValueError, TypeError):
                    args = {}
                questions = args.get("questions", []) if isinstance(args, dict) else []
                text = "\n".join(
                    str(q.get("question") or q.get("title") or "")
                    for q in questions
                    if isinstance(q, dict)
                )
                approval = name == "request_permissions"
                text = text or (
                    str(args.get("reason") or "请回到 Codex 查看权限确认。")
                    if isinstance(args, dict)
                    else "请回到 Codex 查看待回复问题。"
                )
                self._alert(
                    db,
                    row,
                    turn,
                    "request:" + call,
                    "approval_required" if approval else "input_required",
                    text,
                    timestamp,
                    now,
                )
                return
            if pay.get("role") == "user":
                for part in pay.get("content", []):
                    text = part.get("text", "") if isinstance(part, dict) else ""
                    if "<send_user_message_question_reply>" in text:
                        for call in re.findall(r"call_[A-Za-z0-9_-]+", text):
                            self._resolve_waiting(db, row["id"], event_id="request:" + call)
                return
        if event_type == "event_msg" and kind == "turn_aborted":
            self._resolve_waiting(db, row["id"], turn)
            self._alert(
                db,
                row,
                turn,
                "paused:" + str(turn),
                "paused",
                "本轮运行已中断。原因：" + str(pay.get("reason") or "未提供；请回到 Codex 检查。"),
                pay.get("completed_at") or timestamp,
                now,
            )
            return
        if event_type != "event_msg" or kind != "task_complete":
            return
        result = pay.get("last_agent_message")
        completed = pay.get("completed_at")
        error = pay.get("error")
        if error:
            self._resolve_waiting(db, row["id"], turn)
            message = (
                str(error.get("message") or error.get("codex_error_info") or "运行失败")
                if isinstance(error, dict)
                else str(error)
            )
            self._alert(
                db, row, turn, "failed:" + str(turn), "failed", message, completed or timestamp, now
            )
            return
        if db.execute(
            "SELECT 1 FROM alerts WHERE thread_id=? AND turn_id=? AND kind IN ('input_required','approval_required') AND resolved_at IS NULL LIMIT 1",
            (row["id"], turn),
        ).fetchone():
            return
        if isinstance(result, str) and self._reply_request(result):
            self._alert(
                db,
                row,
                turn,
                "reply:" + str(turn),
                "input_required",
                result,
                completed or timestamp,
                now,
            )
            return
        activated = db.execute("SELECT value FROM meta WHERE key='activated_at'").fetchone()[0]
        if (
            not turn
            or pay.get("error")
            or not isinstance(result, str)
            or not result.strip()
            or not isinstance(completed, (int, float))
            or completed < activated
        ):
            return
        payload = {
            "type": "agent-turn-complete",
            "thread-id": row["id"],
            "turn-id": turn,
            "task-name": row.get("name") or row.get("title") or "Codex 会话",
            "last-assistant-message": result,
        }
        db.execute(
            """INSERT OR IGNORE INTO deliveries
            (thread_id,turn_id,payload,ready_at,created_at) VALUES (?,?,?,?,?)""",
            (
                row["id"],
                turn,
                json.dumps(payload, ensure_ascii=False),
                max(now, completed) + self.grace_seconds,
                now,
            ),
        )

    def scan(self):
        """Incremental reads, preserving incomplete trailing JSONL records."""
        threads = self._threads()
        now = time.time()
        with self._store() as db:
            for row in threads.values():
                path = Path(row["rollout_path"])
                checkpoint = db.execute(
                    "SELECT offset,active_turn FROM files WHERE path=?", (str(path),)
                ).fetchone()
                offset = checkpoint[0] if checkpoint else 0
                row["_turn"] = checkpoint[1] if checkpoint else None
                try:
                    size = path.stat().st_size
                    if size < offset:
                        offset = 0
                    if size == offset:
                        continue
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read(8 * 1024 * 1024)
                except OSError:
                    continue
                end = chunk.rfind(b"\n") + 1
                if not end:
                    continue
                for line in chunk[:end].splitlines():
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            self._record(db, row, event, now)
                    except (ValueError, UnicodeError):
                        continue
                db.execute(
                    "INSERT OR REPLACE INTO files(path,offset,active_turn) VALUES (?,?,?)",
                    (str(path), offset + end, row.get("_turn")),
                )
            # Keep only a week of delivered result copies; pending retries survive.
            db.execute("DELETE FROM deliveries WHERE delivered_at < ?", (now - 7 * 86400,))
            db.execute(
                "DELETE FROM alerts WHERE delivered_at < ? OR resolved_at < ?",
                (now - 7 * 86400, now - 7 * 86400),
            )
            self._quota_due(db, now)

    def hint(self, payload: dict[str, Any]) -> str:
        """Treat notify as a hint, never as proof that a user reply is complete."""
        if payload.get("type") != "agent-turn-complete":
            return "ignored_event"
        tid = payload.get("thread-id") or payload.get("thread_id")
        turn = payload.get("turn-id") or payload.get("turn_id")
        row = self._threads().get(tid)
        if row is None or not turn:
            return "ignored_internal"
        # Normal polling handles this too; tail lookup covers hook/scan ordering.
        path = Path(row["rollout_path"])
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 8 * 1024 * 1024))
                lines = handle.read().splitlines()
        except OSError:
            return "waiting_completion"
        with self._store() as db:
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                pay = event.get("payload", {})
                if pay.get("type") == "task_complete" and pay.get("turn_id") == turn:
                    self._record(db, row, event, time.time())
                    break
            item = db.execute(
                "SELECT delivered_at FROM deliveries WHERE thread_id=? AND turn_id=?", (tid, turn)
            ).fetchone()
        return "duplicate" if item and item[0] else "queued"

    def ready(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._store() as db:
            rows = db.execute(
                """SELECT payload FROM deliveries WHERE delivered_at IS NULL
                AND ready_at<=? AND next_attempt<=? ORDER BY ready_at LIMIT 10""",
                (now, now),
            )
            payloads = [json.loads(r[0]) for r in rows]
            alerts = db.execute(
                """SELECT payload FROM alerts WHERE delivered_at IS NULL
                AND resolved_at IS NULL AND ready_at<=? AND next_attempt<=? ORDER BY ready_at LIMIT 10""",
                (now, now),
            )
            return [json.loads(r[0]) for r in alerts] + payloads

    def finish(self, payload: dict[str, Any], success: bool):
        if payload.get("event-id"):
            key = (payload["thread-id"], payload["event-id"])
            with self._store() as db:
                if success:
                    db.execute(
                        "UPDATE alerts SET delivered_at=?,payload='{}' WHERE thread_id=? AND event_id=?",
                        (time.time(), *key),
                    )
                else:
                    row = db.execute(
                        "SELECT attempts FROM alerts WHERE thread_id=? AND event_id=?", key
                    ).fetchone()
                    attempts = (row[0] if row else 0) + 1
                    db.execute(
                        "UPDATE alerts SET attempts=?,next_attempt=? WHERE thread_id=? AND event_id=?",
                        (attempts, time.time() + min(300, 5 * 2 ** min(attempts, 6)), *key),
                    )
            return
        key = (payload["thread-id"], payload["turn-id"])
        with self._store() as db:
            if success:
                db.execute(
                    "UPDATE deliveries SET delivered_at=?, payload='{}' WHERE thread_id=? AND turn_id=?",
                    (time.time(), *key),
                )
            else:
                row = db.execute(
                    "SELECT attempts FROM deliveries WHERE thread_id=? AND turn_id=?", key
                ).fetchone()
                attempts = (row[0] if row else 0) + 1
                db.execute(
                    """UPDATE deliveries SET attempts=?,next_attempt=?
                    WHERE thread_id=? AND turn_id=?""",
                    (attempts, time.time() + min(300, 5 * 2 ** min(attempts, 6)), *key),
                )
