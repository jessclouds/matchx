# Hackathon Match (MatchX)

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

## How the product works

### Organiser
1. `/newevent`
2. Paste or forward the hackathon announcement (first line becomes the event name)
3. The bot replies with a unique **Find Teammates** link — `https://t.me/<bot>?start=<event_code>`
4. `/myevents` lists your events, links and participant counts

### Participant
1. Open the organiser's link → `/start <event_code>`
2. Six questions: school → school preference → discipline → team status → skills offered → skills needed
3. Browse candidates one at a time: **🤝 Request match** or **⏭ Skip**
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

**Deliberately not used:** discipline is display-only and never affects eligibility or
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
