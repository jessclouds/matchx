"""Shared test setup: make the project importable, load .env, and clean up leftovers.

Tests only ever write into events they create themselves (organised by a synthetic
test user id, or named with a test prefix), so cleanup can be scoped precisely and
never touches real hackathon data.
"""

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / ".env")

TEST_ORGANISER_MIN = 900_000_000
TEST_ORGANISER_MAX = 919_999_999
TEST_EVENT_NAME_PREFIXES = ("pytest %", "E2E %", "Quantum Hack 2026", "Same Name Hack")


def purge_test_events() -> None:
    """Delete every event a test created, plus everything hanging off it."""
    if not os.getenv("DATABASE_URL"):
        return
    import db

    conditions = " OR ".join(["name LIKE %s"] * len(TEST_EVENT_NAME_PREFIXES))
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT event_code FROM events
            WHERE (organiser_telegram_id BETWEEN %s AND %s) OR {conditions}
            """,
            (TEST_ORGANISER_MIN, TEST_ORGANISER_MAX, *TEST_EVENT_NAME_PREFIXES),
        )
        codes = [row["event_code"] for row in cur.fetchall()]
        for code in codes:
            cur.execute("DELETE FROM matches   WHERE event_code = %s", (code,))
            cur.execute("DELETE FROM interests WHERE event_code = %s", (code,))
            cur.execute("DELETE FROM profiles  WHERE event_code = %s", (code,))
            cur.execute("DELETE FROM events    WHERE event_code = %s", (code,))
        conn.commit()


@pytest.fixture(scope="session", autouse=True)
def clean_database():
    purge_test_events()
    yield
    purge_test_events()
