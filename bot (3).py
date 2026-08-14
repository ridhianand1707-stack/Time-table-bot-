"""
B&FS Term IV Timetable Bot
---------------------------
Lets classmates pick their subjects (+ batch, where relevant) once,
then ask for today's / tomorrow's / any day's personal class schedule.

Run:  python3 bot.py
Needs: BOT_TOKEN environment variable (get one from @BotFather on Telegram)
"""

import json
import os
import random
import re
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import sheets_fetch

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")
SCHEDULE_PATH = os.path.join(os.path.dirname(__file__), "schedule.json")
IST = ZoneInfo("Asia/Kolkata")

# Set this to YOUR numeric Telegram chat id as a Railway env var to enable
# the approval gate — new users will need your Approve tap before they can
# use anything. Get your id by messaging @userinfobot on Telegram.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
if ADMIN_CHAT_ID:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)


def now_ist():
    """Server clocks (Railway/Render) run on UTC. Without this, /today would
    show the wrong date for a chunk of the night in India (e.g. at 1am IST,
    UTC is still 'yesterday')."""
    return datetime.now(IST)

# How often to re-pull the live Google Sheet, in seconds. 15 min is a
# reasonable balance between freshness and not hammering Sheets.
REFRESH_SECONDS = 15 * 60

# Subjects relevant to the B&FS section. Add/remove here if the elective mix changes.
SUBJECTS = {
    "FMA": "Financial Management & Accounting",
    "DT": "Design Thinking",
    "RM": "Risk Management",
    "TASS": "TASS",
    "FIS": "FIS",
    "AFSA": "AFSA",
    "IB": "International Business",
    "SAPM": "Security Analysis & Portfolio Mgmt",
    "SM": "Strategic Management",
    "TFEM": "TFEM",
    "FD-II": "Financial Derivatives - II (Core, Prof. Himadri Das)",
    "CRA": "CRA (Prof. Jayatu Sen, post mid-term)",
    "FM": "Financial Modelling (Post Mid Term)",
}

# Subjects that run in multiple batches in the sheet -> ask which batch.
# FD-II is BFS's own core subject (single track, no batch split) — kept
# separate from "FD" in the combined sheet, which belongs to other sections.
BATCHED_SUBJECTS = {"FIS", "SAPM", "TASS", "AFSA"}
BATCH_OPTIONS = ["I", "II", "III", "ALL"]  # ALL = show every batch's slot

# 24-hour start time for each session, used to compute reminder times.
SESSION_START_24H = {
    "S1": (8, 30), "S2": (10, 15), "S3": (12, 0),
    "S4": (14, 30), "S5": (16, 15), "S6": (18, 0),
}
REMINDER_MINUTES_BEFORE = 20
QUIZ_REMINDER_HOURS_BEFORE = 24
DEFAULT_QUIZ_TIME = (18, 0)  # fallback if a quiz's exact time isn't listed on the sheet

CLASS_REMINDER_QUIPS = [
    "🚨 Alert! In {mins} minutes you'll be legally required to *pretend* to pay attention.",
    "⏰ {mins}-minute warning: time to locate your dignity and your notebook, in that order.",
    "🏃 Class in {mins} min! Coffee now, or regret later — your call.",
    "📢 Breaking news: your class starts in {mins} minutes. Experts recommend showing up.",
    "🎬 And... action! Your class starts in {mins} minutes. Try not to walk in like you just woke up (even if you did).",
    "🔔 Ding ding! {mins} minutes till class. Time to switch from \"main character\" to \"attentive student\" mode.",
    "🚀 T-minus {mins} minutes to liftoff. Destination: your classroom. Snacks optional, attendance not.",
    "📚 {mins}-minute PSA: close that reel, open your notes (or at least pretend to).",
    "🫠 {mins} minutes of freedom left. Spend them wisely, or at least caffeinated.",
    "🎯 {mins} min to class. This is the universe's way of telling you to stop scrolling.",
]

