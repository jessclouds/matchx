"""Hackathon Match — Telegram bot entrypoint.

Flow: organiser creates an event (/newevent) and shares its ?start=<code> link →
participants onboard → browse candidates ranked by the MatchX rules → request a
match → the recipient accepts or declines → usernames are revealed only on a
mutual yes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

import db
import keyboards as kb
from config import PERSISTENCE_FILE, deep_link, may_create_events, require_bot_token, setup_logging
from constants import (
    DISCIPLINES,
    HELP_TEXT,
    MAX_SKILLS,
    NOTE_ASK_TEXT,
    NOTE_EXISTING_PROMPT,
    NOTE_MAX_LENGTH,
    NOTE_PROMPT,
    NEED_QUESTION,
    NO_USERNAME_MESSAGE,
    OFFER_QUESTION,
    SCHOOL_PREFERENCES,
    SCHOOLS,
    STATUSES,
    format_skills,
)
from db import DatabaseError, run_db
from matching import Profile, rank_candidates, score
from metrics import STATS

setup_logging()
logger = logging.getLogger("hackathon_match")

TOKEN = require_bot_token()

DB_ERROR_TEXT = "I couldn't reach the database just now. Please try again in a moment."

# Every db.* call runs in a worker thread via run_db(). Python's default executor is
# min(32, cpu_count + 4) threads — about 6 on a small Railway box, which would cap the
# bot at ~6 database operations at once no matter how big the connection pool is.
# Sized just above db.POOL_MAX_SIZE so the pool is the limiter rather than an invisible
# thread shortage: the pool has a timeout and a fail-fast path, a thread famine has
# neither. There is no point going far above it — the pooler cannot serve more.
DB_EXECUTOR_WORKERS = int(os.getenv("DB_EXECUTOR_WORKERS", str(db.POOL_MAX_SIZE + 2)))

# How often the one-line health heartbeat is written to stdout (Railway logs).
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "60"))

# Onboarding order. Each answer either chains to the next step or, when the user is
# editing a single field, saves and returns to the profile screen.
STEPS = ["school", "pref", "discipline", "status", "offer", "need", "note"]


# --------------------------------------------------------------------- helpers


async def send(update: Update, text: str, reply_markup=None) -> None:
    """Send into the current chat regardless of whether this was a message or a tap."""
    chat = update.effective_chat
    if chat is None:
        return
    await chat.send_message(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def safe_edit(query, text: str | None = None, reply_markup=None) -> None:
    """Edit a message, tolerating double-taps and stale/identical content."""
    try:
        if text is None:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
        else:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.debug("Could not edit message: %s", exc)


async def notify(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, reply_markup=None) -> bool:
    """Message another user. Never raises — they may have blocked the bot."""
    try:
        await context.bot.send_message(
            chat_id=user_id, text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
        )
        STATS.record_telegram("sent")
        return True
    except Forbidden:
        STATS.record_telegram("blocked")
        logger.info("User %s has blocked the bot; notification dropped.", user_id)
    except RetryAfter as exc:
        STATS.record_telegram("retry_after")
        logger.warning("Telegram flood limit notifying %s: retry after %ss", user_id, exc.retry_after)
    except TelegramError as exc:
        STATS.record_telegram("failed")
        logger.warning("Could not notify %s: %s", user_id, exc)
    return False


def username_of(update: Update) -> str | None:
    user = update.effective_user
    return user.username if user else None


async def require_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """A Telegram username is mandatory — it is the only contact channel we reveal."""
    username = username_of(update)
    if not username:
        await send(update, NO_USERNAME_MESSAGE)
        return None
    return username


async def refresh_username(update: Update) -> None:
    """Keep stored usernames current (and blank them if someone removes theirs)."""
    user = update.effective_user
    if user is None:
        return
    try:
        await run_db(db.update_username, user.id, user.username)
    except DatabaseError:
        pass  # cosmetic; never block the user for this


async def current_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Profile | None:
    """The profile for the event the user is currently working in."""
    user_id = update.effective_user.id
    event_code = context.user_data.get("event_code")

    if event_code:
        profile = await run_db(db.get_profile, user_id, event_code)
        if profile:
            return profile

    profile = await run_db(db.get_latest_profile, user_id)
    if profile:
        context.user_data["event_code"] = profile.event_code
    return profile


EVENT_NAME_CACHE_SIZE = 500


async def event_name_for(context: ContextTypes.DEFAULT_TYPE, event_code: str) -> str:
    """Event names never change, so cache them — bounded, so it cannot grow forever."""
    cache = context.bot_data.setdefault("event_names", {})
    if event_code not in cache:
        if len(cache) >= EVENT_NAME_CACHE_SIZE:
            cache.clear()
        cache[event_code] = await run_db(db.get_event, event_code) or event_code
    return cache[event_code]


async def no_profile_prompt(update: Update) -> None:
    await send(
        update,
        "You're not signed up for a hackathon yet.\n\n"
        "Open your hackathon's Find Teammates link to join. Organisers can create one with /newevent.",
    )


def db_guard(handler: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Turn a database outage into a friendly message instead of a stack trace."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await handler(update, context)
        except DatabaseError:
            logger.exception("Database error in %s", handler.__name__)
            if update.callback_query:
                # The query may already have been answered, so the message is the
                # reliable channel — the alert is only a nicety on top.
                try:
                    await update.callback_query.answer(DB_ERROR_TEXT, show_alert=True)
                except TelegramError:
                    pass
            try:
                await send(update, DB_ERROR_TEXT, reply_markup=kb.home_keyboard())
            except TelegramError:
                pass

    wrapper.__name__ = handler.__name__
    return wrapper


# --------------------------------------------------------------- abuse guard

# /matches sends one card per pending request, each with its own buttons. Capped so a
# popular participant cannot trigger a burst Telegram throttles (1 msg/s per chat).
MAX_REQUEST_CARDS_PER_RUN = 5

# Event-creation abuse limits. /newevent stays open to everyone — no whitelist — but a
# single account cannot mass-create. Two independent bounds:
#   * how many open events one organiser may hold at once, and
#   * how many they may create per hour.
# The hourly limit deliberately tolerates a burst: creating two or three events
# back-to-back is normal (parallel tracks, or redoing one after a typo), so a flat
# cooldown between consecutive creations would block legitimate organisers. Only a
# scripted loop reaches the hourly figure.
MAX_ACTIVE_EVENTS_PER_ORGANISER = 10
MAX_EVENTS_PER_HOUR_PER_ORGANISER = 5
EVENT_RATE_WINDOW_SECONDS = 3600

# A single bot process, so a bounded in-memory window is enough: it costs nothing,
# survives nothing (which is fine for spam control) and cannot grow without limit.
_ACTION_WINDOW_SECONDS = 60
_MAX_ACTIONS_PER_WINDOW = 40
_MAX_REQUESTS_PER_WINDOW = 15
_recent_actions: dict[int, list[float]] = {}


def _too_many(user_id: int, limit: int) -> bool:
    """True when this user has exceeded `limit` actions in the last minute."""
    now = time.monotonic()
    hits = [stamp for stamp in _recent_actions.get(user_id, ()) if now - stamp < _ACTION_WINDOW_SECONDS]
    hits.append(now)
    _recent_actions[user_id] = hits

    if len(_recent_actions) > 5000:            # keep the dict bounded
        for stale_id in [
            uid for uid, stamps in _recent_actions.items()
            if not stamps or now - stamps[-1] > _ACTION_WINDOW_SECONDS
        ][:2000]:
            _recent_actions.pop(stale_id, None)

    return len(hits) > limit

# ------------------------------------------------------------------ main menu


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, intro: str | None = None) -> None:
    profile = await current_profile(update, context)
    if not profile:
        await no_profile_prompt(update)
        return

    event_name = await event_name_for(context, profile.event_code)
    match_count, pending_count = await run_db(
        db.menu_counts, profile.telegram_user_id, profile.event_code
    )

    lines = [intro] if intro else []
    lines.append(f"<b>{kb.esc(event_name)}</b>")
    if pending_count:
        lines.append(f"{pending_count} request{'s' if pending_count > 1 else ''} waiting for your answer.")
    lines.append("What would you like to do?")

    await send(update, "\n\n".join(lines), reply_markup=kb.main_menu_keyboard(match_count, pending_count))


@db_guard
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await refresh_username(update)

    raw_code = context.args[0] if context.args else ""
    event_code = db.normalise_event_code(raw_code)

    if raw_code and not event_code:
        await send(update, "That link looks malformed. Ask your organiser for the Find Teammates link again.")
        return

    if not event_code:
        # No deep link: existing users go straight to their menu.
        profile = await current_profile(update, context)
        if profile:
            await show_menu(update, context, intro="Welcome back.")
        else:
            await no_profile_prompt(update)
        return

    event = await run_db(db.get_event_row, event_code)
    if event is None:
        await send(update, "I don't recognise that event code. Check the link with your organiser.")
        profile = await current_profile(update, context)
        if profile:
            await show_menu(update, context)
        return

    event_name = event["name"]
    if not event["is_active"]:
        await send(update, f"<b>{kb.esc(event_name)}</b> has closed, so it's no longer matching teammates.")
        profile = await current_profile(update, context)
        if profile:
            await show_menu(update, context)
        return

    if not await require_username(update, context):
        return

    context.user_data["event_code"] = event_code
    existing = await run_db(db.get_profile, update.effective_user.id, event_code)

    if existing:
        await show_menu(update, context, intro=f"Welcome back to <b>{kb.esc(event_name)}</b>.")
        return

    context.user_data["draft"] = {"event_code": event_code}
    context.user_data.pop("edit_field", None)
    await send(
        update,
        f"You're joining the teammate-matching pool for <b>{kb.esc(event_name)}</b>.\n\n"
        "Not the right hackathon? Close this and open the link your organiser shared.\n\n"
        "Six quick questions and I'll start finding you teammates.",
    )
    await ask_school(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, HELP_TEXT, reply_markup=kb.home_keyboard())


@db_guard
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await refresh_username(update)
    await show_menu(update, context)


# ------------------------------------------------------------------ onboarding


def draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault("draft", {})


async def ask_step(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str) -> None:
    asker = {
        "school": ask_school,
        "pref": ask_school_preference,
        "discipline": ask_discipline,
        "status": ask_team_status,
        "offer": ask_skills_offered,
        "need": ask_skills_needed,
        "note": ask_note,
    }[step]
    await asker(update, context)


async def advance(update: Update, context: ContextTypes.DEFAULT_TYPE, step: str) -> None:
    """After answering `step`: save (edit mode) or move to the next question."""
    if context.user_data.get("edit_field") == step:
        context.user_data.pop("edit_field", None)
        await save_and_confirm(update, context)
        return

    index = STEPS.index(step) + 1
    if index >= len(STEPS):          # last question answered — save the profile
        await save_and_confirm(update, context)
        return
    await ask_step(update, context, STEPS[index])


async def ask_school(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, "Which school are you from?", reply_markup=kb.build_keyboard("school", SCHOOLS, 3))


async def handle_school_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    school = query.data.split("_", 1)[1]
    draft(context)["school"] = school
    await safe_edit(query, f"School: {kb.esc(school)}")
    await advance(update, context, "school")


async def ask_school_preference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(
        update,
        "Would you prefer teammates from your own school?",
        reply_markup=kb.build_keyboard("pref", SCHOOL_PREFERENCES, 1),
    )


async def handle_preference_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    preference = query.data.split("_", 1)[1]
    draft(context)["school_preference"] = preference
    label = dict(SCHOOL_PREFERENCES).get(preference, preference)
    await safe_edit(query, f"School preference: {kb.esc(label)}")
    await advance(update, context, "pref")


async def ask_discipline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(
        update,
        "What's your main field of study?\n<i>Shown on your card — it doesn't affect matching.</i>",
        reply_markup=kb.build_keyboard("discipline", DISCIPLINES, 2),
    )


async def handle_discipline_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    discipline = query.data.split("_", 1)[1]
    draft(context)["discipline"] = discipline
    label = dict(DISCIPLINES).get(discipline, discipline)
    await safe_edit(query, f"Discipline: {kb.esc(label)}")
    await advance(update, context, "discipline")


async def ask_team_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send(update, "Where are you at right now?", reply_markup=kb.build_keyboard("status", STATUSES, 1))


async def handle_team_status_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    team_status = query.data.split("_", 1)[1]
    draft(context)["team_status"] = team_status
    label = dict(STATUSES).get(team_status, team_status)
    await safe_edit(query, f"Status: {kb.esc(label)}")
    await advance(update, context, "status")


def _team_status(context: ContextTypes.DEFAULT_TYPE) -> str:
    return draft(context).get("team_status", "looking")


async def ask_skills_offered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    selected = set(draft(context).get("offer_skills", set()))
    await send(
        update,
        OFFER_QUESTION[_team_status(context)],
        reply_markup=kb.build_skill_keyboard("offer", selected, show_wildcard=False),
    )


async def ask_skills_needed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    selected = set(draft(context).get("need_skills", set()))
    await send(
        update,
        NEED_QUESTION[_team_status(context)],
        reply_markup=kb.build_skill_keyboard("need", selected, show_wildcard=True),
    )


async def handle_skill_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    prefix, value = query.data.split("_", 1)          # 'offer_ai_data' -> 'offer', 'ai_data'
    key = f"{prefix}_skills"
    data = draft(context)
    selected: set[str] = set(data.get(key, set()))

    if value == "any":
        if prefix == "offer":                          # defensive: no wildcard on the offer step
            await query.answer()
            return
        data[key] = set()
        data["open_to_any"] = True
        await query.answer("Open to anyone")
        await safe_edit(query, "Looking for: anyone — no preference")
        await advance(update, context, "need")
        return

    if value == "done":
        if prefix == "offer" and not selected:
            await query.answer("Pick at least one skill you can bring.", show_alert=True)
            return
        if prefix == "need":
            data["open_to_any"] = not selected
        data[key] = selected
        await query.answer()
        label = "You offer" if prefix == "offer" else "Looking for"
        empty = "anyone — no preference"
        await safe_edit(query, f"{label}: {kb.esc(format_skills(selected, empty))}")
        await advance(update, context, "offer" if prefix == "offer" else "need")
        return

    if value in selected:
        selected.discard(value)
    elif len(selected) >= MAX_SKILLS:
        await query.answer(f"You can pick at most {MAX_SKILLS}. Tap one to remove it first.", show_alert=True)
        return
    else:
        selected.add(value)

    data[key] = selected
    await query.answer()
    await safe_edit(query, reply_markup=kb.build_skill_keyboard(prefix, selected, show_wildcard=(prefix == "need")))


# ------------------------------------------------------------------ note step

# Optional one-line note shown on the participant's card. It is never used for
# eligibility or ranking — it exists so people can say what they want to build.


async def ask_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    existing = draft(context).get("note")
    if existing:
        await send(
            update,
            NOTE_EXISTING_PROMPT.format(note=kb.esc(existing)),
            reply_markup=kb.note_keyboard(has_note=True),
        )
        return
    await send(update, NOTE_PROMPT, reply_markup=kb.note_keyboard())


async def handle_note_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "add":
        context.user_data["mode"] = "await_note"
        await safe_edit(query, NOTE_ASK_TEXT, reply_markup=kb.note_input_keyboard())
        return

    context.user_data.pop("mode", None)

    if choice == "remove":
        draft(context)["note"] = None
        await safe_edit(query, "Note removed.")
        await advance(update, context, "note")
        return

    # Skip: leave any existing note exactly as it was.
    draft(context).setdefault("note", None)
    await safe_edit(query, "Note unchanged." if draft(context).get("note") else "No note added.")
    await advance(update, context, "note")


@db_guard
async def capture_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One free-text message, validated against the character limit."""
    text = " ".join((update.message.text or "").split())

    if not text:
        await send(update, NOTE_ASK_TEXT, reply_markup=kb.note_input_keyboard())
        return

    if len(text) > NOTE_MAX_LENGTH:
        await send(
            update,
            f"That's {len(text)} characters — the limit is {NOTE_MAX_LENGTH}. "
            "Please send a shorter version.",
            reply_markup=kb.note_input_keyboard(),
        )
        return

    context.user_data.pop("mode", None)
    draft(context)["note"] = text
    await send(update, "Note saved.")
    await advance(update, context, "note")


