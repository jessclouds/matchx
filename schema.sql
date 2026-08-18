-- MatchX database schema
-- Run against Postgres (Supabase). Order matters: events before profiles,
-- because profiles has a foreign key pointing at events.

CREATE TABLE events (
    event_code   TEXT PRIMARY KEY,          -- 'ideate2026' — the deep link parameter
    name         TEXT NOT NULL,             -- 'IDEATE 2026' — shown to users
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE profiles (
    telegram_user_id  BIGINT NOT NULL,
    event_code        TEXT NOT NULL REFERENCES events(event_code),
    telegram_username TEXT,                 -- nullable: not every Telegram user has one

    school             TEXT NOT NULL,
    school_preference  TEXT NOT NULL,     -- 'same' / 'different' / 'none'
    discipline         TEXT NOT NULL,
    team_status        TEXT NOT NULL,     -- 'looking' / 'has_team'

    skills_offered     TEXT[] NOT NULL,
    skills_needed      TEXT[] NOT NULL,

    open_to_any        BOOLEAN NOT NULL DEFAULT false,   -- ← add this

    is_active          BOOLEAN NOT NULL DEFAULT true,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- A person may join many hackathons, so identity is the PAIR,
    -- not the telegram_user_id alone.
    PRIMARY KEY (telegram_user_id, event_code)
    
);
