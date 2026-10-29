import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "completion_monitor", Path(__file__).resolve().parents[1] / "completion_monitor.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CompletionMonitor = MODULE.CompletionMonitor


class CompletionMonitorTests(unittest.TestCase):
    def test_malformed_metadata_does_not_block_other_completions(self):
        self.append("user-a", {"type": "event_msg", "payload": []})
        self.append(
            "user-a",
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "internal_chat_message_metadata_passthrough": "invalid",
                },
            },
        )
        self.append("user-a", self.completion())
        self.monitor.scan()
        self.now.return_value = 1010
        self.assertEqual(len(self.monitor.ready()), 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = patch.object(MODULE.time, "time", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        with self.database() as db:
            db.execute(
                "CREATE TABLE threads(id TEXT,rollout_path TEXT,title TEXT,name TEXT,thread_source TEXT,source TEXT)"
            )
        self.add_thread("user-a")
        self.add_thread("user-b")
        self.add_thread("child", source="subagent")
        self.monitor = self.make_monitor()

    def add_thread(self, tid, source="user"):
        path = self.root / (tid + ".jsonl")
        path.write_text("", encoding="utf-8")
        with self.database() as db:
            db.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?,?)",
                (tid, str(path), "聊天 " + tid, None, source, "vscode"),
            )

    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as db:
            with db:
                yield db

    def make_monitor(self):
        return CompletionMonitor(self.root, self.root / "monitor.sqlite", grace_seconds=10)

    def append(self, tid, event, partial=False):
        with (self.root / (tid + ".jsonl")).open("ab") as handle:
            handle.write(
                json.dumps(event, ensure_ascii=False).encode("utf-8") + (b"" if partial else b"\n")
            )

    def completion(self, turn="turn-1", result="最终结果", error=None):
        return {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn,
                "completed_at": self.now.return_value,
                "last_agent_message": result,
                "error": error,
            },
        }

    def hint(self, tid="user-a", turn="turn-1"):
        return {
            "type": "agent-turn-complete",
            "thread-id": tid,
            "turn-id": turn,
            "last-assistant-message": "不能相信这个较早的 hook 正文",
        }

    def test_two_chats_no_hook_long_reply_and_grace(self):
        self.append("user-a", self.completion(result="长回复" * 20000))
        self.append("user-b", self.completion())
        self.monitor.scan()
        self.assertEqual(self.monitor.ready(), [])
        self.now.return_value = 1009
        self.assertEqual(self.monitor.ready(), [])
        self.now.return_value = 1010
        ready = self.monitor.ready()
        self.assertEqual({p["thread-id"] for p in ready}, {"user-a", "user-b"})
        self.assertEqual(ready[0]["last-assistant-message"], "长回复" * 20000)
        self.assertEqual(ready[0]["task-name"], "聊天 user-a")

    def test_internal_subagent_and_commentary_never_send(self):
        self.assertEqual(self.monitor.hint(self.hint("title-generator")), "ignored_internal")
        self.assertEqual(self.monitor.hint(self.hint("suggestions")), "ignored_internal")
        self.assertEqual(self.monitor.hint(self.hint("child")), "ignored_internal")
        self.monitor.hint(self.hint())
        self.append("child", self.completion())
        self.append(
            "user-a",
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [],
                },
            },
        )
        self.monitor.scan()
        self.now.return_value = 2000
        self.assertEqual(self.monitor.ready(), [])

    def test_hook_scan_duplicate_restart_and_retry(self):
        self.append("user-a", self.completion())
        self.monitor.hint(self.hint())
        self.monitor.scan()
        self.now.return_value = 1010
        ready = self.monitor.ready()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["last-assistant-message"], "最终结果")
        self.monitor.finish(ready[0], False)
        self.assertEqual(self.make_monitor().ready(), [])
        self.now.return_value = 1020
        self.monitor = self.make_monitor()
        self.assertEqual(len(self.monitor.ready()), 1)
        self.monitor.finish(ready[0], True)
        self.assertEqual(self.monitor.hint(self.hint()), "duplicate")
        self.monitor.scan()
        self.assertEqual(self.make_monitor().ready(), [])

    def test_new_thread_partial_record_and_error(self):
        self.add_thread("new")
        self.append("new", self.completion(), partial=True)
        self.append("user-a", self.completion(error={"message": "usage limit"}))
        self.append("user-b", self.completion(result=None))
        self.monitor.scan()
        self.now.return_value = 1010
        failed = self.monitor.ready()
        self.assertEqual([p["notification-kind"] for p in failed], ["failed"])
        self.monitor.finish(failed[0], True)
        with (self.root / "new.jsonl").open("ab") as handle:
            handle.write(b"\n")
        self.monitor.scan()
        self.now.return_value = 1020
        self.assertEqual([p["thread-id"] for p in self.monitor.ready()], ["new"])

    def test_first_start_skips_history_restart_catches_downtime(self):
        self.append("user-a", self.completion())
        fresh = CompletionMonitor(self.root, self.root / "fresh.sqlite", 10)
        fresh.scan()
        self.now.return_value = 2000
        self.assertEqual(fresh.ready(), [])
        self.append("user-a", self.completion(turn="after-restart"))
        restarted = CompletionMonitor(self.root, self.root / "fresh.sqlite", 10)
        restarted.scan()
        self.now.return_value = 2010
        self.assertEqual([p["turn-id"] for p in restarted.ready()], ["after-restart"])

    def request(self, call="call_question", async_question=True, name=None):
        return {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": call,
                "name": name
                or ("request_user_input_async" if async_question else "request_user_input"),
                "arguments": json.dumps({"questions": [{"title": "选择哪个模型？"}]}),
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
            },
        }

    def output(self, value, call="call_question"):
        return {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call,
                "output": json.dumps(value),
            },
        }

    def test_capacity_and_other_failures_and_pause_are_separate_from_success(self):
        self.append(
            "user-a",
            self.completion(
                error={"message": "Selected model is at capacity. Please try a different model."},
                result=None,
            ),
        )
        self.append(
            "user-b",
            {
                "type": "event_msg",
                "payload": {
                    "type": "turn_aborted",
                    "turn_id": "turn-1",
                    "completed_at": 1000,
                    "reason": "interrupted",
                },
            },
        )
        self.monitor.scan()
        self.now.return_value = 1002
        alerts = self.monitor.ready()
        self.assertEqual({p["notification-kind"] for p in alerts}, {"failed", "paused"})
        self.assertIn("at capacity", alerts[0]["last-assistant-message"])
        for payload in alerts:
            self.monitor.finish(payload, True)
        self.assertEqual(self.make_monitor().ready(), [])

    def test_async_ack_is_not_answer_and_no_duplicate_success_notice(self):
        self.append("user-a", self.request())
        self.append("user-a", self.output({"accepted": True}))
        self.append("user-a", self.completion(result="等你选择后继续。"))
        self.monitor.scan()
        self.now.return_value = 1010
        ready = self.monitor.ready()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["notification-kind"], "input_required")
        self.monitor.finish(ready[0], True)
        self.assertEqual(self.make_monitor().ready(), [])

    def test_answer_before_notification_cancels_waiting(self):
        self.append("user-a", self.request(async_question=False))
        self.append("user-a", self.output({"answers": {"q1": {"answers": ["a"]}}}))
        self.monitor.scan()
        self.now.return_value = 1010
        self.assertEqual(self.monitor.ready(), [])

    def test_async_reply_envelope_cancels_waiting(self):
        self.append("user-a", self.request())
        self.append("user-a", self.output({"accepted": True}))
        self.append(
            "user-a",
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<send_user_message_question_reply>call_question</send_user_message_question_reply>",
                        }
                    ],
                },
            },
        )
        self.monitor.scan()
        self.now.return_value = 1010
        self.assertEqual(self.monitor.ready(), [])

    def test_waiting_then_completion_same_turn_and_retry_persists(self):
        self.append("user-a", self.request(async_question=False))
        self.monitor.scan()
        self.now.return_value = 1002
        request = self.monitor.ready()[0]
        self.monitor.finish(request, False)
        self.assertEqual(self.make_monitor().ready(), [])
        self.now.return_value = 1012
        self.assertEqual(self.make_monitor().ready()[0]["event-id"], request["event-id"])
        self.monitor.finish(request, True)
        self.append("user-a", self.output({"answers": {}}))
        self.append("user-a", self.completion())
        self.monitor.scan()
        self.now.return_value = 1022
        complete = self.monitor.ready()
        self.assertEqual(len(complete), 1)
        self.assertNotIn("notification-kind", complete[0])

    def test_permissions_request_and_explicit_final_question(self):
        self.append("user-a", self.request(name="request_permissions"))
        self.append("user-b", self.completion(result="请确认是否继续执行？"))
        self.monitor.scan()
        self.now.return_value = 1002
        self.assertEqual(
            {p["notification-kind"] for p in self.monitor.ready()},
            {"approval_required", "input_required"},
        )

    def test_old_events_and_new_turn_do_not_leave_stale_questions(self):
        self.append("user-a", self.request())
        self.append(
            "user-a",
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2"}},
        )
        self.append("user-b", self.completion(result=None, error={"message": "old error"}))
        with self.monitor._store() as db:
            db.execute("UPDATE meta SET value=1001 WHERE key='attention_since'")
        self.monitor.scan()
        self.now.return_value = 1010
        self.assertEqual(self.monitor.ready(), [])

    def test_upgrade_preserves_old_completion_deduplication(self):
        path = self.root / "v1.sqlite"
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value REAL);
                    INSERT INTO meta VALUES ('activated_at',900);
                    CREATE TABLE files(path TEXT PRIMARY KEY,offset INTEGER);
                    CREATE TABLE deliveries(thread_id TEXT,turn_id TEXT,payload TEXT,ready_at REAL,
                        delivered_at REAL,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,
                        created_at REAL,PRIMARY KEY(thread_id,turn_id));
                    INSERT INTO deliveries VALUES ('user-a','turn-1','{}',900,901,0,0,900);""")
        upgraded = CompletionMonitor(self.root, path, 10)
        self.append("user-a", self.completion())
        upgraded.scan()
        self.now.return_value = 1010
        self.assertEqual(upgraded.ready(), [])
        self.assertEqual(upgraded.hint(self.hint()), "duplicate")

    def quota(self, reset=1100, secondary=None, limit="codex", observed=None):
        from datetime import datetime, timezone

        rates = {
            "limit_id": limit,
            "primary": {"used_percent": 50, "window_minutes": 300, "resets_at": reset},
        }
        if secondary is not None:
            rates["secondary"] = {
                "used_percent": 30,
                "window_minutes": 10080,
                "resets_at": secondary,
            }
        return {
            "timestamp": datetime.fromtimestamp(
                observed or self.now.return_value, timezone.utc
            ).isoformat(),
            "type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rates},
        }

    def test_quota_reset_without_new_chat_activity_and_restart_retry(self):
        self.append("user-a", self.quota())
        self.monitor.scan()
        self.now.return_value = 1099
        self.monitor.scan()
        self.assertEqual(self.monitor.ready(), [])
        self.now.return_value = 1100
        restarted = self.make_monitor()
        restarted.scan()
        self.assertEqual(restarted.ready(), [])
        self.now.return_value = 1102
        ready = restarted.ready()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["notification-kind"], "quota_reset")
        self.assertIn("预计重置时间已到", ready[0]["last-assistant-message"])
        restarted.finish(ready[0], False)
        self.assertEqual(self.make_monitor().ready(), [])
        self.now.return_value = 1112
        self.assertEqual(len(self.make_monitor().ready()), 1)
        restarted.finish(ready[0], True)
        self.now.return_value = 999999
        restarted.scan()
        self.assertEqual(self.make_monitor().ready(), [])

    def test_quota_shared_across_chats_jitter_and_stale_records(self):
        self.append("user-a", self.quota(reset=1100, observed=1000))
        self.append("user-b", self.quota(reset=1110, observed=1001))
        self.monitor.scan()
        self.append("user-a", self.quota(reset=900, observed=999))
        self.now.return_value = 1100
        self.monitor.scan()
        self.assertEqual(self.monitor.ready(), [])
        self.now.return_value = 1110
        self.monitor.scan()
        self.now.return_value = 1112
        ready = self.monitor.ready()
        self.assertEqual(len(ready), 1)
        self.monitor.finish(ready[0], True)
        self.append("user-b", self.quota(reset=1120, observed=1113))
        self.now.return_value = 1122
        self.monitor.scan()
        self.assertEqual(self.monitor.ready(), [])

    def test_quota_two_windows_and_different_model_buckets(self):
        self.append("user-a", self.quota(reset=1100, secondary=1100))
        self.append("user-b", self.quota(reset=1100, limit="codex_other_model"))
        self.monitor.scan()
        self.now.return_value = 1100
        self.monitor.scan()
        self.now.return_value = 1102
        ready = self.monitor.ready()
        self.assertEqual(len(ready), 3)
        self.assertEqual(len({p["event-id"] for p in ready}), 3)
        self.assertTrue(any("7天" in p["last-assistant-message"] for p in ready))

    def test_quota_fresh_snapshot_confirms_previous_reset_once(self):
        self.append("user-a", self.quota())
        self.monitor.scan()
        self.now.return_value = 1200
        self.append("user-a", self.quota(reset=19100))
        self.monitor.scan()
        self.now.return_value = 1202
        ready = self.monitor.ready()
        self.assertEqual(len(ready), 1)
        self.assertIn("已进入下一周期", ready[0]["last-assistant-message"])
        self.monitor.finish(ready[0], True)
        self.now.return_value = 19100
        self.monitor.scan()
        self.now.return_value = 19102
        self.assertEqual(len(self.monitor.ready()), 1)
        self.assertNotEqual(self.monitor.ready()[0]["event-id"], ready[0]["event-id"])

    def test_quota_first_enable_seeds_future_only_and_missing_timestamp_is_ignored(self):
        self.append("user-a", self.quota(reset=1100, secondary=900))
        self.append("user-b", self.quota(reset=None, observed=1001, limit="unknown"))
        fresh = CompletionMonitor(self.root, self.root / "quota-new.sqlite", 10)
        self.now.return_value = 1050
        fresh.scan()
        self.assertEqual(fresh.ready(), [])
        self.now.return_value = 1100
        fresh.scan()
        self.now.return_value = 1102
        ready = fresh.ready()
        self.assertEqual(len(ready), 1)
        self.assertIn("5小时", ready[0]["last-assistant-message"])

    def test_quota_percentage_persists_and_older_snapshot_does_not_overwrite(self):
        event = self.quota()
        event["payload"]["rate_limits"]["primary"]["used_percent"] = 28
        self.append("user-a", event)
        self.monitor.scan()
        self.assertEqual(self.make_monitor().quota_status()[0]["used_percent"], 28)
        older = self.quota(observed=999)
        self.append("user-b", older)
        self.monitor.scan()
        self.assertEqual(self.monitor.quota_status()[0]["used_percent"], 28)

    def test_upgrade_backfills_percentage_at_equal_snapshot_time(self):
        self.append("user-a", self.quota())
        self.monitor.scan()
        # Reproduce the pre-1.6 schema, without deleting its reset/notification state.
        with self.monitor._store() as db:
            db.execute("ALTER TABLE quota_windows DROP COLUMN used_percent")
            db.execute("DELETE FROM meta WHERE key='quota_usage_seeded'")
        upgraded = self.make_monitor()
        self.assertEqual(upgraded.quota_status()[0]["used_percent"], 50)
        self.assertEqual(upgraded.quota_status()[0]["reset_at"], 1100)


if __name__ == "__main__":
    unittest.main()
