# MatchX

A Telegram bot that pairs hackathon participants with the teammates they actually
need. Organisers create an event and share one link; participants answer six taps,
browse ranked candidates, and swap Telegram usernames **only after both sides say yes**.

---

## Run it

```bash
# 1. Install
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure
cp .env.example .env      # then fill in BOT_TOKEN, BOT_USERNAME, DATABASE_URL

# 3. Create the tables (safe to re-run; it's idempotent)
.venv/bin/python db.py
#    (or, if you prefer psql:  psql "$DATABASE_URL" -f schema.sql)

# 4. Start the bot
.venv/bin/python main.py
```

The bot logs `Connected as @YourBot` when it's live. Stop it with Ctrl-C.

## Check it works

One command walks two simulated users through the entire journey — onboarding,
browsing, a match request, an accept — prints every message they'd see in Telegram,
verifies the rules, and deletes its own test data:

```bash
.venv/bin/python selfcheck.py
```

The full suite:

```bash
.venv/bin/python -m pytest tests/ -q
```

* `tests/test_matching.py` — the ranking rules (pure, instant)
* `tests/test_db_integration.py` — upserts, mutual matches, exclusions, event isolation
* `tests/test_flow_e2e.py` — two simulated users driving the real handlers end to end

Database tests create their own throwaway events and delete them afterwards; they
skip automatically when `DATABASE_URL` / `BOT_TOKEN` are unset.

---

## Running it somewhere that isn't your laptop

The bot uses long polling, so it needs **no public URL, no webhook, no open port** —
just a process that stays alive. While it is stopped, event links do nothing.

Any always-on host works; the repo ships a `Dockerfile` and a `Procfile`.

**Railway (Hobby plan):**
1. Push this repo to GitHub, then Railway → **New Project → Deploy from GitHub repo**
2. `railway.json` pins the build for you: Nixpacks (not the Dockerfile),
   `python main.py` as the start command, and **1 replica**
3. Add the environment variables below under **Variables** (Railway keeps them secret)
4. Settings → **turn App Sleeping OFF**. A polling bot receives no inbound HTTP, so a
   sleeping service would never wake up
5. No public domain, no port, no health check — it is a worker, not a web service.
   Railway may note that no port was detected; that is expected
6. Pick a region near your Supabase project to keep query latency down

Required: `BOT_TOKEN`, `BOT_USERNAME`, `DATABASE_URL`.
Optional: `LOG_LEVEL` (default INFO), `ORGANISER_IDS`, `PERSISTENCE_FILE`
(default `bot_state.pickle` next to the code; `/tmp/bot_state.pickle` also works).

Build and start, if you ever need to set them by hand:

```
build:  pip install -r requirements.txt
start:  python main.py
```

Verified locally against a clean `python:3.13-slim` container using exactly the files
git would serve: `pip install -r requirements.txt` then `python main.py` reaches
"Application started" in ~9s and uses ~48 MB.

**Other hosts** (Render, Fly.io, Koyeb): the `Procfile` runs `python main.py` as a
worker, or use the `Dockerfile`. Same environment variables either way.

**Creating events without Telegram**

```bash
python newevent.py "IDEATE 2026"     # prints a ready-to-share link
python newevent.py --list            # every event, its link and participant count
python newevent.py --close <code>    # finished hackathon: stops matching, keeps data
python newevent.py --reopen <code>
```

Handy for preparing links in advance, or handing an organiser a link without giving
them bot access. Set `ORGANISER_IDS=<telegram id>,<telegram id>` to limit who may run
`/newevent` inside Telegram; leave it unset and anyone can (fine for a pilot).

---

## How the product works

### Organiser
1. `/newevent` (`/cancel` backs out at any step; the flow is per organiser, so two
   organisers can run it at the same time without interfering)
2. Type the hackathon's **name** — this is authoritative and is what participants see
   when they join; it is never inferred from the announcement text
3. Paste or forward the hackathon announcement
4. The bot replies with **that same announcement, unchanged**, with this appended:

   > 🤝 **Looking for teammates?**
   > MatchX helps you find people with complementary skills and connect when both sides are interested.
   > **Find teammates:** `https://t.me/<bot>?start=<event_code>`

   Copy that message and post it as-is. A follow-up message (event code, tips) is sent
   separately so it never ends up in the copy-paste.
5. `/myevents` lists your events, links and participant counts

Every generated link maps to that hackathon's pool only — participants who join through
different links never see each other.

### Participant
1. Open the organiser's link — the bot confirms *"You're joining the teammate-matching
   pool for &lt;hackathon name&gt;"* so they can check they're in the right pool.
   Participants never type or pick an event code; the link carries it.
2. Six questions: school → school preference → discipline → team status → skills offered
   → skills needed, then an optional one-line note (up to 160 characters) — e.g. the
   track they want, what they're building, or the teammate they're after. The note is
   display only: it never affects eligibility or ranking, and it can be added, replaced
   or removed later from the profile screen.
