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
import sqlite3
from datetime import datetime, timedelta
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
    "RM": "Research Methods (incl. TA / EBFM post mid-term)",
    "TASS": "TASS",
    "FIS": "FIS",
    "AFSA": "AFSA",
    "IB": "International Business",
    "SAPM": "Security Analysis & Portfolio Mgmt",
    "SM": "Strategic Management",
    "TFEM": "TFEM",
    "FD": "FD",
}

# Subjects that run in multiple batches in the sheet -> ask which batch.
BATCHED_SUBJECTS = {"FIS", "SAPM", "TASS", "AFSA", "FD"}
BATCH_OPTIONS = ["I", "II", "III", "ALL"]  # ALL = show every batch's slot

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
    try:
        SCHEDULE = sheets_fetch.fetch_and_build()
        print(f"Refreshed schedule from live sheet: {len(SCHEDULE)} dates.")
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


# ---------------------------------------------------------------------------
# Schedule lookup
# ---------------------------------------------------------------------------

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
        matches = []
        for cls in sess["classes"]:
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

        if matches:
            found_any = True
            lines.append(f"\n🕒 *{sess['time']}*")
            for m in matches:
                lines.append(f"  • {m}")

        if sess["events"]:
            found_any = True
            for ev in sess["events"]:
                lines.append(f"\n📌 {ev}")

    if not found_any:
        lines.append("\nNo classes from your selected subjects today. 🎉")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I'm the B&FS Term IV timetable bot. 👋\n\n"
        "First, tell me which subjects you're taking with /setup — "
        "I'll remember it, so you only do this once.\n\n"
        "Then just ask me:\n"
        "/today — today's classes\n"
        "/tomorrow — tomorrow's classes\n"
        "/mysubjects — see what I have saved for you\n"
        "/setup — change your subjects anytime\n"
        "/refresh — force-pull the latest sheet data right now"
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

    if data == "select_all":
        for code in SUBJECTS:
            batch = "ALL" if code in BATCHED_SUBJECTS else None
            set_user_subject(chat_id, code, batch)
        await query.edit_message_reply_markup(reply_markup=subject_keyboard(chat_id))
        return

    if data == "clear_all":
        clear_user(chat_id)
        await query.edit_message_reply_markup(reply_markup=subject_keyboard(chat_id))
        return

    if data.startswith("toggle:"):
        code = data.split(":", 1)[1]
        my_subjects = get_user_subjects(chat_id)
        if code in my_subjects:
            remove_user_subject(chat_id, code)
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
        await query.edit_message_reply_markup(reply_markup=subject_keyboard(chat_id))

    elif data.startswith("batch:"):
        _, code, batch = data.split(":")
        set_user_subject(chat_id, code, batch)
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
            f"merge the results. Try /today or /tomorrow now."
        )


async def mysubjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
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
    date_str = now_ist().strftime("%Y-%m-%d")
    await update.message.reply_text(format_day(date_str, chat_id), parse_mode="Markdown")


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    date_str = (now_ist() + timedelta(days=1)).strftime("%Y-%m-%d")
    await update.message.reply_text(format_day(date_str, chat_id), parse_mode="Markdown")


async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    for i in range(7):
        date_str = (now_ist() + timedelta(days=i)).strftime("%Y-%m-%d")
        await update.message.reply_text(format_day(date_str, chat_id), parse_mode="Markdown")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    clear_user(chat_id)
    await update.message.reply_text("Cleared. Run /setup to pick your subjects again.")


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets anyone force an immediate pull from the live sheet, instead of
    waiting for the automatic timer."""
    global SCHEDULE
    msg = await update.message.reply_text("Pulling the latest sheet data...")
    try:
        SCHEDULE = sheets_fetch.fetch_and_build()
        await msg.edit_text(f"Updated — {len(SCHEDULE)} dates loaded from the live sheet.")
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
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Keep the timetable fresh automatically without restarting the bot.
    app.job_queue.run_repeating(refresh_schedule_job, interval=REFRESH_SECONDS, first=REFRESH_SECONDS)

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
