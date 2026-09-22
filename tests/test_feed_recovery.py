"""Regression coverage for honest queue progress and article-owned images."""
import unittest
from datetime import timedelta
from unittest.mock import patch

from bs4 import BeautifulSoup

from server import ingestion, news_reports, personal_app
from tests.test_agent import FakeStore
from tests.test_news_reports import event, successful_runner


class MemoryStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.config.update(followed_teams=["arsenal"], reports={"auto_generate": True})

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)


class ArticleImageTests(unittest.TestCase):
    def roundup(self):
        url = "https://www.theguardian.com/football/2026/sep/11/champions-league-review-troy-parrott-jose-mourinho-michael-olise"
        asset = "0d072" + "a" * 35
        return {"id": "ev_roundup", "team_id": "barcelona", "source_name": "卫报 · 巴塞罗那", "url": url,
                "title": "Champions League review: Irish heroics, rejected resignations and Mourinho’s return",
                "images": [{"url": f"https://i.guim.co.uk/img/media/{asset}/{crop}/master/2000.jpg?width={width}&quality=85",
                            "caption": "", "source_url": url, "credit": "卫报 · 巴塞罗那"}
                           for crop, width in (("0_0_2000_1200", 500), ("0_0_2000_1200", 900), ("10_10_1900_1000", 900))]}

    def test_barcelona_roundup_does_not_display_uncaptioned_mourinho_montage(self):
        source = self.roundup()
        self.assertEqual(len({ingestion.image_identity(image["url"]) for image in source["images"]}), 1)
        self.assertEqual(ingestion.source_images(source), [])

    def test_feed_name_credit_and_partial_alias_do_not_prove_roundup_image_identity(self):
        source = self.roundup()
        source["source_name"] = "FC Barcelona official news"
        source["images"][0].update(credit="Barcelona / Barça / 巴萨", caption="Barcaville supporters and José Mourinho")
        self.assertEqual(ingestion.source_images(source), [])
        source["images"][0]["caption"] = "Barça players prepare for training."
        self.assertEqual(len(ingestion.source_images(source)), 1)
        source["images"][0]["caption"] = "巴萨球员参加训练。"
        self.assertEqual(len(ingestion.source_images(source)), 1)

    def test_explicit_team_headline_and_normal_articles_keep_attributable_images(self):
        source = self.roundup()
        source["title"] = "Barcelona review: training plans for the coming week"
        self.assertEqual(len(ingestion.source_images(source)), 1)
        source["title"] = "The captain returns to training ahead of the next fixture"
        self.assertEqual(len(ingestion.source_images(source)), 1)
        for title in ("European football roundup", "Champions League – live", "European football as it happened", "欧冠综述：一周看点"):
            source["title"] = title
            self.assertEqual(ingestion.source_images(source), [], title)

    def test_article_figure_caption_overrides_generic_metadata_for_roundup_identity(self):
        source = self.roundup()
        image = source["images"][0]["url"]
        markup = f'''<meta property="og:image" content="{image}">
            <script type="application/ld+json">{{"@type":"NewsArticle","url":"{source['url']}",
            "image":{{"url":"{image}","caption":"European football"}}}}</script>
            <article><p>Several teams prepare for the next round.</p><figure><img src="{image}">
            <figcaption>FC Barcelona players in training.</figcaption></figure></article>'''
        source["images"] = ingestion.extract_images(BeautifulSoup(markup, "html.parser"), source["url"], source["source_name"])
        self.assertEqual(source["images"][0]["caption"], "FC Barcelona players in training.")
        self.assertEqual(len(ingestion.source_images(source)), 1)

    def test_collection_social_artwork_and_related_cards_are_not_article_images(self):
        markup = '''<meta property="og:image" content="https://cdn.example.com/section.jpg">
            <main><article><figure><img src="https://cdn.example.com/unrelated.jpg"></figure>
            <p>A link to a different story.</p></article><article><p>Another story.</p></article></main>'''
        for url in ("https://www.theguardian.com/football/barcelona", "https://example.com/team/latest"):
            self.assertEqual(ingestion.extract_images(BeautifulSoup(markup, "html.parser"), url, "Publisher"), [])

    def test_same_guardian_asset_is_deduplicated_across_crops_and_query_sizes(self):
        asset = "a" * 40
        first = f"https://i.guim.co.uk/img/media/{asset}/0_0_2000_1200/master/2000.jpg?width=500&quality=85&s=abc"
        second = f"https://i.guim.co.uk/img/media/{asset}/10_10_1900_1000/master/1900.jpg?width=900&s=def"
        markup = f'''<meta property="og:image" content="{first}"><meta name="twitter:image" content="{second}">
            <article><p>Training session has resumed.</p><figure><img src="{second}"><figcaption>Actual training photo</figcaption></figure></article>'''
        images = ingestion.extract_images(BeautifulSoup(markup, "html.parser"), "https://www.theguardian.com/football/2026/sep/16/training", "Guardian")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["caption"], "Actual training photo")
        self.assertNotEqual(ingestion.image_identity("https://cdn.example.com/photo?id=1&w=300"),
                            ingestion.image_identity("https://cdn.example.com/photo?id=2&w=300"))
        self.assertEqual(ingestion.image_identity("https://cdn.example.com/photo.jpg?w=300&q=90"),
                         ingestion.image_identity("https://cdn.example.com/photo.jpg?w=900&q=50"))

    def test_images_require_exact_source_identity_and_current_visibility(self):
        source = {"id": "ev_one", "url": "https://example.com/news/one", "images": [
            {"url": "https://cdn.example.com/a.jpg", "source_url": "https://example.com/news/two"},
            {"url": "https://cdn.example.com/b.jpg", "source_url": "https://example.com/news/one", "evidence_id": "ev_other"},
            {"url": "https://cdn.example.com/c.jpg", "source_url": "https://example.com/news/one"},
            {"url": "https://127.0.0.1/d.jpg", "source_url": "https://example.com/news/one"}]}
        self.assertEqual([image["url"] for image in ingestion.source_images(source)], ["https://cdn.example.com/c.jpg"])
        for flags in ({"private": True}, {"deleted": True}, {"visibility": "hidden"}, {"spoiler_redacted": True}):
            self.assertEqual(ingestion.source_images({**source, **flags}), [])

    def test_related_article_figures_are_excluded_but_lazy_primary_image_is_kept(self):
        markup = '''<article><p>Training session has resumed.</p>
            <figure><img data-src="https://cdn.example.com/team.jpg"><figcaption>Training</figcaption></figure>
            <aside><figure><img src="https://cdn.example.com/sidebar.jpg"></figure></aside>
            <div class="related-content"><figure><img src="https://cdn.example.com/other.jpg"></figure></div></article>'''
        images = ingestion.extract_images(BeautifulSoup(markup, "html.parser"), "https://example.com/news/training", "Club")
        self.assertEqual([image["url"] for image in images], ["https://cdn.example.com/team.jpg"])


class FeedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MemoryStore()
        event(self.store)

    async def test_refresh_can_resume_exhausted_report_without_erasing_daily_usage(self):
        row = news_reports.enqueue_reports(self.store)[0]
        row.update(status="failed", attempts=3, retry_at=None, attempt_history=[news_reports.now().isoformat()] * 3)
        self.store.put("reports", row)
        feed = personal_app.feed_view(self.store, "arsenal")
        self.assertEqual(feed["processing"]["failed"], 1)
        self.assertIn("手动整理", feed["processing"]["note"])
        retried = news_reports.retry_pending_reports(self.store, "arsenal")
        self.assertEqual(retried["queued"], 1)
        self.assertEqual(news_reports.report_budget(self.store)["used_today"], 3)
        result = await news_reports.process_report_queue(self.store, runner=successful_runner)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(news_reports.report_budget(self.store)["used_today"], 4)

    async def test_configuration_change_resumes_failed_jobs_but_reads_do_not(self):
        row = news_reports.enqueue_reports(self.store)[0]
        row.update(status="failed", attempts=3, retry_at=None, attempt_history=[news_reports.now().isoformat()])
        self.store.put("reports", row)
        self.store.config["llm"]["model"] = "fixed-provider-model"
        self.assertEqual(news_reports.list_reports(self.store)[0]["status"], "failed")
        queued = news_reports.enqueue_reports(self.store)[0]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["attempt_history"], row["attempt_history"])

    async def test_worker_pins_feed_role_and_uses_its_configuration(self):
        self.store.config["llm"]["model"] = ""
        settings = {**self.store.settings(), "llm": {**self.store.settings()["llm"], "model": "news-model"}}
        with patch("server.model_config.role_settings", return_value=settings), patch("server.model_config.role_selection", return_value={"provider_id": "news", "model": "news-model"}):
            row = news_reports.enqueue_reports(self.store)[0]
            self.assertEqual(row["status"], "queued")
            await news_reports.process_report_queue(self.store, runner=successful_runner)
        run = self.store.list("runs")[0]
        self.assertEqual((run["provider_id"], run["model"], run["model_role"]), ("news", "news-model", "feed"))

    async def test_feed_waiting_reasons_exclude_fixture_only_events(self):
        fixture = {**self.store.get("evidence", "ev_injury"), "id": "fixture_source", "fixture_record": True}
        self.store.put("evidence", fixture)
        self.store.put("events", {"id": "fixture_event", "mode": "live", "team_id": "arsenal", "evidence_ids": [fixture["id"]]})
        self.store.config["llm"]["model"] = ""
        news_reports.enqueue_reports(self.store)
        feed = personal_app.feed_view(self.store, "arsenal")
        self.assertEqual(feed["pending_count"], 1)
        self.assertEqual(feed["processing"]["needs_configuration"], 1)
        self.assertEqual(feed["processing"]["total_pending"], 1)
        self.assertIn("球队动态模型", feed["processing"]["note"])

    async def test_retry_cooldown_is_visible_without_daily_quota_block(self):
        row = news_reports.enqueue_reports(self.store)[0]
        row.update(status="failed", retry_at=(news_reports.now() + timedelta(minutes=5)).isoformat())
        self.store.put("reports", row)
        self.assertEqual(personal_app.feed_view(self.store, "arsenal")["processing"]["retrying"], 1)
        row["attempt_history"] = [news_reports.now().isoformat()] * 12
        self.store.put("reports", row)
        summary = personal_app.feed_view(self.store, "arsenal")["processing"]
        self.assertEqual(summary["budget_blocked"], 0)
        self.assertEqual(summary["retrying"], 1)
        self.assertTrue(summary["can_retry"])
        self.assertEqual(summary["next_retry_at"], row["retry_at"])
        self.assertNotIn("额度", summary["note"])

    async def test_cited_new_capture_keeps_link_when_old_capture_has_same_url(self):
        first = self.store.get("evidence", "ev_transfer")
        newer = self.store.get("evidence", "ev_injury")
        for source in (first, newer):
            source["url"] = "https://example.com/news/training"
            self.store.put("evidence", source)
        current = self.store.get("events", "event_one")
        current["evidence_ids"] = [first["id"], newer["id"]]
        links = personal_app.source_links(self.store, current, evidence_ids={newer["id"]})
        self.assertEqual([link["id"] for link in links], [newer["id"]])

    async def test_feed_uses_images_only_from_report_citations(self):
        first = self.store.get("evidence", "ev_injury")
        first.update(url="https://example.com/news/training", images=[{"url": "https://cdn.example.com/training.jpg?w=300", "source_url": "https://example.com/news/training"},
                                                                    {"url": "https://cdn.example.com/training.jpg?w=900", "source_url": "https://example.com/news/training"}])
        self.store.put("evidence", first)
        unrelated = self.store.get("evidence", "ev_transfer")
        unrelated.update(url="https://example.com/news/unrelated", images=[{"url": "https://cdn.example.com/unrelated.jpg", "source_url": "https://example.com/news/unrelated"}])
        self.store.put("evidence", unrelated)
        current = self.store.get("events", "event_one")
        current["evidence_ids"].append(unrelated["id"])
        self.store.put("events", current)
        news_reports.enqueue_reports(self.store)
        await news_reports.process_report_queue(self.store, runner=successful_runner)
        feed = personal_app.feed_view(self.store, "arsenal")
        self.assertEqual(len(feed["items"]), 1)
        self.assertEqual([image["url"] for image in feed["items"][0]["images"]], ["https://cdn.example.com/training.jpg?w=300"])
        self.assertEqual([source["id"] for source in feed["items"][0]["sources"]], [first["id"]])


if __name__ == "__main__":
    unittest.main()
