"""Deterministic MatchX ranking. Pure functions — no database, no Telegram.

Spec (authoritative, do not extend):

Hard eligibility filters are EXACTLY:
  1. same hackathon/event
  2. candidate is actively looking
  3. candidate is not the current user
  4. candidate.offers ∩ user.needs is non-empty

Ranking is lexicographic on:
  1. stated school-preference fit, if a preference exists
  2. len(candidate.offers ∩ user.needs)
  3. len(user.offers ∩ candidate.needs)
  4. exact ties are rotated/randomised

Discipline is DISPLAY ONLY — it never affects eligibility or ranking.
Team status only establishes that the user is actively looking — it is never scored.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from constants import ALL_SKILL_VALUES


@dataclass(frozen=True)
class Profile:
    """A participant's profile as the matcher sees it."""

    telegram_user_id: int
    event_code: str
    telegram_username: str | None
    school: str
    school_preference: str      # 'same' | 'different' | 'none'
    discipline: str             # display only
    team_status: str            # display only ('looking' | 'has_team')
    skills_offered: tuple[str, ...] = ()
    skills_needed: tuple[str, ...] = ()
    open_to_any: bool = False
    is_active: bool = True

    @property
    def offers(self) -> frozenset[str]:
        return frozenset(self.skills_offered)

    @property
    def needs(self) -> frozenset[str]:
        """Needs used for matching.

        The wildcard ("No preference — open to anyone") and an empty selection both
        mean "any skill counts", so the whole catalogue is the effective need set.
        """
        stated = frozenset(self.skills_needed)
        if self.open_to_any or not stated:
            return ALL_SKILL_VALUES
        return stated

    @property
    def is_matchable(self) -> bool:
        """A profile only enters matchmaking when it is complete and usable.

        A Telegram username is mandatory: it is the only contact channel revealed
        after a mutual accept.
        """
        return bool(self.telegram_username) and bool(self.skills_offered) and self.is_active


@dataclass(frozen=True)
class ScoredCandidate:
    profile: Profile
    school_fit: int         # 1 = matches stated preference, 0 = no preference / no fit
    offers_for_me: tuple[str, ...]   # candidate.offers ∩ me.needs  (never empty — hard filter)
    needs_from_me: tuple[str, ...]   # me.offers ∩ candidate.needs

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.school_fit, len(self.offers_for_me), len(self.needs_from_me))


def school_fit(me: Profile, candidate: Profile) -> int:
    """1 when the candidate satisfies the user's stated school preference.

    'none' (No preference) MUST have zero effect: every candidate scores 0.
    """
    preference = (me.school_preference or "none").lower()
    if preference == "same":
        return int(candidate.school == me.school)
    if preference == "different":
        return int(candidate.school != me.school)
    return 0


def is_eligible(me: Profile, candidate: Profile) -> bool:
    """The four hard filters, and nothing else."""
    if candidate.event_code != me.event_code:
        return False
    if candidate.telegram_user_id == me.telegram_user_id:
        return False
    if not candidate.is_matchable:              # "actively looking" + usable profile
        return False
    if not (candidate.offers & me.needs):       # they must offer something I need
        return False
    return True


def score(me: Profile, candidate: Profile) -> ScoredCandidate:
    return ScoredCandidate(
        profile=candidate,
        school_fit=school_fit(me, candidate),
        offers_for_me=tuple(sorted(candidate.offers & me.needs)),
        needs_from_me=tuple(sorted(me.offers & candidate.needs)),
    )


def rank_candidates(
    me: Profile,
    pool: list[Profile],
    exclude_user_ids: set[int] | None = None,
    rng: random.Random | None = None,
) -> list[ScoredCandidate]:
    """Filter `pool` down to eligible candidates and order them best-first.

    `exclude_user_ids` removes people already interacted with (requested, skipped,
    declined, matched). Exact ties are shuffled so the same person is not always
    shown first.
    """
    excluded = exclude_user_ids or set()
    rng = rng or random.Random()

    eligible = [
        score(me, candidate)
        for candidate in pool
        if candidate.telegram_user_id not in excluded and is_eligible(me, candidate)
    ]

    # Shuffle first, then a stable sort keeps ties in shuffled (random) order.
    rng.shuffle(eligible)
    eligible.sort(key=lambda c: c.sort_key, reverse=True)
    return eligible
