import time
import logging
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter, Application, ChatMemberHandler, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

REGISTERED_COMMANDS = set()


def alias_key(alias):
    return hashlib.sha1(alias.encode()).hexdigest()[:10]


DEFAULT_TIMEZONE = "Asia/Hong_Kong"
TIMEZONE_OPTIONS = ["UTC-08:00", "UTC-05:00", "UTC+00:00", "UTC+01:00", "UTC+05:30", "UTC+08:00", "UTC+09:00", "UTC+10:00", "UTC+12:00"]
TIMEZONE_LABELS = {
    "UTC-08:00": ("UTC-08:00", "UTC-08:00"), "UTC-05:00": ("UTC-05:00", "UTC-05:00"),
    "UTC+00:00": ("UTC+00:00", "UTC+00:00"), "UTC+01:00": ("UTC+01:00", "UTC+01:00"),
    "UTC+05:30": ("UTC+05:30", "UTC+05:30"), "UTC+08:00": ("UTC+08:00", "UTC+08:00"),
    "UTC+09:00": ("UTC+09:00", "UTC+09:00"), "UTC+10:00": ("UTC+10:00", "UTC+10:00"),
    "UTC+12:00": ("UTC+12:00", "UTC+12:00"),
    "Asia/Hong_Kong": ("UTC+08:00", "UTC+08:00"), "Asia/Shanghai": ("UTC+08:00", "UTC+08:00"),
    "UTC": ("UTC+00:00", "UTC+00:00"), "Europe/London": ("UTC+00:00", "UTC+00:00"),
    "America/New_York": ("UTC-05:00", "UTC-05:00"),
}
DEFAULTS = {"checkin": ["签到", "/checkin"], "score": ["/score"], "addpoints": ["/addpoints"], "subpoints": ["/subpoints"]}
DEFAULTS.update({"rank": ["/rank"], "today": ["/today"]})
ACTION_LABELS = {
    "checkin": ("签到", "Check-in"), "score": ("查看积分", "View points"),
    "addpoints": ("增加积分", "Add points"), "subpoints": ("扣除积分", "Remove points"),
    "rank": ("累计积分排名", "All-time ranking"), "today": ("今日积分排名", "Today's ranking"),
}
SETTING_LABELS = {
    "min_chars": ("最低消息长度", "Minimum message length"),
    "daily_limit": ("每日聊天积分上限", "Daily chat point limit"),
    "checkin_points": ("签到奖励", "Check-in reward"),
}
DB_PATH = os.getenv("DB_PATH", "points.db")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
db_lock = threading.RLock()
COMMANDS = [
    BotCommand("start", "管理群组积分和规则 / Manage groups and points"),
    BotCommand("score", "查看我的积分和今日记录 / View my points and today"),
    BotCommand("checkin", "签到并领取每日积分 / Check in for daily points"),
    BotCommand("addpoints", "给成员增加积分（管理员） / Add member points"),
    BotCommand("subpoints", "扣除成员积分（管理员） / Remove member points"),
    BotCommand("activate", "激活本群积分功能 / Activate points for this group"),
    BotCommand("rank", "查看群组累计积分排名 / View all-time ranking"),
    BotCommand("today", "查看今日积分排名 / View today's ranking"),
]


def now(tz_name=DEFAULT_TIMEZONE, now_fn=None):
    return (now_fn or datetime.now)(timezone_for(tz_name)).isoformat(timespec="seconds")


def day(tz_name=DEFAULT_TIMEZONE, now_fn=None):
    return (now_fn or datetime.now)(timezone_for(tz_name)).date().isoformat()


def timezone_for(name):
    match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", name)
    if match:
        minutes = int(match[2]) * 60 + int(match[3])
        return dt_timezone(timedelta(minutes=minutes if match[1] == "+" else -minutes), name)
    return ZoneInfo(name)


