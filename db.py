"""All database access for Hackathon Match.

Every SQL statement in the project lives here. Handlers never build SQL.

Connections are opened per operation (Supabase's pooler handles that fine) and the
synchronous calls are wrapped by `run_db()` so they never block the asyncio loop.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import random
import re
import string
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

import psycopg
from psycopg.rows import dict_row

from config import require_database_url
from matching import Profile

logger = logging.getLogger(__name__)

T = TypeVar("T")

DATABASE_URL = require_database_url()

# Columns needed to build a Profile, in one place so every query agrees.
_COLUMN_NAMES = (
    "telegram_user_id", "event_code", "telegram_username", "school", "school_preference",
    "discipline", "team_status", "skills_offered", "skills_needed", "open_to_any", "is_active",
)
_PROFILE_COLUMNS = ", ".join(_COLUMN_NAMES)
# Qualified variant for queries that join another table carrying event_code.
_P_COLUMNS = ", ".join(f"p.{name}" for name in _COLUMN_NAMES)


class DatabaseError(RuntimeError):
    """Raised for any database failure. The message is for logs, never for users."""


try:
    from psycopg_pool import ConnectionPool

    _pool: "ConnectionPool | None" = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        max_idle=120,
        max_lifetime=1800,
        timeout=15,
        check=ConnectionPool.check_connection,
        kwargs={"row_factory": dict_row, "connect_timeout": 10},
        open=False,
    )
except ImportError:  # pragma: no cover - pool is a soft dependency
    _pool = None
    logger.info("psycopg_pool not installed — using one connection per query.")


def get_connection():
    """A pooled connection, as a context manager. Falls back to a fresh connection."""
    if _pool is None:
        return psycopg.connect(DATABASE_URL, connect_timeout=10, row_factory=dict_row)
    _pool.open()  # no-op once the pool is running
    return _pool.connection()


def close_pool() -> None:
    """Release pooled connections on shutdown (safe to call more than once)."""
    if _pool is not None and not _pool.closed:
        _pool.close()


atexit.register(close_pool)


def _run(fn: Callable[[psycopg.Cursor], T]) -> T:
    """Run `fn` inside one transaction, committing on success.

    Supabase's pooler drops idle connections, so a dead-connection failure is
    retried once with a fresh connection before it is reported as an error.
    """
    last_error: Exception | None = None

    for attempt in (1, 2):
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
                return result
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            last_error = exc
            logger.warning("Database connection problem (attempt %s/2): %s", attempt, exc)
            continue
        except psycopg.Error as exc:
            logger.exception("Database operation failed: %s", type(exc).__name__)
            raise DatabaseError(str(exc)) from exc

    logger.error("Database unreachable after retry: %s", last_error)
    raise DatabaseError(str(last_error)) from last_error


async def run_db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Await a synchronous db.* function without blocking the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _row_to_profile(row: dict[str, Any]) -> Profile:
    return Profile(
        telegram_user_id=row["telegram_user_id"],
        event_code=row["event_code"],
        telegram_username=row["telegram_username"],
        school=row["school"],
        school_preference=row["school_preference"],
        discipline=row["discipline"],
        team_status=row["team_status"],
        skills_offered=tuple(row["skills_offered"] or ()),
        skills_needed=tuple(row["skills_needed"] or ()),
        open_to_any=bool(row["open_to_any"]),
        is_active=bool(row["is_active"]),
    )


# --------------------------------------------------------------------- events


def get_event(event_code: str) -> str | None:
    """Look up a hackathon by its code. Returns the display name, or None."""
    if not event_code:
        return None

    def q(cur: psycopg.Cursor) -> str | None:
        cur.execute("SELECT name FROM events WHERE event_code = %s", (event_code,))
        row = cur.fetchone()
        return row["name"] if row else None

    return _run(q)


def normalise_event_code(raw: str | None) -> str:
    """Deep-link payloads are user input — keep only what an event code may contain."""
    if not raw:
        return ""
    return re.sub(r"[^A-Za-z0-9_-]", "", raw.strip())[:64].lower()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())[:20]
    return slug or "hack"


def create_event(
    name: str,
    announcement: str | None = None,
    organiser_telegram_id: int | None = None,
) -> tuple[str, str]:
    """Create an event with a unique code. Returns (event_code, name)."""
    base = _slugify(name)

    def q(cur: psycopg.Cursor) -> tuple[str, str]:
        for _ in range(10):
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
            code = f"{base}{suffix}"
            cur.execute(
                """
                INSERT INTO events (event_code, name, announcement, organiser_telegram_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_code) DO NOTHING
                RETURNING event_code, name
                """,
                (code, name[:120], announcement, organiser_telegram_id),
            )
            row = cur.fetchone()
            if row:
                return row["event_code"], row["name"]
        raise DatabaseError("could not allocate a unique event code")

    return _run(q)


def list_events_for_organiser(organiser_telegram_id: int) -> list[dict[str, Any]]:
    def q(cur: psycopg.Cursor) -> list[dict[str, Any]]:
        cur.execute(
            """
            SELECT e.event_code, e.name, e.created_at,
                   (SELECT count(*) FROM profiles p WHERE p.event_code = e.event_code) AS participants
            FROM events e
            WHERE e.organiser_telegram_id = %s
            ORDER BY e.created_at DESC
            LIMIT 20
            """,
            (organiser_telegram_id,),
        )
        return list(cur.fetchall())

    return _run(q)


# ------------------------------------------------------------------- profiles


def save_profile(
    telegram_user_id: int,
    event_code: str,
    telegram_username: str | None,
    school: str,
    school_preference: str,
    discipline: str,
    team_status: str,
    skills_offered: Sequence[str],
    skills_needed: Sequence[str],
    open_to_any: bool,
) -> None:
    """Insert a profile, or overwrite it if one already exists for this user + event.

    Identity is (telegram_user_id, event_code), so re-running onboarding updates the
    same row instead of creating a duplicate. `created_at` is deliberately untouched.
    """

    def q(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO profiles (
                telegram_user_id, event_code, telegram_username,
                school, school_preference, discipline, team_status,
                skills_offered, skills_needed, open_to_any, is_active, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, now())
            ON CONFLICT (telegram_user_id, event_code) DO UPDATE SET
                telegram_username = EXCLUDED.telegram_username,
                school            = EXCLUDED.school,
                school_preference = EXCLUDED.school_preference,
                discipline        = EXCLUDED.discipline,
                team_status       = EXCLUDED.team_status,
                skills_offered    = EXCLUDED.skills_offered,
                skills_needed     = EXCLUDED.skills_needed,
                open_to_any       = EXCLUDED.open_to_any,
                is_active         = true,
                updated_at        = now()
            """,
            (
                telegram_user_id,
                event_code,
                telegram_username,
                school,
                school_preference,
                discipline,
                team_status,
                list(skills_offered),
                list(skills_needed),
                bool(open_to_any),
            ),
        )

    _run(q)


