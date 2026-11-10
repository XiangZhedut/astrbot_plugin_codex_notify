import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import File, Plain, Reply
from astrbot_plugin_codex_notify.file_transfer import (
    MAX_BYTES,
    FileTransfer,
    PublicResolver,
    linked_files,
)
from astrbot_plugin_codex_notify.main import CodexNotifyPlugin


class FileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.service = FileTransfer(self.root / "transfer", self.codex)
        self.artifact = self.workspace / "报告 文件.txt"
        self.artifact.write_text("report", encoding="utf-8")
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT,cwd TEXT,thread_source TEXT)")
            db.execute("INSERT INTO threads VALUES ('chat',?,'user')", (str(self.workspace),))
        self.remote = Mock()
        self.remote.find_route.return_value = "chat"
        self.payload = {
            "type": "agent-turn-complete",
            "thread-id": "chat",
            "turn-id": "turn",
            "last-assistant-message": f"[报告](<{self.artifact}>)",
        }

    def test_links_only_no_workspace_scan_or_escape(self):
        (self.workspace / "unlinked.txt").touch()
        outside = self.root / "private.txt"
        outside.touch()
        hidden = self.workspace / ".secret"
        hidden.touch()
        text = (
            self.payload["last-assistant-message"] + f" [x](<{outside}>) [s](<{hidden}>)"
            " [remote](https://example.com/report.txt)"
        )
        self.assertEqual(linked_files(text, self.workspace), [self.artifact])
        self.assertEqual(linked_files("报告已保存", self.workspace), [])

    def test_session_requires_selection_owner_target_and_expiry(self):
        with self.assertRaises(ValueError):
            self.service.session("owner", "target")
        self.service.select("owner", "target", "quoted", self.remote)
        for user, target in [("stranger", "target"), ("owner", "elsewhere")]:
            with self.assertRaises(ValueError):
                self.service.session(user, target)
        self.service.session("owner", "target")["expires"] = 0
        with self.assertRaises(ValueError):
            self.service.session("owner", "target")

    async def test_stage_accumulates_and_submission_keeps_selected_route(self):
        self.service.select("owner", "target", "quoted", self.remote)
        self.service._copy = AsyncMock(side_effect=lambda s, d: d.write_text("input"))
        component = SimpleNamespace(name="data.csv", get_file=AsyncMock(return_value="unused"))
        count = await self.service.stage("owner", "target", "m1", [component])
        self.assertEqual(count, 1)
        with self.assertRaises(ValueError):
            await self.service.stage("owner", "target", "m1", [component])
        quoted, prompt = self.service.prompt("owner", "target", "分析数据")
        self.assertEqual(quoted, "quoted")
        self.assertIn("分析数据", prompt)
        path = self.service.session("owner", "target")["files"][0]
        self.assertIn(str(path), prompt)
        self.service.cancel("owner", "target", delete=False)
        self.assertTrue(path.exists())

    async def test_failed_download_rolls_back_whole_message(self):
        self.service.select("owner", "target", "quoted", self.remote)
        self.service._copy = AsyncMock(side_effect=ValueError("too large"))
        component = SimpleNamespace(name="x", get_file=AsyncMock(return_value="unused"))
        with self.assertRaises(ValueError):
            await self.service.stage("owner", "target", "m", [component])
        self.assertEqual(self.service.session("owner", "target")["files"], [])

    def test_oversized_copy_is_bounded(self):
        path = self.workspace / "large.bin"
        with path.open("wb") as handle:
            handle.truncate(MAX_BYTES + 1)
        destination = self.root / "copy.bin"
        with self.assertRaises(ValueError):
            self.service._bounded_copy(path, destination)
        self.assertLessEqual(destination.stat().st_size, MAX_BYTES)

    async def test_private_remote_addresses_rejected(self):
        for address in ["http://127.0.0.1/file", "http://[::1]/file", "http://10.0.0.1/file"]:
            with self.assertRaises(ValueError):
                await self.service._copy(address, self.root / "download")
        with patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            AsyncMock(return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]),
        ):
            with self.assertRaises(ValueError):
                await PublicResolver().resolve("internal.example", 80)

    async def test_artifacts_snapshotted_and_delivered_once(self):
        self.service.queue("target", self.payload)
        self.artifact.write_text("changed")
        self.service.queue("target", self.payload)
        sent = []

        async def send(target, path, name):
            sent.append(Path(path).read_text())
            return True

        await self.service.drain("other-target", send, AsyncMock())
        self.assertEqual(sent, [])
        await self.service.drain("target", send, AsyncMock())
        await self.service.drain("target", send, AsyncMock())
        self.assertEqual(sent, ["report"])

    async def test_uncertain_send_never_repeats_attachment_after_restart(self):
        self.service.queue("target", self.payload)
        sender = AsyncMock(side_effect=TimeoutError())
        await self.service.drain("target", sender, AsyncMock())
        self.service = FileTransfer(self.root / "transfer", self.codex)
        notice = AsyncMock(return_value=True)
        await self.service.drain("target", sender, notice)
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(notice.await_count, 1)

    def test_noncompletion_and_internal_sessions_do_not_queue(self):
        self.service.queue("target", {**self.payload, "notification-kind": "attention"})
        self.service.queue("target", {**self.payload, "thread-id": "missing"})
        with closing(sqlite3.connect(self.service.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def plugin(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.config = {"enable_codex_file_upload": True}
        plugin._files = self.service
        plugin._current_target = lambda: "target"
        plugin._read_state = lambda: {"binding_owner": "owner"}
        plugin._remote_control = self.remote
        self.remote.dispatch = AsyncMock(return_value="chat")
        return plugin

    @staticmethod
    def event(components, owner="owner", target="target"):
        class Result:
            def __init__(self, text):
                self.text = text

            def stop_event(self):
                return self

        return SimpleNamespace(
            unified_msg_origin=target,
            get_messages=lambda: components,
            get_sender_id=lambda: owner,
            message_obj=SimpleNamespace(message_id="m"),
            plain_result=Result,
        )

    async def test_handler_select_stage_and_submit_without_quoting_file(self):
        plugin = self.plugin()
        quote = Reply(id="notice", message_str="【Codex回复标记：" + "a" * 24 + "】")
        results = [
            r async for r in plugin.codex_file_command(self.event([quote, Plain("/上传Codex")]))
        ]
        self.assertIn("已选择", results[0].text)
        self.service._copy = AsyncMock(side_effect=lambda s, d: d.write_text("input"))
        component = File(name="table.csv", url="https://example.com/file")
        results = [r async for r in plugin.codex_file_command(self.event([component]))]
        self.assertIn("已暂存", results[0].text)
        self.remote.dispatch.assert_not_called()
        results = [
            r async for r in plugin.codex_file_command(self.event([Plain("/发送Codex 分析表格")]))
        ]
        self.assertIn("已向 Codex", results[0].text)
        args = self.remote.dispatch.call_args.kwargs
        self.assertIn("分析表格", args["prompt"])
        self.assertIn("【Codex回复标记：", args["quoted"])
        self.assertTrue(args["file_upload"])
        self.assertEqual(self.service.sessions, {})

    async def test_handler_rejects_stranger_and_disabled_upload(self):
        plugin = self.plugin()
        results = [
            r
            async for r in plugin.codex_file_command(
                self.event([Plain("/上传Codex")], owner="stranger")
            )
        ]
        self.assertIn("仅允许", results[0].text)
        plugin.config["enable_codex_file_upload"] = False
        results = [r async for r in plugin.codex_file_command(self.event([Plain("/上传Codex")]))]
        self.assertIn("未开启", results[0].text)
        self.remote.find_route.assert_not_called()

    async def test_handler_uncertain_submit_preserves_files_but_clears_session(self):
        plugin = self.plugin()
        self.service.select("owner", "target", "quoted", self.remote)
        uploaded = self.root / "upload.txt"
        uploaded.write_text("input")
        self.service.session("owner", "target")["files"].append(uploaded)
        self.remote.dispatch.side_effect = TimeoutError()
        results = [
            r async for r in plugin.codex_file_command(self.event([Plain("/发送Codex 分析")]))
        ]
        self.assertIn("未确认", results[0].text)
        self.assertEqual(self.service.sessions, {})
        self.assertTrue(uploaded.exists())
