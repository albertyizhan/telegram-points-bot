import hashlib
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

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
SETTING_LABELS = {
    "min_chars": ("最小字数", "Minimum message length"),
    "daily_limit": ("每日聊天积分上限", "Daily chat points limit"),
    "checkin_points": ("每日签到积分", "Daily check-in points"),
}
DB_PATH = os.getenv("DB_PATH", "points.db")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
db_lock = threading.RLock()
COMMANDS = [
    BotCommand("start", "打开私聊管理后台 / Open admin panel"),
    BotCommand("score", "查看当前群积分 / View your score"),
    BotCommand("checkin", "每日签到 / Daily check-in"),
    BotCommand("addpoints", "增加成员积分 / Add member points"),
    BotCommand("subpoints", "减少成员积分 / Subtract member points"),
    BotCommand("activate", "激活群组 / Activate group"),
    BotCommand("rank", "总积分排行 / Total ranking"),
    BotCommand("today", "今日积分排行 / Today's ranking"),
]


def now(tz_name=DEFAULT_TIMEZONE):
    return datetime.now(timezone_for(tz_name)).isoformat(timespec="seconds")


def day(tz_name=DEFAULT_TIMEZONE):
    return datetime.now(timezone_for(tz_name)).date().isoformat()


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
}


def tr_error(chat_id, message):
    return tr(chat_id, message, ERROR_EN.get(message, message))


