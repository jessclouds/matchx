"""Inline keyboards and card rendering.

Callback data grammar (kept short — Telegram allows 64 bytes):
    school_<v> pref_<v> discipline_<v> status_<v>   onboarding answers
    offer_<v>  need_<v>                             multi-select skills
    menu:<action>                                   main navigation
    edit:<field>                                    edit one profile field
    browse:req:<uid> | browse:skip:<uid> | browse:next | browse:reset
    resp:yes:<uid> | resp:no:<uid>                  answer an incoming request
    ev:<event_code>                                 switch event
"""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from constants import (
    DISCIPLINE_LABELS,
    MAX_SKILLS,
    SCHOOL_PREF_LABELS,
    SKILLS,
    STATUS_LABELS,
    format_skills,
)
from matching import Profile, ScoredCandidate


def build_keyboard(prefix: str, options: list[tuple[str, str]], per_row: int = 2) -> InlineKeyboardMarkup:
    """A one-choice-per-tap keyboard for the onboarding questions."""
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for value, label in options:
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{value}"))
        if len(row) == per_row:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def build_skill_keyboard(prefix: str, selected: set[str], show_wildcard: bool = False) -> InlineKeyboardMarkup:
    """Skill keyboard with ✅ on chosen skills, plus a Done button."""
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for value, label in SKILLS:
        text = f"✅ {label}" if value in selected else label
        row.append(InlineKeyboardButton(text, callback_data=f"{prefix}_{value}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    if show_wildcard:
        mark = "✅ " if prefix == "need" and not selected else ""
        keyboard.append([InlineKeyboardButton(
            f"{mark}✨ No preference — open to anyone",
            callback_data=f"{prefix}_any",
        )])

    count = f" ({len(selected)}/{MAX_SKILLS})" if selected else ""
    keyboard.append([InlineKeyboardButton(f"Done{count} ➡️", callback_data=f"{prefix}_done")])

    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard(match_count: int = 0, pending_count: int = 0) -> InlineKeyboardMarkup:
    matches_label = "💬 My matches"
    if match_count or pending_count:
        badge = str(match_count) + (f" · {pending_count} new" if pending_count else "")
        matches_label = f"💬 My matches ({badge})"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Find teammates", callback_data="menu:find")],
        [InlineKeyboardButton(matches_label, callback_data="menu:matches")],
        [InlineKeyboardButton("👤 My profile", callback_data="menu:profile")],
        [InlineKeyboardButton("❓ Help", callback_data="menu:help")],
    ])


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu:home")]])


def profile_keyboard(is_active: bool) -> InlineKeyboardMarkup:
    pause = (
        InlineKeyboardButton("⏸ Pause matchmaking", callback_data="menu:pause")
        if is_active
        else InlineKeyboardButton("▶️ Resume matchmaking", callback_data="menu:resume")
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit profile", callback_data="menu:edit")],
        [pause],
        [InlineKeyboardButton("🔍 Find teammates", callback_data="menu:find"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu:home")],
    ])


def edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏫 School", callback_data="edit:school"),
         InlineKeyboardButton("🎯 School preference", callback_data="edit:pref")],
        [InlineKeyboardButton("📚 Discipline", callback_data="edit:discipline"),
         InlineKeyboardButton("👥 Team status", callback_data="edit:status")],
        [InlineKeyboardButton("💪 Skills I offer", callback_data="edit:offer"),
         InlineKeyboardButton("🔎 Skills I need", callback_data="edit:need")],
        [InlineKeyboardButton("🔁 Redo everything", callback_data="menu:restart")],
        [InlineKeyboardButton("⬅️ Back", callback_data="menu:profile")],
    ])


def browse_keyboard(candidate_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Request match", callback_data=f"browse:req:{candidate_id}"),
         InlineKeyboardButton("⏭ Skip", callback_data=f"browse:skip:{candidate_id}")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu:home")],
    ])


def request_response_keyboard(requester_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept", callback_data=f"resp:yes:{requester_id}"),
         InlineKeyboardButton("🚫 Decline", callback_data=f"resp:no:{requester_id}")],
    ])


