"""The photo batch planner: what it may decide, and what it may never decide.

Two properties matter here. The planner orders work among players the local
queue already lists, and it cannot introduce one — no field carries a photo
address, a licence or an identity claim. And a planner that is absent or broken
leaves the refresh exactly as it was.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import photo_planning, team_portraits
from server.model_config import AgentFailure, MODEL_ROLES, role_selection
from server.storage import Store
from tests.support import IsolatedAppCase


def queue_report(untried):
    return {"team_id": "chelsea", "total": 30, "available": 13, "missing": 17,
            "untried_count": len(untried), "untried": untried, "settled": [], "hint": None}


def player(player_id, name="球员"):
    return {"player_id": player_id, "name": name, "position_group": "DF",
            "has_nationality": True, "has_birth_date": True, "reason_code": "not_yet_resolved"}


def tool_reply(player_ids):
    return {"content": "", "tool_calls": [{"function": {"name": "submit_photo_batch",
                                                        "arguments": json.dumps({"player_ids": player_ids})}}]}


class BatchLimitTests(unittest.TestCase):
    def test_no_deadline_keeps_the_historical_ceiling(self):
        self.assertEqual(team_portraits.batch_limit(None), team_portraits.PLAYER_PAGE_LIMIT)

    def test_the_batch_shrinks_with_the_time_left(self):
        # 6.5s per player: a minute fits nine, and the ceiling still caps it.
        self.assertEqual(team_portraits.batch_limit(60), 9)
        self.assertEqual(team_portraits.batch_limit(600), team_portraits.PLAYER_PAGE_LIMIT)

    def test_a_pass_always_tries_at_least_one_player(self):
        # An expired budget must not turn into a silently empty pass.
        self.assertEqual(team_portraits.batch_limit(0), 1)
        self.assertEqual(team_portraits.batch_limit(-5), 1)


class PlannerConfigTests(IsolatedAppCase):
    def test_the_role_is_unassigned_by_default(self):
        self.assertIn("photos", MODEL_ROLES)
        self.assertIsNone(role_selection(self.app.state.store, "photos"))
        self.assertIsNone(photo_planning.planner_config(self.app.state.store))

    def test_photo_planning_never_inherits_the_active_model(self):
        # A reader who configured a chat model did not ask for a paid planning
        # call on every team refresh.
        from server import model_config
        store = self.app.state.store
        store.set_meta("active_model", {"provider_id": "p", "model": "m"})
        self.assertIsNone(model_config.role_selection(store, "photos"))
        # Verification keeps the same protection, and the writing roles do not.
        self.assertIsNone(model_config.role_selection(store, "verify"))
        self.assertEqual(model_config.role_selection(store, "chat"), {"provider_id": "p", "model": "m"})

    def test_a_removed_provider_yields_no_planner_rather_than_an_error(self):
        store = self.app.state.store
        store.set_meta("model_roles", {"photos": {"provider_id": "gone", "model": "m"}})
        # The role is assigned, but its provider no longer exists: the refresh
        # must fall back to the cursor order instead of failing.
        self.assertIsNone(photo_planning.planner_config(store))


class PlannerRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_planner_only_ever_sees_missing_players(self):
        sent = {}

        async def requester(config, messages, duration):
            sent["payload"] = json.loads(messages[1]["content"])
            sent["tools"] = config
            return tool_reply(["2"])

        report = queue_report([player("1"), player("2")])
        with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
            order = await photo_planning.ordered_batch(object(), report, requester=requester)
        self.assertEqual(order, ["2"])
        fields = set(sent["payload"]["players"][0])
        self.assertEqual(fields, {"player_id", "name", "position_group", "has_nationality", "has_birth_date"})

    async def test_an_id_outside_the_queue_is_dropped(self):
        async def requester(config, messages, duration):
            # A model may only order existing work; an invented id is not work.
            return tool_reply(["999", "1"])

        report = queue_report([player("1"), player("2")])
        with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
            order = await photo_planning.ordered_batch(object(), report, requester=requester)
        self.assertEqual(order, ["1"])

    async def test_an_order_with_no_usable_id_falls_back_to_the_cursor(self):
        async def requester(config, messages, duration):
            return tool_reply(["999"])

        report = queue_report([player("1")])
        with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
            self.assertIsNone(await photo_planning.ordered_batch(object(), report, requester=requester))

    async def test_surplus_ids_beyond_one_batch_are_refused(self):
        async def requester(config, messages, duration):
            return tool_reply([str(index) for index in range(1, 20)])

        report = queue_report([player(str(index)) for index in range(1, 20)])
        with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
            self.assertIsNone(await photo_planning.ordered_batch(object(), report, requester=requester))

    async def test_a_broken_reply_never_stops_the_refresh(self):
        for reply in ({"content": "no tool call"}, {"tool_calls": []},
                      {"tool_calls": [{"function": {"name": "submit_photo_batch", "arguments": "{not json"}}]}):
            with self.subTest(reply=reply):
                async def requester(config, messages, duration, reply=reply):
                    return reply

                report = queue_report([player("1")])
                with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
                    self.assertIsNone(await photo_planning.ordered_batch(object(), report, requester=requester))

    async def test_a_failing_service_leaves_the_order_unchanged(self):
        async def requester(config, messages, duration):
            raise AgentFailure("服务不可用")

        report = queue_report([player("1")])
        with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
            self.assertIsNone(await photo_planning.ordered_batch(object(), report, requester=requester))

    async def test_no_model_assigned_means_no_request_at_all(self):
        calls = []

        async def requester(config, messages, duration):
            calls.append(config)
            return tool_reply(["1"])

        report = queue_report([player("1")])
        with patch.object(photo_planning, "planner_config", return_value=None):
            self.assertIsNone(await photo_planning.ordered_batch(object(), report, requester=requester))
        self.assertEqual(calls, [])

    async def test_an_empty_queue_needs_no_model_call(self):
        calls = []

        async def requester(config, messages, duration):
            calls.append(config)
            return tool_reply([])

        with patch.object(photo_planning, "planner_config", return_value={"model": "m"}):
            self.assertEqual(await photo_planning.ordered_batch(object(), queue_report([]), requester=requester), [])
        self.assertEqual(calls, [])


class OrderedSelectionTests(unittest.TestCase):
    def roster(self):
        return [{"id": str(index), "provider_id": str(100 + index)} for index in range(1, 5)]

    def test_the_order_reorders_without_dropping_anyone(self):
        roster = self.roster()
        # The order is a preference among real work, not a filter that hides
        # the rest of the squad.
        ordered = team_portraits._ordered_selection(roster, ["3", "1"], roster)
        self.assertEqual([row["id"] for row in ordered], ["3", "1", "2", "4"])

    def test_a_player_outside_the_roster_is_refused(self):
        roster = self.roster()
        with self.assertRaises(Exception):
            team_portraits._ordered_selection(roster, ["999"], roster)

    def test_an_explicit_order_does_not_advance_the_cursor(self):
        # It is a selection, not a rotation, so the next default pass must not
        # resume from the wrong offset.
        import inspect
        source = inspect.getsource(team_portraits._enrich_wikimedia_photos)
        self.assertIn("if not explicit_selection and requested:", source)


class PhotoQueueTests(IsolatedAppCase):
    def snapshot(self, rows):
        return {"team_id": "chelsea", "season_id": "PL-2026", "roster": rows}

    def row(self, player_id, **changes):
        return {"id": player_id, "provider_id": str(1000 + int(player_id)), "name": f"球员{player_id}",
                "position_group": "MF", "nationality": "英格兰", "birth_date": "1998-01-01", **changes}

    def test_the_queue_separates_untried_from_settled_work(self):
        rows = [self.row("1", photo_url="https://example.test/a.png"),
                self.row("2"),
                self.row("3", reason_code="community_missing", retryable=False),
                self.row("4", reason_code="source_withdrawn", retryable=True)]
        report = team_portraits.photo_queue(self.app.state.store, self.snapshot(rows))
        self.assertEqual((report["total"], report["available"], report["missing"]), (4, 1, 3))
        self.assertEqual({entry["player_id"] for entry in report["untried"]}, {"1002", "1004"})
        self.assertEqual({entry["player_id"] for entry in report["settled"]}, {"1003"})

    def test_a_player_missing_both_identity_facts_is_surfaced_separately(self):
        # Neither source can match without a name anchor, so asking again will
        # not help; the count says so instead of hiding them among the untried.
        rows = [self.row("2", nationality="", birth_date=""), self.row("3")]
        report = team_portraits.photo_queue(self.app.state.store, self.snapshot(rows))
        self.assertEqual(report["no_identity_facts_count"], 1)
        self.assertIn("maintain_team_data", report["hint"])

    def test_the_report_never_carries_a_photo_or_identity_evidence(self):
        rows = [self.row("2", photo_credit="某摄影师", photo_source_url="https://example.test/secret")]
        report = team_portraits.photo_queue(self.app.state.store, self.snapshot(rows))
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("某摄影师", rendered)
        for entry in report["untried"]:
            self.assertNotIn("photo_url", entry)
            self.assertNotIn("birth_date", entry)

    def test_a_recorded_cursor_is_reported_without_advancing_it(self):
        from server.team_center import _snapshot_id  # noqa: F401  (import proves the key shape is shared)
        store = self.app.state.store
        before = store.get_meta("wikimedia_cursor_v1_chelsea_PL-2026", {})
        report = team_portraits.photo_queue(store, self.snapshot([self.row("2")]))
        self.assertEqual(report["cursors"]["wikimedia"], {"next": 0, "size": 1})
        self.assertEqual(store.get_meta("wikimedia_cursor_v1_chelsea_PL-2026", {}), before)


if __name__ == "__main__":
    unittest.main()


class NamesakeQueueTests(unittest.TestCase):
    """The queue has to distinguish "blocked by a namesake" from "no record".

    Both are settled against the community source, and neither is worth
    retrying against it. They differ in what the caller should do next: a
    namesake block means another source can still answer, while an absent
    record means this player is simply not in the catalogue.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "queue.sqlite3")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def snapshot(self, rows):
        return {"team_id": "chelsea", "season_id": "PL-2026", "roster": rows}

    def test_a_namesake_row_is_settled_and_named_as_such(self):
        report = team_portraits.photo_queue(self.store, self.snapshot([
            {"provider_id": "189007", "name": "Reece James", "photo_url": "",
             "reason_code": "community_ambiguous", "retryable": False, "namesake_blocked": True},
        ]))
        self.assertEqual(report["settled_count"], 1)
        self.assertEqual(report["untried_count"], 0)
        entry = report["settled"][0]
        self.assertTrue(entry["namesake_blocked"])
        self.assertEqual(entry["reason_code"], "community_namesake_blocked")

    def test_an_ordinary_ambiguity_keeps_its_own_code(self):
        report = team_portraits.photo_queue(self.store, self.snapshot([
            {"provider_id": "1", "name": "Two Namesakes", "photo_url": "",
             "reason_code": "community_ambiguous", "retryable": False},
        ]))
        entry = report["settled"][0]
        self.assertNotIn("namesake_blocked", entry)
        self.assertEqual(entry["reason_code"], "community_ambiguous")

    def test_the_queue_still_carries_no_photo_address(self):
        report = team_portraits.photo_queue(self.store, self.snapshot([
            {"provider_id": "1", "name": "X", "photo_url": "",
             "reason_code": "community_ambiguous", "retryable": False, "namesake_blocked": True},
        ]))
        blob = json.dumps(report)
        self.assertNotIn("http", blob)
        self.assertNotIn("photo_url", blob)
