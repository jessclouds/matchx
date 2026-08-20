"""Question options, labels and user-facing copy.

Single source of truth for anything that appears on a button or in a message.
"""

from __future__ import annotations

from typing import Final

SCHOOLS: Final[list[tuple[str, str]]] = [
    ("NUS", "NUS"), ("NTU", "NTU"), ("SMU", "SMU"),
    ("SUTD", "SUTD"), ("SIT", "SIT"), ("SUSS", "SUSS"),
    ("Polytechnic", "Polytechnic"), ("Junior College", "Junior College"), ("Other", "Other"),
]

SCHOOL_PREFERENCES: Final[list[tuple[str, str]]] = [
    ("same", "Prefer my school"),
    ("different", "Prefer a different school"),
    ("none", "No preference"),
]

# Display only. Never used for eligibility or ranking.
DISCIPLINES: Final[list[tuple[str, str]]] = [
    ("med", "Medicine / Health"),
    ("comp", "Computing / AI / Data"),
    ("eng", "Engineering"),
    ("design", "Design"),
    ("business", "Business"),
    ("science", "Science"),
    ("law", "Law"),
    ("humanities", "Humanities / Social Sci"),
    ("other", "Other"),
]

STATUSES: Final[list[tuple[str, str]]] = [
    ("looking", "Solo — looking for a team"),
    ("has_team", "Have a team — need more teammates"),
]

SKILLS: Final[list[tuple[str, str]]] = [
    ("software", "Software / App Dev"),
    ("ai_data", "AI / Data"),
    ("healthcare", "Healthcare / Clinical"),
    ("user_research", "User Research"),
    ("uiux", "UI / UX Design"),
    ("hardware", "Hardware / Engineering"),
    ("business", "Business / Pitching"),
    ("marketing", "Marketing / Ops"),
    ("legal", "Legal / Regulatory"),
]

ALL_SKILL_VALUES: Final[frozenset[str]] = frozenset(value for value, _ in SKILLS)

SCHOOL_LABELS: Final[dict[str, str]] = dict(SCHOOLS)
SCHOOL_PREF_LABELS: Final[dict[str, str]] = dict(SCHOOL_PREFERENCES)
DISCIPLINE_LABELS: Final[dict[str, str]] = dict(DISCIPLINES)
STATUS_LABELS: Final[dict[str, str]] = dict(STATUSES)
SKILL_LABELS: Final[dict[str, str]] = dict(SKILLS)

MAX_SKILLS: Final[int] = 3

OFFER_QUESTION: Final[dict[str, str]] = {
    "looking": f"What can you bring to a team? (pick up to {MAX_SKILLS})",
    "has_team": f"What does your team already have? (pick up to {MAX_SKILLS})",
}

NEED_QUESTION: Final[dict[str, str]] = {
    "looking": f"What would you like teammates to bring? (pick up to {MAX_SKILLS})",
    "has_team": f"What skills does your team need? (pick up to {MAX_SKILLS})",
}

NO_USERNAME_MESSAGE: Final[str] = (
    "⚠️ <b>You need a Telegram username first</b>\n\n"
    "Your @username is the only way a teammate can reach you after you both say yes, "
    "so Hackathon Match can't add you without one.\n\n"
    "<b>How to set one</b>\n"
    "1. Telegram → Settings\n"
    "2. Tap your name → <b>Username</b>\n"
    "3. Pick one that's available and save\n\n"
    "Then send /start here again — takes 30 seconds."
)

HELP_TEXT: Final[str] = (
    "<b>Hackathon Match</b> — find teammates at your hackathon.\n\n"
    "<b>How it works</b>\n"
    "1. Join with your hackathon's link\n"
    "2. Answer 6 quick questions\n"
    "3. Browse teammates whose skills fit what you need\n"
    "4. Request a match — if they accept, you both get each other's @username\n\n"
    "<b>Commands</b>\n"
    "/start — open the menu (or join with an event link)\n"
    "/find — browse teammates\n"
    "/profile — view or edit your profile\n"
    "/matches — your matches and pending requests\n"
    "/restart — redo your profile for this event\n"
    "/newevent — organisers: create a hackathon link\n"
    "/help — this message"
)

def skill_label(value: str) -> str:
    return SKILL_LABELS.get(value, value)


def format_skills(values, empty_text: str = "—") -> str:
    """Turn ['software', 'ai_data'] into 'Software / App Dev, AI / Data' (stable order)."""
    chosen = set(values or ())
    if not chosen:
        return empty_text
    labels = [label for value, label in SKILLS if value in chosen]
    # Anything not in the catalogue (legacy rows) still gets shown.
    labels += [v for v in sorted(chosen) if v not in SKILL_LABELS]
    return ", ".join(labels)
