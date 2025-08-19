import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums.chat_member_status import ChatMemberStatus
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_GROUP_ID = int(os.getenv("ALLOWED_GROUP_ID", "0"))
TZ = os.getenv("TZ", "Asia/Tashkent")

# --- Timezone helpers ---
TZINFO = ZoneInfo(TZ)

# --- Database ---
DB_PATH = "bot.db"

def db_init():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            first_name TEXT,
            username TEXT,
            joined_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS challenges(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE,
            title TEXT,
            type TEXT,             -- 'bool' | 'minutes'
            threshold INTEGER,      -- minimal minutes for 'minutes', ignored for 'bool'
            window_start TEXT,      -- 'HH:MM'
            window_end TEXT         -- 'HH:MM'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_challenges(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            challenge_id INTEGER,
            UNIQUE(user_id, challenge_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS checkins(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            challenge_id INTEGER,
            on_date TEXT,          -- 'YYYY-MM-DD' (local)
            value INTEGER,         -- minutes for 'minutes', 1 for 'bool'
            created_at TEXT
        )""")

        # seed default 3 challenges if not exist
        defaults = [
            ("reading15", "Reading15 — 15 daqiqa kitob o‘qish", "minutes", 15, "21:00", "23:59"),
            ("wake6",     "Wake6 — Ertalab 06:00 gacha uyg‘onish", "bool", 0, "05:40", "07:00"),
            ("sport20",   "Sport20 — 20 daqiqa jismoniy mashq", "minutes", 20, "19:00", "23:59"),
        ]
        for slug, title, typ, thr, ws, we in defaults:
            c.execute("INSERT OR IGNORE INTO challenges(slug,title,type,threshold,window_start,window_end) VALUES(?,?,?,?,?,?)",
                      (slug, title, typ, thr, ws, we))

def db():
    return sqlite3.connect(DB_PATH)

# --- Helpers ---
def now_local():
    return datetime.now(TZINFO)

def today_local_str():
    return now_local().date().isoformat()

def in_window(ch_row, dt: datetime):
    ws = datetime.combine(dt.date(), time.fromisoformat(ch_row["window_start"]), TZINFO)
    we = datetime.combine(dt.date(), time.fromisoformat(ch_row["window_end"]), TZINFO)
    return ws <= dt <= we

def fetchone_dict(c):
    row = c.fetchone()
    if not row:
        return None
    cols = [d[0] for d in c.description]
    return dict(zip(cols, row))

def fetchall_dict(c):
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, r)) for r in rows]

def get_user(tg_id):
    with closing(db()) as conn:
        c = conn.cursor()
        c.execute("SELECT id, tg_id, first_name, username, joined_at FROM users WHERE tg_id=?", (tg_id,))
        r = c.fetchone()
        if r: return {"id": r[0], "tg_id": r[1], "first_name": r[2], "username": r[3], "joined_at": r[4]}
        return None

def upsert_user(tg_id, first_name, username):
    u = get_user(tg_id)
    now = now_local().isoformat()
    with closing(db()) as conn, conn:
        c = conn.cursor()
        if u:
            c.execute("UPDATE users SET first_name=?, username=? WHERE tg_id=?", (first_name, username, tg_id))
            return u
        c.execute("INSERT INTO users(tg_id, first_name, username, joined_at) VALUES(?,?,?,?)",
                  (tg_id, first_name, username, now))
        return get_user(tg_id)

def get_challenges():
    with closing(db()) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM challenges ORDER BY id")
        return fetchall_dict(c)

def get_challenge_by_slug(slug):
    with closing(db()) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM challenges WHERE slug=?", (slug,))
        return fetchone_dict(c)

def join_challenge(user_id, ch_id):
    with closing(db()) as conn, conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_challenges(user_id, challenge_id) VALUES(?,?)", (user_id, ch_id))

def user_challenges(user_id):
    with closing(db()) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(\"\"\"SELECT c.* FROM challenges c
                    JOIN user_challenges uc ON uc.challenge_id=c.id
                    WHERE uc.user_id=? ORDER BY c.id\"\"\", (user_id,))
        return fetchall_dict(c)

def set_checkin(user_id, ch_id, value):
    today = today_local_str()
    now = now_local().isoformat()
    with closing(db()) as conn, conn:
        c = conn.cursor()
        # ensure one per day per challenge (replace)
        c.execute(\"\"\"DELETE FROM checkins WHERE user_id=? AND challenge_id=? AND on_date=?\"\"\",\
                  (user_id, ch_id, today))
        c.execute(\"\"\"INSERT INTO checkins(user_id, challenge_id, on_date, value, created_at)
                     VALUES(?,?,?,?,?)\"\"\", (user_id, ch_id, today, value, now))

def has_checkin(user_id, ch_id, d: date):
    with closing(db()) as conn:
        c = conn.cursor()
        c.execute(\"\"\"SELECT 1 FROM checkins WHERE user_id=? AND challenge_id=? AND on_date=?\"\"\",\
                  (user_id, ch_id, d.isoformat()))
        return c.fetchone() is not None

def streak_days(user_id, ch_id):
    # count consecutive days ending today where checkin exists
    d = date.fromisoformat(today_local_str())
    s = 0
    while has_checkin(user_id, ch_id, d):
        s += 1
        d -= timedelta(days=1)
    return s

def week_bounds(dt: datetime):
    # Monday..Sunday of current week (local)
    start = dt - timedelta(days=dt.weekday())
    end = start + timedelta(days=6)
    return start.date(), end.date()

def weekly_points_by_user():
    start, end = week_bounds(now_local())
    with closing(db()) as conn:
        c = conn.cursor()
        c.execute(\"\"\"SELECT u.tg_id, u.first_name, u.username, COUNT(*) as pts
                     FROM checkins ch
                     JOIN users u ON u.id=ch.user_id
                     WHERE date(ch.on_date) BETWEEN ? AND ?
                     GROUP BY u.tg_id, u.first_name, u.username
                     ORDER BY pts DESC, u.first_name ASC
                  \"\"\", (start.isoformat(), end.isoformat()))
        return c.fetchall()

# --- Bot setup ---
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

async def is_member_of_allowed_group(user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(ALLOWED_GROUP_ID, user_id)
        return m.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)
    except Exception:
        return False

# --- Keyboards ---
def kb_main():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏁 Challenge tanlash", callback_data="menu_join")
    kb.button(text="✅ Check-in", callback_data="menu_checkin")
    kb.button(text="📊 Statistika", callback_data="menu_stats")
    kb.button(text="🏆 Leaderboard", callback_data="menu_lb")
    kb.adjust(2,2)
    return kb.as_markup()

def kb_join(chs, user_joined_ids):
    kb = InlineKeyboardBuilder()
    for ch in chs:
        joined = " (bor)" if ch["id"] in user_joined_ids else ""
        kb.button(text=f"➕ {ch['title']}{joined}", callback_data=f"join:{ch['slug']}")
    kb.button(text="⬅️ Orqaga", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

def kb_checkin_list(chs):
    kb = InlineKeyboardBuilder()
    for ch in chs:
        kb.button(text=f"✅ {ch['title']}", callback_data=f"checkin:{ch['slug']}")
    kb.button(text="⬅️ Orqaga", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

def kb_checkin_action(ch):
    kb = InlineKeyboardBuilder()
    if ch["type"] == "minutes":
        kb.button(text=f"⏱ Minimal ({ch['threshold']} daq) — ✅", callback_data=f"do:{ch['slug']}:min")
    else:
        kb.button(text="✅ Bajardim", callback_data=f"do:{ch['slug']}:ok")
    kb.button(text="❌ Bekor qilish", callback_data="back_checkin")
    kb.adjust(1,1)
    return kb.as_markup()

# --- Handlers ---
@dp.message(CommandStart())
async def start(m: Message):
    # Only allow DM usage if user is a member of allowed group
    if m.chat.type != "private":
        return await m.reply("👋 Salom! Menga **shaxsiy** (DM) yozing: bu bot DM+guruh rejimida ishlaydi.")
    if not await is_member_of_allowed_group(m.from_user.id):
        return await m.reply("❗ Kirish cheklangan. Ruxsat faqat admin qo‘shgan guruh a’zolari uchun.")

    u = upsert_user(m.from_user.id, m.from_user.first_name or "", m.from_user.username or "")
    await m.answer(
        "Assalomu alaykum!\n"
        "Siz ruxsatlangan guruh a’zosisiz. Quyidagi menyudan foydalaning:",
        reply_markup=kb_main()
    )

@dp.callback_query(F.data == "back_main")
async def back_main(c: CallbackQuery):
    await c.message.edit_text("Asosiy menyu:", reply_markup=kb_main())
    await c.answer()

@dp.callback_query(F.data == "menu_join")
async def menu_join(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not u:
        return await c.answer("Avval /start bosing", show_alert=True)
    chs = get_challenges()
    uch = user_challenges(u["id"])
    joined_ids = {x["id"] for x in uch}
    await c.message.edit_text("Qaysi challenge(lar)ni tanlaysiz? (ko‘pini tanlash mumkin)", reply_markup=kb_join(chs, joined_ids))
    await c.answer()

@dp.callback_query(F.data.startswith("join:"))
async def join_cb(c: CallbackQuery):
    slug = c.data.split(":")[1]
    ch = get_challenge_by_slug(slug)
    if not ch:
        return await c.answer("Topilmadi.", show_alert=True)
    u = get_user(c.from_user.id)
    join_challenge(u["id"], ch["id"])
    # refresh
    chs = get_challenges()
    uch = user_challenges(u["id"])
    joined_ids = {x["id"] for x in uch}
    await c.message.edit_text("Yangilandi. Tanlashni davom ettirishingiz mumkin:", reply_markup=kb_join(chs, joined_ids))
    await c.answer("Qo‘shildingiz!", show_alert=False)

@dp.callback_query(F.data == "menu_checkin")
async def menu_checkin(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not u:
        return await c.answer("Avval /start bosing", show_alert=True)
    chs = user_challenges(u["id"])
    if not chs:
        return await c.answer("Avval challenge tanlang (🏁).", show_alert=True)
    await c.message.edit_text("Qaysi challenge uchun bugungi check-inni belgilaymiz?", reply_markup=kb_checkin_list(chs))
    await c.answer()

@dp.callback_query(F.data == "back_checkin")
async def back_checkin(c: CallbackQuery):
    await menu_checkin(c)

@dp.callback_query(F.data.startswith("checkin:"))
async def checkin_pick(c: CallbackQuery):
    slug = c.data.split(":")[1]
    ch = get_challenge_by_slug(slug)
    if not ch:
        return await c.answer("Topilmadi.", show_alert=True)
    # show action
    ws, we = ch["window_start"], ch["window_end"]
    await c.message.edit_text(
        f"**{ch['title']}**\n"
        f"⏰ Oyna: {ws}–{we}\n"
        f"Bugun uchun belgilaymizmi?",
        reply_markup=kb_checkin_action(ch),
        parse_mode="Markdown"
    )
    await c.answer()

@dp.callback_query(F.data.startswith("do:"))
async def do_checkin(c: CallbackQuery):
    _, slug, mode = c.data.split(":")
    ch = get_challenge_by_slug(slug)
    if not ch:
        return await c.answer("Topilmadi.", show_alert=True)
    now = now_local()
    if not in_window(ch, now):
        return await c.answer(f"Bu challenge oynasi hozir yopiq. Oyna: {ch['window_start']}–{ch['window_end']}", show_alert=True)

    u = get_user(c.from_user.id)
    # ensure user joined this challenge
    user_chs = {x["id"] for x in user_challenges(u["id"])}
    if ch["id"] not in user_chs:
        return await c.answer("Avval challenge ga qo‘shiling (🏁).", show_alert=True)

    value = 1
    if ch["type"] == "minutes":
        # minimal thresholdni belgilaymiz
        value = max(ch["threshold"], 1)

    set_checkin(u["id"], ch["id"], value)
    s = streak_days(u["id"], ch["id"])
    await c.message.edit_text(f"✅ **{ch['title']}** uchun bugungi check-in saqlandi!\n🔥 Joriy streak: **{s}** kun", parse_mode="Markdown", reply_markup=kb_main())
    await c.answer()

@dp.callback_query(F.data == "menu_stats")
async def menu_stats(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not u:
        return await c.answer("Avval /start bosing", show_alert=True)
    chs = user_challenges(u["id"])
    if not chs:
        return await c.answer("Avval challenge tanlang (🏁).", show_alert=True)
    lines = []
    for ch in chs:
        s = streak_days(u["id"], ch["id"])
        lines.append(f"• {ch['title']} → streak: {s} kun")
    await c.message.edit_text("📊 Shaxsiy statistika:\n" + "\n".join(lines), reply_markup=kb_main())
    await c.answer()

@dp.callback_query(F.data == "menu_lb")
async def menu_lb(c: CallbackQuery):
    table = weekly_points_by_user()
    if not table:
        return await c.answer("Bu hafta hali check-inlar yo‘q.", show_alert=True)
    start, end = week_bounds(now_local())
    lines = [f"🏆 Haftalik reyting ({start.isoformat()} – {end.isoformat()}):"]
    medal = ["🥇","🥈","🥉"]
    for i, row in enumerate(table):
        name = row[1] or ""
        username = row[2] or ""
        tag = f"@{username}" if username else name
        mark = medal[i] if i < 3 else f"{i+1}."
        lines.append(f"{mark} {tag}: {row[3]} ball")
    await c.message.edit_text("\n".join(lines), reply_markup=kb_main())
    await c.answer()

# --- Group reminders (scheduler) ---
scheduler = AsyncIOScheduler(timezone=TZ)

async def send_group_reminder(text: str):
    try:
        await bot.send_message(ALLOWED_GROUP_ID, text, disable_notification=True)
    except Exception:
        pass

def schedule_reminders():
    # Reading15: 21:00 (start eslatma)
    scheduler.add_job(lambda: asyncio.create_task(send_group_reminder("📚 *Reading15* boshlandi (21:00–23:59). Bugun check-in qilishni unutmang!")),
                      CronTrigger(hour=21, minute=0))
    # Sport20: 19:00
    scheduler.add_job(lambda: asyncio.create_task(send_group_reminder("🏃 *Sport20* oynasi ochiq (19:00–23:59). Harakat va check-in!")),
                      CronTrigger(hour=19, minute=0))
    # Wake6: 05:40
    scheduler.add_job(lambda: asyncio.create_task(send_group_reminder("⏰ *Wake6* oynasi ochildi (05:40–07:00). Erta tong – kuch!")),
                      CronTrigger(hour=5, minute=40))

# --- Run ---
async def main():
    db_init()
    schedule_reminders()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
