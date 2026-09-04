import os
import tempfile
import unittest

# Keep bot's import-time runtime store out of the project directory.
_IMPORT_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_IMPORT_DB.close()
os.environ["DB_PATH"] = _IMPORT_DB.name
from bot import COMMANDS, Store, timezone_for


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

    def test_high_volume_messages_respect_daily_limit(self):
        self.s.set_setting(-1, "min_chars", 1)
        self.s.set_setting(-1, "daily_limit", 1000)
        for _ in range(1000):
            self.assertEqual(self.s.award_chat(-1, 42, "busy", "Busy", "x"), 1)
        self.assertEqual(self.s.award_chat(-1, 42, "busy", "Busy", "x"), 0)
        self.assertEqual(self.s.score(-1, 42)[:2], (1000, 1000))

if __name__ == "__main__": unittest.main()
