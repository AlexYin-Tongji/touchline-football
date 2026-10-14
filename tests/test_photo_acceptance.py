"""The acceptance step: are the remaining photo gaps reachable, or answered?

These pin down the three-way split that makes the loop honest — a route failure
is worth retrying, a roster gap belongs to the roster tool, and a source that
holds no such player is not a failure to retry at all.
"""
import unittest

from server import photo_acceptance


def row(name, **extra):
    return {"provider_id": str(abs(hash(name)) % 10 ** 6), "name": name, **extra}


class ClassificationTests(unittest.TestCase):
    def test_a_transport_failure_is_worth_retrying(self):
        for code in ("timeout", "dns_failed", "connection_error", "connection_reset",
                     "tls_error", "tls_certificate", "unavailable", "http_rate_limited",
                     "http_unavailable", "not_yet_resolved", "source_unavailable"):
            with self.subTest(code=code):
                self.assertEqual(photo_acceptance.classify(code), photo_acceptance.FIX_ROUTE)

    def test_a_missing_roster_fact_is_not_a_photo_pass_problem(self):
        for code in ("missing_name", "missing_nationality"):
            with self.subTest(code=code):
                self.assertEqual(photo_acceptance.classify(code), photo_acceptance.FIX_ROSTER)

    def test_a_player_the_sources_do_not_hold_is_not_a_retry(self):
        # This is the distinction the old loop could not make: "no record" and
        # "request failed" both looked like a gap. Retrying the first forever is
        # the behaviour that burned paced requests for no new answer.
        for code in ("community_missing", "community_no_image", "missing_article", "no_free_image",
                     "wikidata_no_espn_id"):
            with self.subTest(code=code):
                self.assertEqual(photo_acceptance.classify(code), photo_acceptance.FIX_SOURCE)

    def test_two_candidates_is_its_own_kind(self):
        for code in ("community_ambiguous", "ambiguous_identity"):
            with self.subTest(code=code):
                self.assertEqual(photo_acceptance.classify(code), photo_acceptance.FIX_AMBIGUOUS)

    def test_a_durable_refusal_is_settled(self):
        for code in ("license_not_free", "source_withdrawn", "rejected", "community_rejected",
                     "wikidata_birth_conflict"):
            with self.subTest(code=code):
                self.assertEqual(photo_acceptance.classify(code), photo_acceptance.FIX_NOTHING)

    def test_a_revalidation_trigger_is_retried_not_treated_as_a_verdict(self):
        self.assertEqual(photo_acceptance.classify("identity_revalidation"), photo_acceptance.FIX_ROUTE)

    def test_an_unknown_code_is_retried_rather_than_declared_settled(self):
        # Stopping on a code this module has never seen would silently abandon
        # whatever new failure the sources started reporting.
        self.assertEqual(photo_acceptance.classify("some_new_failure"), photo_acceptance.FIX_ROUTE)

    def test_an_absent_code_reads_as_never_attempted(self):
        self.assertEqual(photo_acceptance.classify(""), photo_acceptance.FIX_UNKNOWN)
        self.assertEqual(photo_acceptance.classify(None), photo_acceptance.FIX_UNKNOWN)


class AssessmentTests(unittest.TestCase):
    def test_a_complete_squad_is_accepted(self):
        report = photo_acceptance.assess([row("A", photo_url="https://x/a.png"), row("B", photo_url="https://x/b.png")])
        self.assertTrue(report["accepted"])
        self.assertEqual(report["coverage"], 1.0)
        self.assertEqual(report["counts"][photo_acceptance.FIX_ROUTE], 0)

    def test_a_route_failure_makes_the_squad_unaccepted(self):
        report = photo_acceptance.assess([row("A", photo_url="https://x/a.png"),
                                          row("B", reason_code="timeout", retryable=True)])
        self.assertFalse(report["accepted"])
        self.assertEqual(report["actionable_count"], 1)
        self.assertEqual(report["retryable"][0]["name"], "B")

    def test_a_community_record_with_no_image_is_not_a_route_failure(self):
        # The source answered. The file is absent. Treating that as a transport
        # failure kept the pass "unaccepted" and invited another identical ask.
        report = photo_acceptance.assess([row("A", reason_code="community_no_image", retryable=False)])
        self.assertEqual(report["actionable_count"], 0)
        self.assertEqual(report["counts"][photo_acceptance.FIX_SOURCE], 1)
        self.assertTrue(report["accepted"])

    def test_a_namesake_flag_is_not_reported_as_an_ambiguous_retry(self):
        report = photo_acceptance.assess([row(
            "Reece James", reason_code="community_ambiguous", retryable=False, namesake_blocked=True)])
        self.assertEqual(report["counts"][photo_acceptance.FIX_NAMESAKE], 1)
        self.assertEqual(report["counts"][photo_acceptance.FIX_AMBIGUOUS], 0)
        self.assertEqual(report["actionable_count"], 0)
        self.assertEqual(photo_acceptance.shortfall(report, threshold=1.0), [])

    def test_a_source_gap_alone_does_not_make_the_squad_unaccepted(self):
        # Nothing failed and nothing is retryable; the database simply does not
        # hold these players. Reporting that as unfinished work would keep a
        # loop running against a question that has already been answered.
        report = photo_acceptance.assess([row("A", photo_url="https://x/a.png"),
                                          row("B", reason_code="community_missing"),
                                          row("C", reason_code="missing_article")])
        self.assertTrue(report["accepted"])
        self.assertEqual(report["actionable_count"], 0)
        self.assertEqual(len(report["source_gaps"]), 2)

    def test_the_kinds_are_reported_separately(self):
        report = photo_acceptance.assess([
            row("ok", photo_url="https://x/a.png"),
            row("route", reason_code="timeout"),
            row("needs", reason_code="missing_nationality"),
            row("absent", reason_code="community_missing"),
            row("two", reason_code="community_ambiguous"),
            row("refused", reason_code="license_not_free"),
            row("fresh", retryable=True),
        ])
        counts = report["counts"]
        self.assertEqual(counts[photo_acceptance.FIX_ROUTE], 2)      # timeout + retryable
        self.assertEqual(counts[photo_acceptance.FIX_ROSTER], 1)
        self.assertEqual(counts[photo_acceptance.FIX_SOURCE], 1)
        self.assertEqual(counts[photo_acceptance.FIX_AMBIGUOUS], 1)
        self.assertEqual(counts[photo_acceptance.FIX_NOTHING], 1)
        self.assertEqual(report["total"], 7)
        self.assertEqual(report["with_photo"], 1)
        self.assertEqual(report["missing"], 6)

    def test_a_row_with_a_retryable_flag_and_no_code_counts_as_retryable(self):
        report = photo_acceptance.assess([row("A", retryable=True)])
        self.assertEqual(report["counts"][photo_acceptance.FIX_ROUTE], 1)

    def test_an_empty_or_broken_roster_is_not_an_error(self):
        for roster in ([], None, ["not a dict"], [{}]):
            with self.subTest(roster=roster):
                report = photo_acceptance.assess(roster)
                self.assertTrue(report["accepted"])
                self.assertEqual(report["counts"][photo_acceptance.FIX_ROUTE], 0)

    def test_the_report_carries_no_photo_or_identity_payload(self):
        # A caller may decide who to ask about, never what the answer is.
        report = photo_acceptance.assess([row("A", reason_code="timeout", photo_url="")])
        entry = report["retryable"][0]
        self.assertEqual(set(entry), {"player_id", "name", "reason_code"})


