"""The review loop and its photo domain: what it may decide, never write.

Three properties matter here.

* Only photos that already carry a source may be reviewed. The review judges a
  picture the sources proved; admitting an unsourced row would let a model's
  answer stand in for the provenance check it never made.
* A decision is a next action, not a write. Nothing in these modules touches a
  player field, and "retry" is not permission to delete a photo.
* The bytes are untrusted. A source's Content-Type is a claim and the file's own
  magic number is the answer; a mismatch is refused rather than base64'd into a
  paid request.
"""
import json
import unittest
from unittest.mock import patch

from server import photo_review, review_loop, review_records
from server.ingestion import IngestionError
from server.provider import IMAGE_MEDIA_TYPES, image_block, make_payload

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def row(player_id, name="球员", match="community_name_and_birthdate"):
    return {"id": player_id, "name": name, "photo_url": f"https://example.test/{player_id}.png",
            "photo_source_url": "https://example.test/player", "photo_match": match,
            "photo_credit": "来源", "photo_license": "CC BY-SA 4.0"}


def reply(items):
    return {"content": "", "tool_calls": [{"function": {"name": "submit_review",
                                                        "arguments": json.dumps({"items": items})}}]}


def fake_download(data=PNG, error=None):
    async def download(url, *, allowed_hosts=None):
        if error is not None:
            raise error
        return data, url, "image/png"
    return download


