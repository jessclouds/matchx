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
    ("software", "Software Dev"),
    ("ai_data", "AI / Data"),
    ("healthcare", "Healthcare"),
    ("user_research", "User Research"),
    ("uiux", "UI / UX Design"),
    ("hardware", "Hardware"),
    ("business", "Business Pitching"),
    ("marketing", "Marketing"),
    ("legal", "Legal / Regulatory"),
]

ALL_SKILL_VALUES: Final[frozenset[str]] = frozenset(value for value, _ in SKILLS)

SCHOOL_LABELS: Final[dict[str, str]] = dict(SCHOOLS)
SCHOOL_PREF_LABELS: Final[dict[str, str]] = dict(SCHOOL_PREFERENCES)
DISCIPLINE_LABELS: Final[dict[str, str]] = dict(DISCIPLINES)
STATUS_LABELS: Final[dict[str, str]] = dict(STATUSES)
SKILL_LABELS: Final[dict[str, str]] = dict(SKILLS)

MAX_SKILLS: Final[int] = 3

# Optional free-text note on a profile. Display only — never used for matching.
NOTE_MAX_LENGTH: Final[int] = 160

NOTE_PROMPT: Final[str] = (
    "Want to add a short note to your profile?\n"
    "<i>e.g. preferred hackathon track/topic, what you're building, or what kind of "
    "teammate you're looking for</i>"
)

NOTE_ASK_TEXT: Final[str] = (
    f"Send your note in one message (up to {NOTE_MAX_LENGTH} characters)."
)

NOTE_EXISTING_PROMPT: Final[str] = "Your note:\n\n{note}\n\nReplace it, or remove it?"

OFFER_QUESTION: Final[dict[str, str]] = {
    "looking": f"What can you bring to a team? (pick up to {MAX_SKILLS})",
    "has_team": f"What does your team already have? (pick up to {MAX_SKILLS})",
}

NEED_QUESTION: Final[dict[str, str]] = {
    "looking": f"What would you like teammates to bring? (pick up to {MAX_SKILLS})",
    "has_team": f"What skills does your team need? (pick up to {MAX_SKILLS})",
}

NO_USERNAME_MESSAGE: Final[str] = (
    "You need a Telegram username before you can use MatchX.\n\n"
    "Your @username is the only way a teammate can reach you after you both say yes.\n\n"
    "To set one: Telegram → Settings → tap your name → Username.\n\n"
    "Then send /start here again."
)

HELP_TEXT: Final[str] = (
    "MatchX helps you find teammates at your hackathon.\n\n"
    "<b>How it works</b>\n"
    "1. Join with your hackathon's link\n"
    "2. Answer six quick questions\n"
    "3. Browse teammates whose skills fit what you need\n"
    "4. Request a match — if they accept, you both get each other's @username\n\n"
    "<b>Commands</b>\n"
    "/start — open the menu, or join with an event link\n"
    "/find — browse teammates\n"
    "/profile — view or edit your profile\n"
    "/matches — your matches and requests\n"
    "/restart — redo your profile\n"
    "/newevent — organisers: create a hackathon link\n"
    "/help — this message"
)


def skill_label(value: str) -> str:
    return SKILL_LABELS.get(value, value)


def format_skills(values, empty_text: str = "—") -> str:
    """Turn ['software', 'ai_data'] into 'Software Dev, AI / Data' (stable order)."""
    chosen = set(values or ())
    if not chosen:
        return empty_text
    labels = [label for value, label in SKILLS if value in chosen]
    # Anything not in the catalogue (legacy rows) still gets shown.
    labels += [v for v in sorted(chosen) if v not in SKILL_LABELS]
    return ", ".join(labels)