def get_profile(telegram_user_id: int, event_code: str) -> Profile | None:
    def q(cur: psycopg.Cursor) -> Profile | None:
        cur.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE telegram_user_id = %s AND event_code = %s",
            (telegram_user_id, event_code),
        )
        row = cur.fetchone()
        return _row_to_profile(row) if row else None

    return _run(q)


def get_latest_profile(telegram_user_id: int) -> Profile | None:
    """Most recently updated profile across all events — used when /start has no code."""

    def q(cur: psycopg.Cursor) -> Profile | None:
        cur.execute(
            f"""
            SELECT {_PROFILE_COLUMNS} FROM profiles
            WHERE telegram_user_id = %s
            ORDER BY updated_at DESC LIMIT 1
            """,
            (telegram_user_id,),
        )
        row = cur.fetchone()
        return _row_to_profile(row) if row else None

    return _run(q)


def list_profiles_for_user(telegram_user_id: int) -> list[dict[str, Any]]:
    def q(cur: psycopg.Cursor) -> list[dict[str, Any]]:
        cur.execute(
            """
            SELECT p.event_code, e.name
            FROM profiles p JOIN events e USING (event_code)
            WHERE p.telegram_user_id = %s
            ORDER BY p.updated_at DESC
            """,
            (telegram_user_id,),
        )
        return list(cur.fetchall())

    return _run(q)


