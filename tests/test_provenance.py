"""Provenance is attribution, freshness and disagreement — never truth."""
from datetime import datetime, timedelta, timezone
import unittest

from server import provenance as p


def stamp(days_ago=0.0, **offset):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago, **offset)
    return moment.isoformat()


_UNSET = object()


def claim(field_name="height_cm", value=185, source_id="espn", tier=p.SourceTier.DATASET, **extra):
    # Only default the clock when the caller did not speak about it at all: an
    # explicit "" means "the source never dated this" and must survive.
    if extra.get("fetched_at", _UNSET) is _UNSET:
        extra["fetched_at"] = stamp()
    return p.Claim(field=field_name, value=value, source_id=source_id, tier=tier, **extra)


class ClaimShapeTests(unittest.TestCase):
    def test_a_claim_without_a_source_is_refused(self):
        for missing in ("", "   ", None):
            with self.assertRaises(p.ProvenanceError):
                p.Claim(field="height_cm", value=185, source_id=missing, tier=p.SourceTier.PRESS)

    def test_a_claim_without_a_field_is_refused(self):
        with self.assertRaises(p.ProvenanceError):
            p.Claim(field="", value=185, source_id="espn", tier=p.SourceTier.PRESS)

    def test_an_unknown_tier_is_refused(self):
        with self.assertRaises(p.ProvenanceError):
            p.Claim(field="height_cm", value=185, source_id="espn", tier="authoritative")

    def test_a_tier_is_never_taken_from_arbitrary_input(self):
        # A stored row may carry the tier as text; only a known value survives.
        self.assertEqual(claim(tier="press").tier, p.SourceTier.PRESS)
        with self.assertRaises(p.ProvenanceError):
            claim(tier="official-ish")

    def test_retrieval_time_and_source_time_stay_apart(self):
        row = claim(fetched_at="2026-09-01T00:00:00+00:00", observed_at="2025-01-01T00:00:00+00:00")
        self.assertEqual(row.fetched_at, "2026-09-01T00:00:00+00:00")
        self.assertEqual(row.observed_at, "2025-01-01T00:00:00+00:00")

    def test_derived_values_record_their_inputs(self):
        base = claim()
        result = p.derived("age", 24, inputs=[base], algorithm="season_start_minus_birth_date")
        self.assertEqual(result.tier, p.SourceTier.DERIVED)
        self.assertEqual(result.derivation["algorithm"], "season_start_minus_birth_date")
        self.assertTrue(result.derivation["inputs"])


