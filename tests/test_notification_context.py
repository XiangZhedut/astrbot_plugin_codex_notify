import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_codex_notify.main import CodexNotifyPlugin
from astrbot_plugin_codex_notify.notification_context import (
    context_record,
    enrich_legacy,
    turn_details,
)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "turn.jsonl"
        self.result = "完整结果" * 1000
        events = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
            {
                "type": "response_item",
                "payload": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "对应用户指令"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>metadata</environment_context>",
                        }
                    ],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "last_agent_message": self.result,
                },
            },
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}},
            {
                "type": "response_item",
                "payload": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "其他轮次隐私"}],
                },
            },
        ]
        self.journal.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads(id TEXT,thread_source TEXT,rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES ('chat','user',?)", (str(self.journal),))
            db.execute("INSERT INTO threads VALUES ('internal','subagent',?)", (str(self.journal),))

    def test_exact_user_turn_and_full_result(self):
        details = turn_details("chat", "t1", self.root)
        self.assertEqual(details["user_instruction"], "对应用户指令")
        self.assertEqual(details["full_result"], self.result)
        self.assertEqual(turn_details("internal", "t1", self.root), {})
        record = context_record(
            "target",
            "短摘要",
            {
                "thread-id": "chat",
                "turn-id": "t1",
                "type": "agent-turn-complete",
                "last-assistant-message": self.result,
            },
            self.root,
        )
        self.assertEqual(record["full_result"], self.result)
        self.assertEqual(record["user_instruction"], "对应用户指令")

    def test_legacy_matches_only_existing_notification(self):
        path = self.root / "monitor.sqlite"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE deliveries(thread_id TEXT,turn_id TEXT,delivered_at REAL)")
            db.execute("INSERT INTO deliveries VALUES ('chat','t1',1000)")
        record = enrich_legacy({"target": "here", "time": 1000, "text": "摘要"}, path, self.root)
        self.assertEqual(record["full_result"], self.result)
        unchanged = {"target": "here", "time": 2000, "text": "摘要"}
        self.assertEqual(enrich_legacy(unchanged, path, self.root), unchanged)

    async def test_full_result_injection_is_scoped_and_idempotent(self):
        plugin = CodexNotifyPlugin.__new__(CodexNotifyPlugin)
        plugin.config = {"codex_notifications_in_context": True}
        record = context_record(
            "here",
            "摘要",
            {
                "thread-id": "chat",
                "turn-id": "t1",
                "type": "agent-turn-complete",
                "last-assistant-message": self.result,
            },
            self.root,
        )
        plugin._read_state = lambda: {
            "context_notifications": [
                record,
                {
                    **record,
                    "target": "elsewhere",
                    "full_result": "别的会话内容",
                },
            ]
        }
        request = SimpleNamespace(system_prompt="已由其他插件重建的提示词")
        event = SimpleNamespace(unified_msg_origin="here")
        await plugin.codex_notification_context(event, request)
        await plugin.codex_notification_context(event, request)
        self.assertIn(self.result, request.system_prompt)
        self.assertIn("对应用户指令", request.system_prompt)
        self.assertNotIn("别的会话内容", request.system_prompt)
        self.assertEqual(request.system_prompt.count("<Codex通知记录>"), 1)