def update_username(telegram_user_id: int, telegram_username: str | None) -> None:
    """Keep the stored @username fresh — people rename themselves."""

    def q(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            UPDATE profiles SET telegram_username = %s, updated_at = now()
            WHERE telegram_user_id = %s AND telegram_username IS DISTINCT FROM %s
            """,
            (telegram_username, telegram_user_id, telegram_username),
        )

    _run(q)


def set_active(telegram_user_id: int, event_code: str, is_active: bool) -> None:
    def q(cur: psycopg.Cursor) -> None:
        cur.execute(
            "UPDATE profiles SET is_active = %s, updated_at = now() WHERE telegram_user_id = %s AND event_code = %s",
            (is_active, telegram_user_id, event_code),
        )

    _run(q)


def get_event_pool(event_code: str) -> list[Profile]:
    """Every active profile in one event. Events are fully isolated from each other."""

    def q(cur: psycopg.Cursor) -> list[Profile]:
        cur.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM profiles WHERE event_code = %s AND is_active",
            (event_code,),
        )
        return [_row_to_profile(row) for row in cur.fetchall()]

    return _run(q)


# ------------------------------------------------------------------ interests


def _pair_lock(cur: psycopg.Cursor, event_code: str, a: int, b: int) -> None:
    """Serialise everything touching one pair, so simultaneous likes cannot race."""
    lo, hi = sorted((a, b))
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{event_code}:{lo}:{hi}",))


def get_interacted_user_ids(telegram_user_id: int, event_code: str) -> set[int]:
    """Everyone this user should not be shown again while browsing."""

    def q(cur: psycopg.Cursor) -> set[int]:
        cur.execute(
            """
            SELECT to_user_id AS other FROM interests
            WHERE event_code = %s AND from_user_id = %s
            UNION
            SELECT from_user_id FROM interests
            WHERE event_code = %s AND to_user_id = %s AND status IN ('pending', 'accepted', 'declined')
            UNION
            SELECT CASE WHEN user_a = %s THEN user_b ELSE user_a END FROM matches
            WHERE event_code = %s AND %s IN (user_a, user_b)
            """,
            (event_code, telegram_user_id, event_code, telegram_user_id,
             telegram_user_id, event_code, telegram_user_id),
        )
        return {row["other"] for row in cur.fetchall()}

    return _run(q)


def get_skipped_user_ids(telegram_user_id: int, event_code: str) -> set[int]:
    def q(cur: psycopg.Cursor) -> set[int]:
        cur.execute(
            "SELECT to_user_id FROM interests WHERE event_code = %s AND from_user_id = %s AND status = 'skipped'",
            (event_code, telegram_user_id),
        )
        return {row["to_user_id"] for row in cur.fetchall()}

    return _run(q)


def clear_skips(telegram_user_id: int, event_code: str) -> int:
    """Second pass: let a user see the people they skipped earlier."""

    def q(cur: psycopg.Cursor) -> int:
        cur.execute(
            "DELETE FROM interests WHERE event_code = %s AND from_user_id = %s AND status = 'skipped'",
            (event_code, telegram_user_id),
        )
        return cur.rowcount

    return _run(q)


def record_skip(telegram_user_id: int, event_code: str, other_user_id: int) -> None:
    """A skip is private — the other person is never told."""
    if telegram_user_id == other_user_id:
        return

    def q(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO interests (event_code, from_user_id, to_user_id, status)
            VALUES (%s, %s, %s, 'skipped')
            ON CONFLICT (event_code, from_user_id, to_user_id) DO NOTHING
            """,
            (event_code, telegram_user_id, other_user_id),
        )

    _run(q)


def _insert_match(cur: psycopg.Cursor, event_code: str, a: int, b: int) -> bool:
    """Create the match row exactly once. True only when this call created it."""
    lo, hi = sorted((a, b))
    cur.execute(
        """
        INSERT INTO matches (event_code, user_a, user_b) VALUES (%s, %s, %s)
        ON CONFLICT (event_code, user_a, user_b) DO NOTHING
        RETURNING id
        """,
        (event_code, lo, hi),
    )
    return cur.fetchone() is not None


