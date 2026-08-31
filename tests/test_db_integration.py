"""Integration tests against the real Postgres/Supabase database.

Each test runs inside a throwaway event that is deleted afterwards, so it never
touches real hackathon data. Skipped automatically when DATABASE_URL is absent.
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import requires_db  # noqa: E402

pytestmark = requires_db

import db  # noqa: E402
from matching import rank_candidates  # noqa: E402

ALICE, BOB, CAROL = 900_000_001, 900_000_002, 900_000_003


def _delete_event(event_code: str) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM matches   WHERE event_code = %s", (event_code,))
        cur.execute("DELETE FROM interests WHERE event_code = %s", (event_code,))
        cur.execute("DELETE FROM profiles  WHERE event_code = %s", (event_code,))
        cur.execute("DELETE FROM events    WHERE event_code = %s", (event_code,))
        conn.commit()


@pytest.fixture
def event():
    code, _ = db.create_event(f"pytest {uuid.uuid4().hex[:6]}", "test announcement", 1)
    try:
        yield code
    finally:
        _delete_event(code)


@pytest.fixture
def other_event():
    code, _ = db.create_event(f"pytest other {uuid.uuid4().hex[:6]}", None, 1)
    try:
        yield code
    finally:
        _delete_event(code)


def seed(event_code, user_id, *, username="tester", school="NUS", pref="none",
         offers=("software",), needs=("uiux",), open_to_any=False, status="looking"):
    db.save_profile(user_id, event_code, username, school, pref, "comp", status,
                    list(offers), list(needs), open_to_any)


# ------------------------------------------------------------------ profiles

def test_event_lookup_and_link(event):
    assert db.get_event(event) is not None
    assert db.get_event("definitely-not-an-event") is None


def test_created_event_codes_are_unique():
    a, _ = db.create_event("Same Name Hack")
    b, _ = db.create_event("Same Name Hack")
    try:
        assert a != b
    finally:
        _delete_event(a)
        _delete_event(b)


def test_save_profile_upserts_instead_of_duplicating(event):
    seed(event, ALICE, offers=("software",))
    seed(event, ALICE, offers=("ai_data", "uiux"), needs=("legal",), username="alice2")

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM profiles WHERE event_code = %s AND telegram_user_id = %s",
                    (event, ALICE))
        assert cur.fetchone()["n"] == 1

    profile = db.get_profile(ALICE, event)
    assert profile.telegram_username == "alice2"
    assert set(profile.skills_offered) == {"ai_data", "uiux"}
    assert set(profile.skills_needed) == {"legal"}
    assert profile.is_active is True


def test_empty_arrays_and_wildcard_round_trip(event):
    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking", ["software"], [], True)
    profile = db.get_profile(ALICE, event)
    assert profile.skills_needed == ()
    assert profile.open_to_any is True
    assert profile.needs == db.Profile("x", "y", None, "", "", "", "").needs  # wildcard = all skills


def test_missing_profile_returns_none(event):
    assert db.get_profile(123_456_789, event) is None


def test_pause_hides_profile_from_the_pool(event):
    seed(event, ALICE)
    seed(event, BOB)
    db.set_active(BOB, event, False)
    pool_ids = {p.telegram_user_id for p in db.get_event_pool(event)}
    assert pool_ids == {ALICE}


def test_events_are_isolated(event, other_event):
    seed(event, ALICE, offers=("uiux",), needs=("software",))
    seed(other_event, BOB, offers=("software",), needs=("uiux",))
    me = db.get_profile(ALICE, event)
    pool = db.get_event_pool(event)
    assert [p.telegram_user_id for p in pool] == [ALICE]
    assert rank_candidates(me, pool) == []


# ----------------------------------------------------------------- matching

def test_one_sided_interest_creates_no_match(event):
    seed(event, ALICE, offers=("software",), needs=("uiux",))
    seed(event, BOB, offers=("uiux",), needs=("software",))

    assert db.request_match(event, ALICE, BOB) == "requested"
    assert db.get_matches(ALICE, event) == []
    assert db.get_matches(BOB, event) == []
    assert [p.telegram_user_id for p in db.get_incoming_requests(BOB, event)] == [ALICE]
    assert [p.telegram_user_id for p in db.get_outgoing_requests(ALICE, event)] == [BOB]


def test_repeated_request_is_idempotent(event):
    seed(event, ALICE)
    seed(event, BOB, offers=("uiux",), needs=("software",))
    assert db.request_match(event, ALICE, BOB) == "requested"
    assert db.request_match(event, ALICE, BOB) == "already_pending"
    assert db.request_match(event, ALICE, BOB) == "already_pending"
    assert len(db.get_incoming_requests(BOB, event)) == 1


def test_accept_creates_exactly_one_match_and_reveals_username(event):
    seed(event, ALICE, username="alice")
    seed(event, BOB, username="bob", offers=("uiux",), needs=("software",))

    db.request_match(event, ALICE, BOB)
    assert db.respond_to_request(event, ALICE, BOB, accept=True) == "matched"

    alice_matches = db.get_matches(ALICE, event)
    bob_matches = db.get_matches(BOB, event)
    assert [p.telegram_username for p in alice_matches] == ["bob"]
    assert [p.telegram_username for p in bob_matches] == ["alice"]

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_second_accept_does_not_duplicate_the_match(event):
    seed(event, ALICE)
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.request_match(event, ALICE, BOB)
    assert db.respond_to_request(event, ALICE, BOB, accept=True) == "matched"
    assert db.respond_to_request(event, ALICE, BOB, accept=True) == "already_matched"

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_reciprocal_request_completes_the_match_once(event):
    """B never sees A's request but independently requests A."""
    seed(event, ALICE)
    seed(event, BOB, offers=("uiux",), needs=("software",))
    assert db.request_match(event, ALICE, BOB) == "requested"
    assert db.request_match(event, BOB, ALICE) == "matched"
    assert db.request_match(event, BOB, ALICE) == "already_matched"
    assert db.request_match(event, ALICE, BOB) == "already_matched"

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1
    assert len(db.get_matches(ALICE, event)) == 1
    assert len(db.get_matches(BOB, event)) == 1