def tr(chat_id, zh, en):
    """Return the selected language for a group; private/global messages stay Chinese."""
    try:
        r = store.conn.execute("SELECT language FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        return en if r and r[0] == "en" else zh
    except (NameError, sqlite3.Error):
        return zh


ERROR_EN = {
    "数量必须是 1 到 1000000 的整数": "Amount must be an integer from 1 to 1,000,000.",
    "用户需要先在群里发言": "The member must speak in the group first.",
    "积分不能低于 0": "Points cannot go below 0.",
    "本群尚未激活积分功能": "Points are not activated for this group.",
    "设置必须是 0 到 1000000 的整数": "Settings must be an integer from 0 to 1,000,000.",
}


def tr_error(chat_id, message):
    return tr(chat_id, message, ERROR_EN.get(message, message))


class Store:
    def __init__(self, path=DB_PATH, now_fn=None):
        self.path = os.fspath(path)
        self.now_fn = now_fn
        self.conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.init()

    def init(self):
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, authorized_by INTEGER);
            CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY REFERENCES chats(chat_id) ON DELETE CASCADE, min_chars INTEGER NOT NULL DEFAULT 5, daily_limit INTEGER NOT NULL DEFAULT 20, checkin_points INTEGER NOT NULL DEFAULT 5, language TEXT NOT NULL DEFAULT 'zh', timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong');
            CREATE TABLE IF NOT EXISTS auth_codes(code_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, used_at TEXT, used_by INTEGER, used_chat_id INTEGER);
            CREATE TABLE IF NOT EXISTS licenses(user_id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE, bound_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(chat_id INTEGER, user_id INTEGER, username TEXT, display_name TEXT NOT NULL, total_points INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(chat_id,user_id), FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS daily(chat_id INTEGER, user_id INTEGER, day TEXT, chat_points INTEGER NOT NULL DEFAULT 0, checked_in INTEGER NOT NULL DEFAULT 0, checkin_points INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,user_id,day), FOREIGN KEY(chat_id,user_id) REFERENCES users(chat_id,user_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS aliases(chat_id INTEGER, action TEXT, alias TEXT, PRIMARY KEY(chat_id,alias), FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS adjustments(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, operator_id INTEGER, target_id INTEGER, delta INTEGER, before_points INTEGER, after_points INTEGER, method TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS data_imports(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, operator_id INTEGER NOT NULL, imported_at TEXT NOT NULL, user_count INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'json');
            CREATE TABLE IF NOT EXISTS chat_departures(chat_id INTEGER PRIMARY KEY, was_enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS chat_migrations(old_id INTEGER PRIMARY KEY, new_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS message_fingerprints(chat_id INTEGER,user_id INTEGER,fingerprint TEXT,created_at TEXT NOT NULL,PRIMARY KEY(chat_id,user_id,fingerprint));
            """)
            self.conn.commit()
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 3: raise ValueError("数据库版本高于当前程序支持的版本")
            if version < 3 and self.conn.execute("SELECT 1 FROM chats LIMIT 1").fetchone():
                from pathlib import Path
                directory=Path(self.path).resolve().parent / "backups"
                directory.mkdir(exist_ok=True)
                self.backup(str(directory / ("pre-migration-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db")))
            if version < 1:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    for table, column, definition in (
                        ("chats", "authorized_by", "INTEGER"),
                        ("settings", "language", "TEXT NOT NULL DEFAULT 'zh'"),
                        ("settings", "timezone", "TEXT NOT NULL DEFAULT 'Asia/Hong_Kong'"),
                        ("adjustments", "reason", "TEXT NOT NULL DEFAULT ''"),
                    ):
                        if column not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    self.conn.execute("PRAGMA user_version=1")
            if version < 2:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    if "reverts_id" not in {r[1] for r in self.conn.execute("PRAGMA table_info(adjustments)")}:
                        self.conn.execute("ALTER TABLE adjustments ADD COLUMN reverts_id INTEGER REFERENCES adjustments(id)")
                    for r in self.conn.execute("SELECT id,chat_id,reason FROM adjustments WHERE method='undo' ORDER BY id").fetchall():
                        match=re.fullmatch(r"撤销调整 #([0-9]+)",r["reason"])
                        if match and self.conn.execute("SELECT 1 FROM adjustments WHERE id=? AND chat_id=?",(int(match[1]),r["chat_id"])).fetchone():
                            if not self.conn.execute("SELECT 1 FROM adjustments WHERE reverts_id=?",(int(match[1]),)).fetchone():
                                self.conn.execute("UPDATE adjustments SET reverts_id=? WHERE id=?",(int(match[1]),r["id"]))
                    self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_adjustments_reverts ON adjustments(reverts_id) WHERE reverts_id IS NOT NULL")
                    self.conn.execute("DELETE FROM message_fingerprints")
                    self.conn.execute("PRAGMA user_version=2")
            if version < 3:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    rows=self.conn.execute("SELECT rowid,* FROM aliases ORDER BY rowid").fetchall()
                    seen=set()
                    for row in rows:
                        normalized=row["alias"].lower()
                        key=(row["chat_id"],normalized)
                        if key in seen:
                            logging.getLogger(__name__).warning("Duplicate alias removed in group %s: %s",*key)
                            self.conn.execute("DELETE FROM aliases WHERE rowid=?",(row["rowid"],))
                        else:
                            self.conn.execute("UPDATE aliases SET alias=? WHERE rowid=?",(normalized,row["rowid"]))
                            seen.add(key)
                    self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_unique ON aliases(chat_id,alias)")
                    self.conn.execute("PRAGMA user_version=3")
            # Migrate old one-group code bindings to the new one-license-per-person model.
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) SELECT chat_id FROM chats")
            self.conn.execute("INSERT OR IGNORE INTO licenses(user_id,code_hash,bound_at) SELECT used_by,code_hash,COALESCE(used_at,created_at) FROM auth_codes WHERE used_by IS NOT NULL")
            self.conn.execute("UPDATE chats SET authorized_by=(SELECT used_by FROM auth_codes WHERE used_chat_id=chats.chat_id AND used_by IS NOT NULL ORDER BY used_at LIMIT 1) WHERE authorized_by IS NULL")
            self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_daily_chat_day_user ON daily(chat_id, day, user_id);
            CREATE INDEX IF NOT EXISTS idx_users_chat_username ON users(chat_id, lower(username));
            CREATE INDEX IF NOT EXISTS idx_adjustments_chat_target_id ON adjustments(chat_id, target_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_chats_enabled_authorized ON chats(enabled, authorized_by);
            CREATE INDEX IF NOT EXISTS idx_aliases_chat_alias ON aliases(chat_id, lower(alias));
            """)

    def close(self): self.conn.close()

    def authorized(self, chat_id):
        r = self.conn.execute("SELECT enabled FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r[0])

    def authorize(self, chat_id, title, code, user_id, owner=False):
        with db_lock, self.conn:
            if self.authorized(chat_id): return False
            if not owner:
                if self.conn.execute("SELECT COUNT(*) FROM chats WHERE enabled=1 AND authorized_by=?", (user_id,)).fetchone()[0] >= 3: return False
                license_row = self.conn.execute("SELECT code_hash FROM licenses WHERE user_id=?", (user_id,)).fetchone()
                if license_row:
                    if code and hashlib.sha256(code.encode()).hexdigest() != license_row[0]: return False
                else:
                    if not code: return False
                    h = hashlib.sha256(code.encode()).hexdigest()
                    r = self.conn.execute("SELECT used_at FROM auth_codes WHERE code_hash=?", (h,)).fetchone()
                    if not r or r[0]: return False
                    self.conn.execute("INSERT INTO licenses(user_id,code_hash,bound_at) VALUES(?,?,?)", (user_id,h,now(now_fn=self.now_fn)))
                    self.conn.execute("UPDATE auth_codes SET used_at=?,used_by=?,used_chat_id=? WHERE code_hash=? AND used_at IS NULL", (now(now_fn=self.now_fn),user_id,chat_id,h))
            self.conn.execute("DELETE FROM chat_departures WHERE chat_id=?", (chat_id,))
            t = now(now_fn=self.now_fn)
            self.conn.execute("INSERT INTO chats(chat_id,title,enabled,created_at,authorized_by) VALUES(?,?,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,enabled=1,authorized_by=excluded.authorized_by", (chat_id,title,t,user_id))
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
            return True

    def generate_code(self):
        code = secrets.token_urlsafe(12)
        with self.conn:
            self.conn.execute("INSERT INTO auth_codes(code_hash,created_at) VALUES(?,?)", (hashlib.sha256(code.encode()).hexdigest(), now(now_fn=self.now_fn)))
        return code

    def unused_codes(self):
        return self.conn.execute("SELECT created_at,substr(code_hash,1,12) AS hash FROM auth_codes WHERE used_at IS NULL ORDER BY created_at DESC").fetchall()

    def chats(self, enabled_only=True):
        q = "SELECT * FROM chats" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY title"
        return self.conn.execute(q).fetchall()

    def revoke(self, chat_id):
        with self.conn: self.conn.execute("UPDATE chats SET enabled=0 WHERE chat_id=?", (chat_id,))

    def migrate_chat(self, old_id, new_id):
        """Move the whole group atomically; never merge two existing groups."""
        with db_lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("PRAGMA defer_foreign_keys = ON")
            if not self.conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (old_id,)).fetchone():
                return False
            if self.conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (new_id,)).fetchone():
                raise ValueError(f"群迁移冲突：{old_id} → {new_id}，目标群已有数据")
            departure = self.conn.execute("SELECT was_enabled FROM chat_departures WHERE chat_id=?", (old_id,)).fetchone()
            tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            for row in tables:
                table = row[0].replace('"', '""')
                if table in ("chat_migrations", "chat_departures"):
                    continue
                columns = self.conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                for column in columns:
                    if column[1] in ("chat_id", "used_chat_id"):
                        name = column[1]
                        self.conn.execute(f'UPDATE "{table}" SET "{name}"=? WHERE "{name}"=?', (new_id, old_id))
            if departure:
                self.conn.execute("UPDATE chats SET enabled=? WHERE chat_id=?", (departure[0], new_id))
                self.conn.execute("DELETE FROM chat_departures WHERE chat_id=?", (old_id,))
            self.conn.execute("INSERT INTO chat_migrations(old_id,new_id) VALUES(?,?)", (old_id,new_id))
            return True

    def bot_removed(self, chat_id):
        with db_lock, self.conn:
            if self.conn.execute("SELECT 1 FROM chat_migrations WHERE old_id=?", (chat_id,)).fetchone():
                return
            row = self.conn.execute("SELECT enabled FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
            if row:
                self.conn.execute("INSERT OR IGNORE INTO chat_departures VALUES(?,?)", (chat_id,row[0]))
                self.conn.execute("UPDATE chats SET enabled=0 WHERE chat_id=?", (chat_id,))

    def update_title(self, chat_id, title):
        with self.conn:
            self.conn.execute("UPDATE chats SET title=? WHERE chat_id=?", (title,chat_id))

    def upsert_user(self, chat_id, user_id, username, display_name):
        with self.conn:
            self.conn.execute("INSERT INTO users(chat_id,user_id,username,display_name,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,updated_at=excluded.updated_at", (chat_id,user_id,username,display_name,self.chat_now(chat_id)))

    def settings(self, chat_id):
        return self.conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()

    def set_setting(self, chat_id, key, value):
        if key not in ("min_chars", "daily_limit", "checkin_points"): raise ValueError("invalid setting")
        if not isinstance(value, int) or not 0 <= value <= 1000000: raise ValueError("设置必须是 0 到 1000000 的整数")
        with self.conn: self.conn.execute(f"UPDATE settings SET {key}=? WHERE chat_id=?", (value,chat_id))

    def set_language(self, chat_id, language):
        if language not in ("zh", "en"): raise ValueError("invalid language")
        with self.conn: self.conn.execute("UPDATE settings SET language=? WHERE chat_id=?", (language, chat_id))

    def set_timezone(self, chat_id, timezone):
        try: timezone_for(timezone)
        except Exception as exc: raise ValueError("invalid timezone") from exc
        with self.conn: self.conn.execute("UPDATE settings SET timezone=? WHERE chat_id=?", (timezone, chat_id))

    def local_day(self, chat_id):
        s = self.settings(chat_id)
        return day(s["timezone"] if s else DEFAULT_TIMEZONE, self.now_fn)

    def chat_now(self, chat_id):
        s = self.settings(chat_id)
        return now(s["timezone"] if s else DEFAULT_TIMEZONE, self.now_fn)

    def resolve_alias(self, chat_id, text):
        clean = text.strip()
        if clean.startswith("/"):
            clean = clean.split()[0].split("@",1)[0].lower()
        else: clean = clean.lower()
        for action, names in DEFAULTS.items():
            if clean in [x.lower() for x in names]: return action
        r = self.conn.execute("SELECT action FROM aliases WHERE chat_id=? AND lower(alias)=?", (chat_id,clean)).fetchone()
        return r[0] if r else None

    def add_alias(self, chat_id, action, alias):
        alias = alias.strip().lower()
        if not alias or len(alias)>16 or action not in DEFAULTS: return False
        if alias.startswith("/"): alias = alias.split()[0].split("@",1)[0].lower()
        if alias.lstrip("/") in REGISTERED_COMMANDS: return False
        if any(alias.lower() == x.lower() for xs in DEFAULTS.values() for x in xs): return False
        try:
            with self.conn: self.conn.execute("INSERT INTO aliases(chat_id,action,alias) VALUES(?,?,?)", (chat_id,action,alias))
            return True
        except sqlite3.IntegrityError: return False

    def remove_alias(self, chat_id, alias):
        with self.conn: return self.conn.execute("DELETE FROM aliases WHERE chat_id=? AND lower(alias)=?", (chat_id,alias.lower())).rowcount > 0

    def aliases(self, chat_id): return self.conn.execute("SELECT action,alias FROM aliases WHERE chat_id=? ORDER BY action,alias", (chat_id,)).fetchall()

    def award_chat(self, chat_id, user_id, username, display_name, text):
        s = self.conn.execute("SELECT c.enabled,s.min_chars,s.daily_limit,s.timezone FROM chats c JOIN settings s ON s.chat_id=c.chat_id WHERE c.chat_id=?", (chat_id,)).fetchone()
        if not s or not s[0]: return 0
        count = len(re.sub(r"\s", "", text, flags=re.UNICODE))
        if not count or (s["min_chars"] and count < s["min_chars"]): return 0
        current_day = day(s["timezone"], self.now_fn)
        timestamp = now(s["timezone"], self.now_fn)
        with db_lock, self.conn:
            self.conn.execute("INSERT INTO users(chat_id,user_id,username,display_name,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,updated_at=excluded.updated_at", (chat_id,user_id,username,display_name,timestamp))
            self.conn.execute("INSERT INTO daily(chat_id,user_id,day) VALUES(?,?,?) ON CONFLICT DO NOTHING", (chat_id,user_id,current_day))
            if s["daily_limit"]:
                cursor = self.conn.execute("UPDATE daily SET chat_points=chat_points+1 WHERE chat_id=? AND user_id=? AND day=? AND chat_points<?", (chat_id,user_id,current_day,s["daily_limit"]))
            else:
                cursor = self.conn.execute("UPDATE daily SET chat_points=chat_points+1 WHERE chat_id=? AND user_id=? AND day=?", (chat_id,user_id,current_day))
            add = 1 if cursor.rowcount else 0
            if add: self.conn.execute("UPDATE users SET total_points=total_points+1,updated_at=? WHERE chat_id=? AND user_id=?", (timestamp,chat_id,user_id))
            return add

    def checkin(self, chat_id, user_id, username, display_name):
        s = self.conn.execute("SELECT c.enabled,s.checkin_points,s.timezone FROM chats c JOIN settings s ON s.chat_id=c.chat_id WHERE c.chat_id=?", (chat_id,)).fetchone()
        if not s or not s[0]: return (False,0,0)
        points = s["checkin_points"]
        current_day = day(s["timezone"], self.now_fn)
        timestamp = now(s["timezone"], self.now_fn)
        with db_lock, self.conn:
            self.conn.execute("INSERT INTO users(chat_id,user_id,username,display_name,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,updated_at=excluded.updated_at", (chat_id,user_id,username,display_name,timestamp))
            self.conn.execute("INSERT INTO daily(chat_id,user_id,day) VALUES(?,?,?) ON CONFLICT DO NOTHING", (chat_id,user_id,current_day))
            cursor = self.conn.execute("UPDATE daily SET checked_in=1,checkin_points=? WHERE chat_id=? AND user_id=? AND day=? AND checked_in=0", (points,chat_id,user_id,current_day))
            if not cursor.rowcount: return (False,0,self.total(chat_id,user_id))
            self.conn.execute("UPDATE users SET total_points=total_points+?,updated_at=? WHERE chat_id=? AND user_id=?", (points,timestamp,chat_id,user_id))
            return (True,points,self.total(chat_id,user_id))

    def total(self, chat_id, user_id):
        r=self.conn.execute("SELECT total_points FROM users WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone(); return r[0] if r else 0

    def score(self, chat_id, user_id):
        u=self.conn.execute("SELECT total_points FROM users WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        d=self.conn.execute("SELECT chat_points,checked_in FROM daily WHERE chat_id=? AND user_id=? AND day=?", (chat_id,user_id,self.local_day(chat_id))).fetchone()
        return (u[0] if u else 0, d[0] if d else 0, bool(d and d[1]))

    def find_user(self, chat_id, query):
        if str(query).isdigit(): return self.conn.execute("SELECT * FROM users WHERE chat_id=? AND user_id=?", (chat_id,int(query))).fetchone()
        q=query.lstrip("@").lower(); rows=self.conn.execute("SELECT * FROM users WHERE chat_id=? AND lower(username)=?", (chat_id,q)).fetchall()
        return rows[0] if len(rows)==1 else (None if not rows else "conflict")

    def _adjust(self, chat_id, operator_id, target_id, delta, method, reason="", reverts_id=None):
        if not 1 <= abs(delta) <= 1000000: raise ValueError("数量必须是 1 到 1000000 的整数")
        if not self.authorized(chat_id): raise ValueError("本群尚未激活积分功能")
        r=self.conn.execute("SELECT total_points FROM users WHERE chat_id=? AND user_id=?",(chat_id,target_id)).fetchone()
        if not r: raise ValueError("用户需要先在群里发言")
        before=r[0]; after=before+delta
        if after<0: raise ValueError("积分不能低于 0")
        self.conn.execute("UPDATE users SET total_points=?,updated_at=? WHERE chat_id=? AND user_id=?",(after,self.chat_now(chat_id),chat_id,target_id))
        self.conn.execute("INSERT INTO adjustments(chat_id,operator_id,target_id,delta,before_points,after_points,method,created_at,reason,reverts_id) VALUES(?,?,?,?,?,?,?,?,?,?)",(chat_id,operator_id,target_id,delta,before,after,method,self.chat_now(chat_id),reason[:200],reverts_id))
        return before,after

    def adjust(self, chat_id, operator_id, target_id, delta, method, reason=""):
        with db_lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            return self._adjust(chat_id,operator_id,target_id,delta,method,reason)

    def undo_adjustment(self, chat_id, operator_id, adjustment_id):
        with db_lock, self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row=self.conn.execute("SELECT target_id,delta,method,reverts_id FROM adjustments WHERE id=? AND chat_id=?",(adjustment_id,chat_id)).fetchone()
            if not row: raise ValueError("找不到该调整记录")
            if row["reverts_id"] is not None or row["method"] in ("undo","import") or self.conn.execute("SELECT 1 FROM adjustments WHERE reverts_id=?",(adjustment_id,)).fetchone():
                raise ValueError("该记录已撤销或属于不可撤销记录")
            return self._adjust(chat_id,operator_id,row[0],-row[1],"undo",f"撤销调整 #{adjustment_id}",adjustment_id)

    def recent(self, chat_id, user_id, page=0, size=20):
        return self.conn.execute("SELECT * FROM adjustments WHERE chat_id=? AND target_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (chat_id,user_id,size,page*size)).fetchall()

    def stats(self, chat_id, today=False):
        if today: return self.conn.execute("SELECT u.user_id,u.display_name,u.username,d.chat_points+d.checkin_points AS points FROM users u JOIN daily d ON d.chat_id=u.chat_id AND d.user_id=u.user_id WHERE u.chat_id=? AND d.day=? ORDER BY points DESC LIMIT 20", (chat_id,self.local_day(chat_id))).fetchall()
        return self.conn.execute("SELECT user_id,display_name,username,total_points AS points FROM users WHERE chat_id=? ORDER BY points DESC LIMIT 20", (chat_id,)).fetchall()

    def ranking(self, chat_id, today=False, page=0, size=15):
        offset = max(0, page) * size
        if today:
            current_day = self.local_day(chat_id)
            rows = self.conn.execute("SELECT u.user_id,u.display_name,u.username,d.chat_points+d.checkin_points AS points FROM users u JOIN daily d ON d.chat_id=u.chat_id AND d.user_id=u.user_id WHERE u.chat_id=? AND d.day=? ORDER BY points DESC,u.user_id LIMIT ? OFFSET ?", (chat_id,current_day,size,offset)).fetchall()
            total = self.conn.execute("SELECT COUNT(*) FROM daily WHERE chat_id=? AND day=?", (chat_id,current_day)).fetchone()[0]
        else:
            rows = self.conn.execute("SELECT user_id,display_name,username,total_points AS points FROM users WHERE chat_id=? ORDER BY points DESC,user_id LIMIT ? OFFSET ?", (chat_id,size,offset)).fetchall()
            total = self.conn.execute("SELECT COUNT(*) FROM users WHERE chat_id=?", (chat_id,)).fetchone()[0]
        return rows, total

    def export_chat(self, chat_id):
        """Return a portable JSON snapshot of one group's points data."""
        settings = self.settings(chat_id)
        if not settings: raise ValueError("群组不存在")
        users = [dict(r) for r in self.conn.execute("SELECT user_id,username,display_name,total_points,updated_at FROM users WHERE chat_id=?", (chat_id,))]
        daily = [dict(r) for r in self.conn.execute("SELECT user_id,day,chat_points,checked_in,checkin_points FROM daily WHERE chat_id=?", (chat_id,))]
        return json.dumps({"version": 1, "chat_id": chat_id, "settings": dict(settings), "users": users, "daily": daily}, ensure_ascii=False, indent=2)

    def import_chat(self, chat_id, operator_id, payload, source="json"):
        """Merge a snapshot and leave an auditable import record."""
        if isinstance(payload, str) and len(payload.encode("utf-8")) > 10 * 1024 * 1024: raise ValueError("导入文件过大")
        data = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(data, dict) or data.get("chat_id") != chat_id or not isinstance(data.get("users", []), list): raise ValueError("导入文件不是本群数据，已取消")
        if not isinstance(data.get("daily", []), list): raise ValueError("导入文件中的每日记录格式无效")
        def integer(value, maximum=9223372036854775807):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("导入文件中的数值必须是有效的非负整数")
        seen=set()
        for u in data["users"]:
            if not isinstance(u, dict) or "user_id" not in u: raise ValueError("导入文件中的成员格式无效")
            integer(u["user_id"]); integer(u.get("total_points", 0))
            if u["user_id"] in seen: raise ValueError("导入文件包含重复成员")
            seen.add(u["user_id"])
            if not u["user_id"]: raise ValueError("导入文件中的成员 ID 无效")
            if u.get("username") is not None and not isinstance(u["username"], str): raise ValueError("导入文件中的用户名无效")
            if u.get("display_name") is not None and not isinstance(u["display_name"], str): raise ValueError("导入文件中的昵称无效")
            if u.get("updated_at"):
                try: datetime.fromisoformat(u["updated_at"])
                except (TypeError, ValueError): raise ValueError("导入文件中的更新时间无效")
        for d in data.get("daily", []):
            if not isinstance(d, dict) or "user_id" not in d or "day" not in d: raise ValueError("导入文件中的每日记录格式无效")
            integer(d["user_id"]); integer(d.get("chat_points", 0)); integer(d.get("checkin_points", 0), 1000000)
            if type(d.get("checked_in", 0)) not in (int, bool) or d.get("checked_in", 0) not in (0, 1): raise ValueError("导入文件中的签到状态无效")
            try:
                if not isinstance(d["day"], str) or datetime.strptime(d["day"], "%Y-%m-%d").strftime("%Y-%m-%d") != d["day"]: raise ValueError()
            except (TypeError, ValueError): raise ValueError("导入文件中的日期无效")
            if not d.get("checked_in", 0) and d.get("checkin_points", 0): raise ValueError("未签到的记录不能包含签到奖励")
        count = 0
        with db_lock, self.conn:
            settings = data.get("settings")
            if isinstance(settings, dict):
                values = [settings.get(key, default) for key, default in (("min_chars", 5), ("daily_limit", 20), ("checkin_points", 5))]
                for value in values: integer(value, 1000000)
                if any(value < 0 or value > 1000000 for value in values): raise ValueError("导入文件中的设置值无效")
                language = settings.get("language", "zh"); timezone = settings.get("timezone", DEFAULT_TIMEZONE)
                if language not in ("zh", "en"): raise ValueError("导入文件中的语言设置无效")
                try: timezone_for(timezone)
                except (TypeError, ValueError, KeyError): raise ValueError("导入文件中的时区无效")
                self.conn.execute("UPDATE settings SET min_chars=?,daily_limit=?,checkin_points=?,language=?,timezone=? WHERE chat_id=?", (*values, language, timezone, chat_id))
            for u in data["users"]:
                uid = int(u["user_id"]); total = int(u.get("total_points", 0))
                if uid <= 0 or total < 0: raise ValueError("导入文件中的成员积分无效")
                before=self.total(chat_id,uid)
                if total!=before:
                    self.conn.execute("INSERT INTO adjustments(chat_id,operator_id,target_id,delta,before_points,after_points,method,created_at,reason) VALUES(?,?,?,?,?,?, 'import',?,?)",(chat_id,operator_id,uid,total-before,before,total,self.chat_now(chat_id),"数据导入"))
                self.conn.execute("INSERT INTO users(chat_id,user_id,username,display_name,total_points,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,total_points=excluded.total_points,updated_at=excluded.updated_at", (chat_id,uid,u.get("username"),u.get("display_name") or str(uid),total,u.get("updated_at") or self.chat_now(chat_id))); count += 1
            for d in data.get("daily", []):
                uid = int(d["user_id"]); chat_points = int(d.get("chat_points", 0)); checkin_points = int(d.get("checkin_points", 0))
                if uid <= 0 or chat_points < 0 or checkin_points < 0 or checkin_points > 1000000:
                    raise ValueError("导入文件中的每日积分记录无效")
                if not self.conn.execute("SELECT 1 FROM users WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone():
                    raise ValueError("导入文件中的每日记录找不到对应成员")
                self.conn.execute("INSERT INTO daily(chat_id,user_id,day,chat_points,checked_in,checkin_points) VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id,user_id,day) DO UPDATE SET chat_points=excluded.chat_points,checked_in=excluded.checked_in,checkin_points=excluded.checkin_points", (chat_id,uid,d["day"],chat_points,int(bool(d.get("checked_in",0))),checkin_points))
            self.conn.execute("INSERT INTO data_imports(chat_id,operator_id,imported_at,user_count,source) VALUES(?,?,?,?,?)", (chat_id,operator_id,self.chat_now(chat_id),count,source))
        return count

    def preview_import(self, chat_id, operator_id, payload):
        clone=Store(":memory:", now_fn=self.now_fn)
        try:
            self.conn.backup(clone.conn)
            clone.import_chat(chat_id,operator_id,payload)
            before={r["user_id"]: r["total_points"] for r in self.conn.execute("SELECT * FROM users WHERE chat_id=?",(chat_id,))}
            data=json.loads(payload) if isinstance(payload,str) else payload
            selected={u["user_id"] for u in data["users"]}
            after={r["user_id"]: r for r in clone.conn.execute("SELECT * FROM users WHERE chat_id=?",(chat_id,)) if r["user_id"] in selected}
            new=sum(uid not in before for uid in after)
            changed=[(uid,r["total_points"]-before.get(uid,0),list_name(r)) for uid,r in after.items() if r["total_points"]!=before.get(uid,0)]
            existing_changes=sum(uid in before for uid,_,_ in changed)
            return {"new":new,"changed":existing_changes,"unchanged":len(after)-new-existing_changes,"delta":sum(d for _,d,_ in changed),"largest":sorted(changed,key=lambda r:abs(r[1]),reverse=True)[:5]}
        finally: clone.close()

    def backup_before_import(self, chat_id):
        from pathlib import Path
        directory=Path(self.path).resolve().parent / "backups"
        directory.mkdir(exist_ok=True)
        target=directory / f"pre-import-{chat_id}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
        self.backup(str(target))
        return target

    def import_history(self, chat_id, limit=20):
        return self.conn.execute("SELECT * FROM data_imports WHERE chat_id=? ORDER BY id DESC LIMIT ?", (chat_id,limit)).fetchall()

    def backup(self, path):
        target = sqlite3.connect(path)
        try:
            with db_lock:
                self.conn.backup(target)
        finally:
            target.close()

store = Store()


_admin_cache = {}


async def admin_level(update, chat_id=None):
    uid=update.effective_user.id; chat_id=chat_id or update.effective_chat.id
    if uid == OWNER_ID: return 3
    key=(chat_id,uid)
    cached=_admin_cache.get(key)
    if cached and cached[0] > time.monotonic(): return cached[1]
    level=0
    try:
        member = await update.get_bot().get_chat_member(chat_id,uid)
        if member.status == "creator": level=3
        elif member.status == "administrator": level=2 if member.can_promote_members else 1
    except Exception: return 0
    _admin_cache[key]=(time.monotonic()+60,level)
    return level


async def is_admin(update, chat_id=None, minimum=1):
    return await admin_level(update, chat_id) >= minimum


def who(u):
    if hasattr(u, "full_name"):
        return "@" + u.username if u.username else u.full_name
    return "@" + u["username"].lstrip("@") if u["username"] else u["display_name"]


def list_name(u):
    """Non-mentioning identity for rankings and audit lists."""
    if hasattr(u, "full_name"):
        name = u.full_name or u.username or str(u.id)
    else:
        name = u["display_name"] or u["username"] or str(u["user_id"])
    return name.replace("@", "＠")


def list_identity(cid, uid):
    u = store.find_user(cid, str(uid))
    return f"{list_name(u)} (ID: {uid})" if u and u != "conflict" else f"ID: {uid}"


def addressed_to_other_bot(text, bot_username):
    command = (text or "").split(None, 1)[0] if (text or "").strip() else ""
    return command.startswith("/") and "@" in command and command.split("@", 1)[1].casefold() != (bot_username or "").casefold()


async def reject_sender(update, management=False):
    m = update.effective_message
    if m and getattr(m, "sender_chat", None):
        if management:
            await m.reply_text(tr(update.effective_chat.id, "请关闭匿名后再操作", "Please disable anonymous mode before continuing"))
        return True
    u = update.effective_user
    return not u or getattr(u, "is_bot", False)


def member_identity(cid, uid):
    u = store.find_user(cid, str(uid))
    return f"{who(u)} (ID: {uid})" if u and u != "conflict" else f"ID: {uid}"


def readable_time(value):
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")


def adjustment_summary(cid, uid, delta, reason=""):
    u = store.find_user(cid, str(uid))
    before = u["total_points"]
    return tr(cid,
        f"确认调整积分\n成员：{member_identity(cid, uid)}\n调整：{delta:+d} 分\n总积分：{before} → {before + delta}\n原因：{reason or '未填写'}",
        f"Confirm point adjustment\nMember: {member_identity(cid, uid)}\nChange: {delta:+d} points\nTotal points: {before} → {before + delta}\nReason: {reason or 'Not provided'}")


def adjustment_history(cid, uid, page=0):
    rows = store.recent(cid, uid, page, 5)
    blocks = []
    for r in rows:
        methods = {"reply": ("回复消息", "Message reply"), "username": ("成员查找", "Member lookup"),
                   "后台": ("私聊管理", "Private management"), "undo": ("撤销调整", "Reversal")}
        method = methods.get(r["method"], (r["method"], r["method"]))
        blocks.append(tr(cid,
            f"记录 #{r['id']} · {readable_time(r['created_at'])} · {r['delta']:+d} 分\n总积分：{r['before_points']} → {r['after_points']}\n操作者：{list_identity(cid, r['operator_id'])}\n方式：{method[0]}\n原因：{r['reason'] or '未填写'}",
            f"Entry #{r['id']} · {readable_time(r['created_at'])} · {r['delta']:+d} points\nTotal points: {r['before_points']} → {r['after_points']}\nBy: {list_identity(cid, r['operator_id'])}\nVia: {method[1]}\nReason: {r['reason'] or 'Not provided'}"))
    return tr(cid, f"积分调整记录\n成员：{list_identity(cid, uid)}\n第 {page + 1} 页",
              f"Point adjustment history\nMember: {list_identity(cid, uid)}\nPage {page + 1}") + "\n\n" + (
        "\n\n".join(blocks) or tr(cid, "该成员暂无积分调整记录。", "No point adjustments for this member yet."))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m=update.effective_message
    if not m: return
    if addressed_to_other_bot(m.text, context.bot.username): return
    if await reject_sender(update, store.resolve_alias(update.effective_chat.id, m.text or "") in ("addpoints", "subpoints")): return
    u=m.from_user
    if update.effective_chat.type == "private": return await private_text(update,context)
    cid=update.effective_chat.id
    action=store.resolve_alias(cid,m.text or "")
    if action and not store.authorized(cid): return
    if action == "checkin":
        ok,pts,total=store.checkin(cid,u.id,u.username,u.full_name)
        await m.reply_text(tr(cid, f"{who(u)}，签到成功！\n本次获得：{pts} 分\n总积分：{total} 分", f"{who(u)}, check-in complete!\nReward: {pts} points\nTotal: {total} points") if ok else tr(cid,f"{who(u)}，今天已签到，明天再来吧。",f"{who(u)}, you have already checked in today. Come back tomorrow."))
    elif action == "score": await score_cmd(update,context)
    elif action in ("rank", "today"): await rank_cmd(update,context,action == "today")
    elif action in ("addpoints","subpoints"): await points_cmd(update,context,action)
    elif not (m.text or "").startswith("/"): store.award_chat(cid,u.id,u.username,u.full_name,m.text)


async def activate(update, context):
    if await reject_sender(update, True): return
    m=update.effective_message
    if update.effective_chat.type == "private": return await m.reply_text("请在群组中使用 /activate 激活。")
    if update.effective_user.id == OWNER_ID:
        code = context.args[0] if context.args else None
        ok=store.authorize(update.effective_chat.id,update.effective_chat.title or str(update.effective_chat.id),code,update.effective_user.id,owner=True)
        return await m.reply_text("本群积分功能已激活。" if ok else "本群已经激活。")
    if not await is_admin(update, minimum=1): return await m.reply_text("只有群主或管理员可以激活本群积分功能。")
    code = context.args[0] if context.args else None
    ok=store.authorize(update.effective_chat.id,update.effective_chat.title or str(update.effective_chat.id),code,update.effective_user.id)
    await m.reply_text("本群积分功能已激活。你绑定的激活码还可用于最多 3 个群。" if ok else ("你已有绑定激活码，请直接发送 /activate；每人最多激活 3 个群。" if not code else "激活码无效、已被其他人使用，或本群已经激活。"))


async def document_handler(update, context):
    if update.effective_chat.type != "private": return
    pending=context.user_data.get("awaiting_import")
    cid=pending["chat_id"] if pending else context.user_data.get("chat_id")
    if not pending or pending["expires_at"] <= time.time():
        context.user_data.pop("awaiting_import",None)
        return await update.effective_message.reply_text("请先点击“导入数据”；导入入口有效期为 10 分钟。",reply_markup=back_keyboard(cid))
    if not cid or not await is_admin(update,cid,2):
        return await update.effective_message.reply_text("权限不足。",reply_markup=back_keyboard())
    document = update.effective_message.document
    if not document.file_name or not document.file_name.lower().endswith(".json"):
        return await update.effective_message.reply_text("只支持 JSON 导入文件。", reply_markup=back_keyboard(cid, f"import:{cid}", "重新导入", "Retry import"))
    try:
        if document.file_size and document.file_size > 10 * 1024 * 1024:
            raise ValueError("导入文件过大")
        payload = (await (await document.get_file()).download_as_bytearray()).decode("utf-8")
        preview = store.preview_import(cid, update.effective_user.id, payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return await update.effective_message.reply_text(f"导入失败：{exc}", reply_markup=back_keyboard(cid, f"import:{cid}", "重新导入", "Retry import"))
    except Exception:
        return await update.effective_message.reply_text("导入失败：无法读取文件。", reply_markup=back_keyboard(cid, f"import:{cid}", "重新导入", "Retry import"))
    markup=confirmation(context,"import",(payload,time.time()+600),cid)
    largest="\n".join(f"{name}: {delta:+d}" for _,delta,name in preview["largest"])
    await update.effective_message.reply_text(f"导入预览 / Import preview\n新增 / New: {preview['new']} · 变化 / Changed: {preview['changed']} · 不变 / Unchanged: {preview['unchanged']}\n总分差额 / Point change: {preview['delta']:+d}\n{largest}",reply_markup=markup)

async def score_cmd(update, context):
    if await reject_sender(update, False): return
    cid=update.effective_chat.id
    if not store.authorized(cid): return await update.effective_message.reply_text(tr(cid,"本群尚未激活积分功能，请先使用 /activate。","Points are not activated. Use /activate first."))
    t,c,checked=store.score(cid,update.effective_user.id); await update.effective_message.reply_text(tr(cid, f"{who(update.effective_user)} 的积分\n总积分：{t} 分\n今日聊天积分：{c} 分\n今日签到：{'已签到' if checked else '未签到'}", f"{who(update.effective_user)}'s points\nTotal points: {t}\nToday's chat points: {c}\nToday's check-in: {'Complete' if checked else 'Not checked in'}"))


async def rank_cmd(update, context, today=False, page=0):
    if await reject_sender(update, False): return
    cid=update.effective_chat.id
    if not store.authorized(cid): return await update.effective_message.reply_text(tr(cid,"本群尚未激活积分功能，请先使用 /activate。","Points are not activated. Use /activate first."))
    rows,total=store.ranking(cid,today,page,15); english=store.settings(cid)["language"] == "en"; title=("Today's ranking" if today else "All-time ranking") if english else ("今日积分排名" if today else "累计积分排名")
    start=page*15; body="\n".join(f"{start+i+1}. {list_name(r)} · {r['points']} " + tr(cid, "分", "points") for i,r in enumerate(rows)) or tr(cid, "暂无积分记录。", "No points recorded yet.")
    buttons=[]
    if page>0: buttons.append(InlineKeyboardButton("Previous page" if english else "上一页",callback_data=f"rank:{cid}:{int(today)}:{page-1}"))
    if start+len(rows)<total: buttons.append(InlineKeyboardButton("Next page" if english else "下一页",callback_data=f"rank:{cid}:{int(today)}:{page+1}"))
    markup=InlineKeyboardMarkup([buttons]) if buttons else None
    await update.effective_message.reply_text(f"{title} ({page+1}/{max(1,(total+14)//15)})\n{body}",reply_markup=markup)


async def points_cmd(update, context, action=None):
    if await reject_sender(update, True): return
    m=update.effective_message; cid=update.effective_chat.id
    if not store.authorized(cid): return await m.reply_text(tr(cid,"本群尚未激活积分功能，请先使用 /activate。","Points are not activated. Use /activate first."))
    if not await is_admin(update): return await m.reply_text(tr(cid,"只有本群管理员可以调整成员积分。","Only group administrators can adjust member points."))
    action=action or ("addpoints" if m.text.startswith("/addpoints") else "subpoints")
    args=context.args or (m.text.split()[1:] if m.text else []); target=m.reply_to_message.from_user if m.reply_to_message else None
    try: amount=int(args[0])
    except (ValueError,IndexError): return await m.reply_text(tr(cid,f"请输入要{'增加' if action == 'addpoints' else '扣除'}的分数和成员：\n/{action} 10 @username\n也可以回复成员消息后发送 /{action} 10。",f"Usage: /{action} 10 @username\nOr reply to a member's message with /{action} 10."))
    if not 1<=amount<=1000000: return await m.reply_text(tr(cid,"数量必须是 1 到 1000000 的整数","Amount must be an integer from 1 to 1,000,000."))
    if not target and len(args)>1:
        found=store.find_user(cid,args[1]);
        if found == "conflict": return await m.reply_text(tr(cid,"用户名匹配到多个成员。","More than one member matches that username."))
        target=found
    if not target: return await m.reply_text(tr(cid,"找不到目标成员；请回复其群消息，或使用已记录的完整用户名。","Member not found. Reply to their message or use a recorded exact username."))
    target_id = target.id if hasattr(target, "id") else target["user_id"]
    target_name = who(target)
    if getattr(target,"is_bot",False): return await m.reply_text(tr(cid,"不能调整机器人积分。","Bot accounts cannot have points adjusted."))
    store.upsert_user(cid, update.effective_user.id, update.effective_user.username, update.effective_user.full_name)
    if hasattr(target, "full_name"):
        store.upsert_user(cid, target_id, target.username, target.full_name)
    try: before,after=store.adjust(cid,update.effective_user.id,target_id,(amount if action=="addpoints" else -amount),"reply" if m.reply_to_message else "username")
    except ValueError as e: return await m.reply_text(tr_error(cid, str(e)))
    delta_text = f"{'+' if action=='addpoints' else '-'}{amount}"
    await m.reply_text(tr(cid, f"已为 {target_name} {'增加' if action == 'addpoints' else '扣除'} {amount} 分\n总积分：{before} → {after} 分", f"{target_name}: {delta_text} points applied\nTotal points: {before} → {after}"))


async def start(update, context):
    if await reject_sender(update, True): return
    if update.effective_chat.type != "private": return await update.effective_message.reply_text("请私聊机器人打开群组管理，查看积分、统计和规则。")
    clear_management_input(context)
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("请选择要管理的群组：" if await has_any_admin(update) else "你不是任何已激活群组的管理员。", reply_markup=await group_keyboard(update))
    await update.effective_message.reply_text("总管理员：管理群组和激活权限", reply_markup=owner_home_keyboard())


async def has_any_admin(update):
    for c in store.chats():
        if await is_admin(update,c["chat_id"]): return True
    return False


async def group_keyboard(update, owner=False, page=0):
    all_chats = store.chats(enabled_only=True)
    if not owner:
        all_chats = [c for c in all_chats if await is_admin(update, c["chat_id"])]
    page_size = 8
    page_count = max(1, (len(all_chats) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    rows=[]
    visible = all_chats[page * page_size:(page + 1) * page_size]
    for c in visible:
        rows.append([InlineKeyboardButton(c["title"],callback_data=f"g:{c['chat_id']}")])
    if not rows: rows.append([InlineKeyboardButton("暂无可管理群组",callback_data="noop")])
    if page_count > 1:
        nav=[]
        if page > 0: nav.append(InlineKeyboardButton("上一页",callback_data=f"home:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{page_count}",callback_data="noop"))
        if page + 1 < page_count: nav.append(InlineKeyboardButton("下一页",callback_data=f"home:{page+1}"))
        rows.append(nav)
    if owner:
        rows.append([InlineKeyboardButton("查找群组",callback_data="ownersearch")])
        rows.append([InlineKeyboardButton("返回上一级",callback_data="home")])
    return InlineKeyboardMarkup(rows)


def owner_authorized_keyboard(page=0):
    chats=store.chats(enabled_only=True); size=8; pages=max(1,(len(chats)+size-1)//size); page=max(0,min(page,pages-1)); rows=[]
    for c in chats[page*size:(page+1)*size]: rows.append([InlineKeyboardButton(c["title"],callback_data=f"revokeask:{c['chat_id']}:owner")])
    if not rows: rows=[[InlineKeyboardButton("暂无已激活群组",callback_data="noop")]]
    if pages>1:
        nav=[]
        if page: nav.append(InlineKeyboardButton("上一页",callback_data=f"owner_auth_page:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="noop"))
        if page+1<pages: nav.append(InlineKeyboardButton("下一页",callback_data=f"owner_auth_page:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("查找群组",callback_data="ownersearch_auth"),InlineKeyboardButton("返回上一级",callback_data="home")])
    return InlineKeyboardMarkup(rows)


def owner_home_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("我的群组",callback_data="ownergroups")],[InlineKeyboardButton("已激活群组",callback_data="ownerauthorized")],[InlineKeyboardButton("生成激活码",callback_data="code"),InlineKeyboardButton("激活码列表",callback_data="codes")]])


def back_keyboard(cid=None, parent=None, zh="返回上级菜单", en="Back"):
    rows = []
    if parent:
        rows.append([InlineKeyboardButton(tr(cid, zh, en), callback_data=parent)])
    if cid and parent != f"g:{cid}":
        rows.append([InlineKeyboardButton(tr(cid, "返回群组管理", "Back to group"), callback_data=f"g:{cid}")])
    if not rows:
        rows.append([InlineKeyboardButton("返回主菜单", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def clear_management_input(context):
    for key in ("state", "edit", "custom", "reason_pending", "pending", "pending_setting", "pending_token", "awaiting_import"):
        context.user_data.pop(key, None)


async def group_page(update, chat_id):
    level=await admin_level(update,chat_id)
    if level < 1: return None
    uid=update.effective_user.id; english=store.settings(chat_id)["language"] == "en"; label=lambda zh,en: en if english else zh
    if english:
        kb=[[InlineKeyboardButton("Member points",callback_data=f"search:{chat_id}")],[InlineKeyboardButton("Today's ranking",callback_data=f"stat:{chat_id}:1")],[InlineKeyboardButton("All-time ranking",callback_data=f"stat:{chat_id}:0")]]
    else:
        kb=[[InlineKeyboardButton("成员积分",callback_data=f"search:{chat_id}")],[InlineKeyboardButton("今日排名",callback_data=f"stat:{chat_id}:1")],[InlineKeyboardButton("累计排名",callback_data=f"stat:{chat_id}:0")]]
    if level >= 2:
        kb.append([InlineKeyboardButton(label("导出数据","Export data"),callback_data=f"export:{chat_id}"),InlineKeyboardButton(label("导入数据","Import data"),callback_data=f"import:{chat_id}")])
        kb.append([InlineKeyboardButton(label("导入记录","Import history"),callback_data=f"imports:{chat_id}")])
        kb.append([InlineKeyboardButton(label("积分规则","Point rules"),callback_data=f"set:{chat_id}")])
        kb.append([InlineKeyboardButton(label("自定义命令","Custom commands"),callback_data=f"alias:{chat_id}")])
    if level >= 2:
        kb.append([InlineKeyboardButton(label("切换为 English","切换为中文"),callback_data=f"lang:{chat_id}:{'zh' if english else 'en'}"),InlineKeyboardButton(label("时区","Timezone"),callback_data=f"tzlist:{chat_id}")])
    if uid == OWNER_ID and store.authorized(chat_id): kb.append([InlineKeyboardButton(label("停用积分","Deactivate points"),callback_data=f"revokeask:{chat_id}")])
    kb.append([InlineKeyboardButton(label("返回群组列表","Back to groups"),callback_data="home:0")])
    chat = store.conn.execute("SELECT title FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    s = store.settings(chat_id)
    return label(f"{chat['title']}\n群组积分管理\n每日重置时区：{s['timezone']}", f"{chat['title']}\nGroup point management\nDaily reset timezone: {s['timezone']}"), InlineKeyboardMarkup(kb)


# Prefix -> (required role, route). Parsing is centralized before dispatch.
CALLBACK_ROLES = {
    **{p: "public" for p in ("noop", "home")},
    **{p: "owner" for p in ("ownergroups", "ownerauthorized", "owner_auth_page", "ownersearch", "ownersearch_auth", "code", "codes", "revokeask", "revokeyes")},
    **{p: "admin1" for p in ("g", "search", "member", "adjust", "recent", "undo_recent", "undo_confirm", "stat", "custom")},
    **{p: "admin2" for p in ("set", "edit", "alias", "addalias", "delalias", "lang", "tz", "tzlist", "export", "import", "imports")},
    "rank": "member", "confirm": "pending", "cancel": "pending",
}
CALLBACK_PATTERNS = {
    "home": r"home(?::[0-9]+)?", "owner_auth_page": r"owner_auth_page:[0-9]+",
    **{p: p+r":-?[0-9]+" for p in ("g","search","set","alias","addalias","tzlist","export","import","imports")},
    "member": r"member:-?[0-9]+:[0-9]+", "recent": r"recent:-?[0-9]+:[0-9]+(?::[0-9]+)?",
    **{p: p+r":-?[0-9]+:[0-9]+:[0-9]+" for p in ("undo_recent","undo_confirm")},
    "adjust": r"adjust:-?[0-9]+:[0-9]+:-?[0-9]+", "custom": r"custom:-?[0-9]+:[0-9]+:-?1",
    "stat": r"stat:-?[0-9]+:[01]", "rank": r"rank:-?[0-9]+:[01]:[0-9]+",
    "edit": r"edit:-?[0-9]+:(min_chars|daily_limit|checkin_points)",
    "lang": r"lang:-?[0-9]+:(zh|en|toggle)", "tz": r"tz:-?[0-9]+:.+",
    "delalias": r"delalias:-?[0-9]+:.+",
    **{p: p+r":-?[0-9]+(?::(?:owner|group))?" for p in ("revokeask","revokeyes")},
    "confirm": r"confirm:[a-f0-9]{8}", "cancel": r"cancel:[a-f0-9]{8}",
}


def confirmation(context, kind, payload, cid):
    clear_management_input(context)
    token=secrets.token_hex(4)
    context.user_data["pending_token"]={"token": token, "kind": kind, "payload": payload, "chat_id": cid}
    return InlineKeyboardMarkup([[InlineKeyboardButton(tr(cid,"确认","Confirm"),callback_data=f"confirm:{token}"), InlineKeyboardButton(tr(cid,"取消","Cancel"),callback_data=f"cancel:{token}")]])


async def callback(update, context):
    q=update.callback_query
    data=q.data
    prefix=data.split(":",1)[0] if isinstance(data,str) else ""
    pending=context.user_data.get("pending_token")
    if prefix in ("confirm","cancel","save_setting","cancel_setting"):
        if not pending or data not in (f"confirm:{pending['token']}",f"cancel:{pending['token']}"):
            return await q.answer("按钮已失效，请重新操作 / This button has expired", show_alert=True)
        role="admin2" if pending["kind"] in ("setting","import") else "admin1"
        cid=pending["chat_id"]
        if not await is_admin(update,cid,2 if role=="admin2" else 1):
            return await q.answer("权限不足 / Permission denied",show_alert=True)
        # Consume before the next await so double clicks cannot execute twice.
        context.user_data.pop("pending_token",None)
        await q.answer()
        if prefix=="cancel":
            return await q.edit_message_text(tr(cid,"已取消操作。","Operation cancelled."),reply_markup=back_keyboard(cid))
        kind=pending["kind"]; payload=pending["payload"]
        if kind=="setting":
            try: store.set_setting(cid,*payload)
            except ValueError as exc: return await q.edit_message_text(tr_error(cid,str(exc)),reply_markup=back_keyboard(cid))
            return await settings_page(q,cid)
        if kind=="import":
            payload,expires=payload
            if expires<=time.time(): return await q.edit_message_text("导入已过期，请重新操作。",reply_markup=back_keyboard(cid))
            try:
                backup=store.backup_before_import(cid)
                count=store.import_chat(cid,q.from_user.id,payload,"button")
            except (ValueError,sqlite3.Error,OSError) as exc:
                return await q.edit_message_text(f"导入失败：{exc}",reply_markup=back_keyboard(cid))
            return await q.edit_message_text(f"导入成功 / Import complete: {count}\n备份 / Backup: {backup.name}",reply_markup=back_keyboard(cid))
        if kind=="undo":
            us,aid=payload
            try: store.undo_adjustment(cid,q.from_user.id,aid)
            except ValueError as exc: return await q.edit_message_text(str(exc),reply_markup=back_keyboard(cid))
            return await member_page(q,context,cid,us,tr(cid,"已撤销记录。","Entry undone."))
        if kind=="adjust":
            tid,delta,reason=payload
            store.upsert_user(cid,q.from_user.id,q.from_user.username,q.from_user.full_name)
            try: before,after=store.adjust(cid,q.from_user.id,tid,delta,"后台",reason)
            except ValueError as exc: return await q.edit_message_text(tr_error(cid,str(exc)),reply_markup=back_keyboard(cid))
            return await member_page(q,context,cid,tid,tr(cid,f"已调整：{before} → {after}",f"Updated: {before} → {after}"))
    route=CALLBACK_ROUTES.get(prefix)
    if not route or not re.fullmatch(CALLBACK_PATTERNS.get(prefix,re.escape(prefix)),data):
        return await q.answer("无效操作 / Invalid operation",show_alert=True)
    role,handler=route
    if role=="owner" and q.from_user.id!=OWNER_ID:
        return await q.answer("权限不足 / Permission denied",show_alert=True)
    if role in ("admin1","admin2","member"):
        cid=int(data.split(":")[1])
        if not store.settings(cid): return await q.answer("无效操作 / Invalid operation",show_alert=True)
        if role=="member":
            if update.effective_chat.type!="private":
                if update.effective_chat.id!=cid: return await q.answer("无效操作 / Invalid operation",show_alert=True)
            else:
                try:
                    member=await update.get_bot().get_chat_member(cid,q.from_user.id)
                    allowed=member.status in ("member","administrator","creator") or (member.status=="restricted" and member.is_member)
                except Exception: allowed=False
                if not allowed: return await q.answer("仅群成员可查看 / Members only",show_alert=True)
        elif not await is_admin(update,cid,2 if role=="admin2" else 1):
            return await q.answer("权限不足 / Permission denied",show_alert=True)
        if update.effective_chat.type=="private": context.user_data["chat_id"]=cid
    clear_management_input(context)
    await q.answer()
    try:
        return await handler(update,context)
    except (ValueError, KeyError, TypeError, IndexError, OverflowError):
        return await q.answer("无效操作 / Invalid operation",show_alert=True)


async def callback_action(update, context):
    q=update.callback_query; uid=q.from_user.id; data=q.data
    if data=="noop": return
    if data.startswith("home:"):
        return await q.edit_message_text("选择群组",reply_markup=await group_keyboard(update,uid==OWNER_ID,int(data.split(":",1)[1])))
    if data == "ownergroups":
        if uid != OWNER_ID: return await q.edit_message_text("只有拥有者可以操作。")
        return await q.edit_message_text("我的群组",reply_markup=await group_keyboard(update,True))
    if data == "ownerauthorized":
        if uid != OWNER_ID: return await q.edit_message_text("只有拥有者可以操作。")
        return await q.edit_message_text("已激活群组：选择要停用的群组",reply_markup=owner_authorized_keyboard(0))
    if data.startswith("owner_auth_page:"):
        if uid != OWNER_ID: return
        return await q.edit_message_text("已激活群组：选择要停用的群组",reply_markup=owner_authorized_keyboard(int(data.split(":",1)[1])))
    if data == "ownersearch":
        if uid != OWNER_ID: return await q.edit_message_text("只有拥有者可以搜索群组。")
        context.user_data["state"]="owner_search"
        return await q.edit_message_text("请输入完整群组 ID 或名称关键词来查找群组：", reply_markup=back_keyboard(parent="ownergroups"))
    if data == "ownersearch_auth":
        if uid != OWNER_ID: return
        context.user_data["state"]="owner_auth_search"
        return await q.edit_message_text("请输入要停用的群组 ID 或名称关键词：", reply_markup=back_keyboard(parent="ownerauthorized"))
    if data=="code":
        if uid!=OWNER_ID:return
        return await q.edit_message_text("新激活码（首次使用后绑定使用者，最多可激活 3 个群）：\n"+store.generate_code(), reply_markup=back_keyboard())
    if data=="codes":
        if uid!=OWNER_ID:return
        rows=store.unused_codes(); return await q.edit_message_text("未使用的激活码记录\n编号 · 创建时间\n"+("\n".join(f"{r['hash']} · {readable_time(r['created_at'])}" for r in rows) if rows else "暂无未使用的激活码。"), reply_markup=back_keyboard())
    if data.startswith("g:"):
        cid=int(data[2:]); page=await group_page(update,cid)
        if not page: return await q.edit_message_text("权限已变化，请重新进入。", reply_markup=back_keyboard())
        context.user_data["chat_id"]=cid
        return await q.edit_message_text(page[0],reply_markup=page[1])
    if data.startswith("export:"):
        cid=int(data[7:]); payload=store.export_chat(cid)
        from telegram import InputFile
        import io
        return await q.message.reply_document(InputFile(io.BytesIO(payload.encode()), filename=f"points-{cid}.json"), reply_markup=back_keyboard(cid))
    if data.startswith("import:"):
        cid=int(data[7:]); context.user_data["chat_id"]=cid
        context.user_data["awaiting_import"]={"chat_id":cid,"expires_at":time.time()+600}
        return await q.edit_message_text(tr(cid,"请把导出的 JSON 文件发送到这里。机器人会核对文件绑定的群组 ID。","Send the exported JSON file here. The bound group ID will be checked."), reply_markup=back_keyboard(cid))
    if data.startswith("imports:"):
        cid=int(data[8:]); rows=store.import_history(cid)
        sources={"button": ("管理菜单导入", "Management menu"), "json": ("JSON 数据导入", "JSON import")}
        body="\n\n".join(tr(cid, f"导入 #{r['id']} · {readable_time(r['imported_at'])}\n操作者：{member_identity(cid, r['operator_id'])}\n导入成员：{r['user_count']} 人\n来源：{sources.get(r['source'], (r['source'], r['source']))[0]}", f"Import #{r['id']} · {readable_time(r['imported_at'])}\nBy: {member_identity(cid, r['operator_id'])}\nMembers imported: {r['user_count']}\nSource: {sources.get(r['source'], (r['source'], r['source']))[1]}") for r in rows) or tr(cid, "本群暂无导入记录。", "No imports for this group yet.")
        return await q.edit_message_text(tr(cid,"导入记录：\n"+body,"Import history:\n"+body), reply_markup=back_keyboard(cid))
    if data=="home": return await q.edit_message_text("总管理员" if uid==OWNER_ID else "选择群组",reply_markup=owner_home_keyboard() if uid==OWNER_ID else await group_keyboard(update,False))
    if data.startswith("search:"):
        cid=int(data[7:]); context.user_data["state"]="search"; context.user_data["chat_id"]=cid; return await q.edit_message_text(tr(cid,"请输入成员的 @用户名或数字 ID，例如 @alice。","Enter the member's @username or numeric ID, e.g. @alice."), reply_markup=back_keyboard(cid))
    if data.startswith("member:"):
        _,cs,us=data.split(":"); cs=int(cs); us=int(us)
        if uid!=OWNER_ID and not await is_admin(update,cs): return await q.edit_message_text("权限已变化，请重新进入。")
        return await member_page(q,context,cs,us)
    if data.startswith("adjust:"):
        _,cs,us,delta=data.split(":"); cs=int(cs); us=int(us); delta=int(delta)
        if not store.find_user(cs,us) or not 1<=abs(delta)<=1000000:
            return await q.edit_message_text("无效操作",reply_markup=back_keyboard(cs))
        markup=confirmation(context,"adjust",(us,delta,""),cs)
        return await q.edit_message_text(adjustment_summary(cs,us,delta),reply_markup=markup)
    if data.startswith("recent:"):
        parts=data.split(":"); cs=int(parts[1]); us=int(parts[2]); page=int(parts[3]) if len(parts)>3 else 0
        rows=store.recent(cs, us, page, 5)
        nav=[]
        if page: nav.append(InlineKeyboardButton(tr(cs, "上一页", "Previous"), callback_data=f"recent:{cs}:{us}:{page-1}"))
        if store.recent(cs, us, page+1, 5): nav.append(InlineKeyboardButton(tr(cs, "下一页", "Next"), callback_data=f"recent:{cs}:{us}:{page+1}"))
        undo_buttons = [[InlineKeyboardButton(tr(cs, f"撤销记录 #{r['id']}", f"Undo entry #{r['id']}"), callback_data=f"undo_recent:{cs}:{us}:{r['id']}")] for r in rows if r["method"] not in ("undo","import") and not store.conn.execute("SELECT 1 FROM adjustments WHERE reverts_id=?", (r["id"],)).fetchone()]
        buttons=([nav] if nav else []) + undo_buttons + [[InlineKeyboardButton(tr(cs, "返回成员", "Back to member"), callback_data=f"member:{cs}:{us}")]]
        return await q.edit_message_text(adjustment_history(cs, us, page), reply_markup=InlineKeyboardMarkup(buttons))
    if data.startswith("undo_recent:"):
        _,cs,us,aid=data.split(":"); cs=int(cs); us=int(us); aid=int(aid)
        row=store.conn.execute("SELECT * FROM adjustments WHERE id=? AND chat_id=? AND target_id=?", (aid,cs,us)).fetchone()
        if not row: return await q.edit_message_text("找不到该调整记录。", reply_markup=back_keyboard(cs))
        markup=confirmation(context,"undo",(us,aid),cs)
        return await q.edit_message_text(tr(cs,f"确认撤销记录 #{aid}？\n本次将调整：{-row['delta']:+d} 分",f"Undo entry #{aid}?\nChange: {-row['delta']:+d} points"),reply_markup=markup)
    if data.startswith("undo_confirm:"):
        return await q.answer("按钮已失效，请重新操作 / This button has expired",show_alert=True)
    if data.startswith("stat:"):
        _,cs,today_flag=data.split(":"); cs=int(cs); rows,total=store.ranking(cs,today_flag=="1",0,15)
        title=tr(cs, "今日积分排名" if today_flag=="1" else "累计积分排名", "Today's ranking" if today_flag=="1" else "All-time ranking")
        body="\n".join(f"{i+1}. {list_name(r)} · {r['points']} " + tr(cs, "分", "points") for i,r in enumerate(rows)) or tr(cs, "暂无积分记录。", "No points recorded yet.")
        buttons=[]
        if total > len(rows): buttons.append(InlineKeyboardButton(tr(cs, "查看完整排名", "View full ranking"), callback_data=f"rank:{cs}:{today_flag}:0"))
        buttons.append(InlineKeyboardButton(tr(cs, "返回群组", "Back to group"), callback_data=f"g:{cs}"))
        return await q.edit_message_text(title+"\n\n"+body, reply_markup=InlineKeyboardMarkup([buttons]))
    if data.startswith("rank:"):
        _,cs,today_flag,page=data.split(":"); cs=int(cs); today_flag=bool(int(today_flag)); page=int(page); rows,total=store.ranking(cs,today_flag,page,15); english=store.settings(cs)["language"] == "en"; title=("Today's ranking" if today_flag else "Total ranking") if english else ("单日积分排行" if today_flag else "累计积分排行"); start=page*15; body="\n".join(f"{start+i+1}. {list_name(r)} · {r['points']} " + tr(cs, "分", "points") for i,r in enumerate(rows)) or tr(cs, "暂无积分记录。", "No points recorded yet."); buttons=[]
        if page>0: buttons.append(InlineKeyboardButton("Previous" if english else "上一页",callback_data=f"rank:{cs}:{int(today_flag)}:{page-1}"))
        if start+len(rows)<total: buttons.append(InlineKeyboardButton("Next" if english else "下一页",callback_data=f"rank:{cs}:{int(today_flag)}:{page+1}"))
        rows=[buttons] if buttons else []
        if update.effective_chat.type == "private":
            rows.extend(back_keyboard(cs).inline_keyboard)
        return await q.edit_message_text(f"{title} ({page+1}/{max(1,(total+14)//15)})\n{body}",reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    if data.startswith("set:"):
        cid=int(data[4:]); return await settings_page(q,cid)
    if data.startswith("lang:"):
        _,cs,language=data.split(":"); cid=int(cs)
        if language == "toggle": language="en" if store.settings(cid)["language"] == "zh" else "zh"
        store.set_language(cid,language); page=await group_page(update,cid); return await q.edit_message_text(page[0],reply_markup=page[1])
    if data.startswith("tz:"):
        _,cs,timezone=data.split(":",2); cid=int(cs); store.set_timezone(cid,timezone); page=await group_page(update,cid); return await q.edit_message_text(page[0],reply_markup=page[1])
    if data.startswith("tzlist:"):
        cid=int(data.split(":",1)[1]); s=store.settings(cid); english=s["language"] == "en"
        rows=[[InlineKeyboardButton(TIMEZONE_LABELS[z][1 if english else 0],callback_data=f"tz:{cid}:{z}")] for z in TIMEZONE_OPTIONS]
        rows.append([InlineKeyboardButton("Back to group" if english else "返回群组",callback_data=f"g:{cid}")])
        return await q.edit_message_text("Choose the timezone used for daily reset" if english else "选择每日积分结算使用的时区",reply_markup=InlineKeyboardMarkup(rows))
    if data.startswith("edit:"):
        _,cs,key=data.split(":"); cs=int(cs); context.user_data["state"]="setting"; context.user_data["edit"]=(cs,key); context.user_data["chat_id"]=cs
        zero = {"min_chars": ("0 表示不限制消息长度", "0 means no minimum length"), "daily_limit": ("0 表示不限制每日聊天积分", "0 means unlimited daily chat points"), "checkin_points": ("0 表示签到不奖励积分", "0 means no check-in reward")}[key]
        return await q.edit_message_text(tr(cs, f"{SETTING_LABELS[key][0]}\n请输入 0 到 1000000 的整数，{zero[0]}。", f"{SETTING_LABELS[key][1]}\nEnter an integer from 0 to 1,000,000; {zero[1]}."), reply_markup=back_keyboard(cs, f"set:{cs}", "返回积分规则", "Back to rules"))
    if data.startswith("alias:"):
        cid=int(data[6:]); ars=store.aliases(cid); english=store.settings(cid)["language"] == "en"; kb=[[InlineKeyboardButton((f"Remove shortcut {r['alias']}" if english else f"删除快捷名称 {r['alias']}"),callback_data=f"delalias:{cid}:{alias_key(r['alias'])}")] for r in ars]
        kb.append([InlineKeyboardButton("Add a command shortcut" if english else "添加自定义命令名称",callback_data=f"addalias:{cid}"),InlineKeyboardButton("Back to group" if english else "返回群组",callback_data=f"g:{cid}")])
        body="\n".join(f"{tr(cid, *ACTION_LABELS[r['action']])}：{r['alias']}" for r in ars)
        return await q.edit_message_text(tr(cid,"自定义命令\n\n"+(body or "尚未添加自定义命令。"),"Custom commands\n\n"+(body or "No custom commands yet.")),reply_markup=InlineKeyboardMarkup(kb))
    if data.startswith("addalias:"):
        cid=int(data[9:]); context.user_data["state"]="alias"; context.user_data["chat_id"]=cid
        functions="\n".join(f"{action} — {tr(cid, *labels)}" for action, labels in ACTION_LABELS.items())
        return await q.edit_message_text(tr(cid,"添加自定义命令\n请输入：功能代码 自定义命令\n例如：score /积分\n\n可选功能：\n"+functions,"Add a custom command\nEnter: function code shortcut\nExample: score /points\n\nAvailable functions:\n"+functions), reply_markup=back_keyboard(cid, f"alias:{cid}", "返回自定义命令", "Back to custom commands"))
    if data.startswith("delalias:"):
        _,cs,key=data.split(":",2); cid=int(cs)
        matches=[r["alias"] for r in store.aliases(cid) if alias_key(r["alias"])==key]
        if len(matches)!=1: return await q.answer("按钮已失效，请重新操作",show_alert=True)
        store.remove_alias(cid,matches[0]); english=store.settings(cid)["language"] == "en"; return await q.edit_message_text("Command shortcut removed." if english else "自定义命令已删除。",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to shortcuts" if english else "返回自定义命令",callback_data=f"alias:{cs}")]]))
    if data.startswith("revokeask:"):
        if uid!=OWNER_ID:return
        parts=data.split(":"); cid=int(parts[1]); owner_list=len(parts)>2 and parts[2]=="owner"; chat=store.conn.execute("SELECT title,enabled FROM chats WHERE chat_id=?",(cid,)).fetchone()
        english=bool(chat and store.settings(cid) and store.settings(cid)["language"] == "en")
        if not chat or not chat[1]: return await q.edit_message_text("This group is already inactive." if english else "该群组已经停用。",reply_markup=await group_keyboard(update,True))
        message=(f"Deactivate points for {chat[0]} ({cid})?\nThe bot will stop tracking points in this group." if english else f"确定要停用群组“{chat[0]}”（{cid}）的积分功能吗？停用后机器人将停止统计该群积分。")
        return await q.edit_message_text(message,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm deactivation" if english else "确认停用积分",callback_data=f"revokeyes:{cid}:{'owner' if owner_list else 'group'}"),InlineKeyboardButton("Cancel" if english else "取消",callback_data="ownerauthorized" if owner_list else f"g:{cid}")]]))
    if data.startswith("revokeyes:"):
        if uid!=OWNER_ID:return
        parts=data.split(":"); cid=int(parts[1]); owner_list=len(parts)>2 and parts[2]=="owner"; store.revoke(cid)
        english=bool(store.settings(cid) and store.settings(cid)["language"] == "en")
        return await q.edit_message_text("Group points deactivated." if english else "群组积分功能已停用。",reply_markup=owner_authorized_keyboard(0) if owner_list else await group_keyboard(update,True))
    if data.startswith("custom:"):
        _,cs,us,sign=data.split(":"); cs=int(cs); context.user_data["state"]="custom"; context.user_data["custom"]=(cs,int(us),int(sign)); return await q.edit_message_text(tr(cs,"请输入调整数量（1-1000000）：","Enter an amount (1-1,000,000):"), reply_markup=back_keyboard(cs, f"member:{cs}:{us}", "返回成员积分", "Back to member"))


CALLBACK_ROUTES = {prefix: (role, callback_action) for prefix,role in CALLBACK_ROLES.items()}


async def member_page(q, context, cid, uid, notice=""):
    u=store.find_user(cid,uid); english=store.settings(cid)["language"] == "en"
    if english:
        text=(notice+"\n\n" if notice else "") + f"{member_identity(cid, uid)}\nTotal points: {u['total_points']}"
        kb=[[InlineKeyboardButton("+1 point",callback_data=f"adjust:{cid}:{uid}:1")],[InlineKeyboardButton("+5 points",callback_data=f"adjust:{cid}:{uid}:5")],[InlineKeyboardButton("+10 points",callback_data=f"adjust:{cid}:{uid}:10")],[InlineKeyboardButton("-1 point",callback_data=f"adjust:{cid}:{uid}:-1")],[InlineKeyboardButton("-5 points",callback_data=f"adjust:{cid}:{uid}:-5")],[InlineKeyboardButton("-10 points",callback_data=f"adjust:{cid}:{uid}:-10")],[InlineKeyboardButton("Add custom amount",callback_data=f"custom:{cid}:{uid}:1")],[InlineKeyboardButton("Remove custom amount",callback_data=f"custom:{cid}:{uid}:-1")],[InlineKeyboardButton("View adjustment history",callback_data=f"recent:{cid}:{uid}")],[InlineKeyboardButton("Find another member",callback_data=f"search:{cid}")],[InlineKeyboardButton("Back to group",callback_data=f"g:{cid}")]]
    else:
        text=(notice+"\n\n" if notice else "") + f"{member_identity(cid, uid)}\n总积分：{u['total_points']} 分"
        kb=[[InlineKeyboardButton("增加 1 分",callback_data=f"adjust:{cid}:{uid}:1"),InlineKeyboardButton("增加 5 分",callback_data=f"adjust:{cid}:{uid}:5"),InlineKeyboardButton("增加 10 分",callback_data=f"adjust:{cid}:{uid}:10")],[InlineKeyboardButton("扣除 1 分",callback_data=f"adjust:{cid}:{uid}:-1"),InlineKeyboardButton("扣除 5 分",callback_data=f"adjust:{cid}:{uid}:-5"),InlineKeyboardButton("扣除 10 分",callback_data=f"adjust:{cid}:{uid}:-10")],[InlineKeyboardButton("自定义增加分数",callback_data=f"custom:{cid}:{uid}:1"),InlineKeyboardButton("自定义扣除分数",callback_data=f"custom:{cid}:{uid}:-1")],[InlineKeyboardButton("查看积分调整记录",callback_data=f"recent:{cid}:{uid}"),InlineKeyboardButton("查找其他成员",callback_data=f"search:{cid}")],[InlineKeyboardButton("返回群组",callback_data=f"g:{cid}")]]
    return await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(kb))


async def settings_page(q, cid):
    s=store.settings(cid); english=s["language"] == "en"; labels={k:v[1 if english else 0] for k,v in SETTING_LABELS.items()}
    title="Point rules" if english else "积分规则"
    min_value = "No minimum" if english and s["min_chars"] == 0 else ("不限" if s["min_chars"] == 0 else str(s["min_chars"]))
    limit_value = "Unlimited" if english and s["daily_limit"] == 0 else ("不限" if s["daily_limit"] == 0 else str(s["daily_limit"]))
    body=f"{labels['min_chars']}: {min_value}\n{labels['daily_limit']}: {limit_value}\n{labels['checkin_points']}: {s['checkin_points']}"
    rows=[[InlineKeyboardButton(labels['min_chars'],callback_data=f"edit:{cid}:min_chars")],[InlineKeyboardButton(labels['daily_limit'],callback_data=f"edit:{cid}:daily_limit")],[InlineKeyboardButton(labels['checkin_points'],callback_data=f"edit:{cid}:checkin_points")],[InlineKeyboardButton("Back to group" if english else "返回群组",callback_data=f"g:{cid}")]]
    return await q.edit_message_text(title+"\n\n"+body,reply_markup=InlineKeyboardMarkup(rows))


async def private_text(update, context):
    state=context.user_data.get("state"); text=update.effective_message.text.strip(); cid=context.user_data.get("chat_id")
    if state=="owner_search":
        if update.effective_user.id != OWNER_ID: context.user_data.pop("state",None); return
        rows=store.chats(enabled_only=False)
        if text.lstrip("-").isdigit():
            wanted=int(text); rows=[r for r in rows if r["chat_id"] == wanted]
        else:
            needle=text.casefold(); rows=[r for r in rows if needle in r["title"].casefold()]
        context.user_data.pop("state",None)
        if not rows: return await update.effective_message.reply_text("没有找到匹配的群组。",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("返回群组列表",callback_data="home:0")]]))
        keyboard=[]
        for r in rows[:20]:
            status=" [已停用]" if not r["enabled"] else ""
            keyboard.append([InlineKeyboardButton(r["title"]+status,callback_data=f"g:{r['chat_id']}")])
        keyboard.append([InlineKeyboardButton("返回群组列表",callback_data="home:0")])
        return await update.effective_message.reply_text(f"找到 {len(rows)} 个群组：",reply_markup=InlineKeyboardMarkup(keyboard))
    if state=="owner_auth_search":
        if update.effective_user.id != OWNER_ID: context.user_data.pop("state",None); return
        rows=store.chats(enabled_only=True); needle=text.casefold();
        if text.lstrip("-").isdigit(): rows=[r for r in rows if r["chat_id"] == int(text)]
        else: rows=[r for r in rows if needle in r["title"].casefold()]
        context.user_data.pop("state",None)
        if not rows: return await update.effective_message.reply_text("没有找到已激活群组。",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("返回已激活群组",callback_data="ownerauthorized")]]))
        return await update.effective_message.reply_text("选择要停用积分功能的群组：",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(r["title"],callback_data=f"revokeask:{r['chat_id']}:owner")] for r in rows[:20]]+[[InlineKeyboardButton("返回已激活群组",callback_data="ownerauthorized")]]))
    if state=="search":
        u=store.find_user(cid,text)
        if u == "conflict" or not u:
            context.user_data.pop("state", None)
        if u == "conflict": return await update.effective_message.reply_text(tr(cid,"该用户名对应多个历史成员，请改用数字 ID。", "Multiple recorded members use this username. Use a numeric ID."), reply_markup=back_keyboard(cid, f"search:{cid}", "重新搜索", "Search again"))
        if not u:return await update.effective_message.reply_text(tr(cid,"未找到该成员。请核对 @用户名或数字 ID，并确认成员已在本群发言或签到。","Member not found. Check the @username or numeric ID and make sure they have spoken or checked in in this group."), reply_markup=back_keyboard(cid, f"search:{cid}", "重新搜索", "Search again"))
        context.user_data.pop("state",None); english=store.settings(cid)["language"] == "en"
        rows=[[InlineKeyboardButton("Open member" if english else "查看成员积分",callback_data=f"member:{cid}:{u['user_id']}")],
              [InlineKeyboardButton(tr(cid, "查找其他成员", "Find another member"), callback_data=f"search:{cid}")]]
        rows.extend(back_keyboard(cid).inline_keyboard)
        return await update.effective_message.reply_text(tr(cid, f"{member_identity(cid, u['user_id'])}\n总积分：{u['total_points']} 分", f"{member_identity(cid, u['user_id'])}\nTotal points: {u['total_points']}"),reply_markup=InlineKeyboardMarkup(rows))
    if state=="setting":
        if not text.isdigit():return await update.effective_message.reply_text(tr(cid,"请输入非负整数。","Enter a non-negative integer."), reply_markup=back_keyboard(cid, f"set:{cid}", "返回积分规则", "Back to rules"))
        cs,key=context.user_data["edit"]; value=int(text); old=store.settings(cs)[key]
        if value>1000000: return await update.effective_message.reply_text(tr_error(cs,"设置必须是 0 到 1000000 的整数"),reply_markup=back_keyboard(cs))
        markup=confirmation(context,"setting",(key,value),cs)
        return await update.effective_message.reply_text(f"{SETTING_LABELS[key][0]}: {old} → {value}",reply_markup=markup)
    if state=="alias":
        p=text.split(None,1)
        if len(p)!=2 or not store.add_alias(cid,p[0].lower(),p[1]):return await update.effective_message.reply_text(tr(cid,"格式错误或别名冲突，请使用：score /points","Invalid format or conflicting alias. Example: score /points"), reply_markup=back_keyboard(cid, f"alias:{cid}", "返回自定义命令", "Back to custom commands"))
        context.user_data.pop("state",None); return await update.effective_message.reply_text(tr(cid,"自定义命令已保存。","Custom command saved."),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to custom commands" if store.settings(cid)["language"] == "en" else "返回自定义命令",callback_data=f"alias:{cid}")]]))
    if state=="reason":
        cs,us,delta=context.user_data["reason_pending"]
        markup=confirmation(context,"adjust",(us,delta,text[:200]),cs)
        return await update.effective_message.reply_text(adjustment_summary(cs,us,delta,text[:200]),reply_markup=markup)
    if state=="custom":
        if not text.isdigit() or not 1<=int(text)<=1000000:
            cs,us,_=context.user_data["custom"]
            return await update.effective_message.reply_text(tr(cs,"请输入 1 到 1000000 的整数。","Enter an integer from 1 to 1,000,000."), reply_markup=back_keyboard(cs, f"member:{cs}:{us}", "返回成员积分", "Back to member"))
        cs,us,sign=context.user_data.pop("custom"); context.user_data.pop("state",None)
        delta=sign*int(text); u=store.find_user(cs,us); context.user_data["state"]="reason"; context.user_data["reason_pending"]=(cs,us,delta)
        chat=store.conn.execute("SELECT title FROM chats WHERE chat_id=?",(cs,)).fetchone(); title=chat[0] if chat else str(cs)
        english=store.settings(cs)["language"] == "en"; return await update.effective_message.reply_text(tr(cs,"请输入调整原因：","Enter the reason for this adjustment:"), reply_markup=back_keyboard(cs, f"member:{cs}:{us}", "返回成员积分", "Back to member"))
    return await update.effective_message.reply_text(tr(cid, "请选择要继续的管理菜单。", "Choose a management menu to continue."), reply_markup=back_keyboard(cid))


async def migrate_group(update, context):
    message = update.effective_message
    old_id = message.migrate_from_chat_id or update.effective_chat.id
    new_id = message.migrate_to_chat_id or update.effective_chat.id
    try:
        store.migrate_chat(old_id, new_id)
    except (ValueError, sqlite3.Error) as exc:
        logging.getLogger(__name__).exception("Group migration failed: %s -> %s", old_id, new_id)
        if OWNER_ID:
            await context.bot.send_message(OWNER_ID, f"群组迁移失败，请检查：{old_id} → {new_id}\n{exc}")


async def bot_membership(update, context):
    if update.my_chat_member.new_chat_member.status in (ChatMember.LEFT, ChatMember.BANNED):
        store.bot_removed(update.effective_chat.id)


async def group_title(update, context):
    store.update_title(update.effective_chat.id, update.effective_message.new_chat_title)


async def post_init(application):
    """Register the static commands shown when a user types '/'."""
    try:
        await application.bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        print(f"命令列表注册失败，将继续运行：{exc}")


async def post_shutdown(application):
    store.close()


async def error_handler(update, context):
    exc = context.error
    if isinstance(exc, BadRequest) and "not modified" in str(exc).lower():
        return
    logging.getLogger(__name__).error("Update processing failed", exc_info=(type(exc), exc, exc.__traceback__))


def command_handlers():
    return {
        "activate": activate, "score": score_cmd, "checkin": text_handler,
        "rank": lambda u,c: rank_cmd(u,c,False), "today": lambda u,c: rank_cmd(u,c,True),
        "addpoints": lambda u,c: points_cmd(u,c,"addpoints"),
        "subpoints": lambda u,c: points_cmd(u,c,"subpoints"), "start": start,
    }


REGISTERED_COMMANDS.update(command_handlers())


def build_app(token):
    app = (Application.builder().token(token).rate_limiter(AIORateLimiter(max_retries=3))
           .post_init(post_init).post_shutdown(post_shutdown).build())
    for command, handler in command_handlers().items():
        app.add_handler(CommandHandler(command, handler, filters=filters.UpdateType.MESSAGE))
    app.add_handler(ChatMemberHandler(bot_membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.StatusUpdate.MIGRATE, migrate_group))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.StatusUpdate.NEW_CHAT_TITLE, group_title))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT, text_handler))
    app.add_error_handler(error_handler)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token=os.getenv("BOT_TOKEN")
    if not token: raise SystemExit("缺少 BOT_TOKEN 环境变量")
    if not OWNER_ID: raise SystemExit("缺少有效 OWNER_ID 环境变量")
    print(f"配置读取成功，数据库：{os.path.abspath(DB_PATH)}")
    build_app(token).run_polling()


if __name__ == "__main__": main()