class Store:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
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
            """)
            for table, column, definition in (
                ("chats", "authorized_by", "INTEGER"),
                ("settings", "language", "TEXT NOT NULL DEFAULT 'zh'"),
                ("settings", "timezone", "TEXT NOT NULL DEFAULT 'Asia/Hong_Kong'"),
            ):
                try: self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError: pass
            # Migrate old one-group code bindings to the new one-license-per-person model.
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) SELECT chat_id FROM chats")
            self.conn.execute("INSERT OR IGNORE INTO licenses(user_id,code_hash,bound_at) SELECT used_by,code_hash,COALESCE(used_at,created_at) FROM auth_codes WHERE used_by IS NOT NULL")
            self.conn.execute("UPDATE chats SET authorized_by=(SELECT used_by FROM auth_codes WHERE used_chat_id=chats.chat_id AND used_by IS NOT NULL ORDER BY used_at LIMIT 1) WHERE authorized_by IS NULL")

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
                    self.conn.execute("INSERT INTO licenses(user_id,code_hash,bound_at) VALUES(?,?,?)", (user_id,h,now()))
                    self.conn.execute("UPDATE auth_codes SET used_at=?,used_by=?,used_chat_id=? WHERE code_hash=? AND used_at IS NULL", (now(),user_id,chat_id,h))
            t = now()
            self.conn.execute("INSERT INTO chats(chat_id,title,enabled,created_at,authorized_by) VALUES(?,?,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,enabled=1,authorized_by=excluded.authorized_by", (chat_id,title,t,user_id))
            self.conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
            return True

    def generate_code(self):
        code = secrets.token_urlsafe(12)
        with self.conn:
            self.conn.execute("INSERT INTO auth_codes(code_hash,created_at) VALUES(?,?)", (hashlib.sha256(code.encode()).hexdigest(), now()))
        return code

    def unused_codes(self):
        return self.conn.execute("SELECT created_at,substr(code_hash,1,12) AS hash FROM auth_codes WHERE used_at IS NULL ORDER BY created_at DESC").fetchall()

    def chats(self, enabled_only=True):
        q = "SELECT * FROM chats" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY title"
        return self.conn.execute(q).fetchall()

    def revoke(self, chat_id):
        with self.conn: self.conn.execute("UPDATE chats SET enabled=0 WHERE chat_id=?", (chat_id,))

    def upsert_user(self, chat_id, user_id, username, display_name):
        with self.conn:
            self.conn.execute("INSERT INTO users(chat_id,user_id,username,display_name,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,display_name=excluded.display_name,updated_at=excluded.updated_at", (chat_id,user_id,username,display_name,self.chat_now(chat_id)))

    def settings(self, chat_id):
        return self.conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()

    def set_setting(self, chat_id, key, value):
        if key not in ("min_chars", "daily_limit", "checkin_points"): raise ValueError("invalid setting")
        if not isinstance(value, int) or value < 0: raise ValueError("设置必须是非负整数")
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
        return day(s["timezone"] if s else DEFAULT_TIMEZONE)

    def chat_now(self, chat_id):
        s = self.settings(chat_id)
        return now(s["timezone"] if s else DEFAULT_TIMEZONE)

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
        alias = alias.strip()
        if not alias or action not in DEFAULTS: return False
        if alias.startswith("/"): alias = alias.split()[0].split("@",1)[0].lower()
        if any(alias.lower() == x.lower() for xs in DEFAULTS.values() for x in xs): return False
        try:
            with self.conn: self.conn.execute("INSERT INTO aliases(chat_id,action,alias) VALUES(?,?,?)", (chat_id,action,alias))
            return True
        except sqlite3.IntegrityError: return False

    def remove_alias(self, chat_id, alias):
        with self.conn: return self.conn.execute("DELETE FROM aliases WHERE chat_id=? AND lower(alias)=?", (chat_id,alias.lower())).rowcount > 0

    def aliases(self, chat_id): return self.conn.execute("SELECT action,alias FROM aliases WHERE chat_id=? ORDER BY action,alias", (chat_id,)).fetchall()

    def award_chat(self, chat_id, user_id, username, display_name, text):
        if not self.authorized(chat_id): return 0
        s = self.settings(chat_id); count = len(re.sub(r"\s", "", text, flags=re.UNICODE))
        self.upsert_user(chat_id,user_id,username,display_name)
        if not count or (s["min_chars"] and count < s["min_chars"]): return 0
        current_day = self.local_day(chat_id)
        with db_lock, self.conn:
            self.conn.execute("INSERT INTO daily(chat_id,user_id,day) VALUES(?,?,?) ON CONFLICT DO NOTHING", (chat_id,user_id,current_day))
            r = self.conn.execute("SELECT chat_points FROM daily WHERE chat_id=? AND user_id=? AND day=?", (chat_id,user_id,current_day)).fetchone()
            add = 1 if s["daily_limit"] == 0 else min(1, max(0, s["daily_limit"] - r[0]))
            if add:
                self.conn.execute("UPDATE daily SET chat_points=chat_points+? WHERE chat_id=? AND user_id=? AND day=?", (add,chat_id,user_id,current_day))
                self.conn.execute("UPDATE users SET total_points=total_points+?,updated_at=? WHERE chat_id=? AND user_id=?", (add,self.chat_now(chat_id),chat_id,user_id))
            return add

    def checkin(self, chat_id, user_id, username, display_name):
        if not self.authorized(chat_id): return (False,0,0)
        self.upsert_user(chat_id,user_id,username,display_name); s=self.settings(chat_id)
        current_day = self.local_day(chat_id)
        with db_lock, self.conn:
            self.conn.execute("INSERT INTO daily(chat_id,user_id,day) VALUES(?,?,?) ON CONFLICT DO NOTHING", (chat_id,user_id,current_day))
            r=self.conn.execute("SELECT checked_in FROM daily WHERE chat_id=? AND user_id=? AND day=?", (chat_id,user_id,current_day)).fetchone()
            if r[0]: return (False,0,self.total(chat_id,user_id))
            self.conn.execute("UPDATE daily SET checked_in=1,checkin_points=? WHERE chat_id=? AND user_id=? AND day=?", (s["checkin_points"],chat_id,user_id,current_day))
            self.conn.execute("UPDATE users SET total_points=total_points+?,updated_at=? WHERE chat_id=? AND user_id=?", (s["checkin_points"],self.chat_now(chat_id),chat_id,user_id))
            return (True,s["checkin_points"],self.total(chat_id,user_id))

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

    def adjust(self, chat_id, operator_id, target_id, delta, method):
        if not 1 <= abs(delta) <= 1000000: raise ValueError("数量必须是 1 到 1000000 的整数")
        with db_lock, self.conn:
            r=self.conn.execute("SELECT total_points FROM users WHERE chat_id=? AND user_id=?", (chat_id,target_id)).fetchone()
            if not r: raise ValueError("用户需要先在群里发言")
            before=r[0]; after=before+delta
            if after<0: raise ValueError("积分不能低于 0")
            self.conn.execute("UPDATE users SET total_points=?,updated_at=? WHERE chat_id=? AND user_id=?", (after,self.chat_now(chat_id),chat_id,target_id))
            self.conn.execute("INSERT INTO adjustments(chat_id,operator_id,target_id,delta,before_points,after_points,method,created_at) VALUES(?,?,?,?,?,?,?,?)", (chat_id,operator_id,target_id,delta,before,after,method,self.chat_now(chat_id)))
            return before,after

    def recent(self, chat_id, user_id): return self.conn.execute("SELECT * FROM adjustments WHERE chat_id=? AND target_id=? ORDER BY id DESC LIMIT 20", (chat_id,user_id)).fetchall()

    def stats(self, chat_id, today=False):
        if today: return self.conn.execute("SELECT u.user_id,u.display_name,u.username,d.chat_points+d.checkin_points AS points FROM users u JOIN daily d ON d.chat_id=u.chat_id AND d.user_id=u.user_id WHERE u.chat_id=? AND d.day=? ORDER BY points DESC LIMIT 20", (chat_id,self.local_day(chat_id))).fetchall()
        return self.conn.execute("SELECT user_id,display_name,username,total_points AS points FROM users WHERE chat_id=? ORDER BY points DESC LIMIT 20", (chat_id,)).fetchall()

    def ranking(self, chat_id, today=False, page=0, size=15):
        offset = max(0, page) * size
        if today:
            rows = self.conn.execute("SELECT u.user_id,u.display_name,u.username,d.chat_points+d.checkin_points AS points FROM users u JOIN daily d ON d.chat_id=u.chat_id AND d.user_id=u.user_id WHERE u.chat_id=? AND d.day=? ORDER BY points DESC,u.user_id LIMIT ? OFFSET ?", (chat_id,self.local_day(chat_id),size,offset)).fetchall()
            total = self.conn.execute("SELECT COUNT(*) FROM users u JOIN daily d ON d.chat_id=u.chat_id AND d.user_id=u.user_id WHERE u.chat_id=? AND d.day=?", (chat_id,self.local_day(chat_id))).fetchone()[0]
        else:
            rows = self.conn.execute("SELECT user_id,display_name,username,total_points AS points FROM users WHERE chat_id=? ORDER BY points DESC,user_id LIMIT ? OFFSET ?", (chat_id,size,offset)).fetchall()
            total = self.conn.execute("SELECT COUNT(*) FROM users WHERE chat_id=?", (chat_id,)).fetchone()[0]
        return rows, total

store = Store()


async def admin_level(update, chat_id=None):
    uid=update.effective_user.id; chat_id=chat_id or update.effective_chat.id
    if uid == OWNER_ID: return 3
    try:
        member = await update.get_bot().get_chat_member(chat_id,uid)
        if member.status == "creator": return 3
        if member.status == "administrator": return 2 if member.can_promote_members else 1
    except Exception: pass
    return 0


async def is_admin(update, chat_id=None, minimum=1):
    return await admin_level(update, chat_id) >= minimum


def who(u): return "@"+u.username if u.username else u.full_name


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m=update.effective_message; u=m.from_user
    if not m or not u or u.is_bot: return
    if update.effective_chat.type == "private": return await private_text(update,context)
    cid=update.effective_chat.id
    if not store.authorized(cid): return
    store.upsert_user(cid,u.id,u.username,u.full_name)
    action=store.resolve_alias(cid,m.text or "")
    if action == "checkin":
        ok,pts,total=store.checkin(cid,u.id,u.username,u.full_name)
        await m.reply_text(tr(cid, f"签到成功：+{pts} 分，当前总积分 {total}", f"Check-in successful: +{pts} points. Total: {total}") if ok else tr(cid,"今天已经签到过了","You have already checked in today."))
    elif action == "score": await score_cmd(update,context)
    elif action in ("rank", "today"): await rank_cmd(update,context,action == "today")
    elif action in ("addpoints","subpoints"): await points_cmd(update,context,action)
    elif not (m.text or "").startswith("/"): store.award_chat(cid,u.id,u.username,u.full_name,m.text)


async def activate(update, context):
    m=update.effective_message
    if update.effective_chat.type == "private": return await m.reply_text("请在群内激活。")
    if update.effective_user.id == OWNER_ID:
        code = context.args[0] if context.args else None
        ok=store.authorize(update.effective_chat.id,update.effective_chat.title or str(update.effective_chat.id),code,update.effective_user.id,owner=True)
        return await m.reply_text("群组授权成功。" if ok else "群组已经授权。")
    if not await is_admin(update, minimum=1): return await m.reply_text("只有群主或管理员可以激活群组。")
    code = context.args[0] if context.args else None
    ok=store.authorize(update.effective_chat.id,update.effective_chat.title or str(update.effective_chat.id),code,update.effective_user.id)
    await m.reply_text("授权成功。之后你绑定的授权码可在最多 3 个群使用。" if ok else ("已绑定个人授权，请直接发送 /activate；每人最多 3 个启用群。" if not code else "授权码无效、已绑定其他人，或群组已经授权。"))


async def score_cmd(update, context):
    cid=update.effective_chat.id
    if not store.authorized(cid): return await update.effective_message.reply_text(tr(cid,"本群尚未授权。","This group is not activated."))
    t,c,checked=store.score(cid,update.effective_user.id); await update.effective_message.reply_text(tr(cid, f"总积分：{t}\n今日聊天积分：{c}\n今日签到：{'已签到' if checked else '未签到'}", f"Total points: {t}\nToday's chat points: {c}\nToday's check-in: {'done' if checked else 'not done'}"))


async def rank_cmd(update, context, today=False, page=0):
    cid=update.effective_chat.id
    if not store.authorized(cid): return await update.effective_message.reply_text(tr(cid,"本群尚未授权。","This group is not activated."))
    rows,total=store.ranking(cid,today,page,15); english=store.settings(cid)["language"] == "en"; title=("Today's ranking" if today else "Total ranking") if english else ("单日积分排行" if today else "总积分排行")
    start=page*15; body="\n".join(f"{start+i+1}. {r['display_name']}: {r['points']}" for i,r in enumerate(rows)) or ("No data" if english else "暂无数据")
    buttons=[]
    if page>0: buttons.append(InlineKeyboardButton("Previous" if english else "上一页",callback_data=f"rank:{cid}:{int(today)}:{page-1}"))
    if start+len(rows)<total: buttons.append(InlineKeyboardButton("Next" if english else "下一页",callback_data=f"rank:{cid}:{int(today)}:{page+1}"))
    markup=InlineKeyboardMarkup([buttons]) if buttons else None
    await update.effective_message.reply_text(f"{title} ({page+1}/{max(1,(total+14)//15)})\n{body}",reply_markup=markup)


async def points_cmd(update, context, action=None):
    m=update.effective_message; cid=update.effective_chat.id
    if not store.authorized(cid): return await m.reply_text(tr(cid,"本群尚未授权。","This group is not activated."))
    if not await is_admin(update): return await m.reply_text(tr(cid,"只有本群管理员或总管理员可以调整积分。","Only a group administrator or the owner can adjust points."))
    action=action or ("addpoints" if m.text.startswith("/addpoints") else "subpoints")
    args=context.args or (m.text.split()[1:] if m.text else []); target=m.reply_to_message.from_user if m.reply_to_message else None
    try: amount=int(args[0])
    except (ValueError,IndexError): return await m.reply_text(tr(cid,"用法：/addpoints 10 @username，或回复消息后发送 /addpoints 10","Usage: /addpoints 10 @username, or reply to a member with /addpoints 10"))
    if not 1<=amount<=1000000: return await m.reply_text(tr(cid,"数量必须是 1 到 1000000 的整数","Amount must be an integer from 1 to 1,000,000."))
    if not target and len(args)>1:
        found=store.find_user(cid,args[1]);
        if found == "conflict": return await m.reply_text(tr(cid,"用户名匹配到多个成员。","More than one member matches that username."))
        target=found
    if not target: return await m.reply_text(tr(cid,"找不到目标成员；请回复其群消息，或使用已记录的完整用户名。","Member not found. Reply to their message or use a recorded exact username."))
    target_id = target.id if hasattr(target, "id") else target["user_id"]
    target_name = who(target) if hasattr(target, "full_name") else ("@" + target["username"] if target["username"] else target["display_name"])
    if getattr(target,"is_bot",False): return await m.reply_text(tr(cid,"不能调整机器人积分。","Bot accounts cannot have points adjusted."))
    try: before,after=store.adjust(cid,update.effective_user.id,target_id,(amount if action=="addpoints" else -amount),"reply" if m.reply_to_message else "username")
    except ValueError as e: return await m.reply_text(tr_error(cid, str(e)))
    delta_text = f"{'+' if action=='addpoints' else '-'}{amount}"
    await m.reply_text(tr(cid, f"{target_name}：{before} -> {after}（{delta_text}）", f"{target_name}: {before} -> {after} ({delta_text})"))


async def start(update, context):
    if update.effective_chat.type != "private": return await update.effective_message.reply_text("请私聊机器人使用后台。")
    if update.effective_user.id != OWNER_ID:
        return await update.effective_message.reply_text("请选择你管理的群组：" if await has_any_admin(update) else "你不是已授权群组管理员。", reply_markup=await group_keyboard(update))
    await update.effective_message.reply_text("总管理员后台", reply_markup=owner_home_keyboard())


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
        if c["enabled"]:
            rows.append([
                InlineKeyboardButton("语言 / Language", callback_data=f"lang:{c['chat_id']}:toggle"),
                InlineKeyboardButton("时区 / Timezone", callback_data=f"tzlist:{c['chat_id']}"),
            ])
    if not rows: rows.append([InlineKeyboardButton("暂无可管理群组",callback_data="noop")])
    if page_count > 1:
        nav=[]
        if page > 0: nav.append(InlineKeyboardButton("上一页",callback_data=f"home:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{page_count}",callback_data="noop"))
        if page + 1 < page_count: nav.append(InlineKeyboardButton("下一页",callback_data=f"home:{page+1}"))
        rows.append(nav)
    if owner:
        rows.append([InlineKeyboardButton("搜索群组",callback_data="ownersearch")])
        rows.append([InlineKeyboardButton("返回",callback_data="home")])
    return InlineKeyboardMarkup(rows)


def owner_authorized_keyboard(page=0):
    chats=store.chats(enabled_only=True); size=8; pages=max(1,(len(chats)+size-1)//size); page=max(0,min(page,pages-1)); rows=[]
    for c in chats[page*size:(page+1)*size]: rows.append([InlineKeyboardButton(c["title"],callback_data=f"revokeask:{c['chat_id']}:owner")])
    if not rows: rows=[[InlineKeyboardButton("暂无已授权群组",callback_data="noop")]]
    if pages>1:
        nav=[]
        if page: nav.append(InlineKeyboardButton("上一页",callback_data=f"owner_auth_page:{page-1}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="noop"))
        if page+1<pages: nav.append(InlineKeyboardButton("下一页",callback_data=f"owner_auth_page:{page+1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("搜索群组",callback_data="ownersearch_auth"),InlineKeyboardButton("返回",callback_data="home")])
    return InlineKeyboardMarkup(rows)


def owner_home_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("您管理的群组",callback_data="ownergroups")],[InlineKeyboardButton("管理已授权群组",callback_data="ownerauthorized")],[InlineKeyboardButton("生成授权码",callback_data="code"),InlineKeyboardButton("未使用授权码",callback_data="codes")]])


async def callback(update, context):
    q=update.callback_query; await q.answer(); uid=q.from_user.id; data=q.data
    if data=="noop": return
    protected=("g:","search:","member:","adjust:","recent:","stat:","set:","edit:","alias:","addalias:","delalias:","custom:","lang:","tz:","tzlist:")
    if uid != OWNER_ID and data.startswith(protected):
        # callback data is prefix:chat_id:..., and Telegram group IDs are negative.
        try: protected_chat=int(data.split(":")[1])
        except (ValueError, IndexError): return await q.edit_message_text("无效操作。")
        required = 2 if data.startswith(("set:","edit:","alias:","addalias:","delalias:","lang:","tz:","tzlist:")) else 1
        if not await is_admin(update, protected_chat, required): return await q.edit_message_text("权限不足或权限已变化，操作取消。")
    if data.startswith("home:"):
        return await q.edit_message_text("后台",reply_markup=await group_keyboard(update,uid==OWNER_ID,int(data.split(":",1)[1])))
    if data == "ownergroups":
        if uid != OWNER_ID: return await q.edit_message_text("只有拥有者可以操作。")
        return await q.edit_message_text("您管理的群组",reply_markup=await group_keyboard(update,True))
    if data == "ownerauthorized":
        if uid != OWNER_ID: return await q.edit_message_text("只有拥有者可以操作。")
        return await q.edit_message_text("管理已授权群组",reply_markup=owner_authorized_keyboard(0))
    if data.startswith("owner_auth_page:"):
        if uid != OWNER_ID: return
        return await q.edit_message_text("管理已授权群组",reply_markup=owner_authorized_keyboard(int(data.split(":",1)[1])))
    if data == "ownersearch":
        if uid != OWNER_ID: return await q.edit_message_text("只有拥有者可以搜索群组。")
        context.user_data["state"]="owner_search"
        return await q.edit_message_text("请输入完整群组 ID 或群组名称关键词：")
    if data == "ownersearch_auth":
        if uid != OWNER_ID: return
        context.user_data["state"]="owner_auth_search"
        return await q.edit_message_text("请输入要解除授权的群组 ID 或名称关键词：")
    if data=="code":
        if uid!=OWNER_ID:return
        return await q.edit_message_text("一次性授权码（只显示这一次）：\n"+store.generate_code())
    if data=="codes":
        if uid!=OWNER_ID:return
        rows=store.unused_codes(); return await q.edit_message_text("未使用授权码：\n"+("\n".join(f"{r['hash']} ({r['created_at']})" for r in rows) if rows else "无"))
    if data.startswith("g:"):
        cid=int(data[2:]);
        level=await admin_level(update,cid)
        if level < 1: return await q.edit_message_text("权限已变化，请重新进入。")
        context.user_data["chat_id"]=cid
        english=store.settings(cid)["language"] == "en"; label=lambda zh,en: en if english else zh
        if english:
            kb=[[InlineKeyboardButton("Member points",callback_data=f"search:{cid}")],[InlineKeyboardButton("Today's stats",callback_data=f"stat:{cid}:1")],[InlineKeyboardButton("All-time stats",callback_data=f"stat:{cid}:0")]]
        else:
            kb=[[InlineKeyboardButton("成员积分管理",callback_data=f"search:{cid}"),InlineKeyboardButton("今日统计",callback_data=f"stat:{cid}:1")],[InlineKeyboardButton("总统计",callback_data=f"stat:{cid}:0")]]
        if level >= 2:
            kb.append([InlineKeyboardButton(label("群组设置","Group settings"),callback_data=f"set:{cid}")])
            kb.append([InlineKeyboardButton(label("别名管理","Aliases"),callback_data=f"alias:{cid}")])
        kb.append([InlineKeyboardButton(label("语言","Language"),callback_data=f"lang:{cid}:{'zh' if english else 'en'}"),InlineKeyboardButton(label("时区","Timezone"),callback_data=f"tzlist:{cid}")])
        if uid == OWNER_ID and store.authorized(cid): kb.append([InlineKeyboardButton(label("撤销群组授权","Revoke access"),callback_data=f"revokeask:{cid}")])
        kb.append([InlineKeyboardButton(label("返回","Back"),callback_data="home")])
        return await q.edit_message_text(label("群组菜单","Group menu"),reply_markup=InlineKeyboardMarkup(kb))
    if data=="home": return await q.edit_message_text("总管理员后台" if uid==OWNER_ID else "后台",reply_markup=owner_home_keyboard() if uid==OWNER_ID else await group_keyboard(update,False))
    if data.startswith("search:"):
        cid=int(data[7:]); context.user_data["state"]="search"; context.user_data["chat_id"]=cid; return await q.edit_message_text(tr(cid,"请输入完整数字 Telegram ID：","Enter the full numeric Telegram ID:"))
    if data.startswith("member:"):
        _,cs,us=data.split(":"); cs=int(cs); us=int(us)
        if uid!=OWNER_ID and not await is_admin(update,cs): return await q.edit_message_text("权限已变化，请重新进入。")
        return await member_page(q,context,cs,us)
    if data.startswith("adjust:"):
        _,cs,us,delta=data.split(":"); delta=int(delta); cs=int(cs); us=int(us); context.user_data["pending"]=(cs,us,delta); u=store.find_user(cs,us); chat=store.conn.execute("SELECT title FROM chats WHERE chat_id=?",(cs,)).fetchone(); title=chat[0] if chat else str(cs); english=store.settings(cs)["language"] == "en"
        confirm=tr(cs,f"群组：{title}\n{u['display_name']}（{us}）\n当前积分：{u['total_points']}\n本次变化：{delta:+d}\n调整后：{u['total_points']+delta}",f"Group: {title}\n{u['display_name']} ({us})\nCurrent points: {u['total_points']}\nChange: {delta:+d}\nAfter: {u['total_points']+delta}")
        return await q.edit_message_text(confirm,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm" if english else "确认",callback_data="confirm"),InlineKeyboardButton("Cancel" if english else "取消",callback_data=f"member:{cs}:{us}")]]))
    if data=="confirm":
        p=context.user_data.pop("pending",None)
        if not p:return await q.edit_message_text("This action was already processed." if uid == OWNER_ID else "该操作已处理。")
        cid,tid,delta=p
        if uid!=OWNER_ID and not await is_admin(update,cid): return await q.edit_message_text("权限已变化，操作取消。")
        try: before,after=store.adjust(cid,uid,tid,delta,"后台")
        except ValueError as e:return await q.edit_message_text(tr_error(cid, str(e)))
        return await member_page(q,context,cid,tid,tr(cid,f"已调整：{before} -> {after}",f"Updated: {before} -> {after}"))
    if data.startswith("recent:"):
        _,cs,us=data.split(":"); cs=int(cs); us=int(us); rows=store.recent(cs,us); return await q.edit_message_text(tr(cs,"最近调整：\n"+("\n".join(f"{r['created_at']} {r['delta']:+d} ({r['before_points']}->{r['after_points']})" for r in rows) if rows else "暂无记录"),"Recent adjustments:\n"+("\n".join(f"{r['created_at']} {r['delta']:+d} ({r['before_points']}->{r['after_points']})" for r in rows) if rows else "No records")))
    if data.startswith("stat:"):
        _,cs,today_flag=data.split(":"); cs=int(cs); rows=store.stats(cs,today_flag=="1"); body="\n".join(f"{i+1}. {r['display_name']}：{r['points']}" for i,r in enumerate(rows)) or "暂无数据"; return await q.edit_message_text(tr(cs,body,body.replace("：",": ").replace("暂无数据","No data")))
    if data.startswith("rank:"):
        _,cs,today_flag,page=data.split(":"); cs=int(cs); today_flag=bool(int(today_flag)); page=int(page); rows,total=store.ranking(cs,today_flag,page,15); english=store.settings(cs)["language"] == "en"; title=("Today's ranking" if today_flag else "Total ranking") if english else ("单日积分排行" if today_flag else "总积分排行"); start=page*15; body="\n".join(f"{start+i+1}. {r['display_name']}: {r['points']}" for i,r in enumerate(rows)) or ("No data" if english else "暂无数据"); buttons=[]
        if page>0: buttons.append(InlineKeyboardButton("Previous" if english else "上一页",callback_data=f"rank:{cs}:{int(today_flag)}:{page-1}"))
        if start+len(rows)<total: buttons.append(InlineKeyboardButton("Next" if english else "下一页",callback_data=f"rank:{cs}:{int(today_flag)}:{page+1}"))
        return await q.edit_message_text(f"{title} ({page+1}/{max(1,(total+14)//15)})\n{body}",reply_markup=InlineKeyboardMarkup([buttons]) if buttons else None)
    if data.startswith("set:"):
        cid=int(data[4:]); return await settings_page(q,cid)
    if data.startswith("lang:"):
        _,cs,language=data.split(":"); cid=int(cs)
        if language == "toggle": language="en" if store.settings(cid)["language"] == "zh" else "zh"
        store.set_language(cid,language); return await settings_page(q,cid)
    if data.startswith("tz:"):
        _,cs,timezone=data.split(":",2); cid=int(cs); store.set_timezone(cid,timezone); return await settings_page(q,cid)
    if data.startswith("tzlist:"):
        cid=int(data.split(":",1)[1]); s=store.settings(cid); english=s["language"] == "en"
        rows=[[InlineKeyboardButton(TIMEZONE_LABELS[z][1 if english else 0],callback_data=f"tz:{cid}:{z}")] for z in TIMEZONE_OPTIONS]
        rows.append([InlineKeyboardButton("Back" if english else "返回",callback_data=f"set:{cid}")])
        return await q.edit_message_text("Choose timezone" if english else "选择时区",reply_markup=InlineKeyboardMarkup(rows))
    if data.startswith("edit:"):
        _,cs,key=data.split(":"); cs=int(cs); context.user_data["state"]="setting"; context.user_data["edit"]=(cs,key); return await q.edit_message_text(tr(cs,"请输入非负整数（填 0 表示不限）：","Enter a non-negative integer (0 means unlimited):"))
    if data=="save_setting":
        p=context.user_data.pop("pending_setting",None)
        if not p:return await q.edit_message_text("该设置已处理。")
        cs,key,value=p
        if uid!=OWNER_ID and not await is_admin(update,cs,2): return await q.edit_message_text("权限不足或权限已变化，操作取消。")
        store.set_setting(cs,key,value); english=store.settings(cs)["language"] == "en"; return await q.edit_message_text("Settings saved." if english else "设置已保存。",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to group" if english else "返回群组",callback_data=f"g:{cs}")]]))
    if data=="cancel_setting":
        context.user_data.pop("pending_setting",None); return await q.edit_message_text("已取消。")
    if data.startswith("alias:"):
        cid=int(data[6:]); ars=store.aliases(cid); english=store.settings(cid)["language"] == "en"; kb=[[InlineKeyboardButton((f"Delete {r['alias']}" if english else f"删除 {r['alias']}"),callback_data=f"delalias:{cid}:{r['alias']}")] for r in ars]
        kb.append([InlineKeyboardButton("Add alias" if english else "添加别名",callback_data=f"addalias:{cid}"),InlineKeyboardButton("Back" if english else "返回",callback_data=f"g:{cid}")])
        return await q.edit_message_text(tr(cid,"别名：\n"+"\n".join(f"{r['action']}: {r['alias']}" for r in ars) if ars else "暂无自定义别名","Aliases:\n"+"\n".join(f"{r['action']}: {r['alias']}" for r in ars) if ars else "No custom aliases"),reply_markup=InlineKeyboardMarkup(kb))
    if data.startswith("addalias:"):
        cid=int(data[9:]); context.user_data["state"]="alias"; context.user_data["chat_id"]=cid; return await q.edit_message_text(tr(cid,"请输入：动作 别名，例如 score /points","Enter: action alias, e.g. score /points"))
    if data.startswith("delalias:"):
        _,cs,alias=data.split(":",2); cid=int(cs); store.remove_alias(cid,alias); english=store.settings(cid)["language"] == "en"; return await q.edit_message_text("Alias deleted." if english else "别名已删除。",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to aliases" if english else "返回别名",callback_data=f"alias:{cs}")]]))
    if data.startswith("revokeask:"):
        if uid!=OWNER_ID:return
        parts=data.split(":"); cid=int(parts[1]); owner_list=len(parts)>2 and parts[2]=="owner"; chat=store.conn.execute("SELECT title,enabled FROM chats WHERE chat_id=?",(cid,)).fetchone()
        english=bool(chat and store.settings(cid) and store.settings(cid)["language"] == "en")
        if not chat or not chat[1]: return await q.edit_message_text("This group is already revoked." if english else "该群组已经撤销授权。",reply_markup=await group_keyboard(update,True))
        message=(f"Revoke access for {chat[0]} ({cid})?\nThe group will stop earning points." if english else f"确定要撤销群组“{chat[0]}”（{cid}）的授权吗？撤销后该群将停止积分统计。")
        return await q.edit_message_text(message,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm revoke" if english else "确认撤销",callback_data=f"revokeyes:{cid}:{'owner' if owner_list else 'group'}"),InlineKeyboardButton("Cancel" if english else "取消",callback_data="ownerauthorized" if owner_list else f"g:{cid}")]]))
    if data.startswith("revokeyes:"):
        if uid!=OWNER_ID:return
        parts=data.split(":"); cid=int(parts[1]); owner_list=len(parts)>2 and parts[2]=="owner"; store.revoke(cid)
        english=bool(store.settings(cid) and store.settings(cid)["language"] == "en")
        return await q.edit_message_text("Group access revoked." if english else "群组授权已撤销。",reply_markup=owner_authorized_keyboard(0) if owner_list else await group_keyboard(update,True))
    if data.startswith("custom:"):
        _,cs,us,sign=data.split(":"); cs=int(cs); context.user_data["state"]="custom"; context.user_data["custom"]=(cs,int(us),int(sign)); return await q.edit_message_text(tr(cs,"请输入调整数量（1-1000000）：","Enter an amount (1-1,000,000):"))


async def member_page(q, context, cid, uid, notice=""):
    u=store.find_user(cid,uid); english=store.settings(cid)["language"] == "en"
    if english:
        text=(notice+"\n" if notice else "") + f"{u['display_name']}\nID: {uid}\nPoints: {u['total_points']}"
        kb=[[InlineKeyboardButton("+1",callback_data=f"adjust:{cid}:{uid}:1")],[InlineKeyboardButton("+5",callback_data=f"adjust:{cid}:{uid}:5")],[InlineKeyboardButton("+10",callback_data=f"adjust:{cid}:{uid}:10")],[InlineKeyboardButton("-1",callback_data=f"adjust:{cid}:{uid}:-1")],[InlineKeyboardButton("-5",callback_data=f"adjust:{cid}:{uid}:-5")],[InlineKeyboardButton("-10",callback_data=f"adjust:{cid}:{uid}:-10")],[InlineKeyboardButton("Custom +",callback_data=f"custom:{cid}:{uid}:1")],[InlineKeyboardButton("Custom -",callback_data=f"custom:{cid}:{uid}:-1")],[InlineKeyboardButton("Recent",callback_data=f"recent:{cid}:{uid}")],[InlineKeyboardButton("Search again",callback_data=f"search:{cid}")],[InlineKeyboardButton("Back",callback_data=f"g:{cid}")]]
    else:
        text=(notice+"\n" if notice else "") + f"{u['display_name']}\nID: {uid}\n积分：{u['total_points']}"
        kb=[[InlineKeyboardButton("+1",callback_data=f"adjust:{cid}:{uid}:1"),InlineKeyboardButton("+5",callback_data=f"adjust:{cid}:{uid}:5"),InlineKeyboardButton("+10",callback_data=f"adjust:{cid}:{uid}:10")],[InlineKeyboardButton("-1",callback_data=f"adjust:{cid}:{uid}:-1"),InlineKeyboardButton("-5",callback_data=f"adjust:{cid}:{uid}:-5"),InlineKeyboardButton("-10",callback_data=f"adjust:{cid}:{uid}:-10")],[InlineKeyboardButton("自定义增加",callback_data=f"custom:{cid}:{uid}:1"),InlineKeyboardButton("自定义减少",callback_data=f"custom:{cid}:{uid}:-1")],[InlineKeyboardButton("最近调整记录",callback_data=f"recent:{cid}:{uid}"),InlineKeyboardButton("重新搜索",callback_data=f"search:{cid}")],[InlineKeyboardButton("返回",callback_data=f"g:{cid}")]]
    return await q.edit_message_text(text,reply_markup=InlineKeyboardMarkup(kb))


async def settings_page(q, cid):
    s=store.settings(cid); english=s["language"] == "en"; labels={k:v[1 if english else 0] for k,v in SETTING_LABELS.items()}
    title="Group settings" if english else "群组设置"
    min_value = "No minimum" if english and s["min_chars"] == 0 else ("不限" if s["min_chars"] == 0 else str(s["min_chars"]))
    limit_value = "Unlimited" if english and s["daily_limit"] == 0 else ("不限" if s["daily_limit"] == 0 else str(s["daily_limit"]))
    language_label = "Language: English" if english else "语言：中文"
    timezone_label = TIMEZONE_LABELS.get(s["timezone"], (s["timezone"], s["timezone"]))[1 if english else 0]
    body=f"{labels['min_chars']}: {min_value}\n{labels['daily_limit']}: {limit_value}\n{labels['checkin_points']}: {s['checkin_points']}\n{language_label}\n{'Timezone' if english else '时区'}: {timezone_label}"
    lang_to="zh" if english else "en"; lang_label="中文" if english else "English"
    rows=[[InlineKeyboardButton(labels['min_chars'],callback_data=f"edit:{cid}:min_chars")],[InlineKeyboardButton(labels['daily_limit'],callback_data=f"edit:{cid}:daily_limit")],[InlineKeyboardButton(labels['checkin_points'],callback_data=f"edit:{cid}:checkin_points")],[InlineKeyboardButton(lang_label,callback_data=f"lang:{cid}:{lang_to}")],[InlineKeyboardButton("Timezone" if english else "选择时区",callback_data=f"tzlist:{cid}")],[InlineKeyboardButton("Back" if english else "返回",callback_data=f"g:{cid}")]]
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
            status=" [已撤销]" if not r["enabled"] else ""
            keyboard.append([InlineKeyboardButton(r["title"]+status,callback_data=f"g:{r['chat_id']}")])
        keyboard.append([InlineKeyboardButton("返回群组列表",callback_data="home:0")])
        return await update.effective_message.reply_text(f"找到 {len(rows)} 个群组：",reply_markup=InlineKeyboardMarkup(keyboard))
    if state=="owner_auth_search":
        if update.effective_user.id != OWNER_ID: context.user_data.pop("state",None); return
        rows=store.chats(enabled_only=True); needle=text.casefold();
        if text.lstrip("-").isdigit(): rows=[r for r in rows if r["chat_id"] == int(text)]
        else: rows=[r for r in rows if needle in r["title"].casefold()]
        context.user_data.pop("state",None)
        if not rows: return await update.effective_message.reply_text("没有找到已授权群组。",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("返回",callback_data="ownerauthorized")]]))
        return await update.effective_message.reply_text("选择要解除授权的群组：",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(r["title"],callback_data=f"revokeask:{r['chat_id']}:owner")] for r in rows[:20]]+[[InlineKeyboardButton("返回",callback_data="ownerauthorized")]]))
    if state=="search":
        if not text.isdigit() or int(text)<=0:return await update.effective_message.reply_text(tr(cid,"请输入有效的正整数 Telegram ID。","Enter a valid positive Telegram ID."))
        u=store.find_user(cid,text)
        if not u:return await update.effective_message.reply_text(tr(cid,"找不到该成员；请让他先在群里发言。","Member not found. They must speak in the group first."))
        context.user_data.pop("state",None); english=store.settings(cid)["language"] == "en"; return await update.effective_message.reply_text(f"{u['display_name']}（{u['user_id']}）{('Points' if english else '积分')}：{u['total_points']}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open member" if english else "打开成员页面",callback_data=f"member:{cid}:{u['user_id']}")]]))
    if state=="setting":
        if not text.isdigit():return await update.effective_message.reply_text(tr(cid,"请输入非负整数。","Enter a non-negative integer."))
        cs,key=context.user_data.pop("edit"); context.user_data.pop("state",None); context.user_data["pending_setting"]=(cs,key,int(text)); old=store.settings(cs)[key]
        label=SETTING_LABELS[key][1 if store.settings(cs)["language"] == "en" else 0]
        english=store.settings(cs)["language"] == "en"; return await update.effective_message.reply_text(f"{'Confirm' if english else '确认修改'} {label}: {old} -> {int(text)}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm" if english else "确认",callback_data="save_setting"),InlineKeyboardButton("Cancel" if english else "取消",callback_data="cancel_setting")]]))
    if state=="alias":
        p=text.split(None,1)
        if len(p)!=2 or not store.add_alias(cid,p[0].lower(),p[1]):return await update.effective_message.reply_text(tr(cid,"格式错误或别名冲突，请使用：score /points","Invalid format or conflicting alias. Example: score /points"))
        context.user_data.pop("state",None); return await update.effective_message.reply_text(tr(cid,"别名已保存。","Alias saved."),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back" if store.settings(cid)["language"] == "en" else "返回",callback_data=f"g:{cid}")]]))
    if state=="custom":
        if not text.isdigit() or not 1<=int(text)<=1000000:return await update.effective_message.reply_text(tr(cid,"请输入 1 到 1000000 的整数。","Enter an integer from 1 to 1,000,000."))
        cs,us,sign=context.user_data.pop("custom"); context.user_data.pop("state",None)
        delta=sign*int(text); u=store.find_user(cs,us); context.user_data["pending"]=(cs,us,delta)
        chat=store.conn.execute("SELECT title FROM chats WHERE chat_id=?",(cs,)).fetchone(); title=chat[0] if chat else str(cs)
        english=store.settings(cs)["language"] == "en"; return await update.effective_message.reply_text(tr(cs,f"群组：{title}\n确认调整 {u['display_name']}（{us}）\n当前积分：{u['total_points']}\n本次变化：{delta:+d}\n调整后：{u['total_points']+delta}",f"Group: {title}\nConfirm adjustment for {u['display_name']} ({us})\nCurrent points: {u['total_points']}\nChange: {delta:+d}\nAfter: {u['total_points']+delta}"),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Confirm" if english else "确认",callback_data="confirm"),InlineKeyboardButton("Cancel" if english else "取消",callback_data=f"member:{cs}:{us}")]]))


async def post_init(application):
    """Register the static commands shown when a user types '/'."""
    try:
        await application.bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        print(f"命令列表注册失败，将继续运行：{exc}")


def build_app(token):
    app=Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler("activate",activate)); app.add_handler(CommandHandler("score",score_cmd)); app.add_handler(CommandHandler("rank",lambda u,c:rank_cmd(u,c,False))); app.add_handler(CommandHandler("today",lambda u,c:rank_cmd(u,c,True))); app.add_handler(CommandHandler("addpoints",lambda u,c:points_cmd(u,c,"addpoints"))); app.add_handler(CommandHandler("subpoints",lambda u,c:points_cmd(u,c,"subpoints"))); app.add_handler(CommandHandler("start",start)); app.add_handler(CallbackQueryHandler(callback)); app.add_handler(MessageHandler(filters.TEXT,text_handler)); return app


def main():
    token=os.getenv("BOT_TOKEN")
    if not token: raise SystemExit("缺少 BOT_TOKEN 环境变量")
    if not OWNER_ID: raise SystemExit("缺少有效 OWNER_ID 环境变量")
    print(f"配置读取成功，数据库：{os.path.abspath(DB_PATH)}")
    build_app(token).run_polling()


if __name__ == "__main__": main()
