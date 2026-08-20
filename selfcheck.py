"""One-command proof that Hackathon Match works end to end.

    python selfcheck.py

Creates a throwaway event, walks two simulated users (Ada and Ben) through
onboarding, browsing, a match request and an accept, prints every message they
would see in Telegram, checks the rules that matter, and deletes the test data.

No phone, no second Telegram account, no manual tapping.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))

import logging  # noqa: E402

logging.disable(logging.INFO)   # keep the transcript readable

import db  # noqa: E402
import main  # noqa: E402
from fake_telegram import Session, World  # noqa: E402

ADA, BEN = 990_000_101, 990_000_102

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"  {GREEN}✓{RESET} {label}" if condition else f"  {RED}✗ {label}{RESET}")
    if not condition:
        failures.append(label)


def step(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")


def show(who: str, session: Session) -> None:
    """Print what this user just received, as they'd see it in Telegram."""
    for msg in session.inbox:
        text = msg.text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
        text = text.replace("<code>", "").replace("</code>", "")
        body = "\n      ".join(text.splitlines())
        print(f"  {DIM}{who} sees:{RESET} {body}")
        if msg.buttons():
            print(f"      {DIM}[{'] ['.join(b.text for b in msg.buttons())}]{RESET}")
    session.clear()


def purge(event_code: str) -> None:
    with db.get_connection() as conn, conn.cursor() as cur:
        for table in ("matches", "interests", "profiles", "events"):
            cur.execute(f"DELETE FROM {table} WHERE event_code = %s", (event_code,))
        conn.commit()


def main_check() -> int:
    print(f"{BOLD}Hackathon Match — self check{RESET}")

    step("0. Database")
    check("database reachable", db.ping())

    world = World(main.build_application())
    event_code, event_name = db.create_event(
        f"Selfcheck Hack {uuid.uuid4().hex[:4]}", "demo announcement", ADA
    )

    try:
        step("1. Organiser creates an event")
        print(f"  {DIM}link:{RESET} https://t.me/{__import__('config').BOT_USERNAME}?start={event_code}")
        check("event is stored and findable by code", db.get_event(event_code) == event_name)

        ada = Session(world, ADA, "ada_dev")
        ben = Session(world, BEN, "ben_design")

        step("2. Ada joins through the link and onboards (developer who needs a designer)")
        ada.command("start", event_code)
        ada.tap("NUS"); ada.tap("No preference"); ada.tap("Computing"); ada.tap("Solo")
        ada.tap("Software"); ada.tap("Done")
        ada.tap("UI / UX"); ada.tap("Done")
        show("Ada", ada)
        ada_profile = db.get_profile(ADA, event_code)
        check("Ada's profile saved to Postgres", ada_profile is not None)
        check("skills stored correctly", set(ada_profile.skills_offered) == {"software"}
              and set(ada_profile.skills_needed) == {"uiux"})

        step("3. Ada re-runs onboarding — must not create a duplicate")
        ada.command("start", event_code)
        ada.clear()
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM profiles WHERE event_code = %s AND telegram_user_id = %s",
                        (event_code, ADA))
            check("still exactly one profile row", cur.fetchone()["n"] == 1)

        step("4. Ben joins (designer who needs a developer)")
        ben.command("start", event_code)
        ben.tap("NUS"); ben.tap("No preference"); ben.tap("Design"); ben.tap("Solo")
        ben.tap("UI / UX"); ben.tap("Done")
        ben.tap("Software"); ben.tap("Done")
        ben.clear()
        check("Ben's profile saved", db.get_profile(BEN, event_code) is not None)

        step("5. Ada browses — Ben is recommended, anonymously")
        ada.command("find")
        card = ada.last.text
        show("Ada", ada)
        check("a candidate card is shown", "Potential teammate" in card)
        check("the card explains the skill fit", "Matches your needs" in card)
        check("no username leaked before matching", "ben_design" not in card)

        step("6. Ada taps 'Request match' — Ben gets her card")
        ada.command("find")
        ada.tap("Request match")
        show("Ada", ada)
        show("Ben", ben)
        check("one-sided interest creates NO match", db.get_matches(ADA, event_code) == [])
        check("Ben was sent the request", [p.telegram_user_id for p in db.get_incoming_requests(BEN, event_code)] == [ADA])

        step("7. Ben taps 'Accept'")
        ben.command("matches")
        ben.tap("Accept")
        show("Ben", ben)
        show("Ada", ada)
        check("Ada now sees Ben's @username", db.get_matches(ADA, event_code)[0].telegram_username == "ben_design")
        check("Ben now sees Ada's @username", db.get_matches(BEN, event_code)[0].telegram_username == "ada_dev")

        step("8. Safety checks")
        ben.tap_data("resp:yes:%d" % ADA)      # stale button pressed again
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM matches WHERE event_code = %s", (event_code,))
            check("double-tapping Accept created no second match", cur.fetchone()["n"] == 1)
        ada.clear(); ben.clear()

        other_code, _ = db.create_event(f"Selfcheck Other {uuid.uuid4().hex[:4]}", None, ADA)
        try:
            db.save_profile(BEN, other_code, "ben_design", "NUS", "none", "design", "looking",
                            ["uiux"], ["software"], False)
            from matching import rank_candidates
            pool = db.get_event_pool(other_code)
            check("other events are invisible to Ada", all(p.telegram_user_id != ADA for p in pool))
            check("Ada has no candidates outside her event",
                  rank_candidates(ada_profile, pool) == [])
        finally:
            purge(other_code)

        ada.command("find")
        check("no candidates left is handled gracefully", "Potential teammate" not in ada.last.text)
        show("Ada", ada)

    finally:
        purge(event_code)
        print(f"\n{DIM}test data deleted{RESET}")

    print()
    if failures:
        print(f"{RED}{BOLD}{len(failures)} check(s) FAILED:{RESET}")
        for f in failures:
            print(f"  {RED}- {f}{RESET}")
        return 1
    print(f"{GREEN}{BOLD}All checks passed — the full journey works.{RESET}")
    print(f"{DIM}Now start the bot for real:  .venv/bin/python main.py{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_check())
