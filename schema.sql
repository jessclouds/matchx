-- MatchX / Hackathon Match — full database schema.
-- Safe to run repeatedly: every statement is idempotent.
-- Run against Postgres (Supabase):  psql "$DATABASE_URL" -f schema.sql

-- ---------------------------------------------------------------- events ----
CREATE TABLE IF NOT EXISTS events (
    event_code   TEXT PRIMARY KEY,          -- 'ideate2026' — the deep link parameter
    name         TEXT NOT NULL,             -- 'IDEATE 2026' — shown to users
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Organiser flow (/newevent) needs to remember who created the event and
-- the announcement text they pasted.
ALTER TABLE events ADD COLUMN IF NOT EXISTS organiser_telegram_id BIGINT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS announcement TEXT;
-- Finished hackathons are closed rather than deleted: they stop matching but keep
-- their history. See newevent.py --close.
ALTER TABLE events ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS events_organiser_idx ON events (organiser_telegram_id);

-- -------------------------------------------------------------- profiles ----
CREATE TABLE IF NOT EXISTS profiles (
    telegram_user_id  BIGINT NOT NULL,
    event_code        TEXT NOT NULL REFERENCES events(event_code) ON DELETE CASCADE,
    telegram_username TEXT,                 -- mandatory in the app, nullable here for legacy rows

    school             TEXT NOT NULL,
    school_preference  TEXT NOT NULL,       -- 'same' / 'different' / 'none'
    discipline         TEXT NOT NULL,       -- DISPLAY ONLY: never used for eligibility or ranking
    team_status        TEXT NOT NULL,       -- 'looking' / 'has_team'

    skills_offered     TEXT[] NOT NULL,
    skills_needed      TEXT[] NOT NULL,

    open_to_any        BOOLEAN NOT NULL DEFAULT false,  -- wildcard: needs = every skill

    is_active          BOOLEAN NOT NULL DEFAULT true,   -- "actively looking" — hard eligibility filter
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A person may join many hackathons, so identity is the PAIR,
    -- not the telegram_user_id alone.
    PRIMARY KEY (telegram_user_id, event_code)
);

ALTER TABLE profiles ADD COLUMN IF NOT EXISTS open_to_any BOOLEAN NOT NULL DEFAULT false;
-- Optional free-text note shown on the participant's card. Display only: it never
-- affects eligibility or ranking.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS note TEXT;

DO $$
BEGIN
    ALTER TABLE profiles ADD CONSTRAINT profiles_note_length CHECK (char_length(note) <= 160);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS is_active   BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMPTZ NOT NULL DEFAULT now();

-- Candidate scans are always "everyone in this event who is active".
CREATE INDEX IF NOT EXISTS profiles_event_active_idx
    ON profiles (event_code, is_active);

-- The hard filter "candidate.offers ∩ user.needs is non-empty" is an array overlap,
-- so it needs a GIN index to stay fast once an event has thousands of profiles.
CREATE INDEX IF NOT EXISTS profiles_offers_gin_idx
    ON profiles USING GIN (skills_offered);

-- ------------------------------------------------------------- interests ----
-- One row per (requester -> recipient) pair, per event.
--   pending  : requester asked, recipient has not answered yet
--   accepted : recipient said yes  -> a row in matches exists
--   declined : recipient said no
--   skipped  : requester skipped this candidate while browsing (never notified)
CREATE TABLE IF NOT EXISTS interests (
    id            BIGSERIAL PRIMARY KEY,
    event_code    TEXT   NOT NULL REFERENCES events(event_code) ON DELETE CASCADE,
    from_user_id  BIGINT NOT NULL,
    to_user_id    BIGINT NOT NULL,
    status        TEXT   NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    responded_at  TIMESTAMPTZ,

    CONSTRAINT interests_not_self       CHECK (from_user_id <> to_user_id),
    CONSTRAINT interests_status_valid   CHECK (status IN ('pending', 'accepted', 'declined', 'skipped')),
    CONSTRAINT interests_unique_pair    UNIQUE (event_code, from_user_id, to_user_id),
    CONSTRAINT interests_from_profile   FOREIGN KEY (from_user_id, event_code)
        REFERENCES profiles (telegram_user_id, event_code) ON DELETE CASCADE,
    CONSTRAINT interests_to_profile     FOREIGN KEY (to_user_id, event_code)
        REFERENCES profiles (telegram_user_id, event_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS interests_to_pending_idx
    ON interests (event_code, to_user_id, status);

-- Browsing excludes everyone this user has already acted on.
CREATE INDEX IF NOT EXISTS interests_from_idx
    ON interests (event_code, from_user_id, status);

-- --------------------------------------------------------------- matches ----
-- Mutual acceptance only. user_a < user_b so a pair can exist at most once.
CREATE TABLE IF NOT EXISTS matches (
    id          BIGSERIAL PRIMARY KEY,
    event_code  TEXT   NOT NULL REFERENCES events(event_code) ON DELETE CASCADE,
    user_a      BIGINT NOT NULL,
    user_b      BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT matches_ordered      CHECK (user_a < user_b),
    CONSTRAINT matches_unique_pair  UNIQUE (event_code, user_a, user_b),
    CONSTRAINT matches_a_profile    FOREIGN KEY (user_a, event_code)
        REFERENCES profiles (telegram_user_id, event_code) ON DELETE CASCADE,
    CONSTRAINT matches_b_profile    FOREIGN KEY (user_b, event_code)
        REFERENCES profiles (telegram_user_id, event_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS matches_user_a_idx ON matches (event_code, user_a);
CREATE INDEX IF NOT EXISTS matches_user_b_idx ON matches (event_code, user_b);

-- Seed events used by the demo (harmless if they already exist).
INSERT INTO events (event_code, name) VALUES
    ('ideate2026',     'IDEATE 2026'),
    ('healthhack2026', 'HealthHack 2026')
ON CONFLICT (event_code) DO NOTHING;
