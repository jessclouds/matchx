"""Environment configuration for Hackathon Match.

Everything secret lives in .env (gitignored). Nothing here is ever shown to users.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: Final[str | None] = os.getenv("BOT_TOKEN")
DATABASE_URL: Final[str | None] = os.getenv("DATABASE_URL")
LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO").upper()

# Persistence file for in-progress onboarding state, so a restart mid-onboarding
# does not strand anyone. All durable data lives in Postgres.
PERSISTENCE_FILE: Final[str] = os.getenv("PERSISTENCE_FILE", "bot_state.pickle")


def _clean_username(raw: str | None) -> str:
    """'@HackathonMatchBot' / 'https://t.me/x' -> bare username."""
    if not raw:
        return "HackathonMatchBot"
    return raw.strip().lstrip("@").rsplit("/", 1)[-1]


BOT_USERNAME: Final[str] = _clean_username(os.getenv("BOT_USERNAME"))


def deep_link(event_code: str) -> str:
    """The 'Find Teammates' link an organiser shares for an event."""
    return f"https://t.me/{BOT_USERNAME}?start={event_code}"


def require_bot_token() -> str:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing — copy .env.example to .env and fill it in.")
    return BOT_TOKEN


def require_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL missing — copy .env.example to .env and fill it in.")
    return DATABASE_URL


def setup_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        level=getattr(logging, LOG_LEVEL, logging.INFO),
    )
    # httpx logs every Telegram API call at INFO; far too chatty.
    logging.getLogger("httpx").setLevel(logging.WARNING)
