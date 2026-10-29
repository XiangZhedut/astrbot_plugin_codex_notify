import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_codex_notify.main import (
    CodexNotifyPlugin,
    _extract_verification_code,
    _task_link,
    format_current_quota,
    format_notification,
)


class FakeConfig(dict):
    async def save_config_async(self):
        return None


class FakeEvent:
    def __init__(self, umo: str, sender_id: str):
        self.unified_msg_origin = umo
        self._sender_id = sender_id

    def get_sender_id(self):
        return self._sender_id


class NotificationHelperTests(unittest.TestCase):
    def test_quota_footer_remaining_unknown_expired_and_time(self):
        # 2026-10-29 13:00 Beijing.
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 10, 29, 13, tzinfo=timezone(timedelta(hours=8))).timestamp()
        windows = [
            {
                "limit_name": "codex",
                "window_minutes": 300,
                "reset_at": now + 3480,
                "used_percent": 28,
            },
            {
                "limit_name": "codex",
                "window_minutes": 10080,
                "reset_at": now + 6 * 86400,
                "used_percent": 34,
            },
        ]
        text = format_current_quota(windows, now)
        self.assertIn("72%/5小时 13:58", text)
        self.assertIn("66%/周 11-04 13:00", text)
        self.assertIn("最近记录", text)
        windows[0]["reset_at"] = now - 1
        windows[1]["used_percent"] = None
        text = format_current_quota(windows, now)
        self.assertIn("待刷新/5小时", text)
        self.assertIn("未知/周", text)
        self.assertNotIn("72%", text)
        self.assertEqual(format_current_quota([], now), "当前额度：暂无可用记录")

    def test_quota_notification_is_account_level_and_honest_about_prediction(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.config = {}
        text = plugin._notification_text(
            {
                "notification-kind": "quota_reset",
                "thread-id": "__codex_account__",
                "last-assistant-message": "预计重置时间已到；以 Codex 面板为准。",
            }
        )
        self.assertIn("额度重置提醒", text)
        self.assertIn("预计", text)
        self.assertNotIn("codex://", text)
        self.assertNotIn("任务已完成", text)

    def test_attention_message_never_uses_success_heading(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.config = {"include_task_link": True, "summary_max_chars": 500}
        for kind, heading in [
            ("failed", "运行受阻"),
            ("paused", "运行已中断"),
            ("input_required", "等待你回复"),
            ("approval_required", "等待你确认"),
        ]:
            with self.subTest(kind=kind):
                text = plugin._notification_text(
                    {
                        "notification-kind": kind,
                        "task-name": "小论文",
                        "thread-id": "thread-1",
                        "last-assistant-message": "Selected model is at capacity. Please try a different model.",
                    }
                )
                self.assertIn(heading, text)
                self.assertIn("任务名：小论文", text)
                self.assertNotIn("任务已完成", text)
                if kind == "failed":
                    self.assertIn("切换模型", text)

    def test_extract_verification_code(self):
        self.assertEqual(_extract_verification_code("1234"), "1234")
        self.assertEqual(_extract_verification_code("验证码 1234"), "1234")
        self.assertEqual(_extract_verification_code("验证码：1234"), "1234")
        self.assertEqual(_extract_verification_code("订单 1234"), "")

    def test_task_link(self):
        self.assertEqual(
            _task_link({"thread-id": "thread 1"}),
            "codex://threads/thread%201",
        )

    def test_each_message_part_can_be_selected(self):
        payload = {
            "thread-id": "thread-1",
            "input-messages": ["测试任务"],
            "last-assistant-message": "测试结果",
        }
        text = format_notification(
            payload,
            prefix="完成",
            include_completion_notice=False,
            include_task_name=True,
            include_task_link=True,
            include_result=True,
            summary_max_chars=500,
        )
        self.assertNotIn("完成", text)
        self.assertIn("任务名：测试任务", text)
        self.assertIn("任务地址：codex://threads/thread-1", text)
        self.assertIn("结果：测试结果", text)

    def test_real_chat_title_takes_priority_over_prompt(self):
        text = format_notification(
            {"task-name": "小论文", "input-messages": ["系统上下文加用户问题"]},
            prefix="完成",
            include_completion_notice=False,
            include_task_name=True,
            include_task_link=False,
            include_result=False,
            summary_max_chars=500,
        )
        self.assertEqual(text, "任务名：小论文")


class CompletionQueueRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_setting_controls_all_outgoing_notices(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.config = {"include_current_quota": True}
        plugin._read_state = Mock(return_value={})
        plugin._write_state = Mock()
        plugin._completion_monitor = Mock()
        plugin._completion_monitor.quota_status.return_value = []
        plugin._prepare_qq_group_proactive_send = Mock()
        plugin.context = Mock()
        plugin.context.send_message = AsyncMock(return_value=True)
        self.assertTrue(await plugin._send("test-target", "运行受阻"))
        chain = plugin.context.send_message.call_args.args[1]
        self.assertIn("当前额度：暂无可用记录", chain.chain[0].text)
        plugin.config["include_current_quota"] = False
        await plugin._send("test-target", "运行已完成")
        self.assertEqual(plugin.context.send_message.call_args.args[1].chain[0].text, "运行已完成")
        self.assertEqual(plugin._completion_monitor.quota_status.call_count, 1)

    async def test_context_is_scoped_and_can_be_disabled(self):
        import time
        from types import SimpleNamespace

        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.config = {"codex_notifications_in_context": True}
        plugin._read_state = Mock(
            return_value={
                "context_notifications": [
                    {"target": "here", "time": time.time(), "text": "已发送的通知"},
                    {"target": "elsewhere", "time": time.time(), "text": "其他会话私密内容"},
                ]
            }
        )
        request = SimpleNamespace(system_prompt="原提示")
        await plugin.codex_notification_context(SimpleNamespace(unified_msg_origin="here"), request)
        self.assertIn("已发送的通知", request.system_prompt)
        self.assertNotIn("其他会话私密内容", request.system_prompt)
        plugin.config["codex_notifications_in_context"] = False
        request.system_prompt = "原提示"
        await plugin.codex_notification_context(SimpleNamespace(unified_msg_origin="here"), request)
        self.assertEqual(request.system_prompt, "原提示")

    async def test_hook_is_queued_without_immediate_platform_send(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.token = "test-token"
        plugin._completion_monitor = Mock()
        plugin._completion_monitor.hint.return_value = "queued"
        plugin._send = AsyncMock()
        request = Mock()
        request.headers = {"X-Codex-Notify-Token": "test-token"}
        request.json = AsyncMock(return_value={"type": "agent-turn-complete", "thread-id": "user"})
        response = await plugin._notify(request)
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(response.text)["status"], "queued")
        plugin._send.assert_not_awaited()

    async def test_unauthorized_hook_does_not_enter_queue(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.token = "test-token"
        plugin._completion_monitor = Mock()
        request = Mock()
        request.headers = {}
        response = await plugin._notify(request)
        self.assertEqual(response.status, 401)
        plugin._completion_monitor.hint.assert_not_called()


class BindingChallengeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        self.plugin.config = FakeConfig(
            verification_timeout_seconds=300,
            verification_max_attempts=3,
            bind_verification_code="----",
        )
        self.plugin._binding_lock = asyncio.Lock()
        self.plugin._pending_binding = None
        self.plugin._binding_expiry_task = None
        self.plugin._save_persisted_target = lambda target: None
        self.plugin._remember_binding_owner = Mock()

    async def asyncTearDown(self):
        task = self.plugin._binding_expiry_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_code_is_bound_to_initiator_and_session(self):
        owner = FakeEvent("platform:GroupMessage:group-1", "user-1")
        other_user = FakeEvent("platform:GroupMessage:group-1", "user-2")
        other_session = FakeEvent("platform:GroupMessage:group-2", "user-1")

        await self.plugin._start_binding(owner)
        code = self.plugin.config["bind_verification_code"]
        self.assertRegex(code, r"^\d{4}$")
        self.assertIsNone(await self.plugin._verify_binding(other_user, code))
        self.assertIsNone(await self.plugin._verify_binding(other_session, code))

        success, _ = await self.plugin._verify_binding(owner, code)
        self.assertTrue(success)
        self.assertEqual(
            self.plugin.config["target_umo"],
            "platform:GroupMessage:group-1",
        )
        self.assertEqual(self.plugin.config["bind_verification_code"], "----")


if __name__ == "__main__":
    unittest.main()