class FreshnessTests(unittest.TestCase):
    def test_settled_facts_are_kept_for_a_year(self):
        self.assertEqual(p.ttl_for("birth_date"), p.YEAR)
        self.assertEqual(p.ttl_for("nationality"), p.YEAR)
        self.assertEqual(p.ttl_for("height_cm"), p.YEAR)

    def test_an_expired_claim_is_stale_not_deleted(self):
        # A result carries an hour; two days later it is past it.
        old = claim(field_name="score", value="2-1", fetched_at=stamp(days_ago=2))
        self.assertEqual(old.freshness(), p.Freshness.STALE)
        self.assertTrue(old.expired())
        self.assertEqual(old.value, "2-1")

    def test_a_future_stamp_is_unknown_rather_than_fresh(self):
        ahead = claim(fetched_at=(datetime.now(timezone.utc) + timedelta(hours=3)).isoformat())
        self.assertEqual(ahead.freshness(), p.Freshness.UNKNOWN)

    def test_a_missing_stamp_is_unknown(self):
        self.assertEqual(claim(fetched_at="").freshness(), p.Freshness.UNKNOWN)

    def test_the_transfer_window_shortens_a_squad_list(self):
        mid_season = p.ttl_for("roster", now="2026-11-01T00:00:00+00:00")
        in_window = p.ttl_for("roster", now="2026-08-10T00:00:00+00:00")
        self.assertEqual(mid_season, 86400)
        self.assertEqual(in_window, p.TRANSFER_WINDOW_TTL)
        self.assertLess(in_window, mid_season)

    def test_the_last_week_of_the_window_is_the_shortest(self):
        # The deadline is not derivable from the calendar; a caller that knows
        # it passes window_end, and that is the only way to reach 6 hours.
        deadline = p.ttl_for("roster", now="2026-08-28T00:00:00+00:00", window_end="2026-09-01T00:00:00+00:00")
        january = p.ttl_for("roster", now="2026-01-10T00:00:00+00:00")
        self.assertEqual(deadline, p.TRANSFER_DEADLINE_TTL)
        self.assertLess(deadline, january)

    def test_a_deadline_far_away_does_not_shorten_the_window_default(self):
        self.assertEqual(p.ttl_for("roster", now="2026-08-01T00:00:00+00:00",
                                   window_end="2026-09-01T00:00:00+00:00"),
                         p.TRANSFER_WINDOW_TTL)

    def test_a_passed_deadline_restores_the_season_clock(self):
        self.assertEqual(p.ttl_for("roster", now="2026-08-28T00:00:00+00:00",
                                   window_end="2026-08-01T00:00:00+00:00"),
                         p.TRANSFER_WINDOW_TTL)

    def test_a_fixture_accelerates_the_fields_that_change_at_kickoff(self):
        early = p.ttl_for("roster", now="2026-11-01T00:00:00+00:00", kickoff="2026-11-08T20:00:00+00:00")
        near = p.ttl_for("roster", now="2026-11-01T00:00:00+00:00", kickoff="2026-11-01T20:00:00+00:00")
        self.assertEqual(early, 86400)
        self.assertEqual(near, p.MATCH_DAY_TTL)

    def test_a_result_is_rechecked_within_the_hour(self):
        self.assertEqual(p.ttl_for("score"), p.HOUR)
        # A kickoff must not make a completed result urgent again.
        self.assertEqual(p.ttl_for("score", now="2026-11-01T00:00:00+00:00",
                                   kickoff="2026-11-01T20:00:00+00:00"), p.HOUR)

    def test_an_explicit_window_beats_the_calendar_guess(self):
        # February is a window month; a league that says otherwise must win.
        self.assertEqual(p.ttl_for("roster", now="2026-02-10T00:00:00+00:00", window=False), 86400)
        self.assertEqual(p.ttl_for("roster", now="2026-11-01T00:00:00+00:00", window=True), p.TRANSFER_WINDOW_TTL)

    def test_a_settled_fact_is_not_accelerated_by_the_window(self):
        # A birthday fetched in July must not inherit a window-sized budget and
        # end up at the front of the review queue forever after.
        self.assertEqual(p.ttl_for("birth_date", now="2026-07-15T00:00:00+00:00"), p.YEAR)
        self.assertEqual(p.ttl_for("nationality", now="2026-08-30T00:00:00+00:00"), p.YEAR)
        self.assertEqual(p.ttl_for("height_cm", now="2026-01-20T00:00:00+00:00"), p.YEAR)

    def test_the_same_field_takes_its_own_budget_without_being_told(self):
        self.assertEqual(claim(field_name="birth_date").ttl_seconds, p.YEAR)
        self.assertEqual(claim(field_name="score").ttl_seconds, p.HOUR)
        self.assertEqual(claim(field_name="roster", fetched_at="2026-08-10T00:00:00+00:00").ttl_seconds,
                         p.TRANSFER_WINDOW_TTL)

    def test_an_overdue_result_outranks_an_older_birth_date(self):
        rows = [claim(field_name="birth_date", value="2000-01-01", fetched_at=stamp(days_ago=400)),
                claim(field_name="score", value="2-1", fetched_at=stamp(days_ago=30))]
        self.assertGreater(rows[1].overdue_ratio(), rows[0].overdue_ratio())