class MediaTypeTests(unittest.TestCase):
    def test_the_file_decides_the_type_not_the_header(self):
        self.assertEqual(photo_review.media_type_of(PNG), "image/png")
        self.assertEqual(photo_review.media_type_of(JPEG), "image/jpeg")
        self.assertEqual(photo_review.media_type_of(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")

    def test_a_riff_file_that_is_not_webp_is_not_an_image(self):
        # A WAV file also starts with RIFF; only the form field separates them.
        self.assertEqual(photo_review.media_type_of(b"RIFF\x00\x00\x00\x00WAVEfmt "), "")

    def test_an_error_page_is_never_forwarded_as_a_portrait(self):
        for payload in (b"<html>403 Forbidden</html>", b"", b'{"error":"nope"}'):
            self.assertEqual(photo_review.media_type_of(payload), "")

    def test_only_the_three_negotiable_types_are_accepted(self):
        self.assertEqual(IMAGE_MEDIA_TYPES, frozenset({"image/jpeg", "image/png", "image/webp"}))


class EligibilityTests(unittest.TestCase):
    def test_only_sourced_photos_enter_the_review(self):
        players = [row("1"), {**row("2"), "photo_source_url": "", "photo_file_page": ""},
                   {**row("3"), "photo_url": ""}, {**row("4"), "photo_match": None},
                   {**row("5"), "photo_url": "http://insecure.test/5.png"}]
        self.assertEqual([item["id"] for item in photo_review.reviewable(players)], ["1"])

    def test_a_file_page_stands_in_when_the_source_url_is_absent(self):
        players = [{**row("1"), "photo_source_url": "", "photo_file_page": "https://commons.test/File:1"}]
        self.assertEqual([item["id"] for item in photo_review.reviewable(players)], ["1"])

    def test_a_source_that_answered_is_not_offered_again(self):
        # The acceptance report is code's own attribution: a source gap and a
        # settled refusal are answers, not work, so they never reach the board.
        acceptance = {"retryable": [{"player_id": "9", "name": "甲", "reason_code": "timeout"}],
                      "source_gaps": [{"player_id": "8", "name": "乙", "reason_code": "community_missing"}]}
        items = review_records.name_items([])
        self.assertEqual(items, [])
        self.assertIn("retryable", acceptance)


class ReviewRequestTests(unittest.IsolatedAsyncioTestCase):
    async def review(self, players, response, *, acceptance=None, download=None, requester=None):
        async def default_requester(config, messages, duration, session):
            return response
        with patch.object(review_loop, "review_config", return_value={"model": "m"}), \
                patch.object(photo_review, "_download", download or fake_download()):
            return await photo_review.review_photos(object(), players, acceptance=acceptance,
                                                    requester=requester or default_requester)

    async def test_the_request_carries_the_images_and_never_an_address(self):
        sent = {}

        async def requester(config, messages, duration, session):
            sent["payload"] = json.loads(messages[1]["content"])
            sent["parts"] = messages[1]["_images"]
            return reply([{"item_id": "1", "action": "keep", "reason": "清晰"}])

        reviews, notes = await self.review([row("1")], None, requester=requester)
        self.assertEqual([item["action"] for item in reviews], ["keep"])
        self.assertEqual(reviews[0]["reason"], "清晰")
        self.assertEqual(notes, [])
        # The model sees an index and a name -- no URL, no licence, no credit.
        self.assertEqual(set(sent["payload"]["items"][0]),
                         {"item_id", "domain", "name", "state", "reason_code", "detail", "attempts", "image_index"})
        self.assertNotIn("example.test", json.dumps(sent["payload"]))
        self.assertNotIn("CC BY", json.dumps(sent["payload"]))
        self.assertEqual(sent["parts"][0]["media_type"], "image/png")

    async def test_the_board_carries_code_s_own_attribution_for_the_gaps(self):
        sent = {}

        async def requester(config, messages, duration, session):
            sent["payload"] = json.loads(messages[1]["content"])
            return reply([{"item_id": "9", "action": "retry", "retry_hint": "换时间"}])

        acceptance = {"retryable": [{"player_id": "9", "name": "甲", "reason_code": "timeout"}],
                      "namesake_blocked": [{"player_id": "7", "name": "丙", "reason_code": "community_namesake_blocked"}]}
        reviews, _ = await self.review([row("1")], None, acceptance=acceptance, requester=requester)
        gaps = {item["item_id"]: item for item in sent["payload"]["items"] if item["domain"] == "photo_gap"}
        self.assertEqual(gaps["9"]["state"], "retryable")
        self.assertEqual(gaps["9"]["reason_code"], "timeout")
        self.assertEqual(gaps["7"]["state"], "namesake_blocked")
        self.assertIsNone(gaps["9"]["image_index"], "a gap carries no picture to index")
        self.assertEqual([item["item_id"] for item in reviews], ["9"])
        self.assertEqual(reviews[0]["retry_hint"], "换时间")

    async def test_a_decision_about_an_item_outside_the_board_is_dropped(self):
        reviews, _ = await self.review([row("1")], reply([
            {"item_id": "999", "action": "retry"}, {"item_id": "1", "action": "keep"}]))
        self.assertEqual([item["item_id"] for item in reviews], ["1"])

    async def test_an_unknown_action_is_refused_rather_than_stored(self):
        reviews, _ = await self.review([row("1")], reply([{"item_id": "1", "action": "delete_it"}]))
        self.assertEqual(reviews, [])

    async def test_an_unknown_effort_falls_back_rather_than_failing_the_item(self):
        # The action is the decision; the effort is a hint. A hint that cannot be
        # read must not discard the decision it came with.
        reviews, _ = await self.review([row("1")], reply([{"item_id": "1", "action": "retry", "effort": "whenever"}]))
        self.assertEqual(reviews[0]["effort"], review_loop.EFFORT_LATER)

    async def test_more_decisions_than_the_board_are_refused_whole(self):
        players = [row(str(index)) for index in range(1, 15)]
        reviews, _ = await self.review(players, reply([{"item_id": str(index), "action": "keep"}
                                                       for index in range(1, 15)]))
        self.assertEqual(reviews, [])

    async def test_a_transport_fault_reads_as_unreadable_not_as_a_decision(self):
        # Turning a network error into "this picture is bad" would delete a good
        # photo over a timeout.
        reviews, notes = await self.review([row("1")], reply([]),
                                           download=fake_download(error=IngestionError("来源请求超时，请稍后重试")))
        self.assertEqual(reviews, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("超时", notes[0])

    async def test_a_non_image_response_is_a_note_not_a_request(self):
        requested = {"count": 0}

        async def requester(config, messages, duration, session):
            requested["count"] += 1
            return reply([])

        with patch.object(review_loop, "review_config", return_value={"model": "m"}), \
                patch.object(photo_review, "_download", fake_download(b"<html>403</html>")):
            reviews, notes = await photo_review.review_photos(object(), [row("1")], requester=requester)
        self.assertEqual(requested["count"], 0, "no paid call for bytes that are not an image")
        self.assertEqual(reviews, [])
        self.assertIn("格式", notes[0])

    async def test_a_gap_still_gets_a_round_when_no_picture_could_be_read(self):
        # The retry decision does not need an image. Letting an unreadable photo
        # cancel the whole round would drop the part of the review that is still
        # answerable.
        asked = {"count": 0}

        async def requester(config, messages, duration, session):
            asked["count"] += 1
            return reply([{"item_id": "9", "action": "retry"}])

        acceptance = {"retryable": [{"player_id": "9", "name": "甲", "reason_code": "timeout"}]}
        with patch.object(review_loop, "review_config", return_value={"model": "m"}), \
                patch.object(photo_review, "_download", fake_download(b"<html>403</html>")):
            reviews, notes = await photo_review.review_photos(object(), [row("1")], acceptance=acceptance,
                                                              requester=requester)
        self.assertEqual(asked["count"], 1)
        self.assertEqual([item["item_id"] for item in reviews], ["9"])
        self.assertEqual(len(notes), 1)

    async def test_no_model_assigned_means_no_request_and_no_download(self):
        async def download(url, *, allowed_hosts=None):
            raise AssertionError("the network must not be touched without a model")

        with patch.object(review_loop, "review_config", return_value=None), \
                patch.object(photo_review, "_download", download):
            reviews, notes = await photo_review.review_photos(object(), [row("1")])
        self.assertEqual((reviews, notes), ([], []))

    async def test_a_broken_reply_leaves_the_photos_untouched(self):
        reviews, _ = await self.review([row("1")], {"content": "no tool call"})
        self.assertEqual(reviews, [])

    async def test_a_failing_service_never_raises_into_the_refresh(self):
        async def requester(config, messages, duration, session):
            raise RuntimeError("service down")

        reviews, _ = await self.review([row("1")], None, requester=requester)
        self.assertEqual(reviews, [])


class WireImageTests(unittest.TestCase):
    """The three protocols must each carry the bytes in their own shape."""

    def payload(self, protocol):
        config = {"protocol": protocol, "model": "m", "base_url": "https://api.test"}
        messages = [{"role": "user", "content": "看这张图",
                     "_images": [{"media_type": "image/png", "data": "AAAA"}]}]
        return make_payload(config, messages)

    def test_anthropic_uses_a_base64_source_block(self):
        payload = self.payload("anthropic_messages")
        part = payload["messages"][0]["content"][1]
        self.assertEqual(part, {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": "AAAA"}})

    def test_responses_uses_an_input_image_part(self):
        payload = self.payload("responses")
        part = payload["input"][0]["content"][1]
        self.assertEqual(part, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"})
        self.assertEqual(payload["input"][0]["content"][0], {"type": "input_text", "text": "看这张图"})

    def test_chat_completions_replaces_content_with_a_part_list(self):
        payload = self.payload("chat_completions")
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "看这张图"})
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}})

    def test_a_text_only_message_is_unchanged_on_every_protocol(self):
        for protocol in ("anthropic_messages", "responses", "chat_completions"):
            config = {"protocol": protocol, "model": "m", "base_url": "https://api.test"}
            payload = make_payload(config, [{"role": "user", "content": "你好"}])
            wire = payload["messages"] if protocol != "responses" else payload["input"]
            self.assertEqual(wire[0]["content"], "你好" if protocol != "anthropic_messages"
                             else [{"type": "text", "text": "你好"}])

    def test_bytes_that_are_not_an_accepted_type_produce_no_block(self):
        self.assertIsNone(image_block("image/gif", b"GIF89a"))
        self.assertIsNone(image_block("image/png", b""))
        self.assertIsNone(image_block("image/png", "not bytes"))
        self.assertIsNotNone(image_block("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "))


class RecordBoardTests(unittest.IsolatedAsyncioTestCase):
    """The record domain: the four kinds of residue code cannot settle."""

    def test_a_player_with_a_reading_is_not_offered(self):
        roster = [{"id": "1", "name": "A", "name_zh": "甲"}, {"id": "2", "name": "B"}]
        self.assertEqual([item["item_id"] for item in review_records.name_items(roster)], ["name:2"])

    def test_a_durable_refusal_never_reaches_the_board(self):
        # A source that answered "no such player" is a verdict, not work.
        roster = [{"id": "1", "name": "A", "reason_code": "community_missing"},
                  {"id": "2", "name": "B", "reason_code": "license_not_free"}]
        self.assertEqual(review_records.name_items(roster), [])

    def test_a_route_failure_does_reach_the_board(self):
        roster = [{"id": "1", "name": "A", "reason_code": "http_rate_limited"}]
        items = review_records.name_items(roster)
        self.assertEqual(items[0]["item_id"], "name:1")
        self.assertEqual(items[0]["state"], "name_unresolved")
        self.assertEqual(items[0]["domain"], "name")

    def test_a_row_without_identity_facts_is_its_own_item(self):
        # Not a photo gap: the community source refuses before any request, so
        # asking again with the same evidence cannot change the answer.
        roster = [{"id": "1", "name": "A", "nationality": "巴西", "birth_date": "1999-01-01"},
                  {"id": "2", "name": "B"},
                  {"id": "3", "name": "C", "nationality": "法国", "birth_date": "2001-05-05"}]
        items = review_records.player_fact_items(roster)
        self.assertEqual([item["item_id"] for item in items], ["facts:2"])
        self.assertEqual(items[0]["state"], "no_identity_facts")
        self.assertEqual(items[0]["domain"], "player_facts")

    def test_a_partly_filled_row_names_the_field_it_is_missing(self):
        roster = [{"id": "1", "name": "A", "nationality": "巴西"}]
        items = review_records.player_fact_items(roster)
        self.assertEqual(items[0]["state"], "missing_birth_date")
        self.assertEqual(items[0]["detail"], "birth_date")

    def test_a_player_carrying_both_facts_is_not_offered(self):
        roster = [{"id": "1", "name": "A", "nationality": "巴西", "birth_date": "2001-05-05"}]
        self.assertEqual(review_records.player_fact_items(roster), [])

    def test_club_items_read_the_team_row_not_a_report(self):
        # club_profile writes collected fields onto the team record, so a gap is
        # visible there. An earlier version read a separate summary that the
        # caller never passed, which made every club item silently absent.
        items = review_records.club_items({"id": "arsenal", "name": "阿森纳", "founded": 1886})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["item_id"], "club:arsenal")
        self.assertEqual(items[0]["state"], "club_fields_missing")
        self.assertIn("主场", items[0]["detail"])
        self.assertIn("昵称", items[0]["detail"])

    def test_a_complete_club_record_offers_nothing(self):
        team = {"id": "arsenal", "founded": 1886, "stadium": "Emirates Stadium", "nickname": "The Gunners"}
        self.assertEqual(review_records.club_items(team), [])

    def test_a_crest_missing_its_attribution_is_an_item(self):
        team = {"id": "arsenal", "name": "阿森纳", "crest_url": "/assets/clubs/arsenal.svg"}
        items = review_records.crest_items(team)
        self.assertEqual(items[0]["item_id"], "crest:arsenal")
        self.assertEqual(items[0]["state"], "crest_missing")
        self.assertIn("crest_attribution", items[0]["detail"])

    def test_a_crest_pointing_off_site_is_a_different_question(self):
        team = {"id": "arsenal", "crest_url": "https://example.test/badge.png",
                "crest_source_url": "https://example.test/src", "crest_attribution": "来源"}
        items = review_records.crest_items(team)
        self.assertEqual(items[0]["state"], "crest_remote")
        self.assertEqual(items[0]["reason_code"], "remote_asset")

    def test_a_complete_local_crest_is_never_offered(self):
        # A badge's bytes are hashed into its attribution record: there is no
        # picture question left once the source and attribution are present.
        team = {"id": "arsenal", "crest_url": "/assets/clubs/arsenal.svg",
                "crest_source_url": "https://example.test/src", "crest_attribution": "来源"}
        self.assertEqual(review_records.crest_items(team), [])

    def test_the_board_spans_every_domain_without_duplicating_an_id(self):
        roster = [{"id": "1", "name": "A", "reason_code": "http_rate_limited"},
                  {"id": "2", "name": "B"}]
        team = {"id": "arsenal", "name": "阿森纳", "crest_url": "/assets/clubs/arsenal.svg"}
        items = review_records.record_items(roster, team)
        domains = {item["domain"] for item in items}
        self.assertEqual(domains, {"name", "player_facts", "club", "crest"})
        self.assertEqual(len({item["item_id"] for item in items}), len(items))

    async def test_a_record_round_needs_no_images(self):
        sent = {}

        async def requester(config, messages, duration, session):
            sent["payload"] = json.loads(messages[1]["content"])
            sent["images"] = messages[1].get("_images")
            return reply([{"item_id": "name:1", "action": "retry", "retry_hint": "换检索词"}])

        with patch.object(review_loop, "review_config", return_value={"model": "m"}):
            decisions = await review_records.review_records(
                object(), review_records.name_items([{"id": "1", "name": "A"}]), requester=requester)
        self.assertIsNone(sent["images"])
        self.assertEqual(sent["payload"]["items"][0]["domain"], "name")
        self.assertEqual(decisions[0]["retry_hint"], "换检索词")

    async def test_no_items_means_no_paid_call(self):
        async def requester(config, messages, duration, session):
            raise AssertionError("an empty board must not reach a model")

        with patch.object(review_loop, "review_config", return_value={"model": "m"}):
            decisions = await review_records.review_records(object(), [], requester=requester)
        self.assertEqual(decisions, [])


class ReviewRecordTests(unittest.IsolatedAsyncioTestCase):
    """The stored record and the line the reader sees."""

    def test_the_note_names_retry_and_investigation_separately(self):
        from server.team_center import photo_review_note
        record = {"reviews": [{"item_id": "1", "action": "retry"},
                              {"item_id": "2", "action": "investigate"},
                              {"item_id": "3", "action": "keep"}],
                  "retry_now": ["1"], "investigate": ["2"]}
        note = photo_review_note(record)
        self.assertIn("立即重试 1 项", note)
        self.assertIn("1 项需进一步核查", note)

    def test_a_fully_settled_pass_says_so(self):
        from server.team_center import photo_review_note
        note = photo_review_note({"reviews": [{"item_id": "1", "action": "keep"}],
                                  "retry_now": [], "investigate": []})
        self.assertIn("均无需动作", note)

    def test_an_empty_record_produces_no_line(self):
        from server.team_center import photo_review_note
        self.assertEqual(photo_review_note({}), "")
        self.assertEqual(photo_review_note(None), "")


if __name__ == "__main__":
    unittest.main()