def request_match(event_code: str, from_user_id: int, to_user_id: int) -> str:
    """Person A asks to team up with Person B.

    Returns one of:
      'requested'       — B now has a pending request to answer
      'already_pending' — A had already asked; B still has not answered
      'matched'         — B had already asked A, so this completes a mutual match
      'already_matched' — they are already matched
      'invalid'         — self-request or missing profile
    """
    if from_user_id == to_user_id:
        return "invalid"

    def q(cur: psycopg.Cursor) -> str:
        _pair_lock(cur, event_code, from_user_id, to_user_id)

        cur.execute(
            """
            SELECT 1 FROM matches
            WHERE event_code = %s AND user_a = %s AND user_b = %s
            """,
            (event_code, *sorted((from_user_id, to_user_id))),
        )
        if cur.fetchone():
            return "already_matched"

        cur.execute(
            "SELECT status FROM interests WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s",
            (event_code, to_user_id, from_user_id),
        )
        reverse = cur.fetchone()

        if reverse and reverse["status"] == "pending":
            # Mutual interest — accept both directions and create the match once.
            cur.execute(
                """
                UPDATE interests SET status = 'accepted', responded_at = now()
                WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s
                """,
                (event_code, to_user_id, from_user_id),
            )
            cur.execute(
                """
                INSERT INTO interests (event_code, from_user_id, to_user_id, status, responded_at)
                VALUES (%s, %s, %s, 'accepted', now())
                ON CONFLICT (event_code, from_user_id, to_user_id)
                DO UPDATE SET status = 'accepted', responded_at = now()
                """,
                (event_code, from_user_id, to_user_id),
            )
            _insert_match(cur, event_code, from_user_id, to_user_id)
            return "matched"

        cur.execute(
            "SELECT status FROM interests WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s",
            (event_code, from_user_id, to_user_id),
        )
        existing = cur.fetchone()
        if existing and existing["status"] == "pending":
            return "already_pending"

        cur.execute(
            """
            INSERT INTO interests (event_code, from_user_id, to_user_id, status, created_at, responded_at)
            VALUES (%s, %s, %s, 'pending', now(), NULL)
            ON CONFLICT (event_code, from_user_id, to_user_id)
            DO UPDATE SET status = 'pending', created_at = now(), responded_at = NULL
            """,
            (event_code, from_user_id, to_user_id),
        )
        return "requested"

    return _run(q)


def respond_to_request(event_code: str, requester_id: int, responder_id: int, accept: bool) -> str:
    """B answers A's request.

    Returns 'matched', 'declined', 'already_matched', 'already_declined' or 'not_found'.
    """
    if requester_id == responder_id:
        return "not_found"

    def q(cur: psycopg.Cursor) -> str:
        _pair_lock(cur, event_code, requester_id, responder_id)

        cur.execute(
            "SELECT 1 FROM matches WHERE event_code = %s AND user_a = %s AND user_b = %s",
            (event_code, *sorted((requester_id, responder_id))),
        )
        if cur.fetchone():
            return "already_matched"

        cur.execute(
            "SELECT status FROM interests WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s",
            (event_code, requester_id, responder_id),
        )
        row = cur.fetchone()
        if not row or row["status"] not in ("pending", "accepted"):
            if row and row["status"] == "declined":
                return "already_declined"
            return "not_found"

        if not accept:
            cur.execute(
                """
                UPDATE interests SET status = 'declined', responded_at = now()
                WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s
                """,
                (event_code, requester_id, responder_id),
            )
            return "declined"

        cur.execute(
            """
            UPDATE interests SET status = 'accepted', responded_at = now()
            WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s
            """,
            (event_code, requester_id, responder_id),
        )
        cur.execute(
            """
            INSERT INTO interests (event_code, from_user_id, to_user_id, status, responded_at)
            VALUES (%s, %s, %s, 'accepted', now())
            ON CONFLICT (event_code, from_user_id, to_user_id)
            DO UPDATE SET status = 'accepted', responded_at = now()
            """,
            (event_code, responder_id, requester_id),
        )
        created = _insert_match(cur, event_code, requester_id, responder_id)
        return "matched" if created else "already_matched"

    return _run(q)


