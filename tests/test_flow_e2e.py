"""End-to-end walkthroughs driving the real handlers from main.build_application()."""

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and os.getenv("BOT_TOKEN")),
    reason="DATABASE_URL and BOT_TOKEN required",
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
            discipline="Computing", status="Solo", offers=("Software",), needs=("UI / UX",)):
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
