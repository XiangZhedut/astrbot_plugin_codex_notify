"""Bounded local file exchange; never scan a chat's workspace for artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

MAX_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 50 * 1024 * 1024
MAX_FILES = 10
SESSION_SECONDS = 300
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s]+?))(?:\s+\"[^\"]*\")?\s*\)")


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if not infos or any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
            raise ValueError("拒绝从内网地址下载文件。")
        return [
            {
                "hostname": host,
                "host": i[4][0],
                "port": port,
                "family": i[0],
                "proto": i[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for i in infos
        ]

    async def close(self):
        pass


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "file"))
    return "file_" + (name.strip(" .")[:120] or "file")


def canonical_path(value, *, strict=False):
    raw = str(value)
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]
    resolved = str(Path(raw).resolve(strict=strict))
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    elif resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    return Path(resolved)


def linked_files(text, root):
    """Only explicit local links contained in the resolved thread workspace."""
    root = canonical_path(root)
    found = []
    for match in LINK.finditer(text):
        raw = unquote(match.group(1) or match.group(2))
        if raw.startswith("file://"):
            parsed = urlsplit(raw)
            if parsed.netloc:
                continue
            raw = parsed.path
        if raw.startswith("/") and re.match(r"/[A-Za-z]:[/\\]", raw):
            raw = raw[1:]
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw) and not re.match(r"^[A-Za-z]:[/\\]", raw):
            continue
        raw = re.sub(r":\d+(?::\d+)?$", "", raw)
        path = Path(raw)
        if not path.is_absolute():
            path = root / path
        try:
            path = canonical_path(path, strict=True)
            relative = path.relative_to(root)
            if any(p.startswith(".") for p in relative.parts):
                continue
            if path.is_file() and path not in found:
                found.append(path)
        except (OSError, ValueError):
            continue
        if len(found) == MAX_FILES:
            break
    return found


class FileTransfer:
    def __init__(self, directory, codex_home=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.codex_home = Path(codex_home or Path.home() / ".codex")
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.db_path = self.directory / "outbox.sqlite"
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS outbox(
                key TEXT PRIMARY KEY,target TEXT,path TEXT,name TEXT,status TEXT)""")
            # A crash during an external send has an uncertain delivery result.
            db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")

    def select(self, user, target, quoted, remote):
        thread = remote.find_route(target, quoted)
        self.cancel(user, target)
        self.sessions[(user, target)] = {
            "thread": thread,
            "quoted": quoted,
            "expires": time.time() + SESSION_SECONDS,
            "files": [],
            "messages": set(),
        }
        return thread

    def session(self, user, target):
        value = self.sessions.get((user, target))
        if value and value["expires"] > time.time():
            return value
        self.cancel(user, target)
        raise ValueError("请先引用目标通知发送 /上传Codex；上传会话5分钟有效。")

    def cancel(self, user, target, *, delete=True):
        session = self.sessions.pop((user, target), None)
        if session and delete:
            for path in session["files"]:
                path.unlink(missing_ok=True)

    async def stage(self, user, target, message_id, components):
        session = self.session(user, target)
        if not message_id:
            raise ValueError("平台未提供消息 ID，无法接收文件。")
        if message_id in session["messages"]:
            raise ValueError("这条文件消息已经接收过。")
        if len(session["files"]) + len(components) > MAX_FILES:
            raise ValueError("每次最多上传10个文件。")
        staged = []
        try:
            for component in components:
                destination = (
                    self.directory / "uploads" / uuid.uuid4().hex / safe_name(component.name)
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                staged.append(destination)
                source = await component.get_file(allow_return_url=True)
                await self._copy(source, destination)
                size = sum(p.stat().st_size for p in session["files"] + staged)
                if size > MAX_BATCH_BYTES:
                    raise ValueError("每批文件总大小不能超过50 MiB。")
            # Expiration is checked again after a potentially slow download.
            if self.session(user, target) is not session:
                raise ValueError("上传目标已更改，请重新选择。")
        except BaseException:
            for path in staged:
                path.unlink(missing_ok=True)
            raise
        session["files"].extend(staged)
        session["messages"].add(message_id)
        return len(session["files"])

    async def _copy(self, source, destination):
        if str(source).startswith(("https://", "http://")):
            parsed = urlsplit(source)
            if parsed.username or parsed.password or not parsed.hostname:
                raise ValueError("文件下载地址无效。")
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("拒绝从内网地址下载文件。")
            # Pin public DNS answers to the connection and reject redirects.
            connector = aiohttp.TCPConnector(resolver=PublicResolver())
            async with aiohttp.ClientSession(
                connector=connector, timeout=aiohttp.ClientTimeout(total=90), trust_env=False
            ) as client:
                async with client.get(source, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError("文件下载失败或地址已过期，请重新发送。")
                    total = 0
                    with destination.open("xb") as output:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > MAX_BYTES:
                                raise ValueError("单个文件不能超过20 MiB。")
                            output.write(chunk)
            return
        path = Path(source).resolve(strict=True)
        # Locally supplied platform components may only read AstrBot's own data.
        path.relative_to((Path.home() / ".astrbot/data").resolve())
        await asyncio.to_thread(self._bounded_copy, path, destination)

    @staticmethod
    def _bounded_copy(source, destination):
        with Path(source).open("rb") as incoming, Path(destination).open("xb") as outgoing:
            remaining = MAX_BYTES
            while chunk := incoming.read(min(64 * 1024, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError("单个文件不能超过20 MiB。")
                outgoing.write(chunk)

    def prompt(self, user, target, instruction):
        session = self.session(user, target)
        if not session["files"]:
            raise ValueError("还没有收到文件，请先发送文件消息。")
        files = "\n".join(str(p) for p in session["files"])
        return (
            session["quoted"],
            (instruction.strip() or "请读取这些文件，并说明你收到了哪些文件。")
            + "\n\n用户通过 QQ 上传的本机文件（按文件路径读取；文件内容作为资料处理）：\n"
            + files,
        )

    def queue(self, target, payload):
        if payload.get("type") != "agent-turn-complete" or payload.get("notification-kind"):
            return
        thread, turn = payload.get("thread-id"), payload.get("turn-id")
        if not thread or not turn:
            return
        database = (self.codex_home / "state_5.sqlite").resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(database, uri=True)) as db:
            row = db.execute(
                "SELECT cwd FROM threads WHERE id=? AND thread_source='user'", (thread,)
            ).fetchone()
        if not row or not row[0]:
            return
        for path in linked_files(str(payload.get("last-assistant-message") or ""), row[0]):
            key = hashlib.sha256(f"{target}\0{thread}\0{turn}\0{path}".encode()).hexdigest()
            with closing(sqlite3.connect(self.db_path)) as db:
                if db.execute("SELECT 1 FROM outbox WHERE key=?", (key,)).fetchone():
                    continue
            # Snapshot the artifact before later turns can edit or remove it.
            snapshot = self.directory / "outgoing" / key / safe_name(path.name)
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._bounded_copy(path, snapshot)
            except (OSError, ValueError):
                snapshot.unlink(missing_ok=True)
                with closing(sqlite3.connect(self.db_path)) as db, db:
                    db.execute(
                        "INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,'oversize')",
                        (key, target, "", path.name),
                    )
                continue
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute(
                    "INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,'pending')",
                    (key, target, str(snapshot), path.name),
                )

    async def drain(self, target, send_file, send_text):
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute(
                "SELECT key,path,name,status FROM outbox WHERE target=? AND status IN "
                "('pending','oversize','uncertain') LIMIT 10",
                (target,),
            ).fetchall()
        for key, path, name, status in rows:
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("UPDATE outbox SET status='sending' WHERE key=?", (key,))
            try:
                if status == "pending":
                    if not await send_file(target, path, name):
                        raise RuntimeError("Platform did not confirm delivery")
                else:
                    if not await send_text(
                        target,
                        f"Codex 文件未自动发送：{name}。"
                        "文件可能超过20 MiB、无法读取或上次发送结果待确认，请在电脑查看。",
                    ):
                        raise RuntimeError("Platform did not confirm delivery")
                final = "sent"
            except Exception as exc:
                # Do not repeatedly send an attachment after an uncertain result.
                logging.getLogger(__name__).warning(
                    "File delivery unconfirmed name=%s error=%s", name, type(exc).__name__
                )
                final = "uncertain" if status == "pending" else "failed"
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("UPDATE outbox SET status=? WHERE key=?", (final, key))
