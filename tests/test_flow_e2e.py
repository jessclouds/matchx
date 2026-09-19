"""End-to-end walkthroughs driving the real handlers from main.build_application()."""

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import DATABASE_AVAILABLE  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (DATABASE_AVAILABLE and os.getenv("BOT_TOKEN")),
    reason="needs a reachable database and BOT_TOKEN",
)

import db  # noqa: E402
import main  # noqa: E402
from fake_telegram import Session, World  # noqa: E402

ALICE, BOB, CAROL, DAVE, ORGANISER = (910_000_001, 910_000_002, 910_000_003, 910_000_004, 910_000_009)


def _purge_event(code: str) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM matches   WHERE event_code = %s", (code,))
        cur.execute("DELETE FROM interests WHERE event_code = %s", (code,))
        cur.execute("DELETE FROM profiles  WHERE event_code = %s", (code,))
        cur.execute("DELETE FROM events    WHERE event_code = %s", (code,))
        conn.commit()


@pytest.fixture(scope="module")
def application():
    return main.build_application()


@pytest.fixture
def world(application):
    created: list[str] = []
    w = World(application)
    w._created_events = created
    yield w
    for code in created:
        _purge_event(code)
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT event_code FROM events WHERE organiser_telegram_id = %s", (ORGANISER,))
        leftovers = [r["event_code"] for r in cur.fetchall()]
    for code in leftovers:
        _purge_event(code)


@pytest.fixture
def event(world):
    code, _ = db.create_event(f"E2E Hack {uuid.uuid4().hex[:6]}", "announcement", ORGANISER)
    world._created_events.append(code)
    return code


def _shown_candidate_id(world: World, event_code: str) -> int:
    """The candidate the bot currently has on screen, from its own browsing state."""
    cursor = world.user_data[ALICE]["cursor"][event_code]
    return world.user_data[ALICE]["history"][event_code][cursor]


def onboard(session: Session, event_code: str, *, school="NUS", pref="No preference",
            discipline="Computing", status="Solo", offers=("Software",), needs=("UI / UX",),
            note=None):
    """Walk one user through the whole questionnaire by tapping real buttons."""
    session.command("start", event_code)
    session.tap(school)
    session.tap(pref)
    session.tap(discipline)
    session.tap(status)
    for skill in offers:
        session.tap(skill)
    session.tap("Done")
    if needs == "any":
        session.tap("No preference — open to anyone")
    else:
        for skill in needs:
            session.tap(skill)
        session.tap("Done")
    # Final, optional step: the profile note.
    if note is None:
        session.tap("Skip")
    else:
        session.tap("Add note")
        session.say(note)


# ------------------------------------------------------------------- entry

def test_start_without_event_code_for_a_new_user(world):
    alice = Session(world, ALICE, "alice")
    alice.command("start")
    assert "not signed up" in alice.last.text


def test_start_with_unknown_event_code(world):
    alice = Session(world, ALICE, "alice")
    alice.command("start", "no-such-event-2026")
    assert "don't recognise" in alice.last.text


def test_start_with_malformed_event_code(world):
    alice = Session(world, ALICE, "alice")
    alice.command("start", "!!!")
    assert "malformed" in alice.last.text.lower()


def test_user_without_username_is_blocked_before_onboarding(world, event):
    ghost = Session(world, DAVE, None)
    ghost.command("start", event)
    assert "username" in ghost.last.text.lower()
    assert not ghost.has_button("NUS"), "onboarding must not start without a username"
    assert db.get_profile(DAVE, event) is None


# -------------------------------------------------------------- onboarding

def test_onboarding_persists_the_profile(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, school="NTU", pref="Prefer my school", discipline="Design",
            status="Have a team", offers=("Software", "AI / Data"), needs=("UI / UX",))

    profile = db.get_profile(ALICE, event)
    assert profile is not None
    assert profile.telegram_username == "alice"
    assert profile.event_code == event
    assert profile.school == "NTU"
    assert profile.school_preference == "same"
    assert profile.discipline == "design"
    assert profile.team_status == "has_team"
    assert set(profile.skills_offered) == {"software", "ai_data"}
    assert set(profile.skills_needed) == {"uiux"}
    assert profile.open_to_any is False
    assert "Profile saved" in alice.all_text()


def test_skill_cap_is_enforced(world, event):
    alice = Session(world, ALICE, "alice")
    alice.command("start", event)
    alice.tap("NUS"); alice.tap("No preference"); alice.tap("Computing"); alice.tap("Solo")
    for skill in ("Software", "AI / Data", "Healthcare"):
        alice.tap(skill)
    query = alice.tap("User Research")
    assert query.answers and "at most 3" in query.answers[-1][0]
    msg, button = alice.find_button("Software")
    assert button.text.startswith("✓"), "selected skills must stay marked"


def test_done_with_no_offered_skills_is_refused(world, event):
    alice = Session(world, ALICE, "alice")
    alice.command("start", event)
    alice.tap("NUS"); alice.tap("No preference"); alice.tap("Computing"); alice.tap("Solo")
    query = alice.tap("Done")
    assert "at least one" in query.answers[-1][0]
    assert db.get_profile(ALICE, event) is None


def test_wildcard_needs_saves_open_to_any(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, needs="any")
    profile = db.get_profile(ALICE, event)
    assert profile.open_to_any is True
    assert profile.skills_needed == ()


def test_repeat_start_does_not_duplicate_or_restart(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    alice.clear()
    alice.command("start", event)
    alice.command("start", event)
    assert "Welcome back" in alice.all_text()
    assert alice.has_button("Find teammates")
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM profiles WHERE event_code = %s AND telegram_user_id = %s",
                    (event, ALICE))
        assert cur.fetchone()["n"] == 1


