# B&FS Term IV Timetable Bot

A Telegram bot for your section. Each classmate picks their own subjects
(electives included) once, then asks the bot for their personal daily
schedule — no more cross-referencing two Excel sheets.

**This version reads live from your mirror Google Sheet** — whenever you or
anyone edits the source timetables, the mirror updates via `IMPORTRANGE`,
and the bot automatically re-pulls it (every 15 min on a timer, or instantly
via `/refresh`). No manual re-uploading needed.

## Files
- `bot.py` — the bot itself
- `parsing.py` — shared logic that turns raw sheet rows into clean schedule data
- `sheets_fetch.py` — pulls both tabs live from the mirror sheet as CSV
- `schedule.json` — a cached copy of the last successful fetch, used as a fallback
  if the live sheet is briefly unreachable when the bot starts
- `build_schedule_from_excel.py` — optional: rebuilds `schedule.json` from the
  original Excel files instead, if you ever want to go back to that
- `requirements.txt` — Python dependencies

## How the live sync works
- Mirror sheet: `https://docs.google.com/spreadsheets/d/12uaoNuJ9kKBcJWusgT6bDa7SyqPF_XRDGBF6a7HTSrU`
  - Sheet1 (`gid=0`) — combined PGDM timetable
  - Sheet2 (`gid=1230466552`) — B&FS-only timetable
- The bot fetches both as CSV exports (`.../export?format=csv&gid=...`), which
  works because the sheet is set to "anyone with the link can view."
- **Important:** if that sharing setting ever gets changed to private, the live
  fetch will fail and the bot will silently fall back to the last cached
  `schedule.json` — so keep the mirror set to link-viewable.
- If the original locked sheets change, your `IMPORTRANGE` formulas in the
  mirror pick that up automatically (Google refreshes those roughly hourly,
  or instantly if you open the mirror and force a recalculation).

## Commands
- `/start` — welcome + instructions
- `/setup` — pick your subjects (tap to select, tap "Batch" if the subject runs in multiple batches)
- `/today`, `/tomorrow` — your personal schedule for that day
- `/week` — next 7 days
- `/mysubjects` — see what's saved for you
- `/reset` — clear your subjects and start over
- `/refresh` — force an immediate re-pull from the live sheet (otherwise it
  auto-refreshes every 15 minutes on its own)

---

## Step 1 — Create the bot (2 minutes)

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, give it a name (e.g. "B&FS Term IV Timetable") and a
   username ending in `bot` (e.g. `bfs_term4_bot`).
3. BotFather will give you a **token** — a long string like
   `123456789:ABCdefGhIJKlmNoPQRstuVwxYZ`. Save it, you'll need it below.
4. Add the bot to your class Telegram group, or just share its `t.me/yourbotname`
   link so people can DM it directly (DM is simpler — each person's subject
   picks stay private to their own chat with the bot).

## Step 2 — Run it somewhere that stays online 24/7

I can't keep this running from our chat — a Telegram bot needs a server that's
always on so it can respond any time someone messages it. Two easy free options:

### Option A: Render.com (recommended, free tier, no credit card)
1. Create a free account at render.com and a new **GitHub repo**, push these
   4 files (`bot.py`, `schedule.json`, `build_schedule.py`, `requirements.txt`) to it.
2. On Render: **New → Background Worker** → connect your repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python bot.py`
5. Under **Environment**, add a variable `BOT_TOKEN` = the token from BotFather.
6. Deploy. Render keeps it running continuously for free (background workers
   don't sleep like free web services do).

### Option B: Your own laptop (fine for testing / a small group)
```bash
pip install -r requirements.txt
export BOT_TOKEN="your-token-here"     # Windows: set BOT_TOKEN=your-token-here
python3 bot.py
```
Leave the terminal open — the bot only responds while this is running.
(For something closer to 24/7 from a laptop, run it inside `tmux`/`screen`
so it survives you closing the terminal, though the laptop itself still
needs to stay on and connected.)

### Option C: Railway.app
Same idea as Render — new project from GitHub repo, set `BOT_TOKEN` env var,
start command `python bot.py`. Railway's free tier has limited monthly hours,
so Render is the better free pick for something that needs to run all term.

---

## If the timetable changes mid-term

Nothing to do — that's the whole point of the live setup. Edits to the
original sheets flow into your `IMPORTRANGE` mirror automatically, and the
bot re-pulls the mirror every 15 minutes (or immediately via `/refresh`).

If you ever want to go back to manually re-uploading Excel files instead:
```bash
python3 build_schedule_from_excel.py
```
This regenerates `schedule.json` from local `.xlsx` files the same way the
original version of this bot worked.

## Notes on the data
- Subjects were merged from both the B&FS-only sheet and the combined PGDM
  sheet, matched by date + session.
- `RM` also covers the same course under its later names `TA` and `EBFM`
  (renamed after the mid-term in the combined sheet) — the bot treats these
  as one subject automatically.
- `TFEM`/`TEFM` (a typo in the source sheet) are also merged automatically.
- Subjects that run in multiple batches (FIS, SAPM, TASS, AFSA, FD) ask each
  student which batch they're in during `/setup`, so they only see their
  own batch's slot.
- Reminders aren't built yet — that's a separate feature for later, as discussed.