@db_guard
async def save_and_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist the draft (upsert on telegram_user_id + event_code) and show the profile."""
    data = draft(context)
    user = update.effective_user
    event_code = data.get("event_code") or context.user_data.get("event_code")

    if not event_code:
        await no_profile_prompt(update)
        return

    username = username_of(update)
    if not username:
        await send(update, NO_USERNAME_MESSAGE)
        return

    required = ("school", "school_preference", "discipline", "team_status")
    missing = [field for field in required if not data.get(field)]
    offered = sorted(data.get("offer_skills", set()))
    if missing or not offered:
        # Someone resumed a half-finished draft — restart cleanly rather than saving junk.
        logger.info("Incomplete draft for user %s (missing=%s)", user.id, missing or "skills_offered")
        await send(update, "Let's finish your profile — a couple of answers are missing.")
        context.user_data.pop("edit_field", None)
        await ask_step(update, context, missing[0] if missing else "offer")
        return

    needed = sorted(data.get("need_skills", set()))
    open_to_any = bool(data.get("open_to_any", False)) or not needed

    await run_db(
        db.save_profile,
        user.id,
        event_code,
        username,
        data["school"],
        data["school_preference"],
        data["discipline"],
        data["team_status"],
        offered,
        needed,
        open_to_any,
        data.get("note"),
    )
    logger.info("Saved profile for user %s at event %s", user.id, event_code)

    context.user_data["event_code"] = event_code
    context.user_data["draft"] = {"event_code": event_code}

    profile = await run_db(db.get_profile, user.id, event_code)
    event_name = await event_name_for(context, event_code)
    await send(update, "Profile saved.\n\n" + kb.render_profile(profile, event_name))
    await find_matches(update, context)


# ---------------------------------------------------------------- profile view


@db_guard
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await refresh_username(update)
    profile = await current_profile(update, context)
    if not profile:
        await no_profile_prompt(update)
        return
    event_name = await event_name_for(context, profile.event_code)
    await send(update, kb.render_profile(profile, event_name), reply_markup=kb.profile_keyboard(profile.is_active))


@db_guard
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Redo the questions for the current event. The same row is overwritten."""
    profile = await current_profile(update, context)
    event_code = context.user_data.get("event_code") or (profile.event_code if profile else None)
    if not event_code:
        await no_profile_prompt(update)
        return
    if not await require_username(update, context):
        return
    context.user_data["draft"] = {"event_code": event_code}
    context.user_data.pop("edit_field", None)
    await send(update, "Starting your profile over. Your matches and requests are kept.")
    await ask_school(update, context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    context.user_data.pop("pending_event_name", None)
    context.user_data.pop("edit_field", None)
    await send(update, "Cancelled.", reply_markup=kb.home_keyboard())


# ------------------------------------------------------------------- browsing

# Browsing history is per user and per event, kept in user_data (persisted by
# PicklePersistence). It is navigation state only — nothing here creates, removes
# or alters a skip, request or match.
MAX_HISTORY = 50

# Candidates are fetched a page at a time. The page is ordered by the same keys the
# ranker uses, so the best candidate is always inside it.
CANDIDATE_PAGE = 25


def _history(context: ContextTypes.DEFAULT_TYPE, event_code: str) -> list[int]:
    return context.user_data.setdefault("history", {}).setdefault(event_code, [])


def _cursor(context: ContextTypes.DEFAULT_TYPE, event_code: str) -> int:
    """Index of the card on screen; len(history) means the end-of-pool message."""
    history = _history(context, event_code)
    return context.user_data.setdefault("cursor", {}).get(event_code, len(history))


def _set_cursor(context: ContextTypes.DEFAULT_TYPE, event_code: str, value: int) -> None:
    context.user_data.setdefault("cursor", {})[event_code] = value


def _remember(context: ContextTypes.DEFAULT_TYPE, event_code: str, candidate_id: int) -> None:
    history = _history(context, event_code)
    if not history or history[-1] != candidate_id:
        history.append(candidate_id)
        del history[:-MAX_HISTORY]
    _set_cursor(context, event_code, len(history) - 1)


def _current_candidate_id(context: ContextTypes.DEFAULT_TYPE, event_code: str) -> int | None:
    history = _history(context, event_code)
    cursor = _cursor(context, event_code)
    return history[cursor] if 0 <= cursor < len(history) else None




@db_guard
async def find_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the single best remaining candidate. Recomputed each time, so it is never stale."""
    profile = await current_profile(update, context)
    if not profile:
        await no_profile_prompt(update)
        return

    if not username_of(update):
        await send(update, NO_USERNAME_MESSAGE)
        return

    if not profile.is_active:
        await send(
            update,
            "Your matchmaking is paused, so you're hidden from others. "
            "Resume it to start browsing again.",
            reply_markup=kb.profile_keyboard(False),
        )
        return

    user_id = profile.telegram_user_id
    # Postgres applies the hard filters and returns one page, so browsing costs a
    # single indexed query no matter how large the event gets.
    page = await run_db(db.find_candidates, profile, CANDIDATE_PAGE + 1)
    ranked = rank_candidates(profile, page[:CANDIDATE_PAGE])

    if not ranked:
        event = await run_db(db.get_event_row, profile.event_code)
        if event and not event["is_active"]:
            await send(
                update,
                f"<b>{kb.esc(event['name'])}</b> has closed. Your matches are still in /matches.",
                reply_markup=kb.home_keyboard(),
            )
            return

        skipped, seen, others = await run_db(
            db.empty_state_counts, profile.event_code, user_id
        )
        if skipped:
            text = ("That's everyone new for now.\n\n"
                    "You can look again at the people you skipped, or widen what you're "
                    "looking for in your profile.")
        elif seen:
            text = ("You've been through everyone here for now.\n\n"
                    "I'll message you when someone answers your request, or when a new "
                    "teammate joins.")
        elif others:
            text = ("Nobody here matches what you're looking for yet.\n\n"
                    "Try adding more skills to what you need, or check back as more people join.")
        else:
            text = ("You're one of the first here.\n\n"
                    "I'll have candidates as soon as more teammates sign up — check back soon.")
        _set_cursor(context, profile.event_code, len(_history(context, profile.event_code)))
        await send(
            update,
            text,
            reply_markup=kb.no_candidates_keyboard(
                bool(skipped),
                can_go_back=bool(_history(context, profile.event_code)),
            ),
        )
        return

    best = ranked[0]
    remaining = len(page) - 1                      # len(page) may be CANDIDATE_PAGE + 1
    _remember(context, profile.event_code, best.profile.telegram_user_id)
    await send(
        update,
        kb.render_candidate(best, remaining=remaining, capped=len(page) > CANDIDATE_PAGE),
        reply_markup=kb.browse_keyboard(
            best.profile.telegram_user_id,
            can_go_back=_cursor(context, profile.event_code) > 0,
        ),
    )




async def show_previous_candidate(update: Update, context: ContextTypes.DEFAULT_TYPE, me: Profile) -> bool:
    """Re-show the card before the current one. Returns False when there is none.

    Purely navigational: it never touches interests or matches, so a request already
    sent — or a match already made — stays exactly as it is.
    """
    history = _history(context, me.event_code)
    index = _cursor(context, me.event_code) - 1

    while index >= 0:
        candidate_id = history[index]
        other = await run_db(db.get_profile, candidate_id, me.event_code)
        if other and other.is_matchable and candidate_id != me.telegram_user_id:
            status = await run_db(db.interaction_status, me.event_code, me.telegram_user_id, candidate_id)
            _set_cursor(context, me.event_code, index)
            await send(
                update,
                kb.render_candidate(score(me, other), note=kb.REVISIT_NOTES.get(status, "")),
                reply_markup=kb.browse_keyboard(candidate_id, can_go_back=index > 0),
            )
            return True
        del history[index]          # they left the pool — drop them from history
        index -= 1

    _set_cursor(context, me.event_code, 0)
    return False

@db_guard
async def handle_browse_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]

    if _too_many(update.effective_user.id, _MAX_ACTIONS_PER_WINDOW):
        await query.answer("Slow down a moment — try again shortly.", show_alert=True)
        return

    profile = await current_profile(update, context)
    if not profile:
        await query.answer()
        await no_profile_prompt(update)
        return

    if action == "reset":
        await query.answer()
        cleared = await run_db(db.clear_skips, profile.telegram_user_id, profile.event_code)
        await safe_edit(query, f"Brought back {cleared} skipped teammate{'s' if cleared != 1 else ''}.")
        await find_matches(update, context)
        return

    if action == "back":
        await query.answer()
        # Leave the card being stepped away from in place, but without live buttons.
        await safe_edit(query, reply_markup=None)
        if not await show_previous_candidate(update, context, profile):
            await send(update, "That's the first teammate you've seen.")
            await find_matches(update, context)
        return

    try:
        candidate_id = int(parts[2])
    except (IndexError, ValueError):
        # Older button, or a plain "next" with nothing to skip.
        candidate_id = _current_candidate_id(context, profile.event_code) if action == "next" else None
        if candidate_id is None:
            await query.answer()
            await find_matches(update, context)
            return

    if candidate_id == profile.telegram_user_id:
        await query.answer()
        await find_matches(update, context)
        return

    if action in ("next", "skip"):
        await query.answer()
        await run_db(db.record_skip, profile.telegram_user_id, profile.event_code, candidate_id)
        await safe_edit(query, "Seen.")
        await find_matches(update, context)
        return

    if action == "req":
        await handle_request(update, context, profile, candidate_id)


async def handle_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    me: Profile,
    candidate_id: int,
) -> None:
    """Request Match: push my card to the recipient so they can answer."""
    query = update.callback_query
    if _too_many(me.telegram_user_id, _MAX_REQUESTS_PER_WINDOW):
        await query.answer("That's a lot of requests at once — try again in a minute.", show_alert=True)
        return
    other = await run_db(db.get_profile, candidate_id, me.event_code)
    if not other:
        await query.answer("That teammate is no longer available.", show_alert=True)
        await find_matches(update, context)
        return

    result = await run_db(db.request_match, me.event_code, me.telegram_user_id, candidate_id)
    event_name = await event_name_for(context, me.event_code)

    if result == "matched":
        await query.answer("It's a match")
        await safe_edit(query, kb.render_match(other), reply_markup=kb.home_keyboard())
        fresh_me = await run_db(db.get_profile, me.telegram_user_id, me.event_code) or me
        await notify(context, candidate_id, kb.render_match(fresh_me), reply_markup=kb.home_keyboard())
        logger.info("Match created (%s): %s <-> %s", me.event_code, me.telegram_user_id, candidate_id)
        return

    if result == "already_matched":
        await query.answer("You're already matched with them.")
        await safe_edit(query, kb.render_match(other), reply_markup=kb.home_keyboard())
        return

    if result == "already_pending":
        await query.answer("Already sent — waiting on their answer.", show_alert=True)
        await safe_edit(query, "Request already sent — waiting for their answer.")
        await find_matches(update, context)
        return

    if result == "closed":
        await query.answer("This hackathon has closed.", show_alert=True)
        await safe_edit(
            query,
            f"<b>{kb.esc(event_name)}</b> has closed, so no new requests can be sent. "
            "Your existing matches are still in /matches.",
            reply_markup=kb.home_keyboard(),
        )
        return

    if result == "invalid":
        await query.answer()
        await find_matches(update, context)
        return

    # 'requested' — deliver my card to them so they never have to find me by chance.
    await query.answer("Request sent")
    await safe_edit(query, "Request sent. I'll tell you when they answer.")

    my_card = score(other, me)   # scored from the recipient's point of view
    delivered = await notify(
        context,
        candidate_id,
        kb.render_request_card(my_card, event_name),
        reply_markup=kb.request_response_keyboard(me.telegram_user_id),
    )
    if not delivered:
        logger.info("Request stored but not delivered to %s (blocked bot?)", candidate_id)
    await find_matches(update, context)


# --------------------------------------------------------- responding to asks


@db_guard
async def handle_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept or decline an incoming request."""
    query = update.callback_query
    parts = query.data.split(":")
    try:
        requester_id = int(parts[2])
    except (IndexError, ValueError):
        await query.answer("That button is no longer valid.", show_alert=True)
        return
    accept = parts[1] == "yes"

    me_id = update.effective_user.id
    event_code = (
        await run_db(db.find_pending_request_event, requester_id, me_id)
        or context.user_data.get("event_code")
    )
    if not event_code:
        await query.answer("That request is no longer open.", show_alert=True)
        await safe_edit(query, "This request is no longer open.", reply_markup=kb.home_keyboard())
        return

    context.user_data.setdefault("event_code", event_code)
    result = await run_db(db.respond_to_request, event_code, requester_id, me_id, accept)

    if result == "closed":
        await query.answer("This hackathon has closed.", show_alert=True)
        await safe_edit(
            query,
            "This hackathon has closed, so new matches can't be made. "
            "Your existing matches are still in /matches.",
            reply_markup=kb.home_keyboard(),
        )
        return

    if result in ("not_found", "already_declined"):
        await query.answer("That request is no longer open.", show_alert=True)
        await safe_edit(query, "This request is no longer open.", reply_markup=kb.home_keyboard())
        return

    requester = await run_db(db.get_profile, requester_id, event_code)

    if result == "already_matched":
        await query.answer("You're already matched")
        if requester:
            await safe_edit(query, kb.render_match(requester), reply_markup=kb.home_keyboard())
        return

    if result == "declined":
        await query.answer("Declined")
        await safe_edit(
            query,
            "Declined. They won't be told who said no.",
            reply_markup=kb.home_keyboard(),
        )
        return

    # result == 'matched' — notify exactly once, on this transition only.
    me = await run_db(db.get_profile, me_id, event_code)
    await query.answer("It's a match")
    if requester:
        await safe_edit(query, kb.render_match(requester), reply_markup=kb.home_keyboard())
    if me:
        await notify(context, requester_id, kb.render_match(me), reply_markup=kb.home_keyboard())
    logger.info("Match created (%s): %s <-> %s", event_code, requester_id, me_id)


@db_guard
async def matches_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Matches, plus anything still waiting on either side."""
    await refresh_username(update)
    profile = await current_profile(update, context)
    if not profile:
        await no_profile_prompt(update)
        return

    user_id, event_code = profile.telegram_user_id, profile.event_code
    matches = await run_db(db.get_matches, user_id, event_code)
    incoming = await run_db(db.get_incoming_requests, user_id, event_code)
    outgoing = await run_db(db.get_outgoing_requests, user_id, event_code)

    if not matches and not incoming and not outgoing:
        await send(
            update,
            "You have no matches yet.\n\n"
            "Browse teammates and send a request — I'll let you know when someone says yes.",
            reply_markup=kb.main_menu_keyboard(),
        )
        return

    # Telegram allows about one message per second to a single chat. The summary parts
    # are therefore sent as ONE message rather than three, and the request cards — which
    # each need their own Accept/Decline buttons, so they cannot be merged — are capped
    # per run. Someone popular with twenty pending requests used to trigger twenty-three
    # sends in a burst, which Telegram throttles and can drop.
    summary: list[str] = []
    if matches:
        summary.append(f"<b>Your matches ({len(matches)})</b>")
        summary.append("\n\n".join(
            f"{kb.esc(p.school)} — {kb.esc(format_skills(p.skills_offered))}\n"
            f"<b>@{kb.esc(p.telegram_username)}</b>" if p.telegram_username else
            f"{kb.esc(p.school)} — contact unavailable"
            for p in matches
        ))
    if outgoing:
        summary.append(
            f"Waiting on {len(outgoing)} person{'s' if len(outgoing) > 1 else ''}. "
            "You'll get a message when they answer."
        )
    if incoming:
        shown = min(len(incoming), MAX_REQUEST_CARDS_PER_RUN)
        summary.append(f"<b>{len(incoming)} request{'s' if len(incoming) > 1 else ''} for you</b>")
        if len(incoming) > shown:
            summary.append(f"Showing the first {shown}. Answer these, then /matches for the rest.")
    if summary:
        await send(update, "\n\n".join(summary))

    if incoming:
        event_name = await event_name_for(context, event_code)
        for requester in incoming[:MAX_REQUEST_CARDS_PER_RUN]:
            await send(
                update,
                kb.render_request_card(score(profile, requester), event_name),
                reply_markup=kb.request_response_keyboard(requester.telegram_user_id),
            )
    else:
        await send(update, "What next?", reply_markup=kb.main_menu_keyboard(len(matches), 0))


# ---------------------------------------------------------------- menu routing


@db_guard
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    await query.answer()

    if action == "home":
        await show_menu(update, context)
    elif action == "find":
        await find_matches(update, context)
    elif action == "profile":
        await profile_command(update, context)
    elif action == "matches":
        await matches_command(update, context)
    elif action == "myevents":
        await myevents_command(update, context)
    elif action == "help":
        await help_command(update, context)
    elif action == "edit":
        await send(update, "What would you like to change?", reply_markup=kb.edit_keyboard())
    elif action == "restart":
        await restart_command(update, context)
    elif action in ("pause", "resume"):
        profile = await current_profile(update, context)
        if not profile:
            await no_profile_prompt(update)
            return
        active = action == "resume"
        await run_db(db.set_active, profile.telegram_user_id, profile.event_code, active)
        text = (
            "You're visible again."
            if active
            else "Matchmaking paused. You won't appear to others until you resume."
        )
        await send(update, text, reply_markup=kb.home_keyboard())


@db_guard
async def handle_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit one field: ask that question, then save and return to the profile."""
    query = update.callback_query
    field = query.data.split(":", 1)[1]
    await query.answer()

    profile = await current_profile(update, context)
    if not profile:
        await no_profile_prompt(update)
        return

    # Seed the draft from the saved profile so unedited fields survive the upsert.
    context.user_data["draft"] = {
        "event_code": profile.event_code,
        "school": profile.school,
        "school_preference": profile.school_preference,
        "discipline": profile.discipline,
        "team_status": profile.team_status,
        "offer_skills": set(profile.skills_offered),
        "need_skills": set(profile.skills_needed),
        "open_to_any": profile.open_to_any,
        "note": profile.note,
    }
    context.user_data["edit_field"] = field
    await ask_step(update, context, field)


@db_guard
async def handle_event_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    event_code = query.data.split(":", 1)[1]
    context.user_data["event_code"] = event_code
    await show_menu(update, context)


@db_guard
async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch between hackathons this user has joined."""
    rows = await run_db(db.list_profiles_for_user, update.effective_user.id)
    if not rows:
        await no_profile_prompt(update)
        return
    if len(rows) == 1:
        context.user_data["event_code"] = rows[0]["event_code"]
        await show_menu(update, context)
        return
    await send(update, "Your hackathons — pick the one you want to work in:",
               reply_markup=kb.event_picker_keyboard(rows))


# ------------------------------------------------------------- organiser flow


@db_guard
async def newevent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not may_create_events(user_id):
        await send(
            update,
            "Creating hackathons is limited to organisers.\n\n"
            f"Ask whoever runs this bot to add your Telegram ID <code>{user_id}</code> "
            "to <code>ORGANISER_IDS</code>.",
        )
        return

    now = time.monotonic()
    recent = [t for t in context.user_data.get("event_creations", ()) if now - t < EVENT_RATE_WINDOW_SECONDS]
    context.user_data["event_creations"] = recent
    if len(recent) >= MAX_EVENTS_PER_HOUR_PER_ORGANISER:
        wait_minutes = max(1, int((EVENT_RATE_WINDOW_SECONDS - (now - recent[0])) // 60))
        await send(
            update,
            f"That's {len(recent)} hackathons in the last hour, which is the limit.\n\n"
            f"Try again in about {wait_minutes} minute{'s' if wait_minutes > 1 else ''} — "
            "/myevents shows the ones you already have.",
        )
        return

    active = await run_db(db.count_active_events_for_organiser, user_id)
    if active >= MAX_ACTIVE_EVENTS_PER_ORGANISER:
        await send(
            update,
            f"You already have {active} active events, which is the limit.\n\n"
            "Open /myevents and close an old event before creating another. "
            "Closing keeps all of its matches and frees up a slot.",
            reply_markup=kb.myevents_link_keyboard(),
        )
        return

    context.user_data["mode"] = "await_event_name"
    context.user_data.pop("pending_event_name", None)
    await send(
        update,
        "Create a hackathon.\n\n"
        "First — what's the hackathon called?\n"
        "<i>Participants see this name when they join, so they can check they're in the "
        "right pool.</i>\n\n"
        "Send /cancel to stop.",
    )


async def capture_event_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 1 of /newevent — the organiser types the hackathon's name."""
    name = (update.message.text or "").strip().splitlines()[0].strip() if update.message.text else ""
    if not name:
        await send(update, "I need a name for the hackathon. Send it as a short line of text, or /cancel.")
        return

    context.user_data["pending_event_name"] = name[:120]
    context.user_data["mode"] = "await_announcement"
    await send(
        update,
        f"Got it — <b>{kb.esc(name[:120])}</b>.\n\n"
        "Now paste or forward the hackathon announcement.\n\n"
        "I'll send it back unchanged, with a Find Teammates link added, "
        "ready to copy-paste into your channel.\n\n"
        "Send /cancel to stop.",
    )


@db_guard
async def create_event_from_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Turn a pasted/forwarded announcement into an isolated event pool + share link.

    The reply is the organiser's announcement, unchanged, with the MatchX call-to-action
    appended — so it can be copy-pasted straight into a hackathon channel.
    """
    name = context.user_data.get("pending_event_name")
    if not name:
        # State was lost (restart, or the steps ran out of order) — ask again.
        context.user_data["mode"] = "await_event_name"
        await send(update, "Let's start with the name — what's the hackathon called?")
        return

    announcement = (update.message.text or update.message.caption or "").strip()
    if not announcement:
        await send(update, "I couldn't read any text there. Paste the announcement, or /cancel.")
        return

    context.user_data.pop("mode", None)          # state is per organiser (user_data)
    context.user_data.pop("pending_event_name", None)

    organiser_id = update.effective_user.id
    event_code, event_name = await run_db(db.create_event, name, announcement[:4000], organiser_id)
    link = deep_link(event_code)
    context.user_data.setdefault("event_creations", []).append(time.monotonic())
    STATS.record_action("event_created")
    logger.info("Organiser %s created event %s", organiser_id, event_code)

    # 1. The ready-to-post message — nothing else in it, so it pastes cleanly.
    for part in kb.render_event_post(announcement, link):
        await send(update, part)

    # 2. Instructions, kept separate so they are not copied along with the post.
    await send(
        update,
        f"<b>{kb.esc(event_name)}</b> is live. Copy the message above and post it.\n\n"
        f"Everyone who joins through that link is matched only with people from this "
        f"hackathon.\n\n"
        f"Event code: <code>{kb.esc(event_code)}</code>  ·  /myevents to see it again.",
    )


@db_guard
async def myevents_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    events = await run_db(db.list_events_for_organiser, update.effective_user.id)
    if not events:
        await send(update, "You haven't created any hackathons yet. Use /newevent to make one.")
        return
    blocks = []
    for e in events:
        status = "Active" if e["is_active"] else "Closed"
        block = (
            f"<b>{kb.esc(e['name'])}</b> — {status}\n"
            f"{e['participants']} joined"
        )
        if e["is_active"]:
            block += f"\n<code>{kb.esc(deep_link(e['event_code']))}</code>"
        blocks.append(block)
    await send(update, "\n\n".join(blocks), reply_markup=kb.myevents_keyboard(events))


@db_guard
async def handle_event_close_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 1 of closing: ask the organiser to confirm."""
    query = update.callback_query
    event_code = query.data.split(":", 1)[1]
    event = await run_db(db.get_event_row, event_code)

    # Ownership is checked here for the message, and again in the database when the
    # close actually happens — a forged button never gets as far as a write.
    if event is None or event["organiser_telegram_id"] != update.effective_user.id:
        await query.answer("That isn't one of your hackathons.", show_alert=True)
        return
    if not event["is_active"]:
        await query.answer("That hackathon is already closed.", show_alert=True)
        return

    await query.answer()
    await send(
        update,
        f"Close <b>{kb.esc(event['name'])}</b>?\n\n"
        "New participants will no longer be able to join or find new teammates. "
        "Existing matches will remain available.",
        reply_markup=kb.confirm_close_keyboard(event_code),
    )


@db_guard
async def handle_event_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 2 of closing: do it, re-checking ownership inside the transaction."""
    query = update.callback_query

    if query.data == "evcloseno":
        await query.answer("Cancelled")
        await safe_edit(query, "Cancelled — the hackathon is still open.")
        return

    event_code = query.data.split(":", 1)[1]
    result = await run_db(db.close_event_as_organiser, event_code, update.effective_user.id)

    if result == "closed":
        name = await event_name_for(context, event_code)
        STATS.record_action("event_closed")
        logger.info("Organiser %s closed event %s", update.effective_user.id, event_code)
        await query.answer("Closed")
        await safe_edit(
            query,
            f"<b>{kb.esc(name)}</b> is closed.\n\n"
            "Matchmaking has stopped. Everyone keeps the matches and usernames they "
            "already have, and it no longer counts towards your open-hackathon limit.",
        )
        return
    if result == "already_closed":
        await query.answer("Already closed.", show_alert=True)
        await safe_edit(query, "That hackathon is already closed.")
        return

    # 'not_owner' / 'not_found' — a stale or forged button. Say the same thing for both
    # so nothing is revealed about events belonging to anyone else.
    logger.warning(
        "Rejected close of %s by user %s (%s)", event_code, update.effective_user.id, result
    )
    await query.answer("That isn't one of your hackathons.", show_alert=True)
    await safe_edit(query, "That isn't one of your hackathons.")


# ------------------------------------------------------------ free-text + errors


@db_guard
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = context.user_data.get("mode")
    if mode == "await_event_name":
        await capture_event_name(update, context)
        return
    if mode == "await_announcement":
        await create_event_from_announcement(update, context)
        return
    if mode == "await_note":
        await capture_note(update, context)
        return

    draft_event = context.user_data.get("draft", {}).get("event_code")
    mid_onboarding = bool(draft_event) and not await run_db(
        db.get_profile, update.effective_user.id, draft_event
    )

    if mid_onboarding or context.user_data.get("edit_field"):
        await send(update, "Tap one of the buttons above to continue, or /restart to begin again.")
        return

    profile = await current_profile(update, context)
    if profile:
        await show_menu(update, context, intro="I work with buttons.")
    else:
        await no_profile_prompt(update)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        # Another poller holds this token — usually the previous instance still
        # shutting down after a redeploy. python-telegram-bot keeps retrying and
        # takes over once it stops, so this is a warning, not a failure.
        logger.warning("Another instance is polling this bot token; waiting to take over.")
        return

    if isinstance(context.error, NetworkError):
        # Transient connectivity (Wi-Fi drop, DNS blip). python-telegram-bot retries
        # polling by itself, so a one-line warning beats a traceback per attempt.
        logger.warning("Network problem talking to Telegram: %s", context.error)
        return

    logger.exception("Unhandled error while processing update", exc_info=context.error)
    if isinstance(update, Update):
        if update.callback_query:
            try:
                await update.callback_query.answer("Something went wrong — please try again.", show_alert=True)
            except TelegramError:
                pass
        elif update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    "Something went wrong on my side. Please try again.",
                    reply_markup=kb.home_keyboard(),
                )
            except TelegramError:
                pass


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.pop("_heartbeat_task", None)
    if task is not None:
        task.cancel()
    db.close_pool()


async def _heartbeat() -> None:
    """One health line per minute to stdout, which is where Railway keeps logs.

    Enough to tell during a hackathon whether MatchX is healthy: how much work it is
    doing, how slow the database is, whether it is shedding load (pool timeouts) and
    whether Telegram is throttling us.
    """
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            pool_stats = db._pool.get_stats() if db._pool is not None else None
            logger.info("health | %s", STATS.format_heartbeat(pool_stats))
        except asyncio.CancelledError:
            raise
        except Exception:                      # never let telemetry kill the bot
            logger.debug("heartbeat failed", exc_info=True)


async def post_init(application: Application) -> None:
    # Replace Python's small default thread pool: run_db() dispatches every database
    # call through it, so its size is the real ceiling on concurrent DB work.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=DB_EXECUTOR_WORKERS, thread_name_prefix="db")
    )
    logger.info(
        "DB concurrency: %s worker threads, pool max %s, pool timeout %ss",
        DB_EXECUTOR_WORKERS, db.POOL_MAX_SIZE, db.POOL_TIMEOUT,
    )
    application.bot_data["_heartbeat_task"] = asyncio.create_task(_heartbeat())

    await application.bot.set_my_commands([
        BotCommand("start", "Open the menu / join a hackathon"),
        BotCommand("find", "Browse potential teammates"),
        BotCommand("matches", "Your matches and requests"),
        BotCommand("profile", "View or edit your profile"),
        BotCommand("events", "Switch hackathon"),
        BotCommand("restart", "Redo your profile"),
        BotCommand("newevent", "Organisers: create a hackathon link"),
        BotCommand("help", "How this works"),
    ])
    me = await application.bot.get_me()
    logger.info("Connected as @%s", me.username)


def build_application() -> Application:
    application = (
        Application.builder()
        .token(TOKEN)
        # Queues and retries outgoing calls so a burst of matches never trips
        # Telegram's flood limits (30 messages/second overall, 1/second per chat).
        .rate_limiter(AIORateLimiter(max_retries=3))
        .concurrent_updates(True)
        .persistence(PicklePersistence(filepath=PERSISTENCE_FILE))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler(["find", "match"], find_matches))
    application.add_handler(CommandHandler(["matches", "connections"], matches_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("events", events_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("newevent", newevent_command))
    application.add_handler(CommandHandler("myevents", myevents_command))

    application.add_handler(CallbackQueryHandler(handle_school_choice, pattern=r"^school_"))
    application.add_handler(CallbackQueryHandler(handle_preference_choice, pattern=r"^pref_"))
    application.add_handler(CallbackQueryHandler(handle_discipline_choice, pattern=r"^discipline_"))
    application.add_handler(CallbackQueryHandler(handle_team_status_choice, pattern=r"^status_"))
    application.add_handler(CallbackQueryHandler(handle_skill_choice, pattern=r"^(offer|need)_"))
    application.add_handler(CallbackQueryHandler(handle_note_choice, pattern=r"^note:"))
    application.add_handler(CallbackQueryHandler(handle_menu, pattern=r"^menu:"))
    application.add_handler(CallbackQueryHandler(handle_edit_field, pattern=r"^edit:"))
    application.add_handler(CallbackQueryHandler(handle_browse_action, pattern=r"^browse:"))
    application.add_handler(CallbackQueryHandler(handle_response, pattern=r"^resp:"))
    # Registered before ^ev: — "evclose"/"evcloseyes" must not be captured by it.
    application.add_handler(CallbackQueryHandler(handle_event_close_request, pattern=r"^evclose:"))
    application.add_handler(CallbackQueryHandler(handle_event_close_confirm, pattern=r"^(evcloseyes:|evcloseno$)"))
    application.add_handler(CallbackQueryHandler(handle_event_switch, pattern=r"^ev:"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(handle_error)
    return application


def main() -> None:
    try:
        db.ping()
    except DatabaseError:
        logger.error("Could not reach the database — check DATABASE_URL in .env")
        raise SystemExit(1)

    logger.info("Hackathon Match starting…")

    # run_polling owns the process lifecycle: it installs handlers for SIGINT/SIGTERM
    # (what Koyeb sends on stop and redeploy) and shuts down cleanly. It must be called
    # exactly once — it closes the event loop on the way out.
    #
    # A redeploy usually overlaps the previous instance, which makes Telegram return
    # Conflict on getUpdates. python-telegram-bot keeps retrying and takes over by
    # itself once the old process lets go, so there is nothing to do here but log it
    # quietly (see handle_error).
    build_application().run_polling(
        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True
    )

    logger.info("Hackathon Match stopped.")


if __name__ == "__main__":
    main()
