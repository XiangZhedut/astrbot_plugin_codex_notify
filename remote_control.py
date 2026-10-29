"""Authenticated QQ requests routed through the existing Codex desktop host."""

import asyncio
import json
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

MARKER = re.compile(r"【Codex回复标记：([0-9a-f]{24})】")


def unpack(result):
    for item in result.get("contentItems", []):
        if item.get("type") == "inputText":
            try:
                value = json.loads(item["text"])
            except (ValueError, KeyError):
                continue
            if isinstance(value, dict):
                return value
    raise RuntimeError("Codex 接口返回内容无法识别，请在桌面检查；不会自动重发。")


class RemoteControl:
    def __init__(self, path: Path, client, config):
        self.path, self.client, self.config = path, client, config
        self.lock = asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS routes(
                token TEXT PRIMARY KEY,target TEXT,thread_id TEXT,sent INTEGER DEFAULT 0,created REAL);
                CREATE TABLE IF NOT EXISTS receipts(
                key TEXT PRIMARY KEY,status TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS cooldown(user TEXT PRIMARY KEY,last_sent REAL);""")

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            with db:
                yield db

    def issue_route(self, target, thread):
        token = secrets.token_hex(12)
        with self.db() as db:
            db.execute(
                "INSERT INTO routes VALUES (?,?,?,0,?)", (token, target, thread, time.time())
            )
        return token

    def mark_sent(self, token):
        with self.db() as db:
            db.execute("UPDATE routes SET sent=1 WHERE token=?", (token,))

    def find_route(self, target, quoted):
        tokens = set(MARKER.findall(quoted))
        if len(tokens) != 1:
            raise ValueError("请引用一条带 Codex回复标记的通知；无法确认唯一目标。")
        with self.db() as db:
            row = db.execute(
                "SELECT thread_id FROM routes WHERE token=? AND target=? AND sent=1",
                (next(iter(tokens)), target),
            ).fetchone()
        if not row:
            raise ValueError("该引用不是当前绑定会话中已登记的通知，已拒绝发送。")
        return row[0]

    async def dispatch(
        self, *, user, target, message_id, prompt, quoted="", create=False, passphrase=""
    ):
        if not message_id:
            raise ValueError("平台未提供消息 ID，无法防止重复发送，已拒绝。")
        if not prompt.strip():
            raise ValueError("请输入要发给 Codex 的指令。")
        if len(prompt) > 20000:
            raise ValueError("指令过长，请限制在20000字以内。")
        async with self.lock:
            if create:
                if not self.config.get("enable_codex_create", False):
                    raise ValueError("Codex 新建聊天功能未开启。")
                expected = str(self.config.get("codex_create_passphrase") or "")
                if not expected or not secrets.compare_digest(
                    expected.encode("utf-8"), passphrase.encode("utf-8")
                ):
                    raise ValueError("新建口令未设置或不正确。")
            elif not self.config.get("enable_codex_reply", False):
                raise ValueError("Codex 回复功能未开启。")
            thread = None if create else self.find_route(target, quoted)
            receipt = json.dumps([target, str(message_id)], separators=(",", ":"))
            now = time.time()
            interval = max(1, min(3600, int(self.config.get("codex_command_interval_seconds", 10))))
            with self.db() as db:
                if db.execute("SELECT 1 FROM receipts WHERE key=?", (receipt,)).fetchone():
                    raise ValueError("这条消息已经处理过或发送结果待确认，不会重复提交。")
                last = db.execute("SELECT last_sent FROM cooldown WHERE user=?", (user,)).fetchone()
                if last and now - last[0] < interval:
                    raise ValueError(
                        f"发送过于频繁，请在 {max(1, int(interval - (now - last[0]) + 1))} 秒后重试。"
                    )
            if not create:
                snapshot = unpack(
                    await asyncio.to_thread(
                        self.client.call,
                        "read_thread",
                        {
                            "threadId": thread,
                            "hostId": "local",
                            "turnLimit": 1,
                            "maxOutputCharsPerItem": 1,
                        },
                    )
                )
                if snapshot.get("thread", {}).get("id") != thread:
                    raise ValueError("目标聊天状态无法确认，已拒绝发送。")
                status = snapshot["thread"].get("status", {}).get("type")
                if status == "active" and not self.config.get("allow_codex_running_reply", False):
                    raise ValueError(
                        "该聊天正在运行；请完成后发送，或在设置中允许向运行中的聊天发送。"
                    )
                if status not in {"idle", "notLoaded", "active"}:
                    raise ValueError("目标聊天当前不可接收指令。")
            # Reserve before IPC: a timeout may mean the desktop accepted it.
            # Never blindly retry mutations after an uncertain response.
            with self.db() as db:
                db.execute("INSERT INTO receipts VALUES (?,'pending',?)", (receipt, now))
                db.execute("INSERT OR REPLACE INTO cooldown VALUES (?,?)", (user, now))
            if create:
                result = unpack(
                    await asyncio.to_thread(
                        self.client.call,
                        "create_thread",
                        {"prompt": prompt, "target": {"type": "projectless"}},
                        allow_write=True,
                    )
                )
                thread = result.get("threadId")
                if not thread:
                    raise RuntimeError(
                        "新建请求已提交但未获得就绪聊天 ID，请在桌面确认，不要重复发送。"
                    )
            else:
                await asyncio.to_thread(
                    self.client.call,
                    "send_message_to_thread",
                    {"threadId": thread, "hostId": "local", "prompt": prompt},
                    allow_write=True,
                )
            with self.db() as db:
                db.execute("UPDATE receipts SET status='accepted' WHERE key=?", (receipt,))
            return thread
