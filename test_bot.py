import os
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import asyncio
import sqlite3
import json

# Keep bot's import-time runtime store out of the project directory.
_IMPORT_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_IMPORT_DB.close()
os.environ["DB_PATH"] = _IMPORT_DB.name
from bot import COMMANDS, Store, timezone_for
from bot import adjustment_history, adjustment_summary, member_identity, who
from bot import callback, private_text, document_handler


class PointsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.s = Store(self.tmp.name)
        self.s.generate_code()
        self.code = self.s.generate_code()
        self.assertTrue(self.s.authorize(-1, "one", self.code, 10))

    def tearDown(self):
        self.s.close(); self.tmp.close()

    def test_authorization_once_and_unauthorized_no_score(self):
        self.assertTrue(self.s.authorize(-2, "two", self.code, 10))
        self.assertFalse(self.s.authorize(-2, "two", None, 10))
        self.assertEqual(self.s.award_chat(-99, 1, "a", "A", "long enough"), 0)

    def test_multi_chat_isolation_and_limits(self):
        self.assertTrue(self.s.authorize(-2, "two", None, 10))
        self.s.set_setting(-1, "min_chars", 3); self.s.set_setting(-1, "daily_limit", 2)
        self.assertEqual(self.s.award_chat(-1, 5, "User", "U", "a b c"), 1)
        self.assertEqual(self.s.award_chat(-1, 5, "User", "U", "abc"), 1)
        self.assertEqual(self.s.award_chat(-1, 5, "User", "U", "abc"), 0)
        self.assertEqual(self.s.award_chat(-2, 5, "User", "U", "abcde"), 1)
        self.assertEqual(self.s.score(-1, 5)[0], 2); self.assertEqual(self.s.score(-2, 5)[0], 1)

    def test_checkin_once_does_not_use_chat_limit(self):
        self.s.set_setting(-1, "daily_limit", 1)
        self.assertEqual(self.s.award_chat(-1, 5, None, "U", "hello"), 1)
        ok, points, total = self.s.checkin(-1, 5, None, "U"); self.assertTrue(ok); self.assertEqual(points, 5); self.assertEqual(total, 6)
        self.assertFalse(self.s.checkin(-1, 5, None, "U")[0])

    def test_zero_minimum_allows_nonempty_text_and_zero_limit_is_unlimited(self):
        self.s.set_setting(-1, "min_chars", 0); self.s.set_setting(-1, "daily_limit", 0)
        self.assertEqual(self.s.award_chat(-1, 5, None, "U", "x"), 1)
        self.assertEqual(self.s.award_chat(-1, 5, None, "U", "x"), 1)
        self.assertEqual(self.s.award_chat(-1, 5, None, "U", "   "), 0)

    def test_aliases_are_isolated_and_conflicts_blocked(self):
        self.assertTrue(self.s.add_alias(-1, "checkin", "打卡")); self.assertFalse(self.s.add_alias(-1, "score", "打卡"))
        self.assertIsNone(self.s.resolve_alias(-2, "打卡")); self.assertEqual(self.s.resolve_alias(-1, "打卡"), "checkin")

    def test_adjustment_and_audit(self):
        self.s.upsert_user(-1, 7, "Alice", "Alice")
        with self.assertRaises(ValueError): self.s.adjust(-1, 99, 7, -1, "username")
        before, after = self.s.adjust(-1, 99, 7, 10, "reply"); self.assertEqual((before,after),(0,10))
        with self.assertRaises(ValueError): self.s.adjust(-1, 99, 7, -11, "reply")
        self.assertEqual(len(self.s.recent(-1,7)), 1)
        self.assertEqual(self.s.find_user(-1, "@alice")["user_id"], 7); self.assertIsNone(self.s.find_user(-1,"missing"))

    def test_member_labels_and_adjustment_history(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.upsert_user(-1, 99, "admin", "Admin")
        self.s.upsert_user(-1, 8, None, "No username")
        self.s.adjust(-1, 99, 7, 10, "reply", "活动奖励")
        with patch("bot.store", self.s):
            self.assertEqual(who(self.s.find_user(-1, "7")), "@alice")
            self.assertEqual(member_identity(-1, 8), "No username (ID: 8)")
            self.assertEqual(member_identity(-1, 100), "ID: 100")
            history = adjustment_history(-1, 7)
            for text in ("Alice", "Admin", "+10 分", "0 → 10", "活动奖励", "回复消息"):
                self.assertIn(text, history)
            self.s.set_language(-1, "en")
            self.assertIn("By: Admin", adjustment_history(-1, 7))
            self.assertIn("Change: -2 points", adjustment_summary(-1, 7, -2))

    def test_telegram_message_boundaries(self):
        from bot import build_app, text_handler, addressed_to_other_bot, points_cmd
        from telegram import Update, Message, Chat, User
        from datetime import datetime, timezone
        async def make_app():
            return build_app("123:fake")
        app = asyncio.run(make_app())
        edited = Update(1, edited_message=Message(1, datetime.now(timezone.utc), Chat(-1, "supergroup"), text="/addpoints 10", from_user=User(7, "Alice", False)))
        self.assertFalse(any(h.check_update(edited) for handlers in app.handlers.values() for h in handlers))
        self.assertTrue(addressed_to_other_bot("/checkin@OtherBot", "MyBot"))
        self.assertFalse(addressed_to_other_bot("/checkin@mYbOt", "MyBot"))
        user = SimpleNamespace(id=7, username="alice", full_name="Alice", is_bot=False)
        message = SimpleNamespace(from_user=user, sender_chat=None, text="/checkin@OtherBot", reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=user, effective_chat=SimpleNamespace(id=-1, type="supergroup"))
        context = SimpleNamespace(bot=SimpleNamespace(username="MyBot"))
        with patch("bot.store", self.s):
            asyncio.run(text_handler(update, context))
            self.assertEqual(self.s.total(-1, 7), 0)
            message.reply_text.assert_not_called()
            for text in ("/checkin", "/checkin@MyBot"):
                message.text=text
                asyncio.run(text_handler(update, context))
            self.assertEqual(self.s.total(-1, 7), 5)
            message.text="hello world"
            user.is_bot=True
            asyncio.run(text_handler(update, context))
            user.is_bot=False
            message.sender_chat=SimpleNamespace(id=-1)
            asyncio.run(text_handler(update, context))
            asyncio.run(points_cmd(update, context, "addpoints"))
            self.assertIn("关闭匿名", message.reply_text.call_args.args[0])
            self.assertEqual(self.s.total(-1, 7), 5)

    def test_lists_never_mention_and_error_logging(self):
        from bot import list_name, error_handler, rank_cmd
        from telegram.error import BadRequest
        self.s.upsert_user(-1, 7, "alice", "Alice@work")
        self.assertEqual(list_name(self.s.find_user(-1, "7")), "Alice＠work")
        update=SimpleNamespace(effective_chat=SimpleNamespace(id=-1), effective_user=SimpleNamespace(id=7, is_bot=False), effective_message=SimpleNamespace(sender_chat=None, reply_text=AsyncMock()))
        with patch("bot.store", self.s):
            asyncio.run(rank_cmd(update, None))
            self.assertNotIn("@", update.effective_message.reply_text.call_args.args[0])
            self.assertNotIn("@", adjustment_history(-1, 7))
        with patch("bot.logging.getLogger") as logger:
            asyncio.run(error_handler(None, SimpleNamespace(error=BadRequest("Message is not modified"))))
            logger.assert_not_called()
            asyncio.run(error_handler(None, SimpleNamespace(error=RuntimeError("failure"))))
            self.assertIn("exc_info", logger.return_value.error.call_args.kwargs)

    def test_adjustment_history_pages_do_not_skip_entries(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        for index in range(12):
            self.s.adjust(-1, 99, 7, 1, "后台", str(index))
        pages = [self.s.recent(-1, 7, page, 5) for page in range(3)]
        self.assertEqual([len(rows) for rows in pages], [5, 5, 2])
        self.assertEqual(len({r["id"] for rows in pages for r in rows}), 12)

    def test_undo_is_once_only_and_preserves_other_changes(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.adjust(-1, 99, 7, 10, "reply")
        aid = self.s.recent(-1, 7)[0]["id"]
        self.s.adjust(-1, 99, 7, 5, "reply")
        self.assertEqual(self.s.undo_adjustment(-1, 99, aid), (15, 5))
        with self.assertRaises(ValueError): self.s.undo_adjustment(-1, 99, aid)
        with self.assertRaises(ValueError): self.s.undo_adjustment(-1, 99, self.s.recent(-1, 7)[0]["id"])
        self.assertEqual(self.s.total(-1, 7), 5)

    def test_import_rejects_invalid_values_and_rolls_back(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        for key, value in (("total_points", -1), ("total_points", 1.5), ("user_id", 0)):
            data = json.loads(self.s.export_chat(-1))
            data["users"][0][key] = value
            with self.assertRaises(ValueError): self.s.import_chat(-1, 99, data)
        for date in ("2026-02-30", "2026-1-01"):
            data = {"chat_id": -1, "users": [{"user_id": 7, "total_points": 100}],
                    "daily": [{"user_id": 7, "day": date}]}
            with self.assertRaises(ValueError): self.s.import_chat(-1, 99, data)
        data = {"chat_id": -1, "users": [{"user_id": 7, "total_points": 100}],
                "daily": [{"user_id": 123, "day": "2026-10-10"}]}
        with self.assertRaises(ValueError): self.s.import_chat(-1, 99, data)
        self.assertEqual(self.s.total(-1, 7), 0)
        self.assertEqual(len(self.s.import_history(-1)), 0)
        with patch("bot.json.loads") as loads:
            with self.assertRaisesRegex(ValueError, "过大"):
                self.s.import_chat(-1, 99, " " * (10 * 1024 * 1024 + 1))
            loads.assert_not_called()

    def test_backup_includes_committed_wal_data(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.adjust(-1, 99, 7, 10, "reply")
        with tempfile.NamedTemporaryFile(suffix=".db") as target:
            self.s.backup(target.name)
            conn = sqlite3.connect(target.name)
            try: self.assertEqual(conn.execute("SELECT total_points FROM users WHERE user_id=7").fetchone()[0], 10)
            finally: conn.close()

    def test_backup_script_uses_env_readonly_and_safe_arguments(self):
        import pathlib
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            shutil.copy("manage.sh", root / "manage.sh")
            (root / ".venv/bin").mkdir(parents=True)
            (root / ".venv/bin/python").symlink_to(os.path.abspath(".venv/bin/python"))
            db=root / "custom ' database.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE proof(value)")
                conn.execute("INSERT INTO proof VALUES(42)")
            (root / ".env").write_text("BOT_TOKEN=fake\nOWNER_ID=1\nDB_PATH=\"custom ' database.db\"\n")
            dest=root / "backup ' output.db"
            result=subprocess.run(["bash", str(root / "manage.sh"), "backup", str(dest)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(dest) as conn:
                self.assertEqual(conn.execute("SELECT value FROM proof").fetchone()[0], 42)
            self.assertFalse((root / "points.db").exists())
            (root / ".env").write_text("BOT_TOKEN=fake\nOWNER_ID=1\nDB_PATH=missing.db\n")
            result=subprocess.run(["bash", str(root / "manage.sh"), "backup"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / "missing.db").exists())

    def test_migration_all_tables_and_removal_order(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.award_chat(-1, 7, "alice", "Alice", "hello")
        self.s.adjust(-1, 99, 7, 10, "reply")
        self.s.add_alias(-1, "score", "积分")
        self.s.import_chat(-1, 99, self.s.export_chat(-1))
        self.s.bot_removed(-1)
        self.assertFalse(self.s.authorized(-1))
        self.assertTrue(self.s.migrate_chat(-1, -1001))
        self.assertTrue(self.s.authorized(-1001))
        self.assertEqual(self.s.total(-1001, 7), 11)
        self.assertEqual(self.s.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        for table, column in (("users","chat_id"), ("daily","chat_id"), ("adjustments","chat_id"), ("data_imports","chat_id"), ("aliases","chat_id"), ("auth_codes","used_chat_id")):
            self.assertEqual(self.s.conn.execute(f"SELECT count(*) FROM {table} WHERE {column}=-1").fetchone()[0], 0)
            self.assertGreater(self.s.conn.execute(f"SELECT count(*) FROM {table} WHERE {column}=-1001").fetchone()[0], 0)
        self.assertFalse(self.s.migrate_chat(-1, -1001))
        self.s.bot_removed(-1)
        self.assertTrue(self.s.authorized(-1001))
        self.s.authorize(-2, "two", None, 10)
        self.s.migrate_chat(-2, -1002)
        self.s.bot_removed(-2)
        self.assertTrue(self.s.authorized(-1002))
        self.s.update_title(-1002, "New title")
        self.assertEqual(self.s.conn.execute("SELECT title FROM chats WHERE chat_id=-1002").fetchone()[0], "New title")
        self.s.bot_removed(-1002)
        self.assertFalse(self.s.authorized(-1002))
        self.assertTrue(self.s.authorize(-3, "three", None, 10))
        self.assertTrue(self.s.authorize(-4, "four", None, 10))

    def test_migration_conflict_rolls_back_and_notifies(self):
        from bot import migrate_group
        self.s.authorize(-2, "two", None, 10)
        self.s.upsert_user(-1, 7, None, "Alice")
        context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        update=SimpleNamespace(effective_chat=SimpleNamespace(id=-1), effective_message=SimpleNamespace(migrate_from_chat_id=None, migrate_to_chat_id=-2))
        with patch("bot.store", self.s), patch("bot.OWNER_ID", 99), patch("bot.logging.getLogger"):
            asyncio.run(migrate_group(update, context))
        context.bot.send_message.assert_awaited_once()
        self.assertIsNotNone(self.s.find_user(-1, "7"))
        self.assertTrue(self.s.authorized(-2))
        self.assertEqual(self.s.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_private_flows_search_cancel_undo_and_ranking(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.adjust(-1, 99, 7, 10, "reply")
        aid = self.s.recent(-1, 7)[0]["id"]
        user = SimpleNamespace(id=99, username="admin", full_name="Admin")
        q = SimpleNamespace(data=f"undo_recent:-1:7:{aid}", from_user=user,
                            answer=AsyncMock(), edit_message_text=AsyncMock())
        message = SimpleNamespace(text="missing", reply_text=AsyncMock())
        update = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(type="private"),
                                 effective_user=user, effective_message=message)
        context = SimpleNamespace(user_data={"chat_id": -1, "state": "search"})
        with patch("bot.store", self.s), patch("bot.is_admin", new=AsyncMock(return_value=True)):
            asyncio.run(private_text(update, context))
            self.assertNotIn("state", context.user_data)
            asyncio.run(callback(update, context))
            self.assertEqual(self.s.total(-1, 7), 10)
            self.assertIn("确认撤销", q.edit_message_text.call_args.args[0])
            q.data=f"undo_confirm:-1:7:{aid}"
            asyncio.run(callback(update, context))
            self.assertEqual(self.s.total(-1, 7), 0)
            q.data="stat:-1:0"
            for uid in range(20, 40): self.s.upsert_user(-1, uid, f"u{uid}", str(uid))
            asyncio.run(callback(update, context))
            markup=q.edit_message_text.call_args.kwargs["reply_markup"]
            self.assertIn("rank:-1:0:0", [b.callback_data for row in markup.inline_keyboard for b in row])

    def test_document_without_filename_has_recovery_buttons(self):
        message=SimpleNamespace(document=SimpleNamespace(file_name=None), reply_text=AsyncMock())
        update=SimpleNamespace(effective_chat=SimpleNamespace(type="private"), effective_message=message)
        with patch("bot.store", self.s), patch("bot.is_admin", new=AsyncMock(return_value=True)):
            asyncio.run(document_handler(update, SimpleNamespace(user_data={"chat_id": -1})))
        self.assertIn("reply_markup", message.reply_text.call_args.kwargs)

    def test_manual_adjustment_leaves_daily_state_unchanged(self):
        self.s.set_setting(-1, "daily_limit", 1)
        self.s.award_chat(-1, 7, "Alice", "Alice", "hello")
        self.s.checkin(-1, 7, "Alice", "Alice")
        self.s.adjust(-1, 99, 7, 10, "后台")
        self.assertEqual(self.s.score(-1, 7), (16, 1, True))

    def test_unknown_username_and_negative_result_rejected(self):
        self.assertIsNone(self.s.find_user(-1, "@nobody"))
        self.s.upsert_user(-1, 9, "bob", "Bob")
        with self.assertRaises(ValueError): self.s.adjust(-1, 1, 9, -1, "reply")

    def test_callback_chat_id_parsing_supports_negative_group_ids(self):
        for data in ("search:-100123:extra", "edit:-100123:min_chars", "adjust:-100123:7:1"):
            self.assertEqual(int(data.split(":")[1]), -100123)

    def test_id_search_is_chat_scoped(self):
        self.s.authorize(-2,"two",None,10)
        self.s.upsert_user(-1, 8, "same", "One"); self.s.upsert_user(-2, 8, "same", "Two")
        self.assertEqual(self.s.find_user(-1, "8")["display_name"], "One"); self.assertEqual(self.s.find_user(-2,"8")["display_name"], "Two")

    def test_personal_license_is_persistent_and_limited_to_three_groups(self):
        self.assertTrue(self.s.authorize(-2, "two", None, 10))
        self.assertTrue(self.s.authorize(-3, "three", None, 10))
        self.assertFalse(self.s.authorize(-4, "four", None, 10))
        self.assertFalse(self.s.authorize(-4, "four", self.code, 11))
        self.s.close()
        self.s = Store(self.tmp.name)
        self.assertTrue(self.s.authorized(-1)); self.assertTrue(self.s.authorized(-2)); self.assertTrue(self.s.authorized(-3))

    def test_language_and_timezone_are_group_scoped(self):
        self.s.set_language(-1, "en"); self.s.set_timezone(-1, "UTC")
        self.assertEqual(self.s.settings(-1)["language"], "en")
        self.assertEqual(self.s.settings(-1)["timezone"], "UTC")
        self.assertEqual(self.s.settings(-2), None)
        self.s.set_timezone(-1, "UTC+08:00")
        self.assertEqual(timezone_for("UTC+08:00").utcoffset(None).total_seconds(), 8 * 3600)

    def test_owner_can_activate_unlimited_groups(self):
        for cid in range(-10, -15, -1):
            self.assertTrue(self.s.authorize(cid, str(cid), None, 999, owner=True))

    def test_command_menu_contains_core_commands(self):
        self.assertEqual({command.command for command in COMMANDS}, {"start", "score", "checkin", "addpoints", "subpoints", "activate", "rank", "today"})

    def test_revoke_is_idempotent_and_keeps_data(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.revoke(-1); self.s.revoke(-1)
        self.assertFalse(self.s.authorized(-1)); self.assertIsNotNone(self.s.find_user(-1, "7"))

    def test_adjustment_is_blocked_after_revoke(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        self.s.revoke(-1)
        with self.assertRaisesRegex(ValueError, "本群尚未激活"):
            self.s.adjust(-1, 99, 7, 1, "后台")

    def test_setting_values_are_bounded(self):
        self.s.set_setting(-1, "daily_limit", 1000000)
        with self.assertRaisesRegex(ValueError, "0 到 1000000"):
            self.s.set_setting(-1, "daily_limit", 1000001)
        with self.assertRaisesRegex(ValueError, "0 到 1000000"):
            self.s.set_setting(-1, "daily_limit", 10**100)

    def test_rankings_are_paginated_and_daily_scoped(self):
        for user_id in range(1, 18): self.s.upsert_user(-1, user_id, f"u{user_id}", f"User {user_id}")
        self.s.conn.execute("UPDATE users SET total_points=user_id WHERE chat_id=-1"); self.s.conn.commit()
        rows,total=self.s.ranking(-1,False,0,15); self.assertEqual((len(rows),total),(15,17)); self.assertEqual(rows[0]["points"],17)
        rows2,total2=self.s.ranking(-1,False,1,15); self.assertEqual((len(rows2),total2),(2,17))
        self.s.award_chat(-1, 1, "u1", "User 1", "hello")
        daily,total_daily=self.s.ranking(-1,True,0,15); self.assertEqual(total_daily,1); self.assertEqual(daily[0]["user_id"],1)

    def test_export_import_is_bound_to_one_chat_and_is_audited(self):
        self.s.upsert_user(-1, 7, "alice", "Alice")
        payload = self.s.export_chat(-1)
        with self.assertRaisesRegex(ValueError, "不是本群"):
            self.s.import_chat(-2, 99, payload)
        self.assertEqual(self.s.import_chat(-1, 99, payload), 1)
        history = self.s.import_history(-1)
        self.assertEqual((history[0]["operator_id"], history[0]["user_count"]), (99, 1))

    def test_import_rejects_unbound_legacy_snapshot(self):
        with self.assertRaisesRegex(ValueError, "不是本群"):
            self.s.import_chat(-1, 99, {"users": []})

    def test_high_volume_messages_respect_daily_limit(self):
        self.s.set_setting(-1, "min_chars", 1)
        self.s.set_setting(-1, "daily_limit", 1000)
        for _ in range(1000):
            self.assertEqual(self.s.award_chat(-1, 42, "busy", "Busy", "x"), 1)
        self.assertEqual(self.s.award_chat(-1, 42, "busy", "Busy", "x"), 0)
        self.assertEqual(self.s.score(-1, 42)[:2], (1000, 1000))

if __name__ == "__main__": unittest.main()