def test_abandoned_onboarding_then_return(world, event):
    alice = Session(world, ALICE, "alice")
    alice.command("start", event)
    alice.tap("NUS")
    assert db.get_profile(ALICE, event) is None      # nothing saved half-way
    alice.say("hello?")                              # free text mid-flow
    assert "button" in alice.last.text.lower()
    alice.command("restart")
    onboard_rest = alice
    onboard_rest.tap("SMU"); onboard_rest.tap("No preference"); onboard_rest.tap("Computing")
    onboard_rest.tap("Solo"); onboard_rest.tap("Software"); onboard_rest.tap("Done")
    onboard_rest.tap("UI / UX"); onboard_rest.tap("Done")
    onboard_rest.tap("Skip")                       # optional note
    assert db.get_profile(ALICE, event).school == "SMU"


# ---------------------------------------------------------------- browsing

def test_first_participant_sees_an_empty_state(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    assert "first" in alice.last.text.lower() or "check back" in alice.last.text.lower()
    assert alice.has_button("Menu")


def test_candidate_card_hides_identity(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    alice.clear()
    alice.command("find")
    card = alice.last.text
    assert "Potential teammate" in card
    assert "bob" not in card.lower(), "usernames must stay hidden before a mutual yes"
    assert "Offers" in card and "Matches your needs" in card
    assert alice.has_button("Request Match") and alice.has_button("Next")


def test_incompatible_users_are_not_recommended(world, event):
    alice = Session(world, ALICE, "alice")
    carol = Session(world, CAROL, "carol")
    onboard(alice, event, offers=("Software",), needs=("Legal",))
    onboard(carol, event, offers=("Marketing",), needs=("Software",))
    alice.clear()
    alice.command("find")
    assert "Potential teammate" not in alice.last.text
    assert "match" in alice.last.text.lower() or "check back" in alice.last.text.lower()


def test_next_moves_on_and_skips_can_be_undone(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    alice.clear()
    alice.command("find")
    alice.tap("Next")
    assert db.get_skipped_user_ids(ALICE, event) == {BOB}
    assert "Potential teammate" not in alice.last.text
    assert alice.has_button("skipped")
    alice.tap("Review people I skipped")
    assert "Potential teammate" in alice.last.text


# ------------------------------------------------------------ back navigation

def _three_candidates(world, event):
    """Alice plus three teammates she is eligible to see."""
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    for uid, username in ((BOB, "bob"), (CAROL, "carol"), (DAVE, "dave")):
        db.save_profile(uid, event, username, "NUS", "none", "design", "looking",
                        ["uiux"], ["software"], False)
    alice.clear()
    return alice


def test_back_is_hidden_until_there_is_history(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    assert "Potential teammate" in alice.last.text
    assert alice.has_button("Request Match") and alice.has_button("Next")
    assert not alice.has_button("Back"), "nothing to go back to on the first card"


def test_next_then_back_returns_to_the_previous_candidate(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    first = _shown_candidate_id(world, event)

    alice.tap("Next")
    second = _shown_candidate_id(world, event)
    assert second != first
    assert alice.has_button("Back")

    alice.tap("Back")
    assert _shown_candidate_id(world, event) == first
    assert "seen earlier" in alice.last.text
    assert "Potential teammate" in alice.last.text


def test_multiple_back_and_next_steps(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    first = _shown_candidate_id(world, event)
    alice.tap("Next")
    second = _shown_candidate_id(world, event)
    alice.tap("Next")
    third = _shown_candidate_id(world, event)
    assert len({first, second, third}) == 3

    alice.tap("Back")
    assert _shown_candidate_id(world, event) == second
    alice.tap("Back")
    assert _shown_candidate_id(world, event) == first
    assert not alice.has_button("Back"), "Back must disappear at the start of history"

    alice.tap("Next")            # forward again from the oldest card
    assert "Potential teammate" in alice.last.text


def test_back_does_not_undo_a_sent_request(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    target = _shown_candidate_id(world, event)
    alice.tap("Request Match")
    assert [p.telegram_user_id for p in db.get_outgoing_requests(ALICE, event)] == [target]

    alice.tap("Back")
    assert _shown_candidate_id(world, event) == target
    assert "already sent them a request" in alice.last.text
    assert [p.telegram_user_id for p in db.get_outgoing_requests(ALICE, event)] == [target]


def test_back_does_not_undo_a_confirmed_match(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))
    db.save_profile(CAROL, event, "carol", "NUS", "none", "design", "looking",
                    ["uiux"], ["software"], False)

    alice.clear()
    alice.command("find")
    while _shown_candidate_id(world, event) != BOB:
        alice.tap("Next")
    alice.tap("Request Match")
    bob.tap("Accept")
    assert len(db.get_matches(ALICE, event)) == 1

    alice.tap("Back") if alice.has_button("Back") else None
    assert len(db.get_matches(ALICE, event)) == 1
    assert [p.telegram_user_id for p in db.get_matches(ALICE, event)] == [BOB]


def test_can_request_someone_after_going_back_to_them(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    skipped = _shown_candidate_id(world, event)
    alice.tap("Next")                       # skipped is now recorded as skipped
    assert skipped in db.get_skipped_user_ids(ALICE, event)

    alice.tap("Back")
    assert _shown_candidate_id(world, event) == skipped
    assert "skipped this one earlier" in alice.last.text
    alice.tap("Request Match")
    assert [p.telegram_user_id for p in db.get_outgoing_requests(ALICE, event)] == [skipped]


def test_repeated_back_and_request_taps_create_no_duplicates(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    target = _shown_candidate_id(world, event)

    msg, button = alice.find_button("Request Match")
    alice.tap_data(button.callback_data, msg)
    alice.tap_data(button.callback_data, msg)      # same stale button, twice more
    alice.tap_data(button.callback_data, msg)

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM interests WHERE event_code = %s AND from_user_id = %s",
                    (event, ALICE))
        assert cur.fetchone()["n"] == 1
    assert [p.telegram_user_id for p in db.get_outgoing_requests(ALICE, event)] == [target]

    back_msg, back_button = alice.find_button("Back") if alice.has_button("Back") else (None, None)
    if back_button:
        alice.tap_data(back_button.callback_data, back_msg)
        alice.tap_data(back_button.callback_data, back_msg)
    assert len(db.get_matches(ALICE, event)) == 0


def test_back_at_the_end_of_the_pool(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    for _ in range(3):
        alice.tap("Next")
    assert "Potential teammate" not in alice.last.text        # end-of-pool message
    assert alice.has_button("Back"), "the end of the pool must not be a dead end"

    alice.tap("Back")
    assert "Potential teammate" in alice.last.text
    assert "seen earlier" in alice.last.text


def test_back_history_is_scoped_to_the_user_and_event(world, event):
    other_code, _ = db.create_event(f"E2E Back {uuid.uuid4().hex[:6]}", None, ORGANISER)
    world._created_events.append(other_code)

    alice = _three_candidates(world, event)
    alice.command("find")
    alice.tap("Next")
    assert alice.has_button("Back")

    # Same person, different hackathon: history starts empty again.
    onboard(alice, other_code, offers=("Software",), needs=("UI / UX",))
    db.save_profile(BOB, other_code, "bob", "NUS", "none", "design", "looking",
                    ["uiux"], ["software"], False)
    alice.clear()
    alice.command("find")
    assert "Potential teammate" in alice.last.text
    assert not alice.has_button("Back"), "history must not leak between events"

    # And another user in the first event has their own empty history.
    bob = Session(world, BOB, "bob")
    bob.clear()
    bob.command("find")
    assert not bob.has_button("Back")


def test_back_skips_people_who_left_the_pool(world, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    first = _shown_candidate_id(world, event)
    alice.tap("Next")
    db.set_active(first, event, False)           # they paused after being seen

    alice.tap("Back")
    shown = _shown_candidate_id(world, event)
    assert shown != first, "a paused teammate must not be re-shown"


# ------------------------------------------------------------ mutual match

def test_request_then_accept_reveals_usernames_to_both(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    alice.clear(); bob.clear()
    alice.command("find")
    alice.tap("Request Match")

    # One-sided interest: no match yet, and no contact details anywhere.
    assert db.get_matches(ALICE, event) == []
    assert "Request sent" in alice.all_text()
    assert "@bob" not in alice.all_text()
    assert "wants to team up" in bob.all_text()
    assert "@alice" not in bob.all_text()
    assert bob.has_button("Accept") and bob.has_button("Decline")

    bob.tap("Accept")
    assert "It's a match" in bob.all_text() and "@alice" in bob.all_text()
    assert "It's a match" in alice.all_text() and "@bob" in alice.all_text()

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_double_tapping_accept_is_safe(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))
    alice.command("find")
    alice.tap("Request Match")

    msg, button = bob.find_button("Accept")
    bob.tap_data(button.callback_data, msg)
    alice_notifications = len([t for t in alice.texts() if "It's a match" in t])
    bob.tap_data(button.callback_data, msg)          # stale button, pressed again
    assert len([t for t in alice.texts() if "It's a match" in t]) == alice_notifications == 1

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_decline_keeps_the_requester_anonymous(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))
    alice.command("find")
    alice.tap("Request Match")
    alice.clear()
    bob.tap("Decline")
    assert "Declined" in bob.last.text
    assert db.get_matches(ALICE, event) == []
    assert alice.texts() == [], "the requester is never told they were declined"


def test_reciprocal_requests_match_without_anyone_accepting(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    alice.command("find")
    alice.tap("Request Match")
    bob.clear()
    bob.command("find")          # Bob has a pending request from Alice, so she is hidden…
    assert "Potential teammate" not in bob.last.text
    # …but he can answer the card she pushed to him.
    assert db.request_match(event, BOB, ALICE) == "matched"
    assert len(db.get_matches(BOB, event)) == 1


def test_matches_screen_lists_contacts_and_pending(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    carol = Session(world, CAROL, "carol")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))
    onboard(carol, event, offers=("UI / UX",), needs=("Software",))

    alice.command("find")
    alice.tap("Request Match")
    other = bob if bob.inbox and "wants to team up" in bob.all_text() else carol
    other.tap("Accept")

    alice.clear()
    alice.command("matches")
    assert "Your matches (1)" in alice.all_text()
    assert f"@{other.user.username}" in alice.all_text()


def test_no_matches_screen(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    alice.clear()
    alice.command("matches")
    assert "no matches yet" in alice.last.text.lower()
    assert alice.has_button("Find teammates")


# ----------------------------------------------------- existing-user journey

def test_profile_view_edit_and_pause(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, school="NUS")

    alice.clear()
    alice.command("profile")
    assert "Your profile" in alice.last.text
    assert alice.has_button("Edit profile")

    alice.tap("Edit profile")
    alice.tap("School")
    alice.tap("SUTD")
    assert db.get_profile(ALICE, event).school == "SUTD"
    assert "Profile saved" in alice.all_text()

    alice.command("profile")
    alice.tap("Pause matchmaking")
    assert db.get_profile(ALICE, event).is_active is False
    alice.clear()
    alice.command("find")
    assert "paused" in alice.last.text.lower()
    alice.tap("Resume matchmaking")
    assert db.get_profile(ALICE, event).is_active is True


def test_editing_skills_keeps_other_fields(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, school="NTU", pref="Prefer my school", offers=("Software",), needs=("UI / UX",))
    alice.command("profile")
    alice.tap("Edit profile")
    alice.tap("Skills I need")
    alice.tap("Legal")
    alice.tap("Done")
    profile = db.get_profile(ALICE, event)
    assert set(profile.skills_needed) == {"uiux", "legal"}
    assert profile.school == "NTU" and profile.school_preference == "same"


def test_paused_user_is_hidden_from_others(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))
    db.set_active(BOB, event, False)
    alice.clear()
    alice.command("find")
    assert "Potential teammate" not in alice.last.text


def test_help_and_menu_navigation(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    alice.clear()
    alice.command("help")
    assert "How it works" in alice.last.text
    alice.tap("Menu")
    assert alice.has_button("Find teammates") and alice.has_button("My profile")
    alice.tap("My profile")
    assert "Your profile" in alice.last.text


def test_events_are_isolated_for_the_same_person(world, event):
    other_code, _ = db.create_event(f"E2E Other {uuid.uuid4().hex[:6]}", None, ORGANISER)
    world._created_events.append(other_code)

    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, other_code, offers=("UI / UX",), needs=("Software",))

    alice.clear()
    alice.command("find")
    assert "Potential teammate" not in alice.last.text, "candidates must not cross events"

    # Alice joins the second hackathon too — separate profile, same person.
    onboard(alice, other_code, offers=("Software",), needs=("UI / UX",))
    assert db.get_profile(ALICE, event) is not None
    assert db.get_profile(ALICE, other_code) is not None
    alice.clear()
    alice.command("find")
    assert "Potential teammate" in alice.last.text


def test_stale_callback_data_does_not_crash(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    alice.tap_data("browse:req:99999999")      # profile that does not exist
    alice.tap_data("browse:skip:notanumber")   # malformed payload
    alice.tap_data("resp:yes:99999999")        # request that was never made
    assert "Traceback" not in alice.all_text()


# ------------------------------------------------------------- organiser

def _event_code_from(text: str) -> str:
    return text.split("?start=")[1].split()[0].strip()


ANNOUNCEMENT = (
    "IDEATE 2026 🚀\n"
    "12–14 September, NUS Enterprise\n"
    "Build health-tech in 48 hours. $5k prize pool & <mentors> from A*STAR.\n"
    "Sign up: example.com/ideate"
)


def test_newevent_asks_for_the_name_first(world):
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("newevent")
    assert "called" in organiser.last.text.lower()
    assert "announcement" not in organiser.last.text.lower()

    organiser.say("IDEATE 2026")
    assert "announcement" in organiser.last.text.lower()
    assert "IDEATE 2026" in organiser.last.text

    organiser.clear()
    organiser.say(ANNOUNCEMENT)
    code = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(code)
    assert db.get_event(code) == "IDEATE 2026"


def test_entered_name_wins_over_the_announcement_text(world):
    """The name the organiser typed is authoritative — never inferred from the text."""
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("newevent")
    organiser.say("HealthHack Singapore 2026")
    organiser.clear()
    organiser.say("SOME OTHER TITLE IN THE POSTER\nregister at example.com")

    code = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(code)
    assert db.get_event(code) == "HealthHack Singapore 2026"

    # And a participant is told which pool they are joining.
    alice = Session(world, ALICE, "alice")
    alice.command("start", code)
    assert "teammate-matching pool for" in alice.inbox[0].text
    assert "HealthHack Singapore 2026" in alice.inbox[0].text
    assert alice.has_button("NUS"), "onboarding still starts right after"


def test_participant_never_types_an_event_code(world, event):
    """Everything comes from the deep link; there is no code to enter or pick."""
    alice = Session(world, ALICE, "alice")
    alice.command("start", event)
    joined = alice.inbox[0].text
    assert "teammate-matching pool for" in joined
    assert not any("code" in b.text.lower() for m in alice.inbox for b in m.buttons())
    onboard_taps = ("NUS", "No preference", "Computing", "Solo")
    for tap in onboard_taps:
        alice.tap(tap)
    alice.tap("Software"); alice.tap("Done"); alice.tap("UI / UX"); alice.tap("Done")
    alice.tap("Skip")                              # optional note
    assert db.get_profile(ALICE, event).event_code == event


def test_newevent_returns_the_announcement_unchanged_with_the_cta(world):
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("newevent")
    organiser.say("IDEATE 2026 🚀")

    organiser.clear()
    organiser.say(ANNOUNCEMENT)

    post = organiser.inbox[0].text          # the ready-to-post message
    plain = post.replace("<b>", "").replace("</b>", "")
    plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

    # The organiser's own words come back untouched, in order, at the top.
    assert plain.startswith(ANNOUNCEMENT), plain
    for line in ANNOUNCEMENT.splitlines():
        assert line in plain

    # …followed by exactly the MatchX call to action.
    assert "🤝 Looking for teammates?" in plain
    assert "complementary skills" in plain
    assert "Find teammates: https://t.me/" in plain
    assert "?start=" in plain

    # Nothing operational is mixed into the copy-paste message.
    for noise in ("copy the message above", "Event code:", "/myevents"):
        assert noise not in post
    assert "Event code:" in organiser.inbox[-1].text     # it lives in the follow-up

    code = _event_code_from(post)
    world._created_events.append(code)
    assert db.get_event(code) == "IDEATE 2026 🚀"      # the name the organiser typed


def test_link_from_newevent_leads_into_that_events_pool_only(world):
    organiser = Session(world, ORGANISER, "organiser")

    organiser.command("newevent")
    organiser.say("Alpha Hack")
    organiser.clear()
    organiser.say("Alpha Hack\nFirst event")
    code_a = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(code_a)

    organiser.command("newevent")
    organiser.say("Beta Hack")
    organiser.clear()
    organiser.say("Beta Hack\nSecond event")
    code_b = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(code_b)

    assert code_a != code_b

    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, code_a, offers=("Software",), needs=("UI / UX",))
    onboard(bob, code_b, offers=("UI / UX",), needs=("Software",))

    assert db.get_profile(ALICE, code_a) is not None
    assert db.get_profile(ALICE, code_b) is None
    assert [p.telegram_user_id for p in db.get_event_pool(code_a)] == [ALICE]
    assert [p.telegram_user_id for p in db.get_event_pool(code_b)] == [BOB]

    alice.clear()
    alice.command("find")
    assert "Potential teammate" not in alice.last.text


def test_newevent_state_is_per_organiser(world):
    """One organiser mid-flow must not swallow anyone else's messages."""
    organiser = Session(world, ORGANISER, "organiser")
    bystander = Session(world, CAROL, "carol")

    organiser.command("newevent")
    organiser.say("Gamma Hack")

    # The bystander types during both of the organiser's steps.
    bystander.say("just chatting, definitely not an announcement")
    assert "?start=" not in bystander.all_text()

    organiser.clear()
    organiser.say("Gamma Hack\nreal announcement")
    assert "?start=" in organiser.inbox[0].text
    world._created_events.append(_event_code_from(organiser.inbox[0].text))

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM events WHERE organiser_telegram_id = %s", (CAROL,))
        assert cur.fetchone()["n"] == 0


def test_second_message_after_newevent_is_not_another_event(world):
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("newevent")
    organiser.say("Delta Hack")
    organiser.clear()
    organiser.say("Delta Hack\nannouncement")
    world._created_events.append(_event_code_from(organiser.inbox[0].text))
    organiser.clear()
    organiser.say("oh and bring your laptop")
    assert "?start=" not in organiser.all_text()


def test_organiser_flow_end_to_end_with_myevents(world):
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("newevent")
    organiser.say("Quantum Hack 2026")
    organiser.clear()
    organiser.say("Quantum Hack 2026\nJoin us this weekend at NUS!")
    code = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(code)

    alice = Session(world, ALICE, "alice")
    onboard(alice, code)
    assert db.get_profile(ALICE, code) is not None

    organiser.clear()
    organiser.command("myevents")
    assert "Quantum Hack 2026" in organiser.last.text and "1 joined" in organiser.last.text


def test_newevent_can_be_cancelled_at_either_step(world):
    organiser = Session(world, ORGANISER, "organiser")

    organiser.command("newevent")            # cancel before naming it
    organiser.command("cancel")
    organiser.say("this should not become an event")
    assert "?start=" not in organiser.all_text()

    organiser.clear()
    organiser.command("newevent")            # cancel after naming it
    organiser.say("Abandoned Hack")
    organiser.command("cancel")
    organiser.say("this announcement should go nowhere")
    assert "?start=" not in organiser.all_text()

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM events WHERE name = %s", ("Abandoned Hack",))
        assert cur.fetchone()["n"] == 0


# ------------------------------------------------------------ failure modes

def test_blocked_recipient_does_not_break_the_requester(world, event, monkeypatch):
    """The other user blocked the bot: the request is still recorded, no crash."""
    from telegram.error import Forbidden

    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    async def blocked(*args, **kwargs):
        raise Forbidden("bot was blocked by the user")

    monkeypatch.setattr(world.bot, "send_message", blocked)

    alice.clear()
    alice.command("find")
    alice.tap("Request Match")
    assert "Request sent" in alice.all_text()
    assert [p.telegram_user_id for p in db.get_incoming_requests(BOB, event)] == [ALICE]


def test_database_outage_shows_a_friendly_message(world, event, monkeypatch):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)

    def boom(*args, **kwargs):
        raise db.DatabaseError("connection refused")

    monkeypatch.setattr(db, "get_profile", boom)
    monkeypatch.setattr(db, "get_latest_profile", boom)

    alice.clear()
    alice.command("matches")
    assert "database" in alice.last.text.lower()
    assert "connection refused" not in alice.all_text(), "internal errors must not leak"
    assert alice.has_button("Menu")


def test_database_outage_during_a_button_tap_only_alerts(world, event, monkeypatch):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    alice.command("profile")

    def boom(*args, **kwargs):
        raise db.DatabaseError("connection refused")

    monkeypatch.setattr(db, "get_profile", boom)
    monkeypatch.setattr(db, "get_latest_profile", boom)

    alice.tap("Find teammates")
    assert "database" in alice.last.text.lower(), alice.debug()
    assert "connection refused" not in alice.all_text()
    assert alice.has_button("Menu")


# ---------------------------------------------------- closed events & limits

def test_closed_event_refuses_new_participants(world, event):
    db.set_event_active(event, False)
    try:
        alice = Session(world, ALICE, "alice")
        alice.command("start", event)
        assert "closed" in alice.last.text.lower()
        assert not alice.has_button("NUS"), "onboarding must not start for a closed event"
        assert db.get_profile(ALICE, event) is None
    finally:
        db.set_event_active(event, True)


def test_closed_event_tells_existing_participants(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)
    db.set_event_active(event, False)
    try:
        alice.clear()
        alice.command("find")
        assert "closed" in alice.last.text.lower()
        assert "/matches" in alice.last.text
        assert alice.has_button("Menu")
    finally:
        db.set_event_active(event, True)


def test_callback_spam_is_rate_limited(world, event, monkeypatch):
    """A burst of taps from one user is throttled, without corrupting anything."""
    import main

    monkeypatch.setattr(main, "_MAX_ACTIONS_PER_WINDOW", 3)

    alice = _three_candidates(world, event)
    alice.command("find")
    msg, button = alice.find_button("Next")

    alerts = []
    for _ in range(6):
        query = alice.tap_data(button.callback_data, msg)
        alerts.extend(text for text, alert in query.answers if alert and text)

    assert any("Slow down" in text for text in alerts), "callback spam should be throttled"
    assert db.get_profile(ALICE, event) is not None, "throttling must not corrupt state"


def test_request_flood_is_rate_limited(world, event, monkeypatch):
    import main

    monkeypatch.setattr(main, "_MAX_REQUESTS_PER_WINDOW", 1)

    alice = _three_candidates(world, event)
    alice.command("find")
    msg, button = alice.find_button("Request Match")

    alerts = []
    for _ in range(3):
        query = alice.tap_data(button.callback_data, msg)
        alerts.extend(text for text, alert in query.answers if alert and text)

    assert any("lot of requests" in text for text in alerts)


def test_rate_limit_window_counts_only_recent_actions():
    """Unit test for the guard itself, independent of any Telegram or DB timing."""
    import main

    main._recent_actions.clear()
    user = 12345
    assert not any(main._too_many(user, 3) for _ in range(3))
    assert main._too_many(user, 3), "the fourth action in the window is throttled"

    # Actions older than the window are forgotten.
    main._recent_actions[user] = [main.time.monotonic() - main._ACTION_WINDOW_SECONDS - 1] * 10
    assert not main._too_many(user, 3)
    main._recent_actions.clear()


# ------------------------------------------------------------- restart safety

def test_everything_survives_a_bot_restart(world, application, event):
    """A restart wipes in-memory state; profiles, requests and matches must remain."""
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))
    alice.command("find")
    alice.tap("Request Match")
    assert [p.telegram_user_id for p in db.get_incoming_requests(BOB, event)] == [ALICE]

    # A brand-new World has empty user_data and bot_data — exactly like a restarted bot.
    restarted = World(application)
    alice2 = Session(restarted, ALICE, "alice")
    bob2 = Session(restarted, BOB, "bob")

    alice2.command("start")                       # no deep link, no remembered state
    assert "Welcome back" in alice2.all_text(), alice2.debug()

    bob2.command("matches")                       # the pending request is still there
    assert "wants to team up" in bob2.all_text()
    bob2.tap("Accept")

    assert [p.telegram_username for p in db.get_matches(ALICE, event)] == ["bob"]
    assert [p.telegram_username for p in db.get_matches(BOB, event)] == ["alice"]
    assert "It's a match" in alice2.all_text(), "the requester is notified after a restart"

    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event,))
        assert cur.fetchone()["n"] == 1


def test_browsing_position_resets_harmlessly_after_restart(world, application, event):
    alice = _three_candidates(world, event)
    alice.command("find")
    alice.tap("Next")
    assert alice.has_button("Back")

    restarted = World(application)
    alice2 = Session(restarted, ALICE, "alice")
    alice2.command("find")
    assert "Potential teammate" in alice2.last.text
    assert not alice2.has_button("Back"), "history is navigation state; it may reset"
    # The durable part — who was skipped — is unaffected.
    assert db.get_skipped_user_ids(ALICE, event)


# ------------------------------------------------------------- profile note

NOTE = "Interested in mental health / healthcare tracks. Hoping to build something we can actually pilot."


def test_note_step_is_offered_after_skills(world, event):
    alice = Session(world, ALICE, "alice")
    alice.command("start", event)
    alice.tap("NUS"); alice.tap("No preference"); alice.tap("Computing"); alice.tap("Solo")
    alice.tap("Software"); alice.tap("Done")
    alice.tap("UI / UX"); alice.tap("Done")

    assert "short note" in alice.last.text
    assert alice.has_button("Add note") and alice.has_button("Skip")
    assert db.get_profile(ALICE, event) is None, "nothing is saved until the note step is answered"


def test_add_note_saves_it_on_the_profile(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, note=NOTE)

    profile = db.get_profile(ALICE, event)
    assert profile is not None
    assert profile.note == NOTE
    assert "Profile saved" in alice.all_text()
    assert NOTE in alice.all_text(), "the note appears on the user's own profile"


def test_own_profile_shows_the_note_without_the_candidate_label(world, event):
    """"User note:" labels someone else's words; on your own profile it is yours."""
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, note=NOTE)
    alice.clear()
    alice.command("profile")
    assert NOTE in alice.last.text
    assert "User note:" not in alice.last.text


def test_skip_leaves_the_profile_without_a_note(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)                       # default: taps Skip
    profile = db.get_profile(ALICE, event)
    assert profile is not None
    assert profile.note is None
    assert "Profile saved" in alice.all_text()


def test_note_longer_than_160_characters_is_rejected_then_accepted(world, event):
    alice = Session(world, ALICE, "alice")
    alice.command("start", event)
    alice.tap("NUS"); alice.tap("No preference"); alice.tap("Computing"); alice.tap("Solo")
    alice.tap("Software"); alice.tap("Done")
    alice.tap("UI / UX"); alice.tap("Done")
    alice.tap("Add note")

    too_long = "x" * 161
    alice.say(too_long)
    assert "161 characters" in alice.last.text and "160" in alice.last.text
    assert db.get_profile(ALICE, event) is None, "an over-long note must not save the profile"
    assert alice.has_button("Skip"), "the user can still back out"

    alice.say("y" * 160)                        # exactly at the limit
    profile = db.get_profile(ALICE, event)
    assert profile is not None and profile.note == "y" * 160


def test_note_appears_at_the_bottom_of_a_candidate_card(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",), note=NOTE)

    alice.clear()
    alice.command("find")
    card = alice.last.text
    assert NOTE in card
    assert f"User note: {NOTE}" in card, "the note must be labelled, not bare text"
    assert card.rstrip().endswith(NOTE), "the note sits at the bottom of the card"
    assert card.index("Offers:") < card.index("User note:")
    assert "bob" not in card.lower(), "the note must not leak identity handling"


def test_card_without_a_note_is_unchanged(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",))
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    alice.clear()
    alice.command("find")
    card = alice.last.text
    assert card.rstrip().endswith("Software Dev") or "Matches your needs" in card
    assert not card.rstrip().endswith("\n")


def test_note_shows_on_an_incoming_request_card(world, event):
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, event, offers=("Software",), needs=("UI / UX",), note=NOTE)
    onboard(bob, event, offers=("UI / UX",), needs=("Software",))

    alice.command("find")
    alice.tap("Request Match")
    assert "wants to team up" in bob.all_text()
    assert f"User note: {NOTE}" in bob.all_text(), "request cards label the note too"


def test_note_can_be_added_edited_and_removed_later(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event)                       # starts with no note
    assert db.get_profile(ALICE, event).note is None

    alice.command("profile")
    alice.tap("Edit profile")
    alice.tap("Note")
    alice.tap("Add note")
    alice.say(NOTE)
    assert db.get_profile(ALICE, event).note == NOTE

    alice.command("profile")                    # replace it
    alice.tap("Edit profile")
    alice.tap("Note")
    assert NOTE in alice.last.text, "the current note is shown before replacing it"
    alice.tap("Replace note")
    alice.say("Now looking for a hardware person.")
    assert db.get_profile(ALICE, event).note == "Now looking for a hardware person."

    alice.command("profile")                    # remove it
    alice.tap("Edit profile")
    alice.tap("Note")
    alice.tap("Remove note")
    assert db.get_profile(ALICE, event).note is None


def test_keeping_a_note_does_not_wipe_it(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, note=NOTE)
    alice.command("profile")
    alice.tap("Edit profile")
    alice.tap("Note")
    alice.tap("Keep it")
    assert db.get_profile(ALICE, event).note == NOTE


def test_editing_other_fields_preserves_the_note(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, school="NUS", note=NOTE)
    alice.command("profile")
    alice.tap("Edit profile")
    alice.tap("School")
    alice.tap("SUTD")
    profile = db.get_profile(ALICE, event)
    assert profile.school == "SUTD"
    assert profile.note == NOTE, "an unrelated edit must not drop the note"


def test_redoing_onboarding_can_clear_the_note(world, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, note=NOTE)
    alice.command("restart")
    alice.tap("NUS"); alice.tap("No preference"); alice.tap("Computing"); alice.tap("Solo")
    alice.tap("Software"); alice.tap("Done")
    alice.tap("UI / UX"); alice.tap("Done")
    alice.tap("Skip")
    assert db.get_profile(ALICE, event).note is None


def test_note_survives_a_restart(world, application, event):
    alice = Session(world, ALICE, "alice")
    onboard(alice, event, note=NOTE)

    restarted = World(application)
    alice2 = Session(restarted, ALICE, "alice")
    alice2.command("profile")
    assert NOTE in alice2.last.text


# ------------------------------------------------- organiser closes an event

def _make_event(world, name_suffix: str, organiser: int = ORGANISER) -> str:
    code, _ = db.create_event(f"E2E Hack {name_suffix}{uuid.uuid4().hex[:4]}", "ann", organiser)
    world._created_events.append(code)
    return code


def test_organiser_can_close_their_own_active_event(world):
    code = _make_event(world, "close")
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("myevents")
    assert "Active" in organiser.last.text
    assert organiser.has_button("Close")

    organiser.tap_data(f"evclose:{code}")
    assert "Close" in organiser.last.text and "Existing matches will remain available" in organiser.last.text

    organiser.tap_data(f"evcloseyes:{code}")
    assert db.get_event_row(code)["is_active"] is False

    organiser.clear()
    organiser.command("myevents")
    assert "Closed" in organiser.last.text


def test_cancelling_the_confirmation_leaves_the_event_active(world):
    code = _make_event(world, "cancel")
    organiser = Session(world, ORGANISER, "organiser")
    organiser.tap_data(f"evclose:{code}")
    organiser.tap_data("evcloseno")
    assert db.get_event_row(code)["is_active"] is True


def test_forged_callback_cannot_close_another_organisers_event(world):
    code = _make_event(world, "owned")
    intruder = Session(world, ALICE, "alice")        # not the organiser
    intruder.tap_data(f"evclose:{code}")
    intruder.tap_data(f"evcloseyes:{code}")          # skip the confirmation entirely
    assert db.get_event_row(code)["is_active"] is True, "a forged button must not close it"
    assert db.close_event_as_organiser(code, ALICE) == "not_owner"


def test_closed_event_frees_a_slot_under_the_active_cap(world):
    code = _make_event(world, "cap")
    before = db.count_active_events_for_organiser(ORGANISER)
    assert db.close_event_as_organiser(code, ORGANISER) == "closed"
    assert db.count_active_events_for_organiser(ORGANISER) == before - 1
    assert db.close_event_as_organiser(code, ORGANISER) == "already_closed"


def test_organiser_can_create_a_replacement_after_closing(world):
    code = _make_event(world, "replace")
    assert db.close_event_as_organiser(code, ORGANISER) == "closed"
    organiser = Session(world, ORGANISER, "organiser")
    organiser.command("newevent")
    organiser.say("Replacement Hack")
    organiser.clear()
    organiser.say("Replacement Hack\nafter closing")
    new_code = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(new_code)
    assert db.get_event_row(new_code)["is_active"] is True


def test_new_participant_cannot_join_a_closed_event(world):
    code = _make_event(world, "nojoin")
    db.close_event_as_organiser(code, ORGANISER)
    newcomer = Session(world, CAROL, "carol")
    newcomer.command("start", code)
    assert "closed" in newcomer.all_text().lower()
    assert db.get_profile(CAROL, code) is None


def test_existing_participant_keeps_closed_event_and_matches(world):
    code = _make_event(world, "keep")
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, code, offers=("Software",), needs=("UI / UX",))
    onboard(bob, code, offers=("UI / UX",), needs=("Software",))

    alice.clear()
    alice.command("find")
    alice.tap("Request Match")
    bob.tap("Accept")          # the request card is already in Bob's inbox
    assert "@alice" in bob.all_text()

    db.close_event_as_organiser(code, ORGANISER)

    # still listed, labelled closed
    alice.clear()
    alice.command("events")
    listed = [e["event_code"] for e in db.list_profiles_for_user(ALICE)]
    assert code in listed
    assert any(not e["is_active"] for e in db.list_profiles_for_user(ALICE) if e["event_code"] == code)

    # historical match and revealed username survive
    alice.clear()
    alice.command("matches")
    assert "@bob" in alice.all_text()
    assert len(db.get_matches(ALICE, code)) == 1


def test_find_is_refused_once_the_event_is_closed(world):
    code = _make_event(world, "nofind")
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, code, offers=("Software",), needs=("UI / UX",))
    onboard(bob, code, offers=("UI / UX",), needs=("Software",))
    db.close_event_as_organiser(code, ORGANISER)

    alice.clear()
    alice.command("find")
    assert "Potential teammate" not in alice.last.text
    assert "closed" in alice.all_text().lower()


def test_new_request_is_refused_once_the_event_is_closed(world):
    code = _make_event(world, "noreq")
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, code, offers=("Software",), needs=("UI / UX",))
    onboard(bob, code, offers=("UI / UX",), needs=("Software",))
    db.close_event_as_organiser(code, ORGANISER)

    assert db.request_match(code, ALICE, BOB) == "closed"
    assert db.respond_to_request(code, ALICE, BOB, True) == "closed"
    assert db.get_matches(ALICE, code) == []


