"""Scale and parity tests for the SQL candidate query.

The hard filters now live in Postgres. These tests prove the SQL returns exactly what
the Python rules in matching.py would return, and that it stays fast on a large pool.
Everything is created inside a throwaway event and deleted afterwards.
"""

import os
import random
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not configured")

import db  # noqa: E402
from constants import SKILLS  # noqa: E402
from matching import is_eligible, rank_candidates  # noqa: E402

SKILL_VALUES = [value for value, _ in SKILLS]
SCHOOLS = ["NUS", "NTU", "SMU", "SUTD", "Other"]
PREFS = ["same", "different", "none"]
BASE_ID = 950_000_000


def _purge(event_code: str) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        for table in ("matches", "interests", "profiles", "events"):
            cur.execute(f"DELETE FROM {table} WHERE event_code = %s", (event_code,))
        conn.commit()


@pytest.fixture
def event():
    code, _ = db.create_event(f"pytest scale {uuid.uuid4().hex[:6]}", None, BASE_ID)
    try:
        yield code
    finally:
        _purge(code)


def seed_pool(event_code: str, count: int, rng: random.Random) -> list[int]:
    """Bulk-insert `count` varied profiles in one round trip."""
    rows = []
    for i in range(count):
        uid = BASE_ID + i + 1
        offers = rng.sample(SKILL_VALUES, rng.randint(1, 3))
        wildcard = rng.random() < 0.15
        needs = [] if wildcard else rng.sample(SKILL_VALUES, rng.randint(0, 3))
        rows.append((
            uid, event_code, f"user{i}", rng.choice(SCHOOLS), rng.choice(PREFS),
            "comp", rng.choice(["looking", "has_team"]), offers, needs,
            wildcard or not needs, rng.random() > 0.1,     # ~10% paused
        ))

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO profiles (telegram_user_id, event_code, telegram_username, school,
                                  school_preference, discipline, team_status, skills_offered,
                                  skills_needed, open_to_any, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (telegram_user_id, event_code) DO NOTHING
            """,
            rows,
        )
        conn.commit()
    return [row[0] for row in rows]


# --------------------------------------------------------------------- parity

def test_sql_filters_match_the_python_rules_exactly(event):
    """Whatever SQL returns must be exactly what is_eligible() would allow."""
    rng = random.Random(7)
    ids = seed_pool(event, 60, rng)

    # Scatter some interactions so the exclusion clauses are exercised too.
    with db.get_connection() as conn, conn.cursor() as cur:
        for viewer in ids[:5]:
            for target in rng.sample(ids, 8):
                if viewer == target:
                    continue
                cur.execute(
                    """
                    INSERT INTO interests (event_code, from_user_id, to_user_id, status)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (event, viewer, target, rng.choice(["skipped", "pending", "declined"])),
                )
        conn.commit()

    pool = db.get_event_pool(event)
    for viewer in ids[:5]:
        me = db.get_profile(viewer, event)
        excluded = db.get_interacted_user_ids(viewer, event)

        from_sql = {p.telegram_user_id for p in db.find_candidates(me, limit=500)}
        from_python = {
            p.telegram_user_id for p in pool
            if p.telegram_user_id not in excluded and is_eligible(me, p)
        }
        assert from_sql == from_python, f"viewer {viewer}: SQL and Python disagree"


def test_sql_page_contains_the_best_candidate(event):
    """The page is ordered by the ranking keys, so the true winner is always in it."""
    rng = random.Random(11)
    ids = seed_pool(event, 80, rng)
    pool = db.get_event_pool(event)

    for viewer in ids[:5]:
        me = db.get_profile(viewer, event)
        excluded = db.get_interacted_user_ids(viewer, event)
        full = rank_candidates(me, pool, exclude_user_ids=excluded)
        if not full:
            continue
        best_key = full[0].sort_key

        page = db.find_candidates(me, limit=25)
        assert page, "SQL found nothing while Python found candidates"
        page_best = rank_candidates(me, page)[0]
        assert page_best.sort_key == best_key, "the page missed the best candidate"


def test_closed_events_stop_matching(event):
    rng = random.Random(3)
    ids = seed_pool(event, 10, rng)
    me = db.get_profile(ids[0], event)
    assert db.find_candidates(me), "sanity: open event has candidates"

    db.set_event_active(event, False)
    assert db.find_candidates(me) == [], "a closed hackathon must stop matching"
    db.set_event_active(event, True)
    assert db.find_candidates(me), "reopening restores matching"


def test_matched_pairs_are_excluded_by_sql(event):
    rng = random.Random(5)
    ids = seed_pool(event, 12, rng)
    me_id = ids[0]
    me = db.get_profile(me_id, event)
    candidates = db.find_candidates(me, limit=50)
    if not candidates:
        pytest.skip("no eligible candidate in this random pool")

    other = candidates[0].telegram_user_id
    db.request_match(event, me_id, other)
    db.respond_to_request(event, me_id, other, accept=True)

    assert other not in {p.telegram_user_id for p in db.find_candidates(me, limit=50)}