def no_candidates_keyboard(has_skips: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_skips:
        rows.append([InlineKeyboardButton("🔄 Review people I skipped", callback_data="browse:reset")])
    rows.append([InlineKeyboardButton("✏️ Edit profile", callback_data="menu:edit"),
                 InlineKeyboardButton("🏠 Menu", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def event_picker_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(e["name"], callback_data=f"ev:{e['event_code']}")] for e in events[:10]]
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------- rendering

def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def render_profile(profile: Profile, event_name: str) -> str:
    """The user's own profile — they may see everything about themselves."""
    needs = "Open to anyone" if profile.open_to_any or not profile.skills_needed else format_skills(profile.skills_needed)
    status_note = "" if profile.is_active else "\n\n⏸ <i>Matchmaking paused — you won't appear to others.</i>"
    return (
        f"👤 <b>Your profile</b> — {esc(event_name)}\n\n"
        f"🏫 School: <b>{esc(profile.school)}</b>\n"
        f"🎯 Prefers: {esc(SCHOOL_PREF_LABELS.get(profile.school_preference, profile.school_preference))}\n"
        f"📚 Discipline: {esc(DISCIPLINE_LABELS.get(profile.discipline, profile.discipline))}\n"
        f"👥 Status: {esc(STATUS_LABELS.get(profile.team_status, profile.team_status))}\n\n"
        f"💪 I offer: {esc(format_skills(profile.skills_offered))}\n"
        f"🔎 I need: {esc(needs)}"
        f"{status_note}"
    )


def _candidate_body(candidate: ScoredCandidate) -> str:
    p = candidate.profile
    needs = "Open to anyone" if p.open_to_any or not p.skills_needed else format_skills(p.skills_needed)
    lines = [
        f"🏫 {esc(p.school)}  ·  📚 {esc(DISCIPLINE_LABELS.get(p.discipline, p.discipline))}",
        f"👥 {esc(STATUS_LABELS.get(p.team_status, p.team_status))}",
        "",
        f"💪 Offers: {esc(format_skills(p.skills_offered))}",
        f"🔎 Looking for: {esc(needs)}",
        "",
        f"✅ <b>Matches your needs:</b> {esc(format_skills(candidate.offers_for_me))}",
    ]
    if candidate.needs_from_me:
        lines.append(f"🙌 <b>You have what they want:</b> {esc(format_skills(candidate.needs_from_me))}")
    return "\n".join(lines)


def render_candidate(candidate: ScoredCandidate, remaining: int) -> str:
    """A candidate card. No name, no @username — those appear only after a mutual yes."""
    tail = f"  ·  {remaining} more in your queue" if remaining > 0 else "  ·  last one for now"
    header = f"🧑‍💻 <b>Potential teammate</b>{tail}"
    return f"{header}\n\n{_candidate_body(candidate)}"


def render_request_card(requester: ScoredCandidate, event_name: str) -> str:
    """Shown to the recipient of a match request — still anonymous until they accept."""
    return (
        f"🤝 <b>Someone at {esc(event_name)} wants to team up with you!</b>\n\n"
        f"{_candidate_body(requester)}\n\n"
        "Accept and you'll swap Telegram usernames. Decline and they're never told who said no."
    )


def render_match(profile: Profile) -> str:
    username = f"@{profile.telegram_username}" if profile.telegram_username else "username unavailable"
    needs = "Open to anyone" if profile.open_to_any or not profile.skills_needed else format_skills(profile.skills_needed)
    return (
        "🎉 <b>It's a match!</b>\n\n"
        f"🏫 {esc(profile.school)}  ·  📚 {esc(DISCIPLINE_LABELS.get(profile.discipline, profile.discipline))}\n"
        f"💪 Offers: {esc(format_skills(profile.skills_offered))}\n"
        f"🔎 Looking for: {esc(needs)}\n\n"
        f"💬 Message them: <b>{esc(username)}</b>\n"
        "<i>Tip: say hi first — mention the hackathon and what you're building.</i>"
    )