QUIZ_REMINDER_QUIPS = [
    "😬 24 hours till your *{subject} {label}*. This is your sign to actually open the notes.",
    "📖 Plot twist: there's a *{subject} {label}* tomorrow. Your future self is counting on you tonight.",
    "🧠 Tomorrow's mission: survive the *{subject} {label}*. Today's mission: prepare so you actually can.",
    "⏳ 24-hour countdown to *{subject} {label}* has begun. Cramming starts... now?",
    "🚨 Quiz alert! *{subject} {label}* is tomorrow. May the odds (and your notes) be in your favor.",
    "📢 Gentle reminder that adulting includes studying for tomorrow's *{subject} {label}*.",
    "🕵️ Rumor has it there's a *{subject} {label}* tomorrow. Rumor also says studying helps.",
    "🎲 24 hours on the clock for *{subject} {label}*. Choose your own adventure: study now, or panic later.",
]


def parse_ampm_time(s):
    """Parses loose formats like '2.15 p.m', '10 a.m', '1:35pm' into 24-hour
    (hour, minute). Returns None if nothing recognizable is found."""
    if not s:
        return None
    m = re.search(r'(\d{1,2})[.:]?(\d{2})?\s*([ap])\.?m\.?', s, re.I)
    if not m:
        return None
    hour = int(m.group(1)) % 12
    minute = int(m.group(2)) if m.group(2) else 0
    if m.group(3).lower() == 'p':
        hour += 12
    return hour, minute

def load_schedule_at_startup():
    """Try to fetch the live sheet first; fall back to the last cached
    schedule.json on disk if the fetch fails (e.g. no network yet)."""
    try:
        sched = sheets_fetch.fetch_and_build()
        print(f"Loaded live schedule: {len(sched)} dates.")
        return sched
    except Exception as e:
        print(f"Live fetch failed at startup ({e}), falling back to cached schedule.json")
        with open(SCHEDULE_PATH) as f:
            return json.load(f)


SCHEDULE = load_schedule_at_startup()


async def refresh_schedule_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs on a timer via the JobQueue to keep SCHEDULE in sync with the
    live Google Sheet without needing a bot restart."""
    global SCHEDULE
    old_schedule = SCHEDULE
    try:
        new_schedule = sheets_fetch.fetch_and_build()
        changes = diff_schedules(old_schedule, new_schedule)
        quiz_changes = diff_quizzes(old_schedule, new_schedule)
        SCHEDULE = new_schedule
        print(f"Refreshed schedule from live sheet: {len(SCHEDULE)} dates.")
        if changes:
            print(f"Detected {len(changes)} schedule change(s), broadcasting...")
            await broadcast_schedule_changes(context.bot, changes)
        if quiz_changes:
            print(f"Detected {len(quiz_changes)} new quiz(zes), notifying relevant users...")
            await notify_quiz_changes(context.bot, quiz_changes)
        schedule_all_reminders_for_all_users(context.application.job_queue)
    except Exception as e:
        print(f"Scheduled refresh failed, keeping previous data: {e}")

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_subjects (
            chat_id INTEGER,
            code TEXT,
            batch TEXT,
            PRIMARY KEY (chat_id, code)
        )"""
    )
    return conn


def get_user_subjects(chat_id):
    conn = db()
    rows = conn.execute(
        "SELECT code, batch FROM user_subjects WHERE chat_id=?", (chat_id,)
    ).fetchall()
    conn.close()
    return {code: batch for code, batch in rows}


def set_user_subject(chat_id, code, batch=None):
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO user_subjects (chat_id, code, batch) VALUES (?,?,?)",
        (chat_id, code, batch),
    )
    conn.commit()
    conn.close()


def remove_user_subject(chat_id, code):
    conn = db()
    conn.execute(
        "DELETE FROM user_subjects WHERE chat_id=? AND code=?", (chat_id, code)
    )
    conn.commit()
    conn.close()


def clear_user(chat_id):
    conn = db()
    conn.execute("DELETE FROM user_subjects WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def get_all_chat_ids():
    conn = db()
    rows = conn.execute("SELECT DISTINCT chat_id FROM user_subjects").fetchall()
    conn.close()
    return [r[0] for r in rows]


def register_chat(chat_id):
    """Tracks every chat that has ever interacted with the bot, so schedule
    change broadcasts reach everyone — even someone who hasn't run /setup
    yet."""
    conn = db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS known_chats (chat_id INTEGER PRIMARY KEY)"
    )
    conn.execute("INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()


def get_all_known_chats():
    conn = db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS known_chats (chat_id INTEGER PRIMARY KEY)"
    )
    rows = conn.execute("SELECT chat_id FROM known_chats").fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Approval gate — if ADMIN_CHAT_ID is set, new users need the admin's
