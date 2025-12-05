import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


@dataclass
class FastSession:
    start: datetime
    end: Optional[datetime]            # None за без фиксиран край
    duration: Optional[timedelta]      # None за без фиксиран край
    completed: bool = False
    status_message_id: Optional[int] = None


# user_id -> {"current": Optional[FastSession], "history": List[FastSession]}
user_sessions: Dict[int, Dict[str, object]] = {}


def get_user_state(user_id: int) -> Dict[str, object]:
    state = user_sessions.get(user_id)
    if state is None:
        state = {"current": None, "history": []}
        user_sessions[user_id] = state
    return state


def format_timedelta(td: timedelta) -> str:
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        total_seconds = 0
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Start fast", callback_data="menu_start_fast")],
        [InlineKeyboardButton("Manage fasts", callback_data="menu_manage_fasts")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_duration_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("18 / 6", callback_data="fast_dur_18"),
            InlineKeyboardButton("20 / 4", callback_data="fast_dur_20"),
        ],
        [
            InlineKeyboardButton("24 h", callback_data="fast_dur_24"),
            InlineKeyboardButton("36 h", callback_data="fast_dur_36"),
        ],
        [
            InlineKeyboardButton("Test me", callback_data="fast_dur_test"),
        ],
        [
            InlineKeyboardButton("⬅ Back", callback_data="menu_main"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_status_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="fast_refresh")],
        [InlineKeyboardButton("⏹ END NOW", callback_data="fast_end_now")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (
        "Fasting bot ready.\n\n"
        "Use the buttons below to start a new fast or manage your last fasts."
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_main_menu_keyboard(),
    )


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    if data == "menu_main":
        await query.edit_message_text(
            text=(
                "Fasting bot main menu.\n\n"
                "Choose an option:"
            ),
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if data == "menu_start_fast":
        await query.edit_message_text(
            text=(
                "Choose fasting duration:\n"
                "- 18 / 6 → 18 hours fast\n"
                "- 20 / 4 → 20 hours fast\n"
                "- 24 h\n"
                "- 36 h\n"
                "- Test me → short test fast (15 minutes)"
            ),
            reply_markup=build_duration_menu_keyboard(),
        )
        return

    if data == "menu_manage_fasts":
        state = get_user_state(user_id)
        history: List[FastSession] = state["history"]  # type: ignore[assignment]

        if not history:
            text = "No previous fasts recorded (last 3 are stored per user)."
        else:
            lines = ["Last fasts (max 3):"]
            for idx, sess in enumerate(history[-3:][::-1], start=1):
                start_str = sess.start.astimezone(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                dur_str = (
                    format_timedelta(sess.duration)
                    if sess.duration is not None
                    else "open-ended"
                )
                status = "completed" if sess.completed else "not completed"
                lines.append(
                    f"{idx}) start: {start_str}, duration: {dur_str}, status: {status}"
                )
            text = "\n".join(lines)

        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅ Back", callback_data="menu_main")]]
            ),
        )
        return

    # If not a menu action, leave to other handlers
    return


def duration_from_callback(data: str) -> Optional[timedelta]:
    if data == "fast_dur_18":
        return timedelta(hours=18)
    if data == "fast_dur_20":
        return timedelta(hours=20)
    if data == "fast_dur_24":
        return timedelta(hours=24)
    if data == "fast_dur_36":
        return timedelta(hours=36)
    if data == "fast_dur_test":
        # Short test fast: 15 minutes
        return timedelta(minutes=15)
    return None


async def handle_fast_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    duration = duration_from_callback(data)
    if duration is None:
        await query.edit_message_text(
            text="Unknown duration option.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    now = datetime.now(timezone.utc)
    end_time = now + duration

    state = get_user_state(user_id)
    current: Optional[FastSession] = state["current"]  # type: ignore[assignment]

    # Преместваме стария current (ако има) в history
    if current is not None:
        history: List[FastSession] = state["history"]  # type: ignore[assignment]
        history.append(current)
        # пазим само последните 3
        if len(history) > 3:
            history[:] = history[-3:]

    # Създаваме нова сесия
    session = FastSession(
        start=now,
        end=end_time,
        duration=duration,
        completed=False,
        status_message_id=None,
    )
    state["current"] = session

    # Премахваме евентуални стари jobs за този user
    job_name = f"fast_end_{user_id}"
    for job in context.application.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    # Насрочваме job за край на фаста
    context.application.job_queue.run_once(
        notify_fast_end,
        when=duration,
        data={"user_id": user_id, "chat_id": chat_id},
        name=job_name,
    )

    # Пращаме статус съобщение
    remaining = end_time - now
    elapsed = now - session.start
    text = (
        "Fast started.\n\n"
        f"Duration: {format_timedelta(duration)}\n"
        f"Elapsed: {format_timedelta(elapsed)}\n"
        f"Remaining: {format_timedelta(remaining)}"
    )

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=build_status_keyboard(),
    )

    session.status_message_id = msg.message_id

    # Обновяваме предишното меню към главно
    await query.edit_message_text(
        text="Fast started. Use the status message to monitor it.",
        reply_markup=build_main_menu_keyboard(),
    )


async def notify_fast_end(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    if job is None:
        return

    data = job.data or {}
    user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    if user_id is None or chat_id is None:
        return

    state = get_user_state(user_id)
    current: Optional[FastSession] = state["current"]  # type: ignore[assignment]

    now = datetime.now(timezone.utc)

    if current is None:
        # Няма активен фаст
        return

    # Маркираме го като завършен
    current.completed = True

    # Преместваме го в history, махаме го от current
    history: List[FastSession] = state["history"]  # type: ignore[assignment]
    history.append(current)
    if len(history) > 3:
        history[:] = history[-3:]
    state["current"] = None

    # Нотификация
    await context.bot.send_message(
        chat_id=chat_id,
        text="Your fast has completed.",
    )


async def handle_status_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    state = get_user_state(user_id)
    current: Optional[FastSession] = state["current"]  # type: ignore[assignment]

    if data == "fast_refresh":
        if current is None:
            await query.edit_message_text(
                text="No active fast.",
                reply_markup=build_main_menu_keyboard(),
            )
            return

        now = datetime.now(timezone.utc)
        elapsed = now - current.start
        if current.end is not None:
            remaining = current.end - now
            remaining_str = format_timedelta(remaining)
        else:
            remaining_str = "N/A"

        text = (
            "Fast status:\n\n"
            f"Duration: {format_timedelta(current.duration) if current.duration else 'open-ended'}\n"
            f"Elapsed: {format_timedelta(elapsed)}\n"
            f"Remaining: {remaining_str}"
        )

        # Обновяваме същото съобщение, където са бутоните
        await query.edit_message_text(
            text=text,
            reply_markup=build_status_keyboard(),
        )
        return

    if data == "fast_end_now":
        if current is None:
            await query.edit_message_text(
                text="No active fast to end.",
                reply_markup=build_main_menu_keyboard(),
            )
            return

        # Спираме job-а за край (ако има)
        job_name = f"fast_end_{user_id}"
        for job in context.application.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

        now = datetime.now(timezone.utc)
        current.completed = True
        current.end = now
        if current.duration is None:
            current.duration = now - current.start

        history: List[FastSession] = state["history"]  # type: ignore[assignment]
        history.append(current)
        if len(history) > 3:
            history[:] = history[-3:]
        state["current"] = None

        elapsed = now - current.start
        text = (
            "Fast ended manually.\n\n"
            f"Total elapsed: {format_timedelta(elapsed)}"
        )

        # Обновяваме статус съобщението – махаме бутоните
        await query.edit_message_text(
            text=text,
            reply_markup=None,
        )

        # Допълнително инфо в ново съобщение с връщане към менюто
        await context.bot.send_message(
            chat_id=chat_id,
            text="Fast saved in history (max 3 stored).",
            reply_markup=build_main_menu_keyboard(),
        )
        return


def main():
    if TOKEN is None:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Менюта
    app.add_handler(
        CallbackQueryHandler(
            handle_menu,
            pattern="^menu_.*",
        )
    )

    # Избор на продължителност
    app.add_handler(
        CallbackQueryHandler(
            handle_fast_duration,
            pattern="^fast_dur_.*",
        )
    )

    # Действия със статус съобщението
    app.add_handler(
        CallbackQueryHandler(
            handle_status_actions,
            pattern="^fast_(refresh|end_now)$",
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
