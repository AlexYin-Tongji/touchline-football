"""Source-integrity and delivery regressions, with an explicit labelled checker.

The model stub exercises the gate, not real-world model accuracy. The semantic
examples are human-written fixtures; live source/model audits are separate.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from server import agent, verification
from server.agent_tools import LocalTools, ToolError, evidence_passages
from server.provider import make_payload
from tests.test_agent import FakeStore, tool_message
from tests.test_agent_delivery import submission


def quoted_submission():
    result = submission()
    result["facts"][0]["quotes"] = [{"evidence_id": "ev_injury",
        "quote": "Medical update: the midfielder has an injury and is unavailable for training."}]
    return result


def checker_result(**verdicts):
    return tool_message("report_source_support", {"claims": [
        {"id": key, "verdict": verdicts.get(key, "supported"), "reason": "逐项与给定原文比较的简短核对结果。"}
        for key in ("fact_1", "summary", "analysis", "unknowns")]})


class QuoteIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.run = self.store.new_run()
        self.local = LocalTools(self.store, self.run, self.store.settings())
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})

    def test_only_nfc_and_whitespace_normalization_allowed(self):
        value = quoted_submission()
        value["facts"][0]["quotes"][0]["quote"] = "Medical\u00a0update: the midfielder\n has an injury and is unavailable for training."
        self.assertIsNotNone(verification.prepare_claims(self.local, value)[1])
        for quote in ("the midfielder has an injury and is available for training.",
                      "中场受伤且不能训练。", "Medical update: ... unavailable for training.",
                      "medical update: the midfielder has an injury and is unavailable for training."):
            value["facts"][0]["quotes"][0]["quote"] = quote
            self.assertIsNone(verification.prepare_claims(self.local, value)[1], quote)
        self.assertNotEqual(verification.normalized("①"), verification.normalized("1"))
        self.assertEqual(verification.normalized("Cafe\u0301"), verification.normalized("Café"))

    def test_unread_foreign_changed_and_missing_quotes_rejected(self):
        for eid in ("ev_other_team", "ev_demo", "ev_result", "ev_transfer"):
            value = quoted_submission()
            value["facts"][0]["evidence_ids"] = [eid]
            value["facts"][0]["quotes"][0]["evidence_id"] = eid
            self.assertIsNone(verification.prepare_claims(self.local, value)[1])
        self.assertIsNone(verification.prepare_claims(self.local, submission())[1])
        value = self.store.get("evidence", "ev_injury")
        value["text"] = "The updated article no longer contains that statement."
        self.store.put("evidence", value)
        self.assertIsNone(verification.prepare_claims(self.local, quoted_submission())[1])

    def test_quote_must_be_within_read_truncation(self):
        value = self.store.get("evidence", "ev_injury")
        value["text"] = "A" * 12000 + " Never read original sentence."
        self.store.put("evidence", value)
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        candidate = quoted_submission()
        candidate["facts"][0]["quotes"][0]["quote"] = "Never read original sentence."
        self.assertIsNone(verification.prepare_claims(self.local, candidate)[1])

    def test_redacted_score_paragraph_cannot_be_quoted(self):
        value = self.store.get("evidence", "ev_injury")
        safe = "Medical update: the midfielder has an injury and is unavailable for training. The club will provide a further update after the next training session and the medical assessment."
        value.update(origin="web", spoiler=True, title="Training update", url="https://example.test/news/training",
                     text=safe + "\n\nThe match ended 3-1 and the team won at home.")
        self.store.put("evidence", value)
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        self.assertIsNotNone(verification.prepare_claims(self.local, quoted_submission())[1])
        candidate = quoted_submission()
        candidate["facts"][0]["quotes"][0]["quote"] = "The match ended 3-1 and the team won at home."
        self.assertIsNone(verification.prepare_claims(self.local, candidate)[1])

    def test_preview_rechecks_time_and_review_rechecks_match_links(self):
        for skill in ("match_preview", "match_review"):
            local = LocalTools(self.store, dict(self.run, skill=skill, match_id="match_one"), self.store.settings())
            local.call("read_evidence", {"evidence_id": "ev_injury"})
            if skill == "match_preview":
                value = self.store.get("evidence", "ev_injury")
                value["fetched_at"] = "2026-09-02T09:00:00Z"
                self.store.put("evidence", value)
            else:
                match = self.store.get("matches", "match_one")
                match["evidence_ids"] = []
                self.store.put("matches", match)
            self.assertIsNone(verification.prepare_claims(local, quoted_submission())[1])

    def test_redacted_quote_cannot_join_across_hidden_paragraph(self):
        value = self.store.get("evidence", "ev_injury")
        first = "Medical update: the midfielder has an injury and is unavailable for training. The club will provide another update following the assessment tomorrow."
        second = "The medical team is continuing to assess the condition and will not set an exact return date before receiving the next assessment."
        value.update(origin="web", spoiler=True, title="Training update", url="https://example.test/news/training",
                     text=first + "\n\nThe match ended 3-1 and the team won at home.\n\n" + second)
        self.store.put("evidence", value)
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        candidate = quoted_submission()
        candidate["facts"][0]["quotes"][0]["quote"] = "assessment tomorrow. The medical team is continuing"
        self.assertIsNone(verification.prepare_claims(self.local, candidate)[1])

    def test_multiple_captures_have_same_source_document_group(self):
        source = self.store.get("evidence", "ev_injury")
        source["url"] = "https://example.test/news/training"
        self.store.put("evidence", source)
        self.store.put("evidence", dict(source, id="ev_again"))
        for eid in ("ev_injury", "ev_again"):
            self.local.call("read_evidence", {"evidence_id": eid})
        value = quoted_submission()
        value["facts"][0]["evidence_ids"].append("ev_again")
        value["facts"][0]["quotes"].append({"evidence_id": "ev_again", "quote": source["text"]})
        _, payload = verification.prepare_claims(self.local, value)
        self.assertEqual(len(payload["sources"]), 2)
        self.assertEqual(len({row["source_document_group"] for row in payload["sources"]}), 1)

    def test_live_failure_shape_partial_view_cannot_claim_full_article_has_no_result(self):
        # Regression from the actual V3 live answer: the model and independent
        # checker both accepted “全文未提…赛果 / 文章不含比赛赛果” from a redacted view.
        value = quoted_submission()
        value["summary"] = "球队已做好准备；全文未提伤病、首发或赛果。"
        value["unknowns"] = ["文章不含比赛赛果或进程信息；避剧透视图下相关段落已隐藏。"]
        for partial_field in ("spoiler_redacted", "truncated"):
            self.local.read_cache["ev_injury"][partial_field] = True
            claims, payload = verification.prepare_claims(self.local, value)
            self.assertIsNotNone(payload, "valid fact can still be independently checked")
            checked = {claim["id"]: claim for claim in claims}
            self.assertEqual(checked["summary"]["verdict"], "unsupported")
            self.assertEqual(checked["unknowns"]["verdict"], "unsupported")
            self.assertNotIn("summary", [row["id"] for row in payload["claims"]])
            self.assertNotIn("unknowns", [row["id"] for row in payload["claims"]])
            self.assertIn("当前可见节选", checked["summary"]["reason"])
            self.local.read_cache["ev_injury"].pop(partial_field)
        # A complete view is not rejected by this narrow mechanical guard.
        self.assertIsNotNone(verification.prepare_claims(self.local, value)[1])
        self.local.read_cache["ev_injury"]["spoiler_redacted"] = True
        value["summary"] = "当前可见节选未提供伤病、首发或赛果信息。"
        value["unknowns"] = ["当前可见原文未提供确认首发；隐藏的段落不作推断。"]
        self.assertIsNotNone(verification.prepare_claims(self.local, value)[1])
        for text in ("全文没有伤病消息", "原文没有任何首发名单", "该官网文章未明示伤病", "文章仅陈述备战心态，未披露首发"):
            self.assertTrue(verification.whole_document_absence(text), text)

    def test_deterministic_passages_are_literal_bounded_and_never_cross_paragraphs(self):
        paragraphs = ["The first visible paragraph contains only training information. " * 20,
                      "This is a separate visible paragraph after hidden content. " * 16,
                      "中场参加了训练，出场安排仍有待确认。" * 70]
        text = "\n\n".join(paragraphs)
        passages = evidence_passages(text)
        self.assertEqual(passages, evidence_passages(text))
        self.assertTrue(passages)
        self.assertTrue(all(8 <= len(p["text"]) <= 700 for p in passages))
        self.assertTrue(all(any(p["text"] in paragraph for paragraph in paragraphs) for p in passages))
        self.assertEqual(len({p["id"] for p in passages}), len(passages))

    def test_passage_ids_resolve_to_actual_quote_and_recheck_current_source(self):
        read = self.local.read_cache["ev_injury"]
        selected = read["passages"][0]
        candidate = quoted_submission()
        candidate["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "passage_id": selected["id"]}]
        self.local.call("submit_answer", candidate)
        canonical = self.local.submission["structured_answer"]
        self.assertEqual(canonical["facts"][0]["quotes"][0]["quote"], selected["text"])
        self.assertIsNotNone(verification.prepare_claims(self.local, canonical)[1])
        source = self.store.get("evidence", "ev_injury")
        source["text"] = "The original statement has been removed and replaced."
        self.store.put("evidence", source)
        self.assertIsNone(verification.prepare_claims(self.local, canonical)[1])

    def test_unknown_or_mismatched_passage_id_is_not_silently_replaced(self):
        candidate = quoted_submission()
        quote = candidate["facts"][0]["quotes"][0]
        quote["passage_id"] = "p_unread"
        self.assertIsNone(verification.prepare_claims(self.local, candidate)[1])
        quote["passage_id"] = self.local.read_cache["ev_injury"]["passages"][0]["id"]
        quote["quote"] = "the midfielder has an injury"
        self.assertIsNone(verification.prepare_claims(self.local, candidate)[1])

    def test_live_opening_game_cannot_be_upgraded_to_group_stage(self):
        source = self.store.get("evidence", "ev_injury")
        source.update(title="Arsenal travel to Napoli", text="Arsenal’s preparations for their opening Champions League game against Napoli have been disrupted.")
        self.store.put("evidence", source)
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = quoted_submission()
        value["summary"] = "阿森纳欧冠小组赛首轮客战那不勒斯的准备受到影响。"
        value["facts"][0].update(title="欧冠首场备战", detail="来源称阿森纳对阵那不勒斯的首场欧冠比赛备战受到影响。",
            quotes=[{"evidence_id": "ev_injury", "quote": source["text"]}])
        claims, payload = verification.prepare_claims(self.local, value)
        self.assertEqual(next(c for c in claims if c["id"] == "summary")["verdict"], "unsupported")
        self.assertNotIn("summary", [c["id"] for c in payload["claims"]])
        self.assertIn("fact_1", [c["id"] for c in payload["claims"]])
        for stage, explicit in (("小组赛", "group stage"), ("淘汰赛", "knockout phase"),
                                ("半决赛", "semi-final"), ("四分之一决赛", "quarter-final"),
                                ("八分之一决赛", "round of 16"), ("决赛", "Champions League final"),
                                ("联赛阶段", "league phase")):
            self.assertTrue(verification.missing_stage_support(stage, "opening Champions League game"))
            self.assertFalse(verification.missing_stage_support(stage, explicit))
        self.assertTrue(verification.missing_stage_support("决赛", "the final step in our quest"))
        self.assertFalse(verification.missing_stage_support("首轮欧冠比赛", source["text"]))

    def test_quote_context_includes_negation_and_source_identity(self):
        source = self.store.get("evidence", "ev_injury")
        source.update(text="The Guardian reports: It is NOT true that the midfielder is ready for training. No official statement has been released.",
                      source_name="The Guardian", url="https://www.theguardian.com/football/story", origin="web")
        self.store.put("evidence", source)
        self.local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = quoted_submission()
        value["facts"][0]["quotes"][0]["quote"] = "the midfielder is ready for training."
        _, payload = verification.prepare_claims(self.local, value)
        self.assertIn("NOT true", payload["sources"][0]["passages"][0])
        self.assertEqual(payload["sources"][0]["source_host"], "www.theguardian.com")

    def test_v3_context_exposes_provenance_and_filters_late_lineup(self):
        match = self.store.get("matches", "match_one")
        match.update(home="Test City", away="Arsenal", source_url="https://example.test/fixtures",
                     source_name="League", updated_at="2026-08-31T09:00:00Z", kickoff_confirmed=True,
                     lineups={"status": "confirmed", "home": ["A"], "away": ["B"],
                              "source_url": "https://example.test/lineups", "updated_at": "2026-09-01T11:00:00Z"})
        self.store.put("matches", match)
        local = LocalTools(self.store, dict(self.run, skill="match_preview", match_id="match_one"), self.store.settings())
        context = local.call("match_context", {"match_id": "match_one"})
        self.assertEqual(context["home"], "Test City")
        self.assertEqual(context["away"], "Arsenal")
        self.assertEqual(context["lineups"]["status"], "unavailable")
        self.assertTrue(context["source_url"])


class VerificationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeStore()
        self.store.config["verification_enabled"] = True
        self.store.config["llm"]["max_rounds"] = 6
        self.store.new_run()

    async def run_with(self, responses):
        with patch.object(agent, "request_completion", new=AsyncMock(side_effect=responses)) as requester:
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        return self.store.get("runs", "run_one"), requester

    async def test_independent_support_request_excludes_assistant_and_secrets(self):
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission()), checker_result()])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verification"]["status"], "supported")
        self.assertEqual(run["verification_attempts"], 1)
        self.assertTrue(run["structured_answer"]["facts"][0]["quotes"])
        check_messages = provider.call_args_list[-1].args[1]
        self.assertEqual([msg["role"] for msg in check_messages], ["system", "user"])
        self.assertNotIn("secret-example", json.dumps(check_messages))
        self.assertNotIn("secret-example", json.dumps(run))

    async def test_passage_id_submission_persists_resolved_original(self):
        source = self.store.get("evidence", "ev_injury")
        passage = evidence_passages(source["text"])[0]
        value = quoted_submission()
        value["facts"][0]["quotes"] = [{"evidence_id": "ev_injury", "passage_id": passage["id"]}]
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", value), checker_result()])
        self.assertEqual(run["verification"]["status"], "supported")
        self.assertEqual(run["structured_answer"]["facts"][0]["quotes"][0]["quote"], source["text"])
        self.assertEqual(run["verification"]["claims"][0]["quotes"][0]["quote"], source["text"])

    async def test_many_facts_get_complete_dynamic_checker_schema_and_output_space(self):
        self.store.config["llm"]["max_output_tokens"] = 16000
        value = quoted_submission()
        value["facts"] = [dict(copy.deepcopy(value["facts"][0]), title=f"相关进展 {index}") for index in range(20)]
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                     tool_message("submit_answer", value)]
        checked_counts = []
        async def provider(config, messages, **kwargs):
            if kwargs["tools"][0]["function"]["name"] != "report_source_support":
                return responses.pop(0)
            payload = json.loads(messages[-1]["content"])
            count = len(payload["claims"])
            checked_counts.append(count)
            schema = kwargs["tools"][0]["function"]["parameters"]["properties"]["claims"]
            self.assertEqual((schema["minItems"], schema["maxItems"]), (count, count))
            self.assertGreater(config["max_output_tokens"], 4000)
            self.assertLessEqual(config["max_output_tokens"], 16000)
            return tool_message("report_source_support", {"claims": [
                {"id": claim["id"], "verdict": "supported", "reason": "该说法受到当前原文支持。"}
                for claim in payload["claims"]]})
        with patch.object(agent, "request_completion", new=provider):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        run = self.store.get("runs", "run_one")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(checked_counts, [23])
        self.assertEqual(len(run["structured_answer"]["facts"]), 20)
        self.assertEqual(run["verification"]["status"], "supported")

    async def test_a_fabricated_quotation_is_refused_by_the_local_gate(self):
        # A quote that does not appear in the text we actually read is a
        # fabricated citation, and it is the one thing delivery still refuses.
        # Note the distinction from the semantic verdicts: the *citation* is
        # wrong here, not somebody's reading of it.
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        value = quoted_submission()
        value["facts"][0]["quotes"][0]["quote"] = "Medical update: the midfielder ... unavailable for training."
        claims, payload = verification.prepare_claims(local, value)
        self.assertTrue(any(claim.get("validation_failed") for claim in claims))
        self.assertIsNone(payload)

    async def test_a_fabricated_quotation_is_handed_back_to_the_model(self):
        # The submission is refused as a correctable tool error rather than
        # ending the task, so the model can point its citation at real text.
        value = quoted_submission()
        value["facts"][0]["quotes"][0]["quote"] = "Medical update: the midfielder ... unavailable for training."
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", value), tool_message("submit_answer", quoted_submission()),
            checker_result(), checker_result()])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verification"]["status"], "supported")
        self.assertNotIn("... unavailable", run["answer"])

    async def test_a_fabricated_quotation_is_never_delivered(self):
        value = quoted_submission()
        value["facts"][0]["quotes"][0]["quote"] = "Medical update: the midfielder ... unavailable for training."
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", value), {"content": "那就这样交付吧。"}])
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("... unavailable", run.get("answer") or "")

    async def test_a_quotation_that_matches_is_delivered(self):
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission()), checker_result()])
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["answer"])

    async def test_an_unsupported_statement_is_delivered_with_a_label(self):
        # The change this project was built around: a claim the checker could
        # not support stays visible, annotated and cited, instead of being
        # deleted on the reader's behalf.
        bad = quoted_submission()
        bad["answer"] = "俱乐部官方确认这名中场将在下一场首发。"
        bad["facts"][0]["detail"] = "俱乐部官方确认这名中场将在下一场首发。"
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", bad),
            checker_result(fact_1="unsupported", summary="unsupported", answer="unsupported")])
        self.assertEqual(run["status"], "completed")
        self.assertIn("将在下一场首发", run["answer"])
        self.assertEqual(run["verification"]["status"], "insufficient")
        annotated = run["annotations"]
        self.assertEqual(annotated["facts"][0]["support"], "unsupported")
        self.assertIn("reason", annotated["facts"][0])
        self.assertEqual(annotated["facts"][0]["evidence_ids"], ["ev_injury"])
        self.assertIn("不代表已获独立事实认证", annotated["notice"])

    async def test_an_unsupported_claim_cannot_be_checked_twice_away(self):
        # There is no repair round to wear the claim down with.
        bad = quoted_submission()
        bad["facts"][0]["detail"] = "俱乐部官方确认这名中场将在下一场首发。"
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", bad),
            checker_result(fact_1="unsupported", summary="unsupported", analysis="unsupported")])
        self.assertEqual(provider.await_count, 3)
        self.assertEqual(run["verification_attempts"], 1)

    async def test_conflicting_analysis_is_delivered_and_labelled(self):
        bad = quoted_submission()
        bad["answer"] = "阿森纳下一场将在主场迎战Test City，使用三中卫阵型。"
        bad["analysis"] = "阿森纳下一场将在主场迎战Test City，使用三中卫阵型。"
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", bad), checker_result(analysis="conflicting", answer="conflicting")])
        self.assertEqual(run["verification"]["status"], "partial")
        self.assertIn("三中卫", run["answer"])
        self.assertEqual(run["annotations"]["prose"]["analysis"]["verdict"], "conflicting")
        self.assertEqual(len(run["structured_answer"]["facts"]), 1)

    async def test_an_unavailable_checker_still_delivers_the_answer(self):
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission()), agent.AgentFailure("模型服务未完成核对。")])
        self.assertEqual(provider.await_count, 3)
        self.assertEqual(run["verification"]["status"], "unavailable")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["delivery"]["status"], "complete")
        self.assertEqual(run["evidence_ids"], ["ev_injury"])

    async def test_live_supported_label_with_explicit_doubt_is_downgraded(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        candidate = quoted_submission()
        candidate["facts"].append(dict(copy.deepcopy(candidate["facts"][0]), title="第二项消息"))
        feedback = {"claims": [
            {"id": "fact_1", "verdict": "supported", "reason": "原文直接支持这项来源说法。"},
            {"id": "fact_2", "verdict": "supported", "reason": "访谈内容均受支持；不过主张注明了精确日期属于新增细节，未在原文引文中体现。"},
            {"id": "summary", "verdict": "supported", "reason": "主稿各要素受支持。但后卫卡拉菲奥里表态在引文中未出现，无法确认该球员是否表态，此点存疑。"},
            {"id": "analysis", "verdict": "supported", "reason": "给出了有前提的观察，未新增事实。"},
            {"id": "unknowns", "verdict": "supported", "reason": "资料未提供首发，与未知项相符。"},
        ]}
        checked = await verification.verify_submission(local, candidate, store.settings()["llm"],
            requester=AsyncMock(return_value=tool_message("report_source_support", feedback)))
        by_id = {c["id"]: c for c in checked["claims"]}
        self.assertEqual(checked["status"], "partial")
        self.assertEqual(by_id["fact_1"]["verdict"], "supported")
        self.assertEqual(by_id["fact_2"]["verdict"], "uncertain")
        self.assertEqual(by_id["summary"]["verdict"], "uncertain")
        self.assertEqual(by_id["unknowns"]["verdict"], "supported")
        self.assertEqual(len(verification.conservative_delivery(candidate, checked)["facts"]), 1)
        for text in ("此点存疑", "不受原文支持", "无法确认这一描述", "但相关细节未体现", "不过出现了新增事实"):
            self.assertTrue(verification.inconsistent_support_reason(text))
        for text in ("资料未提供首发，与未知项相符", "原文说法受支持，未加入新事实", "原文未提阵容，unknowns准确表达了缺口", "这是有前提的分析，但未新增事实"):
            self.assertFalse(verification.inconsistent_support_reason(text))

    async def test_a_missing_quotation_never_reaches_the_checker(self):
        # A submission with no quotations has nothing the checker could match
        # against a source. The local gate rejects it, so a checker request is
        # never spent on a claim with no source behind it.
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        claims, payload = verification.prepare_claims(local, submission())
        self.assertTrue(any(claim.get("validation_failed") for claim in claims))
        self.assertIsNone(payload)
        self.assertFalse(any(claim.get("verdict") == "supported" for claim in claims))

    async def test_a_missing_quotation_is_repaired_by_a_later_submission(self):
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", submission()), tool_message("submit_answer", quoted_submission()),
            checker_result()])
        self.assertEqual(run["verification"]["status"], "supported")
        self.assertEqual(run["status"], "completed")

    async def test_bad_check_format_and_missing_ids_fail_closed(self):
        for response in ({"content": "全部正确"}, tool_message("report_source_support", {"claims": []})):
            run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
                tool_message("submit_answer", quoted_submission()), response, response])
            self.assertEqual(run["verification"]["status"], "unavailable")
            self.store.new_run()

    async def test_a_plain_text_answer_cannot_replace_the_structured_delivery(self):
        # The structured submission is what carries the citations, so prose
        # cannot be accepted as the answer. The submitted version is delivered
        # instead, with its support verdict attached.
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission()), checker_result(fact_1="unsupported"),
            {"content": "其实已确认复出。[ev_injury]"}, {"content": "其实已确认复出。[ev_injury]"}])
        self.assertEqual(run["status"], "completed")
        self.assertNotIn("其实已确认复出", run["answer"])
        self.assertTrue(run["annotations"]["facts"])

    async def test_a_plain_text_answer_cannot_introduce_facts_when_verification_is_off(self):
        self.store.config["verification_enabled"] = False
        run, _ = await self.run_with([{"content": "其实已确认复出。[ev_injury]"}, {"content": "其实已确认复出。[ev_injury]"}])
        self.assertNotIn("其实已确认复出", run.get("answer") or "")

    async def test_an_insufficient_submission_carries_no_new_claims(self):
        # "Not enough evidence" is not a loophole for asserting things without
        # a source: a submission with no facts must deliver the gap itself,
        # not a conclusion dressed as an unknown.
        value = {"status": "insufficient", "answer": "资料不足，无法确认这名球员是否复出。",
                 "facts": [], "analysis": "", "unknowns": ["何时恢复合练"]}
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        result = local.call("submit_answer", value)
        self.assertTrue(result["submitted"])
        self.assertEqual(local.submission["status"], "insufficient")
        self.assertEqual(local.submission["structured_answer"]["facts"], [])

    async def test_an_insufficient_submission_must_state_the_gap(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        with self.assertRaises(ToolError) as caught:
            local.call("submit_answer", {"status": "insufficient", "answer": "球员已经复出了。",
                                         "facts": [], "analysis": "", "unknowns": ["具体时间未知"]})
        self.assertIn("无法", str(caught.exception))

    async def test_an_insufficient_submission_may_not_carry_facts(self):
        store = FakeStore()
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        with self.assertRaises(ToolError):
            local.call("submit_answer", {
                "status": "insufficient", "answer": "资料不足，无法确认。",
                "facts": [{"title": "训练状态", "detail": "来源称暂未参加训练。", "evidence_ids": ["ev_injury"]}],
                "analysis": "", "unknowns": ["何时恢复"]})

    async def test_inflight_verification_is_cancellable(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}), tool_message("submit_answer", quoted_submission())]
        async def provider(*args, **kwargs):
            if responses:
                return responses.pop(0)
            started.set()
            try:
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        cancel = asyncio.Event()
        with patch.object(agent, "request_completion", new=provider):
            task = asyncio.create_task(agent.run_agent(self.store, "run_one", cancel))
            await asyncio.wait_for(started.wait(), 1)
            cancel.set()
            await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.store.get("runs", "run_one")["status"], "cancelled")

    async def test_a_checker_timeout_delivers_the_answer_unchecked(self):
        self.store.config["verification_timeout_seconds"] = 1
        self.store.config["llm"]["timeout_seconds"] = 1
        cancelled = asyncio.Event()
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}), tool_message("submit_answer", quoted_submission())]
        async def provider(*args, **kwargs):
            if responses:
                return responses.pop(0)
            try:
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        with patch.object(agent, "request_completion", new=provider):
            await asyncio.wait_for(agent.run_agent(self.store, "run_one", asyncio.Event()), 5)
        result = self.store.get("runs", "run_one")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result["verification"]["status"], "unavailable")
        # The answer is delivered with an unchecked label rather than withheld.
        self.assertEqual(result["delivery"]["status"], "complete")
        self.assertTrue(result["answer"])

    async def test_strict_chat_transcript_remains_complete_without_a_repair_round(self):
        # One submission now answers the task: there is no repair transcript to
        # reassemble, so every tool call is paired in the same conversation.
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}, "read_1"),
            tool_message("submit_answer", quoted_submission(), "submit_1"), checker_result()]
        request_sessions = []
        async def strict_provider(config, messages, **kwargs):
            wire = make_payload(config, messages, kwargs.get("tools"), kwargs.get("tool_choice", "auto"))
            pending, seen = set(), set()
            for message in wire["messages"]:
                if message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending, "orphan or duplicate tool response")
                    pending.remove(message["tool_call_id"])
                else:
                    self.assertFalse(pending, "every assistant tool call must be answered before another role")
                    if message.get("tool_calls"):
                        for call in message["tool_calls"]:
                            self.assertNotIn(call["id"], seen)
                            seen.add(call["id"])
                            pending.add(call["id"])
            self.assertFalse(pending, "request cannot end with unpaired tool calls")
            request_sessions.append(config.get("_session_id"))
            return responses.pop(0)
        with patch.object(agent, "request_completion", new=strict_provider):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        result = self.store.get("runs", "run_one")
        self.assertEqual(result["verification"]["status"], "supported")
        self.assertEqual(len(request_sessions), 3)
        self.assertTrue(all(not (session or "").endswith("_repair") for session in request_sessions))
    async def test_twenty_one_tool_calls_all_execute_with_paired_results(self):
        batch = {"role": "assistant", "content": None, "tool_calls": [
            tool_message("read_evidence", {"evidence_id": "ev_injury"}, f"read_{index}")["tool_calls"][0]
            for index in range(21)]}
        responses = [batch, tool_message("submit_answer", quoted_submission(), "submit_1"), checker_result()]
        seen_batch = False
        async def strict_provider(config, messages, **kwargs):
            nonlocal seen_batch
            wire = make_payload(config, messages, kwargs.get("tools"), kwargs.get("tool_choice", "auto"))
            pending = set()
            for message in wire["messages"]:
                if message["role"] == "tool":
                    self.assertIn(message["tool_call_id"], pending)
                    pending.remove(message["tool_call_id"])
                else:
                    self.assertFalse(pending)
                    pending.update(call["id"] for call in message.get("tool_calls", []))
            self.assertFalse(pending)
            tool_results = [row for row in wire["messages"] if row["role"] == "tool"]
            if tool_results:
                seen_batch = True
                self.assertEqual(len(tool_results), 21)
                self.assertTrue(all(json.loads(row["content"])["id"] == "ev_injury" for row in tool_results))
            return responses.pop(0)
        with patch.object(agent, "request_completion", new=strict_provider):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        self.assertTrue(seen_batch)
        result = self.store.get("runs", "run_one")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification"]["status"], "supported")

    async def test_abnormally_large_tool_response_stops_before_execution(self):
        batch = {"role": "assistant", "content": None, "tool_calls": [
            tool_message("read_evidence", {"evidence_id": "ev_injury"}, f"read_{index}")["tool_calls"][0]
            for index in range(agent.MAX_REQUESTED_TOOL_CALLS + 1)]}
        result, provider = await self.run_with([batch])
        self.assertEqual(provider.await_count, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["read_evidence_ids"], [])

    async def test_an_unsupported_analysis_is_delivered_not_stripped(self):
        # There is no repair round, so a claim the checker doubts is neither
        # rewritten nor removed: it is delivered with its verdict attached.
        value = quoted_submission()
        value["answer"] = "阿森纳将在主场获胜并派出三中卫。"
        value["analysis"] = "阿森纳将在主场获胜并派出三中卫。"
        run, provider = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", value),
            checker_result(analysis="unsupported", answer="unsupported", summary="unsupported", unknowns="unsupported")])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verification"]["status"], "partial")
        self.assertEqual(run["verification_attempts"], 1)
        self.assertEqual(run["evidence_ids"], ["ev_injury"])
        self.assertIn("三中卫", run["answer"])
        self.assertEqual(run["annotations"]["prose"]["analysis"]["verdict"], "unsupported")
        self.assertEqual(run["structured_answer"]["analysis"], "阿森纳将在主场获胜并派出三中卫。")

    async def test_a_checker_outage_delivers_with_an_unchecked_label(self):
        value = quoted_submission()
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", value),
            agent.AgentFailure("请求参数或接口协议不被服务接受，HTTP 400。")])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verification"]["status"], "unavailable")
        self.assertEqual(run["verification_attempts"], 1)
        self.assertEqual(run["evidence_ids"], ["ev_injury"])
        self.assertEqual(run["delivery"]["status"], "complete")
        self.assertTrue(run["annotations"]["notice"])
        self.assertNotIn("recovery_reason", run)

    async def test_a_changed_source_does_not_revive_an_old_checkpoint(self):
        # The source is edited mid-task. Nothing may be delivered from a check
        # that was performed against the earlier text.
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission())]
        async def provider(*args, **kwargs):
            if responses:
                return responses.pop(0)
            source = self.store.get("evidence", "ev_injury")
            source["text"] += " The source subsequently corrected this statement."
            self.store.put("evidence", source)
            raise agent.AgentFailure("核对请求失败。")
        with patch.object(agent, "request_completion", new=provider):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        run = self.store.get("runs", "run_one")
        self.assertNotIn("recovery_reason", run)

    async def test_a_checker_outage_after_submission_still_delivers_once(self):
        run, _ = await self.run_with([tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission()), agent.AgentFailure("核对连接失败。")])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["verification"]["status"], "unavailable")
        self.assertEqual(run["verification_attempts"], 1)
        self.assertEqual(run["evidence_ids"], ["ev_injury"])

    async def test_a_changed_source_never_yields_a_stale_verified_answer(self):
        responses = [tool_message("read_evidence", {"evidence_id": "ev_injury"}),
            tool_message("submit_answer", quoted_submission()), checker_result(analysis="unsupported")]
        async def provider(*args, **kwargs):
            if responses:
                return responses.pop(0)
            source = self.store.get("evidence", "ev_injury")
            source["text"] += " The source subsequently corrected this statement."
            self.store.put("evidence", source)
            raise agent.AgentFailure("核对请求失败。")
        with patch.object(agent, "request_completion", new=provider):
            await agent.run_agent(self.store, "run_one", asyncio.Event())
        run = self.store.get("runs", "run_one")
        # Nothing checked against the earlier text may be re-labelled from it.
        self.assertTrue(run["answer"].startswith(quoted_submission()["answer"]))
        self.assertEqual(run["annotations"]["facts"][0]["support"], "supported")
        self.assertNotIn("subsequently corrected", json.dumps(run, ensure_ascii=False))

    async def test_source_title_is_context_but_not_support_for_new_details(self):
        store = FakeStore()
        source = store.get("evidence", "ev_injury")
        source["title"] = "Arteta on Champions League return and Napoli trip"
        store.put("evidence", source)
        local = LocalTools(store, store.new_run(), store.settings())
        local.call("read_evidence", {"evidence_id": "ev_injury"})
        stub = AsyncMock(return_value=checker_result(summary="unsupported"))
        checked = await verification.verify_submission(local, quoted_submission(), store.settings()["llm"], requester=stub)
        messages = stub.call_args.args[1]
        payload = json.loads(messages[1]["content"])
        self.assertIn("Napoli", payload["sources"][0]["title"])
        self.assertIn("sources.title", messages[0]["content"])
        self.assertIn("不能据此补写航班延误", messages[0]["content"])
        self.assertEqual(checked["claims"][1]["verdict"], "unsupported")


# Human labelled, invented micro-cases model what the live checker must judge.
# They document criteria; they are not evidence that a live model passes them.
SEMANTIC_CASES = [
    ("training", "The player missed training on 9 September.", "球员9月9日缺席训练。", "supported"),
    ("certainty", "The Guardian understands the player could miss the match.", "俱乐部官方确认球员缺阵。", "unsupported"),
    ("negation", "The manager said he is not ready to return.", "主教练说他已准备好复出。", "conflicting"),
    ("date", "The match is scheduled for 12 September.", "比赛定于9月13日。", "conflicting"),
    ("venue", "Arsenal travel to Liverpool for an away fixture.", "阿森纳将在主场迎战利物浦。", "conflicting"),
    ("lineup", "This is our predicted lineup, not the official team sheet.", "这是已经公布的正式首发。", "conflicting"),
    ("scope", "A player returned to light training.", "他已经获准比赛并将首发。", "unsupported"),
    ("uncertain", "The manager will assess his condition tomorrow.", "球员现在能够出场。", "uncertain"),
]


class LabelledContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_human_labelled_regression_gate_and_context(self):
        for name, source, claim, expected in SEMANTIC_CASES:
            with self.subTest(case=name):
                store = FakeStore()
                item = store.get("evidence", "ev_injury")
                item["text"] = source
                store.put("evidence", item)
                local = LocalTools(store, store.new_run(), store.settings())
                local.call("read_evidence", {"evidence_id": "ev_injury"})
                candidate = quoted_submission()
                candidate["facts"][0].update(detail=claim, quotes=[{"evidence_id": "ev_injury", "quote": source}])
                stub = AsyncMock(return_value=checker_result(fact_1=expected))
                checked = await verification.verify_submission(local, candidate, store.settings()["llm"], requester=stub)
                self.assertEqual(checked["claims"][0]["verdict"], expected)
                payload = json.loads(stub.call_args.args[1][-1]["content"])
                self.assertIn(source, payload["sources"][0]["passages"][0])
                safe = candidate if checked["status"] == "supported" else verification.conservative_delivery(candidate, checked)
                self.assertEqual(bool(safe["facts"]), expected == "supported")
