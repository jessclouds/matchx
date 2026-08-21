"""Create a hackathon and print its Find Teammates link — without opening Telegram.

    python newevent.py "IDEATE 2026"
    python newevent.py "IDEATE 2026" --announcement announcement.txt
    python newevent.py --list

Useful when you want to hand an organiser a ready-made link, or create several
events at once. The Telegram /newevent flow does exactly the same thing.
"""

from __future__ import annotations

import argparse
import sys

import db
from config import BOT_USERNAME, deep_link


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a hackathon event and print its join link.")
    parser.add_argument("name", nargs="?", help="Event name, e.g. \"IDEATE 2026\"")
    parser.add_argument("--announcement", help="Path to a file with the announcement text")
    parser.add_argument("--organiser", type=int, help="Organiser's Telegram user id (optional)")
    parser.add_argument("--list", action="store_true", help="List existing events and their links")
    parser.add_argument("--close", metavar="EVENT_CODE",
                        help="Close a finished hackathon: it stops matching but keeps its data")
    parser.add_argument("--reopen", metavar="EVENT_CODE", help="Reopen a closed hackathon")
    args = parser.parse_args()

    if args.close or args.reopen:
        code = args.close or args.reopen
        active = bool(args.reopen)
        if not db.set_event_active(code, active):
            print(f"No event with code {code!r}.")
            return 1
        print(f"{code} is now {'open' if active else 'closed'}.")
        return 0

    if args.list:
        with db.get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.event_code, e.name, e.is_active,
                       (SELECT count(*) FROM profiles p WHERE p.event_code = e.event_code) AS participants
                FROM events e ORDER BY e.created_at
                """
            )
            rows = cur.fetchall()
        if not rows:
            print("No events yet. Create one:  python newevent.py \"My Hackathon\"")
            return 0
        for row in rows:
            state = "" if row["is_active"] else "  [closed]"
            print(f"\n{row['name']}  ({row['participants']} joined){state}")
            print(f"  {deep_link(row['event_code'])}")
        print()
        return 0

    if not args.name:
        parser.error("give an event name, or use --list")

    announcement = None
    if args.announcement:
        announcement = open(args.announcement, encoding="utf-8").read()

    event_code, event_name = db.create_event(args.name, announcement, args.organiser)
    link = deep_link(event_code)

    print(f"\n✅ Created: {event_name}")
    print(f"   Event code: {event_code}")
    print(f"\n🔗 Share this link with participants:\n   {link}\n")
    print(f"   (Make sure the bot @{BOT_USERNAME} is running, or the link does nothing.)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
