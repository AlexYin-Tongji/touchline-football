"""The agent decides what is worth re-checking, and the order is not "oldest".

A fixed timer per source cannot say which of two stale values actually matters.
A birthday and a match result are both "old" but only one of them is a problem,
and that order is what a limited quota has to be spent on.
"""
from datetime import datetime, timedelta, timezone
import unittest

from server import collection, provenance
from server.provenance import Claim, SourceTier


def stamp(**offset):
    return (datetime.now(timezone.utc) - timedelta(**offset)).isoformat()


def claim(field_name, value, source_id="espn", tier=SourceTier.DATASET, **extra):
    if extra.get("fetched_at", object()) is None:
        extra.pop("fetched_at")
    extra.setdefault("fetched_at", stamp())
    return Claim(field=field_name, value=value, source_id=source_id, tier=tier, **extra)


def player(**fields):
    return {"claims": {name: value for name, value in fields.items()}}


class OrderingTests(unittest.TestCase):
    def test_an_overdue_result_beats_an_older_birthday(self):
        # The whole point: 400 days past a year-long budget is less urgent than
        # 30 days past an hour-long one.
        subjects = {"player_a": player(birth_date=claim("birth_date", "2000-01-01",
                                                        fetched_at=stamp(days=400))),
                    "match_1": player(score=claim("score", "2-1", fetched_at=stamp(days=30)))}
        needs = collection.needs_from(subjects)
        self.assertEqual(needs[0].field, "score")
        self.assertEqual([need.subject_id for need in needs], ["match_1", "player_a"])

    def test_undated_material_ranks_first(self):
        subjects = {"a": player(score=claim("score", "1-0", fetched_at=stamp(days=999))),
                    "b": player(photo_url=claim("photo_url", "https://x/y.png", fetched_at=""))}
        needs = collection.needs_from(subjects)
        self.assertEqual(needs[0].subject_id, "b")
        self.assertEqual(needs[0].overdue_ratio, float("inf"))

    def test_current_claims_are_not_queued(self):
        self.assertEqual(collection.needs_from({"a": player(height_cm=claim("height_cm", 185))}), [])

    def test_the_reason_names_the_field_and_the_overrun(self):
        needs = collection.needs_from({"a": player(score=claim("score", "1-0", fetched_at=stamp(days=30)))})
        self.assertIn("score", needs[0].reason)
        self.assertIn("时效", needs[0].reason)

    def test_a_subject_with_no_claims_is_skipped(self):
        self.assertEqual(collection.needs_from({"a": {}, "b": None}), [])

    def test_a_stored_dictionary_claim_is_read_back(self):
        subjects = {"a": player(score=claim("score", "1-0", fetched_at=stamp(days=30)).safe())}
        needs = collection.needs_from(subjects)
        self.assertEqual(needs[0].field, "score")

    def test_the_limit_is_respected(self):
        subjects = {f"p{index}": player(score=claim("score", "1-0", fetched_at=stamp(days=30 + index)))
                    for index in range(20)}
        self.assertEqual(len(collection.needs_from(subjects, limit=5)), 5)


class CapabilityTests(unittest.TestCase):
    def test_each_field_maps_to_the_source_that_can_answer_it(self):
        self.assertEqual(collection._capability_for("birth_date"), "person")
        self.assertEqual(collection._capability_for("photo_url"), "image")
        self.assertEqual(collection._capability_for("roster"), "squad")
        self.assertEqual(collection._capability_for("score"), "fixture")
        self.assertEqual(collection._capability_for("standings"), "standings")
        self.assertEqual(collection._capability_for("some_novel_field"), "news")

    def test_a_need_carries_the_capability_and_the_tier(self):
        needs = collection.needs_from({"a": player(photo_url=claim("photo_url", "https://x/y.png",
                                                                  fetched_at="", source_id="thesportsdb",
                                                                  tier=SourceTier.COMMUNITY))})
        self.assertEqual(needs[0].capability, "image")
        self.assertEqual(needs[0].tier, "community")


class BudgetTests(unittest.TestCase):
    def needs(self, count=5):
        return [collection.Need(f"p{index}", "score", float(count - index), "过时效")
                for index in range(count)]

    def test_the_budget_bounds_the_plan(self):
        self.assertEqual(len(collection.plan(self.needs(), budget=0).needs), 0)

    def test_an_unaffordable_need_is_reported_rather_than_hidden(self):
        plan = collection.plan(self.needs(5), budget=1)
        self.assertTrue(plan.needs)
        self.assertTrue(plan.skipped)
        self.assertIn("预算", plan.skipped[0]["skipped_because"])

    def test_one_expensive_item_does_not_stop_the_cheaper_ones_behind_it(self):
        # An unmetered source answering a later need must still be reached.
        needs = [collection.Need("a", "score", 9.0, "过时效", capability="news"),
                 collection.Need("b", "birth_date", 8.0, "过时效", capability="person")]
        plan = collection.plan(needs, budget=2)
        self.assertEqual({need.subject_id for need in plan.needs}, {"a", "b"})

    def test_the_plan_accounts_for_what_it_spent(self):
        plan = collection.plan(self.needs(3), budget=10)
        self.assertEqual(plan.spent, 3)

    def test_the_plan_takes_the_most_urgent_first(self):
        plan = collection.plan(self.needs(3), budget=10)
        self.assertEqual([need.overdue_ratio for need in plan.needs], [3.0, 2.0, 1.0])

    def test_an_unsorted_list_is_ordered_by_the_plan(self):
        needs = [collection.Need("late", "score", 1.0, "过时效"),
                 collection.Need("early", "score", 9.0, "过时效")]
        plan = collection.plan(needs, budget=10)
        self.assertEqual([need.subject_id for need in plan.needs], ["early", "late"])


class SummaryTests(unittest.TestCase):
    def test_an_empty_plan_says_so_plainly(self):
        self.assertIn("无需更新", collection.summary(collection.Plan()))

    def test_an_undated_headline_does_not_print_infinity(self):
        needs = collection.needs_from({"a": player(photo_url=claim("photo_url", "https://x/y.png", fetched_at=""))})
        text = collection.summary(collection.plan(needs))
        self.assertNotIn("inf", text)
        self.assertIn("未提供时间", text)

    def test_a_normal_plan_names_the_top_item(self):
        needs = collection.needs_from({"a": player(score=claim("score", "1-0", fetched_at=stamp(days=30)))})
        text = collection.summary(collection.plan(needs))
        self.assertIn("score", text)


class EndToEndTests(unittest.TestCase):
    def test_next_plan_goes_from_stored_claims_to_a_budgeted_plan(self):
        subjects = {"player_a": player(birth_date=claim("birth_date", "2000-01-01", fetched_at=stamp(days=400)),
                                       height_cm=claim("height_cm", 185, fetched_at=stamp(days=400))),
                    "match_1": player(score=claim("score", "2-1", fetched_at=stamp(days=30)))}
        plan = collection.next_plan(subjects, budget=20, limit=10)
        self.assertEqual(plan.needs[0].field, "score")
        self.assertTrue(plan.safe()["needs"])


if __name__ == "__main__":
    unittest.main()