def get_matches(telegram_user_id: int, event_code: str) -> list[Profile]:
    def q(cur: psycopg.Cursor) -> list[Profile]:
        cur.execute(
            f"""
            SELECT {_P_COLUMNS} FROM profiles p
            WHERE p.event_code = %s AND p.telegram_user_id IN (
                SELECT CASE WHEN user_a = %s THEN user_b ELSE user_a END
                FROM matches WHERE event_code = %s AND %s IN (user_a, user_b)
            )
            ORDER BY p.telegram_user_id
            """,
            (event_code, telegram_user_id, event_code, telegram_user_id),
        )
        return [_row_to_profile(row) for row in cur.fetchall()]

    return _run(q)


def get_incoming_requests(telegram_user_id: int, event_code: str) -> list[Profile]:
    """People waiting on this user's answer."""

    def q(cur: psycopg.Cursor) -> list[Profile]:
        cur.execute(
            f"""
            SELECT {_P_COLUMNS} FROM profiles p
            JOIN interests i ON i.from_user_id = p.telegram_user_id AND i.event_code = p.event_code
            WHERE i.event_code = %s AND i.to_user_id = %s AND i.status = 'pending'
            ORDER BY i.created_at
            """,
            (event_code, telegram_user_id),
        )
        return [_row_to_profile(row) for row in cur.fetchall()]

    return _run(q)


def get_outgoing_requests(telegram_user_id: int, event_code: str) -> list[Profile]:
    """Requests this user has sent that are still unanswered."""

    def q(cur: psycopg.Cursor) -> list[Profile]:
        cur.execute(
            f"""
            SELECT {_P_COLUMNS} FROM profiles p
            JOIN interests i ON i.to_user_id = p.telegram_user_id AND i.event_code = p.event_code
            WHERE i.event_code = %s AND i.from_user_id = %s AND i.status = 'pending'
            ORDER BY i.created_at
            """,
            (event_code, telegram_user_id),
        )
        return [_row_to_profile(row) for row in cur.fetchall()]

    return _run(q)


def has_pending_request(event_code: str, from_user_id: int, to_user_id: int) -> bool:
    def q(cur: psycopg.Cursor) -> bool:
        cur.execute(
            """
            SELECT 1 FROM interests
            WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s AND status = 'pending'
            """,
            (event_code, from_user_id, to_user_id),
        )
        return cur.fetchone() is not None

    return _run(q)


def ping() -> bool:
    """Startup health check."""
    def q(cur: psycopg.Cursor) -> bool:
        cur.execute("SELECT 1 AS ok")
        return cur.fetchone()["ok"] == 1

    return _run(q)


def find_pending_request_event(from_user_id: int, to_user_id: int) -> str | None:
    """Which event a still-open request belongs to (callback data can't carry it)."""

    def q(cur: psycopg.Cursor) -> str | None:
        cur.execute(
            """
            SELECT event_code FROM interests
            WHERE from_user_id = %s AND to_user_id = %s AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (from_user_id, to_user_id),
        )
        row = cur.fetchone()
        return row["event_code"] if row else None

    return _run(q)


def apply_schema(path: str = "schema.sql") -> None:
    """Create or upgrade the tables. schema.sql is idempotent, so this is safe to re-run."""
    sql = Path(path).read_text()
    _run(lambda cur: cur.execute(sql))
    logger.info("Schema applied from %s", path)


if __name__ == "__main__":
    # python db.py  →  set up the database, then show what's there.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    apply_schema(str(Path(__file__).with_name("schema.sql")))
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT event_code, name FROM events ORDER BY created_at")
        for row in cur.fetchall():
            print(f"  {row['event_code']:<24} {row['name']}")
        cur.execute("SELECT count(*) AS n FROM profiles")
        print(f"  profiles: {cur.fetchone()['n']}")
