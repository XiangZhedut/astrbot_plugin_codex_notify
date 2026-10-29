import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_codex_notify.main import CodexNotifyPlugin
from astrbot_plugin_codex_notify.remote_control import RemoteControl


def reply(value):
    return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(value)}]}


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {
            "enable_codex_reply": True,
            "enable_codex_create": True,
            "codex_create_passphrase": "secret",
            "codex_command_interval_seconds": 10,
        }
        self.client = Mock()
        self.client.call.side_effect = self.call
        self.status = "idle"
        self.service = RemoteControl(
            Path(self.temp.name) / "remote.sqlite", self.client, self.config
        )
        self.token = self.service.issue_route("target", "thread-a")
        self.service.mark_sent(self.token)
        self.args = {
            "user": "owner",
            "target": "target",
            "message_id": "m1",
            "prompt": "继续",
            "quoted": "【Codex回复标记：" + self.token + "】",
        }

    def call(self, tool, args, **kwargs):
        if tool == "read_thread":
            return reply({"thread": {"id": "thread-a", "status": {"type": self.status}}})
        if tool == "create_thread":
            return reply({"threadId": "thread-new", "hostId": "local"})
        return {"success": True, "contentItems": []}

    async def test_correct_target_and_duplicate_are_not_resent(self):
        self.assertEqual(await self.service.dispatch(**self.args), "thread-a")
        writes = [
            c for c in self.client.call.call_args_list if c.args[0] == "send_message_to_thread"
        ]
        self.assertEqual(writes[0].args[1]["threadId"], "thread-a")
        with self.assertRaises(ValueError):
            await self.service.dispatch(**self.args)
        self.assertEqual(
            len(
                [
                    c
                    for c in self.client.call.call_args_list
                    if c.args[0] == "send_message_to_thread"
                ]
            ),
            1,
        )

    async def test_create_and_reply_share_cooldown(self):
        await self.service.dispatch(**self.args)
        args = {**self.args, "message_id": "m2", "create": True, "passphrase": "secret"}
        with self.assertRaisesRegex(ValueError, "频繁"):
            await self.service.dispatch(**args)
        self.assertFalse(any(c.args[0] == "create_thread" for c in self.client.call.call_args_list))

    async def test_active_requires_switch_and_does_not_interrupt(self):
        self.status = "active"
        with self.assertRaisesRegex(ValueError, "正在运行"):
            await self.service.dispatch(**self.args)
        self.config["allow_codex_running_reply"] = True
        await self.service.dispatch(**self.args)
        self.assertEqual(self.client.call.call_args.args[0], "send_message_to_thread")

    async def test_route_unknown_cross_session_and_unsent_fail_closed(self):
        for args in [
            {**self.args, "target": "other"},
            {**self.args, "quoted": "thread-a"},
            {**self.args, "quoted": "【Codex回复标记：" + "0" * 24 + "】"},
        ]:
            with self.assertRaises(ValueError):
                await self.service.dispatch(**args)
        token = self.service.issue_route("target", "thread-b")
        with self.assertRaises(ValueError):
            self.service.find_route("target", "【Codex回复标记：" + token + "】")
        self.client.call.assert_not_called()

    async def test_create_requires_switch_and_passphrase(self):
        args = {**self.args, "create": True, "passphrase": "bad"}
        with self.assertRaises(ValueError):
            await self.service.dispatch(**args)
        self.config["enable_codex_create"] = False
        with self.assertRaises(ValueError):
            await self.service.dispatch(**{**args, "passphrase": "secret"})
        self.config["enable_codex_create"] = True
        self.assertEqual(
            await self.service.dispatch(**{**args, "passphrase": "secret"}), "thread-new"
        )

    async def test_unicode_create_passphrase(self):
        self.config["codex_create_passphrase"] = "星星口令"
        args = {**self.args, "create": True, "passphrase": "星星口令"}
        self.assertEqual(await self.service.dispatch(**args), "thread-new")

    async def test_uncertain_write_is_not_automatically_retried_after_restart(self):
        def timeout(tool, args, **kw):
            if tool == "send_message_to_thread":
                raise TimeoutError()
            return self.call(tool, args, **kw)

        self.client.call.side_effect = timeout
        with self.assertRaises(TimeoutError):
            await self.service.dispatch(**self.args)
        self.service = RemoteControl(self.service.path, self.client, self.config)
        with self.assertRaisesRegex(ValueError, "已经处理过"):
            await self.service.dispatch(**self.args)

    def test_binding_owner_and_session_both_required(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin._current_target = lambda: "target"
        plugin._read_state = lambda: {"binding_owner": "owner"}
        self.assertTrue(
            plugin._remote_user_allowed(
                SimpleNamespace(unified_msg_origin="target", get_sender_id=lambda: "owner")
            )
        )
        self.assertFalse(
            plugin._remote_user_allowed(
                SimpleNamespace(unified_msg_origin="target", get_sender_id=lambda: "stranger")
            )
        )
        self.assertFalse(
            plugin._remote_user_allowed(
                SimpleNamespace(unified_msg_origin="other", get_sender_id=lambda: "owner")
            )
        )