3. Browse candidates one at a time: **← Back**, **Request Match**, **Next →**
   (Back re-shows the previous card — it never undoes a request or a match, and you
   can request someone you had already passed on)
4. A request pushes your (anonymous) card straight to that person — they never have to
   stumble across you while browsing
5. They **✅ Accept** or **🚫 Decline**. On accept, both sides get the other's `@username`

Everything else lives behind buttons: `/find`, `/matches`, `/profile` (view, edit any
single field, pause/resume matchmaking), `/events` (switch hackathon), `/restart`, `/help`.

---

## Matching rules (authoritative)

**Hard eligibility filters — exactly these four:**
1. same hackathon/event
2. candidate is actively looking (profile complete, has a username, not paused)
3. candidate is not the current user
4. `candidate.offers ∩ user.needs` is non-empty

**Ranking — lexicographic:**
1. stated school-preference fit, if a preference exists
2. `len(candidate.offers ∩ user.needs)`
3. `len(user.offers ∩ candidate.needs)`
4. exact ties are shuffled, so nobody is permanently first

**Deliberately not used:** discipline and the profile note are display-only and never affects eligibility or
ranking; team status only signals that someone is looking and is never scored; there is
no ML, no embedding, no opaque score.

"No preference" for school means school has **zero** effect. The "open to anyone"
wildcard (or picking no needed skills) means every skill counts as needed.

A Telegram username is mandatory — it is the only contact channel revealed, so anyone
without one is told how to create one and is kept out of matchmaking until they do.

---

## Layout

| File | Purpose |
|---|---|
| `main.py` | Telegram handlers and app wiring — the only place that talks to users |
| `db.py` | Every SQL statement, pooled connections, retry on dropped connections |
| `matching.py` | Pure ranking logic (`Profile`, `is_eligible`, `rank_candidates`) |
| `constants.py` | Question options, labels, user-facing copy |
| `keyboards.py` | Inline keyboards and card rendering |
| `config.py` | Environment loading, deep links, logging setup |
| `schema.sql` | Idempotent Postgres schema |

## Operating at scale

Designed for a pilot of many hackathons running at once, each with hundreds of
participants.

* **Browsing is one indexed query.** The four hard filters and the ranking keys are
  applied in Postgres (`db.find_candidates`), which returns a single page of 25.
  Nothing loads a whole event into Python, so cost per card is flat as the event
  grows. `matching.rank_candidates` still does the authoritative ordering, and
  `tests/test_scale.py` asserts the SQL and the Python rules agree exactly.
* **Isolation is enforced in SQL**, not in the UI: every candidate, interest and match
  query is keyed by `event_code`.
* **Exactly one match per pair** is guaranteed by a unique constraint plus a
  transaction-scoped advisory lock, verified by threaded simultaneous-accept tests.
* **All durable state is in Postgres.** A restart loses nothing; only browsing
  position lives in `user_data` (and even that is pickled).
* **Connections** come from a checked pool (max 10) with a retry and short backoff for
  the connections Supabase's pooler drops.
* **Telegram limits** are handled by `AIORateLimiter`, which queues and retries so a
  burst of match notifications cannot trip flood control. Updates are processed
  concurrently.
* **Abuse**: a bounded in-memory window caps a single user at 40 taps and 15 match
  requests per minute.
* **Finished hackathons** are closed rather than deleted — `python newevent.py --close
  <code>` stops matching while keeping the data. Reopen with `--reopen`.

Indexes: `profiles (event_code, is_active)`, GIN on `profiles.skills_offered` (for the
offers ∩ needs overlap), `interests (event_code, from_user_id, status)`,
`interests (event_code, to_user_id, status)`, unique `(event_code, from_user_id,
to_user_id)`, and unique `(event_code, user_a, user_b)` on matches.

## Data model

* `events` — `event_code` (deep-link payload), name, organiser, announcement
* `profiles` — keyed on **(telegram_user_id, event_code)**, so the same person can join
  several hackathons and re-running onboarding updates one row instead of duplicating
* `interests` — one row per (requester → recipient) pair: `pending` / `accepted` /
  `declined` / `skipped`, unique per pair per event
* `matches` — created only on mutual acceptance, `user_a < user_b` with a unique
  constraint so a pair can exist at most once

All matching state is in Postgres, so restarts lose nothing. Concurrent likes on the
same pair are serialised with a Postgres advisory lock, and the match row is inserted
with `ON CONFLICT DO NOTHING … RETURNING`, so both users are notified exactly once.

## Privacy

* Candidate cards show school, discipline, team status and skills — no name, no username
* `@username` is revealed only after a mutual accept
* Declines are silent: the requester is never told who said no
* Skips are private and one-directional
* Secrets live in `.env` only; database errors are logged server-side and users see a
  plain "try again" message