def test_decline_blocks_the_match_and_is_idempotent(event):
    seed(event, ALICE)
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.request_match(event, ALICE, BOB)
    assert db.respond_to_request(event, ALICE, BOB, accept=False) == "declined"
    assert db.respond_to_request(event, ALICE, BOB, accept=False) == "already_declined"
    assert db.respond_to_request(event, ALICE, BOB, accept=True) == "already_declined"
    assert db.get_matches(ALICE, event) == []


def test_responding_to_a_request_that_does_not_exist(event):
    seed(event, ALICE)
    seed(event, BOB)
    assert db.respond_to_request(event, ALICE, BOB, accept=True) == "not_found"
    assert db.respond_to_request(event, ALICE, ALICE, accept=True) == "not_found"


def test_self_request_is_rejected(event):
    seed(event, ALICE)
    assert db.request_match(event, ALICE, ALICE) == "invalid"


def test_pending_request_event_lookup(event):
    seed(event, ALICE)
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.request_match(event, ALICE, BOB)
    assert db.find_pending_request_event(ALICE, BOB) == event
    assert db.find_pending_request_event(BOB, ALICE) is None


# ---------------------------------------------------------------- exclusions

def test_skipped_and_requested_people_are_not_shown_again(event):
    seed(event, ALICE, offers=("software",), needs=("uiux",))
    seed(event, BOB, offers=("uiux",), needs=("software",))
    seed(event, CAROL, offers=("uiux",), needs=("software",))

    me = db.get_profile(ALICE, event)
    pool = db.get_event_pool(event)
    assert len(rank_candidates(me, pool, db.get_interacted_user_ids(ALICE, event))) == 2

    db.record_skip(ALICE, event, BOB)
    remaining = rank_candidates(me, pool, db.get_interacted_user_ids(ALICE, event))
    assert [c.profile.telegram_user_id for c in remaining] == [CAROL]

    db.request_match(event, ALICE, CAROL)
    assert rank_candidates(me, pool, db.get_interacted_user_ids(ALICE, event)) == []


def test_incoming_request_hides_the_requester_from_browsing(event):
    seed(event, ALICE, offers=("software",), needs=("uiux",))
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.request_match(event, BOB, ALICE)
    me = db.get_profile(ALICE, event)
    ranked = rank_candidates(me, db.get_event_pool(event), db.get_interacted_user_ids(ALICE, event))
    assert ranked == []


