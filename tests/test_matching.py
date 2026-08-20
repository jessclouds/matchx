"""Ranking rules from the MatchX spec. Pure logic — no database, no network."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from constants import ALL_SKILL_VALUES
from matching import Profile, is_eligible, rank_candidates, school_fit


def make(uid, *, event="hack1", school="NUS", pref="none", discipline="comp",
         status="looking", offers=("software",), needs=("uiux",),
         open_to_any=False, active=True, username="user"):
    return Profile(
        telegram_user_id=uid, event_code=event, telegram_username=username,
        school=school, school_preference=pref, discipline=discipline,
        team_status=status, skills_offered=tuple(offers), skills_needed=tuple(needs),
        open_to_any=open_to_any, is_active=active,
    )


def ids(ranked):
    return [c.profile.telegram_user_id for c in ranked]


# ------------------------------------------------------------- hard filters

def test_user_never_matches_themselves():
    me = make(1)
    assert not is_eligible(me, me)
    assert ids(rank_candidates(me, [me])) == []


def test_other_events_are_invisible():
    me = make(1, event="hack1", needs=("uiux",))
    other = make(2, event="hack2", offers=("uiux",))
    assert not is_eligible(me, other)
    assert ids(rank_candidates(me, [other])) == []


def test_candidate_must_be_actively_looking():
    me = make(1, needs=("uiux",))
    paused = make(2, offers=("uiux",), active=False)
    assert not is_eligible(me, paused)


def test_candidate_without_username_is_excluded():
    me = make(1, needs=("uiux",))
    anonymous = make(2, offers=("uiux",), username=None)
    assert not is_eligible(me, anonymous)


def test_candidate_with_no_offers_is_excluded():
    me = make(1, needs=("uiux",))
    empty = make(2, offers=())
    assert not is_eligible(me, empty)


def test_offers_must_intersect_my_needs():
    me = make(1, needs=("uiux",))
    mismatch = make(2, offers=("legal",))
    fit = make(3, offers=("uiux",))
    assert not is_eligible(me, mismatch)
    assert is_eligible(me, fit)


def test_wildcard_user_needs_everything():
    me = make(1, needs=(), open_to_any=True)
    assert me.needs == ALL_SKILL_VALUES
    assert is_eligible(me, make(2, offers=("legal",)))


def test_empty_needs_behaves_like_wildcard():
    me = make(1, needs=())
    assert me.needs == ALL_SKILL_VALUES


# ------------------------------------------------------- discipline & status

@pytest.mark.parametrize("discipline", ["med", "comp", "law", "design"])
def test_discipline_never_affects_eligibility_or_rank(discipline):
    me = make(1, discipline="comp", needs=("uiux",))
    candidate = make(2, discipline=discipline, offers=("uiux",), needs=("software",))
    baseline = make(3, discipline="comp", offers=("uiux",), needs=("software",))
    ranked = rank_candidates(me, [candidate, baseline], rng=random.Random(1))
    assert {c.sort_key for c in ranked} == {(0, 1, 1)}   # identical scores


def test_team_status_is_not_scored():
    me = make(1, needs=("uiux",))
    solo = make(2, status="looking", offers=("uiux",), needs=("software",))
    team = make(3, status="has_team", offers=("uiux",), needs=("software",))
    ranked = rank_candidates(me, [solo, team], rng=random.Random(0))
    assert ranked[0].sort_key == ranked[1].sort_key


# ------------------------------------------------------------ school fit

def test_school_preference_same():
    me = make(1, school="NUS", pref="same")
    assert school_fit(me, make(2, school="NUS")) == 1
    assert school_fit(me, make(3, school="NTU")) == 0


def test_school_preference_different():
    me = make(1, school="NUS", pref="different")
    assert school_fit(me, make(2, school="NTU")) == 1
    assert school_fit(me, make(3, school="NUS")) == 0


def test_no_preference_gives_school_zero_effect():
    me = make(1, school="NUS", pref="none", needs=("uiux",))
    same = make(2, school="NUS", offers=("uiux",))
    other = make(3, school="NTU", offers=("uiux",))
    ranked = rank_candidates(me, [same, other], rng=random.Random(0))
    assert all(c.school_fit == 0 for c in ranked)


def test_school_fit_outranks_skill_overlap():
    """Lexicographic order: school fit is the first key."""
    me = make(1, school="NUS", pref="same", offers=("software",), needs=("uiux", "ai_data", "legal"))
    same_school_weak = make(2, school="NUS", offers=("uiux",), needs=())
    other_school_strong = make(3, school="NTU", offers=("uiux", "ai_data", "legal"), needs=())
    assert ids(rank_candidates(me, [other_school_strong, same_school_weak], rng=random.Random(0))) == [2, 3]


def test_offer_overlap_then_reciprocal_overlap():
    me = make(1, pref="none", offers=("software", "ai_data"), needs=("uiux", "legal"))
    two_offers = make(2, offers=("uiux", "legal"), needs=())
    one_offer_reciprocal = make(3, offers=("uiux",), needs=("software", "ai_data"))
    one_offer_none = make(4, offers=("uiux",), needs=("healthcare",))
    ranked = rank_candidates(me, [one_offer_none, one_offer_reciprocal, two_offers], rng=random.Random(0))
    assert ids(ranked) == [2, 3, 4]


def test_exclusions_are_respected():
    me = make(1, needs=("uiux",))
    pool = [make(2, offers=("uiux",)), make(3, offers=("uiux",))]
    assert ids(rank_candidates(me, pool, exclude_user_ids={2})) == [3]


def test_exact_ties_are_randomised():
    me = make(1, pref="none", needs=("uiux",))
    pool = [make(uid, offers=("uiux",), needs=("software",)) for uid in range(2, 8)]
    orders = {tuple(ids(rank_candidates(me, pool, rng=random.Random(seed)))) for seed in range(12)}
    assert len(orders) > 1, "tied candidates should not always appear in the same order"


def test_ranking_is_stable_by_score_despite_shuffle():
    me = make(1, pref="none", offers=("software",), needs=("uiux", "ai_data"))
    strong = make(2, offers=("uiux", "ai_data"), needs=("software",))
    weak = make(3, offers=("uiux",), needs=("healthcare",))
    for seed in range(20):
        assert ids(rank_candidates(me, [weak, strong], rng=random.Random(seed))) == [2, 3]