class MergeTests(unittest.TestCase):
    def test_agreeing_claims_merge_and_keep_every_source(self):
        rows = [claim(source_id="espn"), claim(source_id="api_football")]
        merged = p.merge_claims(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].derivation["merged_from"], ["api_football", "espn"])

    def test_different_values_are_both_kept(self):
        rows = [claim(value=183), claim(value=185, source_id="api_football")]
        self.assertEqual(len(p.merge_claims(rows)), 2)

    def test_a_tier_boundary_is_never_crossed_automatically(self):
        # The same number from an official feed and a forum are different
        # statements and must not collapse into one.
        rows = [claim(value=185, source_id="arsenal_official", tier=p.SourceTier.OFFICIAL),
                claim(value=185, source_id="reddit", tier=p.SourceTier.COMMUNITY)]
        self.assertEqual(len(p.merge_claims(rows)), 2)

    def test_representation_differences_do_not_create_a_conflict(self):
        rows = [claim(value=185), claim(value="185", source_id="api_football"),
                claim(value=" 185 ", source_id="wikidata")]
        merged = p.merge_claims(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(p.conflicts(rows), [])

    def test_case_differences_in_ascii_names_do_not_conflict(self):
        rows = [claim(field_name="position", value="Goalkeeper"),
                claim(field_name="position", value="goalkeeper", source_id="wikidata")]
        self.assertEqual(len(p.merge_claims(rows)), 1)

    def test_dictionary_input_is_accepted_for_stored_rows(self):
        rows = [row.safe() for row in (claim(), claim(source_id="api_football"))]
        self.assertEqual(len(p.merge_claims(rows)), 1)


class ClubRelationTests(unittest.TestCase):
    """A loaned player is at two clubs at once; that is not a disagreement."""

    def loan(self, relation, club, tier=p.SourceTier.DATASET, source_id="espn", fetched=None):
        return p.Claim(field="club_id", value=club, source_id=source_id, tier=tier,
                       fetched_at=fetched or stamp(), derivation={"relation": relation})

    def test_parent_and_current_club_do_not_conflict(self):
        rows = [self.loan("parent_club", "arsenal"), self.loan("current_club", "aston_villa")]
        self.assertEqual(len(p.merge_claims(rows)), 2)
        self.assertEqual(p.conflicts(rows), [])

    def test_a_community_loan_record_is_not_a_contradiction(self):
        # The exact shape that was previously read as a data error: a structured
        # provider names the parent club while a community database names the
        # loan club.
        rows = [self.loan("parent_club", "arsenal", p.SourceTier.DATASET, "espn"),
                self.loan("current_club", "aston_villa", p.SourceTier.COMMUNITY, "thesportsdb")]
        self.assertEqual(p.conflicts(rows), [])
        self.assertEqual({row.value for row in p.merge_claims(rows)}, {"arsenal", "aston_villa"})

    def test_the_same_relation_with_different_clubs_is_a_real_conflict(self):
        rows = [self.loan("current_club", "aston_villa"), self.loan("current_club", "arsenal", source_id="api_football")]
        self.assertEqual(p.conflicts(rows)[0]["field"], "club_id")

    def test_a_youth_club_is_not_the_current_club(self):
        rows = [self.loan("youth_club", "barcelona"), self.loan("current_club", "arsenal")]
        self.assertEqual(len(p.merge_claims(rows)), 2)

    def test_only_club_fields_consider_the_relation(self):
        rows = [claim(field_name="height_cm", value=185, derivation={"relation": "parent_club"}),
                claim(field_name="height_cm", value=185, source_id="api_football")]
        self.assertEqual(len(p.merge_claims(rows)), 1)


class RelayTests(unittest.TestCase):
    """Tier decides the sentence; popularity must not rewrite it."""

    def test_each_tier_has_its_own_wording(self):
        wording = {tier: claim(tier=tier).relay() for tier in p.SourceTier}
        self.assertEqual(len(set(wording.values())), len(p.SourceTier))

    def test_a_popular_community_claim_stays_community(self):
        row = claim(source_id="reddit", tier=p.SourceTier.COMMUNITY, corroboration=12000)
        self.assertEqual(row.tier, p.SourceTier.COMMUNITY)
        self.assertIn("社区", row.relay())
        self.assertIn("多人", row.relay())

    def test_corroboration_never_promotes_a_tier(self):
        for count in (0, 1, 5, 100000):
            self.assertEqual(claim(tier=p.SourceTier.COMMUNITY, corroboration=count).tier,
                             p.SourceTier.COMMUNITY)

    def test_a_quiet_community_claim_is_still_attributed(self):
        self.assertEqual(claim(tier=p.SourceTier.COMMUNITY, corroboration=0).relay(), "社区说法")


class RegistrationTests(unittest.TestCase):
    """A domain may only use the tier a reviewer gave it."""

    def setUp(self):
        self.saved = dict(p._REGISTRY)
        self.addCleanup(lambda: (p._REGISTRY.clear(), p._REGISTRY.update(self.saved)))

    def test_an_identifier_resolves_when_the_url_is_unknown(self):
        p.register("thesportsdb", "thesportsdb.com", p.SourceTier.COMMUNITY)
        row = p.claim_from({"id": "thesportsdb", "url": "https://cdn.thesportsdb.com/x"}, "name", "Someone")
        self.assertEqual(row.tier, p.SourceTier.COMMUNITY)

    def test_the_host_wins_over_a_misleading_identifier(self):
        # A community host may not borrow an official id to raise its tier.
        p.register("reddit", "reddit.com", p.SourceTier.COMMUNITY)
        p.register("arsenal_official", "arsenal.com", p.SourceTier.OFFICIAL)
        row = p.claim_from({"id": "arsenal_official", "url": "https://www.reddit.com/r/x/comments/1"},
                           "injury", "out")
        self.assertEqual(row.tier, p.SourceTier.COMMUNITY)

    def test_a_registered_community_host_cannot_promote_itself(self):
        p.register("reddit", "reddit.com", p.SourceTier.COMMUNITY)
        with self.assertRaises(p.UnregisteredSource):
            p.claim_from({"id": "reddit", "url": "https://www.reddit.com/r/soccer/x"},
                         "injury", "out", tier=p.SourceTier.OFFICIAL)

    def test_an_unregistered_host_cannot_produce_a_displayable_claim(self):
        with self.assertRaises(p.UnregisteredSource):
            p.claim_from({"id": "mystery", "url": "https://unknown.example/post"}, "injury", "out")

    def test_the_registered_tier_is_used_even_when_omitted(self):
        p.register("thesportsdb", "thesportsdb.com", p.SourceTier.COMMUNITY)
        row = p.claim_from({"id": "thesportsdb", "url": "https://www.thesportsdb.com/player/1"}, "name", "Someone")
        self.assertEqual(row.tier, p.SourceTier.COMMUNITY)

    def test_one_host_cannot_be_two_tiers(self):
        p.register("press_a", "example.com", p.SourceTier.PRESS)
        with self.assertRaises(p.ProvenanceError):
            p.register("press_b", "example.com", p.SourceTier.OFFICIAL)

    def test_reregistering_the_same_tier_is_allowed(self):
        p.register("press_a", "example.com", p.SourceTier.PRESS)
        p.register("press_a", "example.com", p.SourceTier.PRESS, name="Updated")
        self.assertEqual(p.registered("example.com")["name"], "Updated")

    def test_a_url_lookup_finds_the_registered_tier(self):
        p.register("bbc", "bbc.co.uk", p.SourceTier.PRESS)
        self.assertEqual(p.tier_for_url("https://www.bbc.co.uk/sport/x"), p.SourceTier.PRESS)
        self.assertIsNone(p.tier_for_url("https://elsewhere.example/x"))


class ReviewQueueTests(unittest.TestCase):
    def test_overdue_fields_come_first(self):
        # Overdueness is relative to each field's own budget: a result a month
        # past its hour is far more overdue than a year-old birth date.
        rows = [claim(field_name="birth_date", value="2000-01-01", fetched_at=stamp(days_ago=400)),
                claim(field_name="score", value="2-1", fetched_at=stamp(days_ago=30))]
        queue = p.reviews_required(rows)
        self.assertEqual([row.field for row in queue], ["score", "birth_date"])

    def test_current_claims_are_not_queued(self):
        self.assertEqual(p.reviews_required([claim(fetched_at=stamp())]), [])

    def test_a_claim_with_no_clock_is_queued_first(self):
        # Undated material cannot be shown as current, so it outranks anything
        # that is merely late — even a result long past its hour.
        queue = p.reviews_required([claim(field_name="score", fetched_at=""),
                                    claim(field_name="score", value="1-0", fetched_at=stamp(days_ago=999))])
        self.assertEqual(queue[0].fetched_at, "")


class FieldViewTests(unittest.TestCase):
    def test_stronger_tiers_lead_but_all_claims_survive(self):
        rows = [claim(value="community", source_id="reddit", tier=p.SourceTier.COMMUNITY),
                claim(value="official", source_id="arsenal_official", tier=p.SourceTier.OFFICIAL)]
        view = p.field_view(rows, "height_cm")
        self.assertEqual([row.tier for row in view], [p.SourceTier.OFFICIAL, p.SourceTier.COMMUNITY])

    def test_another_field_is_not_returned(self):
        self.assertEqual(p.field_view([claim(field_name="weight_kg")], "height_cm"), [])


if __name__ == "__main__":
    unittest.main()