def test_closing_one_event_does_not_affect_another(world):
    first = _make_event(world, "one")
    second = _make_event(world, "two")
    alice = Session(world, ALICE, "alice")
    bob = Session(world, BOB, "bob")
    onboard(alice, second, offers=("Software",), needs=("UI / UX",))
    onboard(bob, second, offers=("UI / UX",), needs=("Software",))

    db.close_event_as_organiser(first, ORGANISER)

    assert db.get_event_row(second)["is_active"] is True
    alice.clear()
    alice.command("find")
    assert "Potential teammate" in alice.last.text
    assert db.request_match(second, ALICE, BOB) in ("requested", "matched")


def test_active_event_cap_blocks_then_recovers_via_myevents(world):
    """The full organiser recovery journey: hit the cap, be told what to do, do it, continue.

    Uses a dedicated organiser id so it cannot be perturbed by other tests' events.
    """
    capper = 910_000_008
    codes = [
        _make_event(world, f"cap{i}", organiser=capper)
        for i in range(main.MAX_ACTIVE_EVENTS_PER_ORGANISER)
    ]
    assert db.count_active_events_for_organiser(capper) == main.MAX_ACTIVE_EVENTS_PER_ORGANISER

    organiser = Session(world, capper, "capper")

    # 2 + 3. /newevent is blocked, and the message teaches the way out.
    organiser.command("newevent")
    blocked = organiser.last.text
    assert "/myevents" in blocked, f"must name /myevents, got: {blocked}"
    assert "close" in blocked.lower()
    assert organiser.has_button("View My Events")
    assert organiser.last.reply_markup is not None

    # 4. the button reaches the event list, which shows them as Active.
    organiser.tap("View My Events")
    listing = organiser.last.text
    assert "Active" in listing

    # 5. close one, with confirmation.
    organiser.tap_data(f"evclose:{codes[0]}")
    assert "Existing matches will remain available" in organiser.last.text
    organiser.tap_data(f"evcloseyes:{codes[0]}")
    assert db.get_event_row(codes[0])["is_active"] is False

    # 6. the closed one no longer counts.
    assert db.count_active_events_for_organiser(capper) == main.MAX_ACTIVE_EVENTS_PER_ORGANISER - 1

    # 7. /newevent works again straight away — no cooldown.
    organiser.clear()
    organiser.command("newevent")
    assert "limit" not in organiser.last.text.lower()
    organiser.say("Recovered Hack")
    organiser.clear()
    organiser.say("Recovered Hack\nafter closing one")
    new_code = _event_code_from(organiser.inbox[0].text)
    world._created_events.append(new_code)
    assert db.get_event_row(new_code)["is_active"] is True


def test_hourly_creation_limit_still_applies(world):
    """5 creations/hour stays enforced, and is separate from the active-event cap."""
    limiter = 910_000_007
    organiser = Session(world, limiter, "limiter")
    for i in range(main.MAX_EVENTS_PER_HOUR_PER_ORGANISER):
        organiser.clear()
        organiser.command("newevent")
        organiser.say(f"Hourly {i}")
        organiser.clear()
        organiser.say(f"Hourly {i}\nannouncement")
        world._created_events.append(_event_code_from(organiser.inbox[0].text))

    organiser.clear()
    organiser.command("newevent")
    assert "last hour" in organiser.last.text.lower()


def test_participants_have_no_cap_on_hackathons_joined(world):
    """Participants stay unrestricted — the caps are organiser-side only."""
    joined = []
    for i in range(6):
        code = _make_event(world, f"many{i}")
        onboard(Session(world, ALICE, "alice"), code, offers=("Software",), needs=("UI / UX",))
        joined.append(code)
    rows = {r["event_code"] for r in db.list_profiles_for_user(ALICE)}
    assert set(joined).issubset(rows), "a participant must be able to join any number of events"