class ShortfallTests(unittest.TestCase):
    def test_the_planner_is_offered_retryable_and_ambiguous_entries_only(self):
        report = photo_acceptance.assess([
            row("route", reason_code="timeout"),
            row("two", reason_code="community_ambiguous"),
            row("absent", reason_code="community_missing"),
            row("needs", reason_code="missing_nationality"),
        ])
        offered = [entry["name"] for entry in photo_acceptance.shortfall(report, threshold=1.0)]
        self.assertEqual(offered, ["route", "two"])

    def test_coverage_at_or_above_the_threshold_offers_nothing(self):
        rows = [row(f"p{index}", photo_url="https://x/p.png") for index in range(9)]
        rows.append(row("route", reason_code="timeout"))
        report = photo_acceptance.assess(rows)
        self.assertEqual(report["coverage"], 0.9)
        self.assertEqual(photo_acceptance.shortfall(report), [])


class ContinuationTests(unittest.TestCase):
    def test_nothing_actionable_stops_the_loop(self):
        report = photo_acceptance.assess([row("A", reason_code="community_missing")])
        self.assertFalse(photo_acceptance.should_continue(report))

    def test_a_route_failure_alone_continues(self):
        report = photo_acceptance.assess([row("A", reason_code="timeout")])
        self.assertTrue(photo_acceptance.should_continue(report))

    def test_a_pass_that_gained_nothing_and_cleared_nothing_stops(self):
        # Same coverage, same outstanding work: the next pass would repeat it.
        before = photo_acceptance.assess([row("A", reason_code="timeout")])
        after = photo_acceptance.assess([row("A", reason_code="timeout")])
        self.assertFalse(photo_acceptance.should_continue(after, before))

    def test_a_pass_that_gained_a_photo_continues(self):
        before = photo_acceptance.assess([row("A", reason_code="timeout"), row("B", reason_code="timeout")])
        after = photo_acceptance.assess([row("A", photo_url="https://x/a.png"), row("B", reason_code="timeout")])
        self.assertTrue(photo_acceptance.should_continue(after, before))

    def test_the_attempt_budget_is_a_hard_stop(self):
        report = photo_acceptance.assess([row("A", reason_code="timeout")])
        self.assertTrue(photo_acceptance.should_continue(report, attempts=0, max_attempts=3))
        self.assertFalse(photo_acceptance.should_continue(report, attempts=3, max_attempts=3))


class SummaryTests(unittest.TestCase):
    def test_the_note_names_each_kind_that_is_present(self):
        report = photo_acceptance.assess([
            row("route", reason_code="timeout"),
            row("absent", reason_code="community_missing"),
        ])
        note = photo_acceptance.summarize(report)
        self.assertIn("照片来源本轮未读取成功", note)
        self.assertIn("没有记录", note)

    def test_a_finished_squad_says_so(self):
        report = photo_acceptance.assess([row("A", photo_url="https://x/a.png")])
        self.assertEqual(photo_acceptance.summarize(report), "全部球员照片已通过核验。")

    def test_the_note_never_promises_a_fix_that_retrying_cannot_deliver(self):
        # A player the sources do not hold is not "pending"; saying so is the
        # point of distinguishing it from a failure.
        report = photo_acceptance.assess([row("A", reason_code="community_missing")])
        note = photo_acceptance.summarize(report)
        self.assertNotIn("重试", note)
        self.assertNotIn("稍后", note)


if __name__ == "__main__":
    unittest.main()