# Approve tap before they can use anything else.
# ---------------------------------------------------------------------------

def _approval_db():
    conn = db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS approved_users "
        "(chat_id INTEGER PRIMARY KEY, username TEXT, approved_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pending_requests "
        "(chat_id INTEGER PRIMARY KEY, username TEXT, requested_at TEXT)"
    )
    return conn


def is_approved(chat_id):
    if not ADMIN_CHAT_ID:
        return True  # gate disabled — no admin configured
    if chat_id == ADMIN_CHAT_ID:
        return True
    conn = _approval_db()
    row = conn.execute("SELECT 1 FROM approved_users WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def add_pending_request(chat_id, username):
    conn = _approval_db()
    conn.execute(
        "INSERT OR IGNORE INTO pending_requests (chat_id, username, requested_at) VALUES (?, ?, ?)",
        (chat_id, username or "", now_ist().isoformat()),
    )
    conn.commit()
    conn.close()


def is_pending(chat_id):
    conn = _approval_db()
    row = conn.execute("SELECT 1 FROM pending_requests WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def approve_user(chat_id, username):
    conn = _approval_db()
    conn.execute(
        "INSERT OR REPLACE INTO approved_users (chat_id, username, approved_at) VALUES (?, ?, ?)",
        (chat_id, username or "", now_ist().isoformat()),
    )
    conn.execute("DELETE FROM pending_requests WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def deny_user(chat_id):
    conn = _approval_db()
    conn.execute("DELETE FROM pending_requests WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def get_pending_requests():
    conn = _approval_db()
    rows = conn.execute("SELECT chat_id, username FROM pending_requests").fetchall()
    conn.close()
    return rows


async def require_approved(update: Update) -> bool:
    """Call at the top of any command that should be gated. Returns True if
    the user can proceed; if not, sends them the right message and returns
    False so the caller just returns immediately."""
    chat_id = update.effective_chat.id
    if is_approved(chat_id):
        return True
    if is_pending(chat_id):
        await update.message.reply_text("Your request is still awaiting approval — hang tight!")
    else:
        await update.message.reply_text(
            "This bot is invite-only right now. Send /start to request access."
        )
    return False


# ---------------------------------------------------------------------------
# Schedule lookup
# ---------------------------------------------------------------------------

def get_session_matches(day_block, sname, my_subjects):
    """Returns a list of formatted class strings for one session that match
    this user's selected subjects (and batch, if applicable)."""
    matches = []
    for cls in day_block["sessions"][sname]["classes"]:
        code = cls["canonical"]
        if code not in my_subjects:
            continue
        wanted_batch = my_subjects[code]
        if cls["batch"] and wanted_batch and wanted_batch != "ALL" and cls["batch"] != wanted_batch:
            continue
        venue = f" @ {cls['venue']}" if cls["venue"] else ""
        prof = f" ({cls['prof']})" if cls["prof"] else ""
        batch_tag = f" [Batch {cls['batch']}]" if cls["batch"] else ""
        matches.append(f"{code}{batch_tag}{prof}{venue}")
    return matches


def get_matched_codes(day_block, sname, my_subjects):
    """Like get_session_matches, but returns just the canonical subject codes
    (no formatting) — used for subject-wise counting in /monthly."""
    codes = []
    for cls in day_block["sessions"][sname]["classes"]:
        code = cls["canonical"]
        if code not in my_subjects:
            continue
        wanted_batch = my_subjects[code]
        if cls["batch"] and wanted_batch and wanted_batch != "ALL" and cls["batch"] != wanted_batch:
            continue
        codes.append(code)
    return codes

def format_day(date_str, chat_id):
    day_block = SCHEDULE.get(date_str)
    pretty_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d %b %Y (%A)")

    if not day_block:
        return f"📅 *{pretty_date}*\nNo data for this date (outside the term schedule)."

    my_subjects = get_user_subjects(chat_id)
    lines = [f"📅 *{pretty_date}*"]
    found_any = False

    for sname in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        sess = day_block["sessions"][sname]
        matches = get_session_matches(day_block, sname, my_subjects)

        if matches:
            found_any = True
            lines.append(f"\n🕒 *{sess['time']}*")
            for m in matches:
                lines.append(f"  • {m}")

        for q in sess.get("quizzes", []):
            if q["subject"] in my_subjects:
                found_any = True
                time_part = f" at {q['time']}" if q["time"] else ""
                venue_part = f" — {q['venue']}" if q["venue"] else ""
                lines.append(f"\n📝 *{q['subject']} {q['label']}*{time_part}{venue_part}")

        if sess["events"]:
            found_any = True
            for ev in sess["events"]:
                lines.append(f"\n📌 {ev}")

    if not found_any:
        lines.append("\nNo classes from your selected subjects today. 🎉")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Change detection — broadcasts a message to everyone when the live sheet
# actually adds/removes a class or event, whenever the schedule is refreshed.
# ---------------------------------------------------------------------------

def _class_label(code, batch):
    return code + (f" [Batch {batch}]" if batch and batch != "ALL" else "")


def diff_schedules(old, new):
    """Compares two schedule dicts and returns a list of plain-language
    change descriptions — classes added/removed per date+session, plus new
    event notices (exam dates, holidays, guest sessions, etc.)."""
    changes = []
    all_dates = sorted(set(old.keys()) | set(new.keys()))

    for d in all_dates:
        old_day, new_day = old.get(d), new.get(d)
        pretty_date = datetime.strptime(d, "%Y-%m-%d").strftime("%d %b")

        if old_day is None:
            changes.append(f"🆕 {pretty_date} added to the schedule")
            continue
        if new_day is None:
            changes.append(f"❌ {pretty_date} removed from the schedule")
            continue

        for sname in SESSION_START_24H:
            old_sess = old_day["sessions"][sname]
            new_sess = new_day["sessions"][sname]
            old_keys = {(c["canonical"], c["batch"]) for c in old_sess["classes"]}
            new_keys = {(c["canonical"], c["batch"]) for c in new_sess["classes"]}

            for code, batch in new_keys - old_keys:
                changes.append(f"➕ {pretty_date} {new_sess['time']}: {_class_label(code, batch)} added")
            for code, batch in old_keys - new_keys:
                changes.append(f"➖ {pretty_date} {old_sess['time']}: {_class_label(code, batch)} removed")

            old_events, new_events = set(old_sess["events"]), set(new_sess["events"])
            for ev in new_events - old_events:
                changes.append(f"📌 {pretty_date} {new_sess['time']}: {ev}")

    return changes


async def broadcast_schedule_changes(bot_obj, changes):
    if not changes:
        return
    MAX_LINES = 25
    shown = changes[:MAX_LINES]
    text = "📢 *Timetable updated*\n\n" + "\n".join(shown)
    if len(changes) > MAX_LINES:
        text += f"\n\n...and {len(changes) - MAX_LINES} more changes."
    for chat_id in get_all_known_chats():
        try:
            await bot_obj.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception as e:
            print(f"Couldn't message {chat_id}: {e}")


def diff_quizzes(old, new):
    """Returns newly-appeared quiz announcements as (date, session, quiz_dict)
    tuples — used to send a PERSONALIZED ping only to people who actually
    have that quiz's subject selected, unlike the general broadcast above."""
    changes = []
    for d, new_day in new.items():
        old_day = old.get(d)
        for sname in SESSION_START_24H:
            new_sess = new_day["sessions"][sname]
            old_quizzes = old_day["sessions"][sname].get("quizzes", []) if old_day else []
            old_keys = {(q["subject"], q["label"]) for q in old_quizzes}
            for q in new_sess.get("quizzes", []):
                if (q["subject"], q["label"]) not in old_keys:
                    changes.append((d, sname, q))
    return changes


async def notify_quiz_changes(bot_obj, quiz_changes):
    if not quiz_changes:
        return
    for chat_id in get_all_known_chats():
        my_subjects = get_user_subjects(chat_id)
        relevant = [(d, sname, q) for (d, sname, q) in quiz_changes if q["subject"] in my_subjects]
        if not relevant:
            continue
        lines = ["📝 *New quiz announced for one of your subjects!*\n"]
        for d, sname, q in relevant:
            pretty_date = datetime.strptime(d, "%Y-%m-%d").strftime("%d %b")
            time_part = f" at {q['time']}" if q["time"] else ""
            venue_part = f" — {q['venue']}" if q["venue"] else ""
            lines.append(f"• {q['subject']} {q['label']} — {pretty_date}{time_part}{venue_part}")
        try:
            await bot_obj.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            print(f"Couldn't message {chat_id}: {e}")


# ---------------------------------------------------------------------------
# Reminders — scheduled 20 min before each matched class start time
# ---------------------------------------------------------------------------

async def send_class_reminder(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    await context.bot.send_message(chat_id=data["chat_id"], text=data["text"], parse_mode="Markdown")


def schedule_reminders_for_chat(job_queue, chat_id, dates=None):
    """(Re)schedules today's + tomorrow's reminders for one user, based on
    their currently saved subjects. Safe to call repeatedly — it clears any
    previously scheduled reminders for those dates first, so it always
    reflects the latest subject selection."""
    my_subjects = get_user_subjects(chat_id)
    if dates is None:
        today_str = now_ist().strftime("%Y-%m-%d")
        tomorrow_str = (now_ist() + timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [today_str, tomorrow_str]

    for date_str in dates:
        day_block = SCHEDULE.get(date_str)
        for sname in ["S1", "S2", "S3", "S4", "S5", "S6"]:
            job_name = f"remind:{chat_id}:{date_str}:{sname}"
            # Clear any existing job for this slot first (e.g. subjects changed).
            for job in job_queue.get_jobs_by_name(job_name):
                job.schedule_removal()

            if not my_subjects or not day_block:
                continue

            matches = get_session_matches(day_block, sname, my_subjects)
            if not matches:
                continue

            year, month, day = (int(x) for x in date_str.split("-"))
            hour, minute = SESSION_START_24H[sname]
            class_start = datetime(year, month, day, hour, minute, tzinfo=IST)
            reminder_time = class_start - timedelta(minutes=REMINDER_MINUTES_BEFORE)

            if reminder_time <= now_ist():
                continue  # this slot's reminder window has already passed

            text = (
                f"{random.choice(CLASS_REMINDER_QUIPS).format(mins=REMINDER_MINUTES_BEFORE)}\n"
                f"🕒 {day_block['sessions'][sname]['time']}\n"
                + "\n".join(f"• {m}" for m in matches)
            )
            job_queue.run_once(
                send_class_reminder,
                when=reminder_time,
                data={"chat_id": chat_id, "text": text},
                name=job_name,
            )


def schedule_quiz_reminders_for_chat(job_queue, chat_id):
    """Schedules a reminder 24 hours before each quiz for the user's
    selected subjects, scanning the WHOLE term (not just today/tomorrow,
    since a quiz could be announced weeks ahead)."""
    my_subjects = get_user_subjects(chat_id)
    if not my_subjects:
        return

    for date_str, day_block in SCHEDULE.items():
        year, month, day = (int(x) for x in date_str.split("-"))
        for sname in SESSION_START_24H:
            for q in day_block["sessions"][sname].get("quizzes", []):
                if q["subject"] not in my_subjects:
                    continue

                job_name = f"quizremind:{chat_id}:{date_str}:{sname}:{q['subject']}:{q['label']}"
                for job in job_queue.get_jobs_by_name(job_name):
                    job.schedule_removal()

                parsed = parse_ampm_time(q.get("time"))
                if parsed:
                    hour, minute = parsed
                    time_note = q["time"]
                else:
                    hour, minute = DEFAULT_QUIZ_TIME
                    time_note = "exact time not listed yet — check with class"

                quiz_dt = datetime(year, month, day, hour, minute, tzinfo=IST)
                reminder_dt = quiz_dt - timedelta(hours=QUIZ_REMINDER_HOURS_BEFORE)
                if reminder_dt <= now_ist():
                    continue

                pretty_date = quiz_dt.strftime("%d %b")
                venue_part = f" @ {q['venue']}" if q["venue"] else ""
                quip = random.choice(QUIZ_REMINDER_QUIPS).format(subject=q["subject"], label=q["label"])
                text = f"{quip}\n🗓 {pretty_date}, {time_note}{venue_part}"

                job_queue.run_once(
                    send_class_reminder,
                    when=reminder_dt,
                    data={"chat_id": chat_id, "text": text},
                    name=job_name,
                )


def schedule_all_reminders_for_chat(job_queue, chat_id, dates=None):
    schedule_reminders_for_chat(job_queue, chat_id, dates=dates)
    schedule_quiz_reminders_for_chat(job_queue, chat_id)


def schedule_all_reminders_for_all_users(job_queue):
    for chat_id in get_all_chat_ids():
        schedule_all_reminders_for_chat(job_queue, chat_id)


async def daily_reminder_scheduling_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs once a day so tomorrow's reminders (and any newly-relevant quiz
    reminders) get queued up automatically."""
    schedule_all_reminders_for_all_users(context.application.job_queue)
    print("Rescheduled reminders for all users.")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat(chat_id)
    user = update.effective_user
    username = f"@{user.username}" if user and user.username else (user.full_name if user else "Unknown")

    if not is_approved(chat_id):
        if not is_pending(chat_id):
            add_pending_request(chat_id, username)
            if ADMIN_CHAT_ID:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve:{chat_id}"),
                    InlineKeyboardButton("❌ Deny", callback_data=f"deny:{chat_id}"),
                ]])
                try:
                    await context.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=f"🔔 New access request from {username} (id: {chat_id})",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    print(f"Couldn't notify admin: {e}")
        await update.message.reply_text(
            "This bot is invite-only right now — I've sent your request to the admin. "
            "You'll get a message here as soon as you're approved!"
        )
        return

    await update.message.reply_text(
        "Hey! I'm the B&FS Term IV timetable bot. 👋\n\n"
        "First, tell me which subjects you're taking with /setup — "
        "I'll remember it, so you only do this once.\n\n"
        "Then just ask me:\n"
        "/today — today's classes\n"
        "/tomorrow — tomorrow's classes\n"
        "/mysubjects — see what I have saved for you\n"
        "/setup — change your subjects anytime\n"
        "/monthly — your total classes broken down by week (e.g. /monthly august)\n"
        "/refresh — force-pull the latest sheet data right now\n\n"
        f"I'll also automatically message you {REMINDER_MINUTES_BEFORE} minutes "
        "before each of your classes starts — no extra setup needed, it kicks "
        "in as soon as you've picked your subjects."
    )


def subject_keyboard(chat_id):
    my_subjects = get_user_subjects(chat_id)
    buttons = []
    buttons.append([
        InlineKeyboardButton("🌟 Select ALL subjects", callback_data="select_all"),
        InlineKeyboardButton("🧹 Clear all", callback_data="clear_all"),
    ])
    for code, name in SUBJECTS.items():
        mark = "✅ " if code in my_subjects else "☐ "
        buttons.append(
            [InlineKeyboardButton(f"{mark}{code} - {name}", callback_data=f"toggle:{code}")]
        )
    buttons.append([InlineKeyboardButton("✅ Done", callback_data="done")])
    return InlineKeyboardMarkup(buttons)


async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat(chat_id)
    if not await require_approved(update):
        return
    await update.message.reply_text(
        "Tap 🌟 Select ALL subjects if you're taking everything B&FS offers "
        "(this also covers every batch, so nothing gets missed). "
        "Otherwise tap individual subjects — tap again to remove. "
        "Hit ✅ Done when finished.",
        reply_markup=subject_keyboard(chat_id),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    data = query.data

    if data.startswith("approve:") or data.startswith("deny:"):
        # Only the admin's own chat can action these buttons.
        if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
            return
        action, target_id_str = data.split(":")
        target_id = int(target_id_str)
        rows = get_pending_requests()
        username = next((u for cid, u in rows if cid == target_id), str(target_id))

        if action == "approve":
            approve_user(target_id, username)
            await query.edit_message_text(f"✅ Approved {username} (id: {target_id})")
            try:
                await context.bot.send_message(
                    chat_id=target_id,
                    text="✅ You've been approved! Send /start to get going, then /setup to pick your subjects.",
                )
            except Exception as e:
                print(f"Couldn't notify approved user {target_id}: {e}")
        else:
            deny_user(target_id)
            await query.edit_message_text(f"❌ Denied {username} (id: {target_id})")
            try:
                await context.bot.send_message(chat_id=target_id, text="Your request to join wasn't approved.")
            except Exception as e:
                print(f"Couldn't notify denied user {target_id}: {e}")
        return

    if data == "select_all":
        for code in SUBJECTS:
            batch = "ALL" if code in BATCHED_SUBJECTS else None
            set_user_subject(chat_id, code, batch)
        schedule_all_reminders_for_chat(context.application.job_queue, chat_id)
        await query.edit_message_reply_markup(reply_markup=subject_keyboard(chat_id))
        return

    if data == "clear_all":
        clear_user(chat_id)
        schedule_all_reminders_for_chat(context.application.job_queue, chat_id)
        await query.edit_message_reply_markup(reply_markup=subject_keyboard(chat_id))
        return

    if data.startswith("toggle:"):
        code = data.split(":", 1)[1]
        my_subjects = get_user_subjects(chat_id)
        if code in my_subjects:
            remove_user_subject(chat_id, code)
            schedule_all_reminders_for_chat(context.application.job_queue, chat_id)
        else:
            if code in BATCHED_SUBJECTS:
                # ask for batch next instead of saving immediately
                batch_buttons = [
                    [InlineKeyboardButton(f"Batch {b}" if b != "ALL" else "All batches", callback_data=f"batch:{code}:{b}")]
                    for b in BATCH_OPTIONS
                ]
                await query.edit_message_text(
                    f"Which batch are you in for *{code}*?",
                    reply_markup=InlineKeyboardMarkup(batch_buttons),
                    parse_mode="Markdown",
                )
                return
            else:
                set_user_subject(chat_id, code, None)
                schedule_all_reminders_for_chat(context.application.job_queue, chat_id)
        await query.edit_message_reply_markup(reply_markup=subject_keyboard(chat_id))

    elif data.startswith("batch:"):
        _, code, batch = data.split(":")
        set_user_subject(chat_id, code, batch)
        schedule_all_reminders_for_chat(context.application.job_queue, chat_id)
        await query.edit_message_text(
            "Tap each subject you're taking (electives included). Tap again to remove. "
            "Hit ✅ Done when finished.",
            reply_markup=subject_keyboard(chat_id),
        )

    elif data == "done":
        my_subjects = get_user_subjects(chat_id)
        if not my_subjects:
            await query.edit_message_text("No subjects selected yet — run /setup again anytime.")
            return
        summary = "\n".join(
            f"• {c}" + (f" (Batch {b})" if b and b != "ALL" else " (all batches)" if b == "ALL" else "")
            for c, b in my_subjects.items()
        )
        await query.edit_message_text(
            f"Saved! Your subjects:\n{summary}\n\n"
            f"Whenever you ask for a day, I scan both the combined PGDM "
            f"timetable and the B&FS-only timetable for these subjects and "
            f"merge the results. I'll also ping you {REMINDER_MINUTES_BEFORE} min "
            f"before each of these classes starts. Try /today or /tomorrow now."
        )


async def mysubjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await require_approved(update):
        return
    my_subjects = get_user_subjects(chat_id)
    if not my_subjects:
        await update.message.reply_text("You haven't set up your subjects yet. Run /setup.")
        return
    summary = "\n".join(
        f"• {c}" + (f" (Batch {b})" if b else "") for c, b in my_subjects.items()
    )
    await update.message.reply_text(f"Your saved subjects:\n{summary}\n\nRun /setup to change.")


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat(chat_id)
    if not await require_approved(update):
        return
    date_str = now_ist().strftime("%Y-%m-%d")
    await update.message.reply_text(format_day(date_str, chat_id), parse_mode="Markdown")


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat(chat_id)
    if not await require_approved(update):
        return
    date_str = (now_ist() + timedelta(days=1)).strftime("%Y-%m-%d")
    await update.message.reply_text(format_day(date_str, chat_id), parse_mode="Markdown")


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await require_approved(update):
        return
    for i in range(7):
        date_str = (now_ist() + timedelta(days=i)).strftime("%Y-%m-%d")
        await update.message.reply_text(format_day(date_str, chat_id), parse_mode="Markdown")


MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def parse_month_arg(args):
    """Accepts /monthly, /monthly august, /monthly 8, /monthly aug 2026 —
    defaults to the current IST month/year if nothing is given."""
    now = now_ist()
    month, year = now.month, now.year
    if args:
        m = args[0].lower()
        if m.isdigit():
            month = int(m)
        elif m in MONTH_NAMES:
            month = MONTH_NAMES[m]
        if len(args) > 1 and args[1].isdigit():
            year = int(args[1])
    return month, year


async def monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat(chat_id)
    if not await require_approved(update):
        return
    my_subjects = get_user_subjects(chat_id)
    if not my_subjects:
        await update.message.reply_text("You haven't set up your subjects yet. Run /setup first.")
        return

    month, year = parse_month_arg(context.args)
    month_label = datetime(year, month, 1).strftime("%B %Y")

    # Group this month's dates into Week 1 = days 1-7, Week 2 = days 8-14, ...
    weeks = {}
    for date_str, day_block in SCHEDULE.items():
        d = datetime.strptime(date_str, "%Y-%m-%d")
        if d.year != year or d.month != month:
            continue
        week_num = (d.day - 1) // 7 + 1
        weeks.setdefault(week_num, {"per_subject": {}, "days": []})
        weeks[week_num]["days"].append(d.day)
        for sname in SESSION_START_24H:
            for code in get_matched_codes(day_block, sname, my_subjects):
                weeks[week_num]["per_subject"][code] = weeks[week_num]["per_subject"].get(code, 0) + 1

    if not weeks:
        await update.message.reply_text(f"No schedule data found for {month_label}.")
        return

    lines = [f"📊 *{month_label} — your classes by week*"]
    grand_total = 0
    for week_num in sorted(weeks):
        info = weeks[week_num]
        day_range = f"{min(info['days'])}-{max(info['days'])} {datetime(year, month, 1).strftime('%b')}"
        week_total = sum(info["per_subject"].values())
        grand_total += week_total
        lines.append(f"\n*Week {week_num}* ({day_range}) — {week_total} classes")
        for code, count in sorted(info["per_subject"].items(), key=lambda x: -x[1]):
            lines.append(f"  • {code}: {count}")
    lines.append(f"\n*Grand total: {grand_total} classes*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: lists everyone currently waiting for approval."""
    chat_id = update.effective_chat.id
    if not ADMIN_CHAT_ID or chat_id != ADMIN_CHAT_ID:
        await update.message.reply_text("This command is admin-only.")
        return
    rows = get_pending_requests()
    if not rows:
        await update.message.reply_text("No pending requests right now.")
        return
    lines = ["🔔 *Pending requests:*\n"]
    for cid, username in rows:
        lines.append(f"• {username} (id: {cid})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await require_approved(update):
        return
    clear_user(chat_id)
    await update.message.reply_text("Cleared. Run /setup to pick your subjects again.")


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets anyone force an immediate pull from the live sheet, instead of
    waiting for the automatic timer."""
    global SCHEDULE
    register_chat(update.effective_chat.id)
    if not await require_approved(update):
        return
    old_schedule = SCHEDULE
    msg = await update.message.reply_text("Pulling the latest sheet data...")
    try:
        new_schedule = sheets_fetch.fetch_and_build()
        changes = diff_schedules(old_schedule, new_schedule)
        quiz_changes = diff_quizzes(old_schedule, new_schedule)
        SCHEDULE = new_schedule
        await msg.edit_text(f"Updated — {len(SCHEDULE)} dates loaded from the live sheet.")
        if changes:
            await broadcast_schedule_changes(context.bot, changes)
        if quiz_changes:
            await notify_quiz_changes(context.bot, quiz_changes)
        schedule_all_reminders_for_all_users(context.application.job_queue)
    except Exception as e:
        await msg.edit_text(f"Couldn't reach the sheet right now ({e}). Still using the last good data.")


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Set the BOT_TOKEN environment variable to your Telegram bot token "
            "(get one from @BotFather)."
        )
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setup", setup))
    app.add_handler(CommandHandler("mysubjects", mysubjects))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("tomorrow", tomorrow))
    app.add_handler(CommandHandler("week", week))
    app.add_handler(CommandHandler("monthly", monthly))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Keep the timetable fresh automatically without restarting the bot.
    app.job_queue.run_repeating(refresh_schedule_job, interval=REFRESH_SECONDS, first=REFRESH_SECONDS)

    # Reminders: queue up today's + tomorrow's for everyone right at startup
    # (covers restarts mid-day), then re-queue once a day for the day ahead.
    schedule_all_reminders_for_all_users(app.job_queue)
    app.job_queue.run_daily(daily_reminder_scheduling_job, time=time(hour=0, minute=10, tzinfo=IST))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