def test_clearing_skips_brings_people_back(event):
    seed(event, ALICE, offers=("software",), needs=("uiux",))
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.record_skip(ALICE, event, BOB)
    assert db.get_skipped_user_ids(ALICE, event) == {BOB}
    assert db.clear_skips(ALICE, event) == 1
    assert db.get_skipped_user_ids(ALICE, event) == set()
    me = db.get_profile(ALICE, event)
    ranked = rank_candidates(me, db.get_event_pool(event), db.get_interacted_user_ids(ALICE, event))
    assert [c.profile.telegram_user_id for c in ranked] == [BOB]


def test_skip_is_one_directional(event):
    seed(event, ALICE, offers=("software",), needs=("uiux",))
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.record_skip(ALICE, event, BOB)
    bob = db.get_profile(BOB, event)
    ranked = rank_candidates(bob, db.get_event_pool(event), db.get_interacted_user_ids(BOB, event))
    assert [c.profile.telegram_user_id for c in ranked] == [ALICE]


def test_repeated_skip_does_not_error(event):
    seed(event, ALICE)
    seed(event, BOB)
    db.record_skip(ALICE, event, BOB)
    db.record_skip(ALICE, event, BOB)
    db.record_skip(ALICE, event, ALICE)   # self-skip is ignored
    assert db.get_skipped_user_ids(ALICE, event) == {BOB}


def test_request_after_skip_still_works(event):
    seed(event, ALICE)
    seed(event, BOB, offers=("uiux",), needs=("software",))
    db.record_skip(ALICE, event, BOB)
    assert db.request_match(event, ALICE, BOB) == "requested"
    assert [p.telegram_user_id for p in db.get_incoming_requests(BOB, event)] == [ALICE]


def test_editing_a_profile_keeps_matches(event):
    seed(event, ALICE, username="alice")
    seed(event, BOB, username="bob", offers=("uiux",), needs=("software",))
    db.request_match(event, ALICE, BOB)
    db.respond_to_request(event, ALICE, BOB, accept=True)
    seed(event, ALICE, username="alice", school="NTU", offers=("legal",), needs=("ai_data",))
    assert [p.telegram_username for p in db.get_matches(ALICE, event)] == ["bob"]


def test_username_refresh_updates_every_event(event, other_event):
    seed(event, ALICE, username="old")
    seed(other_event, ALICE, username="old")
    db.update_username(ALICE, "renamed")
    assert db.get_profile(ALICE, event).telegram_username == "renamed"
    assert db.get_profile(ALICE, other_event).telegram_username == "renamed"
    db.update_username(ALICE, None)
    assert db.get_profile(ALICE, event).telegram_username is None


# ------------------------------------------------------------- profile note

def test_note_round_trips(event):
    note = "Interested in healthcare tracks. Want to ship something pilotable."
    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking",
                    ["software"], ["uiux"], False, note)
    assert db.get_profile(ALICE, event).note == note


def test_note_defaults_to_null(event):
    seed(event, ALICE)
    assert db.get_profile(ALICE, event).note is None


def test_note_is_updated_by_upsert(event):
    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking",
                    ["software"], ["uiux"], False, "first note")
    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking",
                    ["software"], ["uiux"], False, "second note")
    assert db.get_profile(ALICE, event).note == "second note"

    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking",
                    ["software"], ["uiux"], False, None)
    assert db.get_profile(ALICE, event).note is None

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM profiles WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_blank_note_is_stored_as_null(event):
    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking",
                    ["software"], ["uiux"], False, "   ")
    assert db.get_profile(ALICE, event).note is None


def test_note_whitespace_is_collapsed(event):
    db.save_profile(ALICE, event, "alice", "NUS", "none", "comp", "looking",
                    ["software"], ["uiux"], False, "  lots   of\n\nspace  ")
    assert db.get_profile(ALICE, event).note == "lots of space"


def test_database_rejects_an_over_long_note(event):
    """The app validates at 160; the column constraint is the backstop."""
    seed(event, ALICE)
    with db.get_connection() as conn, conn.cursor() as cur:
        with pytest.raises(Exception):
            cur.execute("UPDATE profiles SET note = %s WHERE telegram_user_id = %s AND event_code = %s",
                        ("x" * 161, ALICE, event))
            conn.commit()
        conn.rollback()


def test_note_does_not_affect_candidate_selection(event):
    seed(event, ALICE, offers=("software",), needs=("uiux",))
    db.save_profile(BOB, event, "bob", "NUS", "none", "comp", "looking",
                    ["uiux"], ["software"], False, "a note that mentions legal and hardware")
    me = db.get_profile(ALICE, event)
    ids = [p.telegram_user_id for p in db.find_candidates(me)]
    assert ids == [BOB]