# ----------------------------------------------------------------- performance

@pytest.mark.parametrize("pool_size", [1500])
def test_large_pool_stays_fast(event, pool_size):
    """A browse must stay one indexed query, not a scan of the whole event."""
    rng = random.Random(13)
    ids = seed_pool(event, pool_size, rng)
    me = db.get_profile(ids[0], event)

    timings = []
    for _ in range(5):
        started = time.perf_counter()
        page = db.find_candidates(me, limit=26)
        timings.append(time.perf_counter() - started)
        assert len(page) <= 26, "the page limit must be respected"

    median = sorted(timings)[len(timings) // 2]
    assert median < 2.0, f"candidate query too slow on {pool_size} profiles: {median:.2f}s"


def test_browsing_does_not_grow_with_pool_size(event):
    """Cost per card is flat: the same single query regardless of how many profiles exist."""
    rng = random.Random(17)
    ids = seed_pool(event, 400, rng)
    me = db.get_profile(ids[0], event)

    small = time.perf_counter()
    db.find_candidates(me, limit=26)
    small_elapsed = time.perf_counter() - small

    seed_pool(event, 1200, random.Random(19))
    big = time.perf_counter()
    db.find_candidates(me, limit=26)
    big_elapsed = time.perf_counter() - big

    # Allow generous headroom for network jitter; we only care that it is not linear.
    assert big_elapsed < small_elapsed * 5 + 1.0


# ---------------------------------------------------------------- concurrency

def test_simultaneous_accepts_create_exactly_one_match(event):
    """Both sides accept at the same instant from different threads."""
    import threading

    rng = random.Random(23)
    ids = seed_pool(event, 6, rng)
    a, b = ids[0], ids[1]

    # Both directions pending, so either thread completing would form the match.
    with db.get_connection() as conn, conn.cursor() as cur:
        for src, dst in ((a, b), (b, a)):
            cur.execute(
                """
                INSERT INTO interests (event_code, from_user_id, to_user_id, status)
                VALUES (%s, %s, %s, 'pending') ON CONFLICT DO NOTHING
                """,
                (event, src, dst),
            )
        conn.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def accept(requester: int, responder: int) -> None:
        barrier.wait()
        results.append(db.respond_to_request(event, requester, responder, accept=True))

    threads = [
        threading.Thread(target=accept, args=(a, b)),
        threading.Thread(target=accept, args=(b, a)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["already_matched", "matched"], results
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_concurrent_duplicate_requests_create_one_interest(event):
    """The same request fired from several threads at once."""
    import threading

    rng = random.Random(29)
    ids = seed_pool(event, 6, rng)
    a, b = ids[0], ids[1]

    barrier = threading.Barrier(4)
    results: list[str] = []

    def request() -> None:
        barrier.wait()
        results.append(db.request_match(event, a, b))

    threads = [threading.Thread(target=request) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("requested") == 1, results
    assert set(results) <= {"requested", "already_pending"}
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM interests WHERE event_code = %s AND from_user_id = %s AND to_user_id = %s",
            (event, a, b),
        )
        assert cur.fetchone()["n"] == 1


def test_many_concurrent_browses_share_the_pool_safely(event):
    """Twenty browsers at once must all succeed through the connection pool."""
    import threading

    rng = random.Random(31)
    ids = seed_pool(event, 120, rng)
    viewers = ids[:20]
    errors: list[Exception] = []

    def browse(uid: int) -> None:
        try:
            me = db.get_profile(uid, event)
            db.find_candidates(me, limit=26)
        except Exception as exc:                      # noqa: BLE001 - recorded and re-raised
            errors.append(exc)

    threads = [threading.Thread(target=browse, args=(uid,)) for uid in viewers]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    assert not errors, f"concurrent browsing failed: {errors[:3]}"
    assert elapsed < 20, f"20 concurrent browses took {elapsed:.1f}s"


def test_browsing_pages_through_a_pool_larger_than_one_page(event):
    """Skipping past the page size keeps yielding new, never-repeated candidates."""
    rng = random.Random(37)
    ids = seed_pool(event, 90, rng)
    me_id = ids[0]
    me = db.get_profile(me_id, event)

    seen: list[int] = []
    for _ in range(40):
        page = db.find_candidates(me, limit=26)
        if not page:
            break
        candidate = page[0].telegram_user_id
        assert candidate not in seen, "a skipped candidate came back"
        seen.append(candidate)
        db.record_skip(me_id, event, candidate)

    assert len(seen) > 25, f"paging stalled at the page boundary after {len(seen)}"
    assert len(set(seen)) == len(seen)

    # Everyone still eligible has now been seen, so the pool reports empty.
    assert db.find_candidates(me, limit=26) == [] or len(seen) == 40
