# studybot.py
import asyncio
import aiosqlite
import logging
import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Read token from environment variable
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "studybot.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set. Set the BOT_TOKEN env var before running.")
    raise SystemExit("Set BOT_TOKEN environment variable.")

# Simple SQLite leaderboard
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS leaderboard (
                   chat_id INTEGER,
                   user_id INTEGER,
                   username TEXT,
                   total_minutes INTEGER,
                   PRIMARY KEY(chat_id, user_id)
               )"""
        )
        await db.commit()

async def add_study_minutes(chat_id: int, user_id: int, username: str, minutes: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT total_minutes FROM leaderboard WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        row = await cur.fetchone()
        if row:
            total = row[0] + minutes
            await db.execute("UPDATE leaderboard SET total_minutes = ?, username = ? WHERE chat_id = ? AND user_id = ?", (total, username, chat_id, user_id))
        else:
            await db.execute("INSERT INTO leaderboard(chat_id, user_id, username, total_minutes) VALUES (?, ?, ?, ?)", (chat_id, user_id, username, minutes))
        await db.commit()

# In-memory sessions
active_sessions = {}
session_tasks = {}

async def run_session(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = active_sessions.get(chat_id)
    if not session:
        return
    minutes = session["minutes"]
    start = session["start_time"]
    end_time = start + timedelta(minutes=minutes)
    try:
        while True:
            now = datetime.utcnow()
            remaining = (end_time - now).total_seconds()
            if remaining <= 0:
                members = session["members"]
                if members:
                    for u in members:
                        await add_study_minutes(chat_id, u["id"], u["name"], minutes)
                await context.bot.send_message(chat_id=chat_id, text=f"✅ Study session of {minutes} minutes finished! Participants: {', '.join([m['name'] for m in members]) or 'No one'}")
                active_sessions.pop(chat_id, None)
                session_tasks.pop(chat_id, None)
                return
            # warnings
            if 60 <= remaining < 75 and not session.get("warned_1m"):
                await context.bot.send_message(chat_id=chat_id, text="⏳ 1 minute left!")
                session["warned_1m"] = True
            if 5*60 <= remaining < 5*60 + 30 and not session.get("warned_5m"):
                await context.bot.send_message(chat_id=chat_id, text="⏳ 5 minutes left!")
                session["warned_5m"] = True
            await asyncio.sleep(15)
    except asyncio.CancelledError:
        return

# Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! Commands: /study <minutes> [cycles], /join, /status, /leaderboard, /break <minutes>, /end")

async def study_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: /study <minutes> [cycles]")
        return
    try:
        minutes = int(context.args[0])
        cycles = int(context.args[1]) if len(context.args) > 1 else 1
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please provide a valid number of minutes.")
        return
    if chat_id in active_sessions:
        await update.message.reply_text("There is already an active session. Use /end to stop it.")
        return
    session = {"minutes": minutes, "start_time": datetime.utcnow(), "members": [], "mode": "Pomodoro" if cycles > 1 else "Single", "cycles": cycles, "warned_5m": False, "warned_1m": False}
    active_sessions[chat_id] = session
    task = asyncio.create_task(run_session(chat_id, context))
    session_tasks[chat_id] = task
    await update.message.reply_text(f"📚 Study session started for {minutes} minutes! Type /join to join. Cycles: {cycles}")

async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_sessions:
        await update.message.reply_text("No active session. Start one with /study <minutes>.")
        return
    user = update.effective_user
    session = active_sessions[chat_id]
    if any(u["id"] == user.id for u in session["members"]):
        await update.message.reply_text("You already joined this session.")
        return
    session["members"].append({"id": user.id, "name": user.first_name or user.full_name})
    await update.message.reply_text(f"{user.first_name or user.full_name} joined the session! Participants: {len(session['members'])}")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_sessions:
        await update.message.reply_text("No active session right now.")
        return
    session = active_sessions[chat_id]
    now = datetime.utcnow()
    end_time = session["start_time"] + timedelta(minutes=session["minutes"])
    remaining = max(0, int((end_time - now).total_seconds()))
    mins = remaining // 60
    secs = remaining % 60
    members = ", ".join([m["name"] for m in session["members"]]) or "No participants yet"
    await update.message.reply_text(f"📊 Session status:\nDuration: {session['minutes']} minutes\nTime left: {mins}m {secs}s\nParticipants: {members}")

async def end_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("Only group admins can end the session early.")
            return
    except Exception:
        await update.message.reply_text("Permission check failed; only admins can end sessions.")
        return
    if chat_id not in active_sessions:
        await update.message.reply_text("No active session to end.")
        return
    if task := session_tasks.get(chat_id):
        task.cancel()
    active_sessions.pop(chat_id, None)
    session_tasks.pop(chat_id, None)
    await update.message.reply_text("⛔ Session ended by admin.")

async def break_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /break <minutes>")
        return
    try:
        minutes = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please give an integer number of minutes.")
        return
    await update.message.reply_text(f"☕ Break started for {minutes} minutes. I'll remind when it's over.")
    await asyncio.sleep(minutes * 60)
    await update.message.reply_text("Break over — back to study! 📚")

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT username, total_minutes FROM leaderboard WHERE chat_id = ? ORDER BY total_minutes DESC LIMIT 10", (chat_id,))
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_text("No records yet.")
        return
    text = "🏆 Leaderboard (top)\n"
    for i, r in enumerate(rows, start=1):
        text += f"{i}. {r[0]} — {r[1]} minutes\n"
    await update.message.reply_text(text)

async def main():
    await init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("study", study_command))
    app.add_handler(CommandHandler("join", join_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("end", end_command))
    app.add_handler(CommandHandler("break", break_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    logger.info("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
