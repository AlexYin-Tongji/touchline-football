from tests.support import IsolatedAppCase


class ArticleReaderAPITests(IsolatedAppCase):
    spoiler_free = True

    def import_article(self, **kwargs):
        response = self.client.post("/api/import", json={"team_id": "arsenal", "title": "训练观察与待确认信息", "text": "训练消息仅确认球员参与了当天的公开训练，没有公布接下来一场比赛的正式名单。\n教练谈到了保持耐心的必要性，更多情况仍需等待俱乐部随后发布的正式消息。", **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["evidence"]

    def test_detail_returns_all_paragraphs_and_list_metadata_without_duplication(self):
        article = self.import_article()
        result = self.client.get("/api/evidence/" + article["id"])
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertEqual(data["text"], article["text"])
        self.assertEqual(len(data["paragraphs"]), 2)
        self.assertEqual(data["content_status"], "imported")
        self.assertGreater(data["reading_minutes"], 0)
        state = self.client.get("/api/state?mode=live").json()
        self.assertEqual(state["evidence"][0]["content_status"], "imported")
        self.assertNotIn("paragraphs", state["evidence"][0])
        self.assertEqual(self.client.get("/api/evidence/not_here").status_code, 404)

    def test_hidden_article_detail_does_not_leak_text_or_paragraphs(self):
        article = self.import_article(title="赛后比分 2-1", text="本场最终比分是2-1，球队取得了胜利。这段人工标记的赛后记录应完整隐藏，不能透过正文元数据泄露。", spoiler=True)
        result = self.client.get("/api/evidence/" + article["id"])
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["spoiler_hidden"])
        self.assertNotIn("2-1", result.text)
        self.assertNotIn("球队取得了胜利", result.text)

    def test_article_question_rejects_foreign_mode_and_team(self):
        article = self.import_article()
        base = {"mode": "live", "team_id": "arsenal", "question": "请解释这篇资料", "evidence_ids": [article["id"]]}
        for patch in ({"team_id": "barcelona"}, {"mode": "demo"}, {"evidence_ids": ["missing"]}):
            self.assertEqual(self.client.post("/api/runs", json={**base, **patch}).status_code, 400)
        result = self.client.post("/api/runs", json=base)
        self.assertEqual(result.status_code, 200)
        stored = self.app.state.store.get("runs", result.json()["id"])
        self.assertEqual(stored["input_evidence_ids"], [article["id"]])
        self.assertEqual(result.json()["evidence_ids"], [])


if __name__ == "__main__":
    unittest.main()
