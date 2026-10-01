import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from server import ingestion, research
from tests.support import IsolatedAppCase


class GeneralizedNewsDiscoveryTests(IsolatedAppCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store = self.app.state.store
        self.source = next(source for source in research.source_catalog(self.store, "atletico-madrid")
                           if source["id"].startswith("official_"))
        self.recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.empty = ([], [], {"id": "extra", "state": "fresh", "checked_at": ingestion.now()})

    def article(self, title="Comunicado oficial del primer equipo", *, dated=True):
        meta = '<meta property="article:published_time" content="' + self.recent + '">' if dated else ""
        return ('<html><title>' + title + '</title>' + meta + '<article><p>'
                + "La plantilla prepara la siguiente jornada y mantiene los entrenamientos del primer equipo. " * 12
                + "PUBLIC_ARTICLE_BODY_SENTINEL"
                + '</p></article></html>').encode()

    def discovery_scope(self, source=None):
        return patch.object(research, "source_catalog", return_value=[source or self.source])

    def extras(self):
        return (patch.object(research, "_web_news_index", new=AsyncMock(return_value=self.empty)),
                patch.object(research, "_espn_news_index", new=AsyncMock(return_value=self.empty)))

    def test_local_language_paths_and_article_objects_use_source_identity(self):
        for path in ("/noticias/preparacion-para-la-jornada", "/actualites/preparation-de-la-rencontre",
                     "/nachrichten/vorbereitung-auf-das-spiel", "/notizie/allenamento-della-prima-squadra"):
            with self.subTest(path=path):
                body = ('<main><a href="' + path + '">Preparación del próximo partido</a></main>').encode()
                entries = research._html_candidates(body, self.source["url"], self.source)
                self.assertEqual(len(entries), 1)
        data = {"@type": "NewsArticle", "headline": "Comunicado oficial del primer equipo",
                "mainEntityOfPage": {"@id": "/comunicado-del-club"}, "datePublished": self.recent}
        html = ('<script type="application/ld+json">' + json.dumps(data) + '</script>').encode()
        entries = research._html_candidates(html, self.source["url"], self.source)
        self.assertEqual(entries[0]["url"], "https://www.atleticodemadrid.com/comunicado-del-club")
        self.assertEqual(entries[0]["published_at"], self.recent)

    def test_semantic_card_reads_its_date_without_borrowing_sibling_metadata(self):
        html = ('<main><article><a href="/comunicado-del-club"><h2>Preparación del próximo partido</h2></a>'
                '<time datetime="' + self.recent + '"></time></article>'
                '<article><a href="/noticias/segunda-noticia">Segunda noticia del primer equipo</a></article></main>').encode()
        entries = research._html_candidates(html, self.source["url"], self.source)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["published_at"], self.recent)
        self.assertIsNone(entries[1]["published_at"])

    def test_structured_cards_cannot_grant_external_private_or_nonarticle_access(self):
        targets = ("https://other.invalid/comunicado-del-club", "http://127.0.0.1/private",
                   "/", "/news", "/players/player-profile", "/tickets/match-tickets", "/video/new-video")
        for target in targets:
            with self.subTest(target=target):
                data = {"@type": "NewsArticle", "headline": "Comunicado oficial del primer equipo", "url": target}
                html = ('<script type="application/ld+json">' + json.dumps(data) + '</script>').encode()
                self.assertEqual(research._html_candidates(html, self.source["url"], self.source), [])
        hidden = b'<main><article hidden><a href="/hidden-story"><h2>Hidden article title</h2></a></article></main>'
        self.assertEqual(research._html_candidates(hidden, self.source["url"], self.source), [])

    def test_local_language_womens_and_other_sports_remain_out_of_scope(self):
        for path, title in (("/news/frauen-team", "Frauen bereiten das nächste Spiel vor"),
                            ("/notizie/squadra-femminile", "La squadra femminile si prepara"),
                            ("/notizie/vittoria-primavera", "La Primavera si prepara alla prossima partita"),
                            ("/news/junioren", "Junioren bereiten das nächste Spiel vor"),
                            ("/media-article/under-21s-can-learn", "Under-21s can learn from Senior Cup exit"),
                            ("/media-video/post-match-interview", "Post-match interview with the players"),
                            ("/en/basketball/news/preparation", "First team preparation")):
            with self.subTest(path=path):
                self.assertFalse(research._first_team_entry({"url": self.source["url"] + path, "title": title}))

    def test_inert_cms_news_article_is_bound_by_slug_and_preserves_declared_instant(self):
        current = {"title": "Comunicado oficial del primer equipo", "slug": "comunicado-del-club",
                   "publishedAt": self.recent, "contentBlocks": []}
        unrelated = {"title": "Another article", "slug": "another-article", "publishedAt": "2020-01-01T12:00:00Z", "contentBlocks": []}
        data = {"props": {"pageProps": {"data": {"news": current, "article": unrelated}}}}
        html = ('<html><h1>' + current["title"] + '</h1><script type="application/json">'
                + json.dumps(data) + '</script><article><p>' + "Only visible public text. " * 30 + '</p></article></html>').encode()
        article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/comunicado-del-club", "Official")[0]
        self.assertEqual(article["published_at"], self.recent)
        self.assertEqual(article["publication_precision"], "instant")

    def test_explicit_other_cms_slug_cannot_supply_primary_body_title_or_date(self):
        title = "Comunicado oficial del primer equipo"
        for other_title in ("Another article", title):
            with self.subTest(other_title=other_title):
                unrelated = {"title": other_title, "slug": "another-article",
                             "publishedAt": self.recent, "articleBody": "OTHER_STORY_SENTINEL " * 40}
                data = {"props": {"pageProps": {"data": {"news": unrelated}}}}
                html = ('<html><title>' + title + '</title><h1>' + title
                        + '</h1><script type="application/json">' + json.dumps(data)
                        + '</script><article><p>' + "Only this article's visible public text. " * 20
                        + '</p></article></html>').encode()
                article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/comunicado-del-club", "Official")[0]
                self.assertEqual(article["title"], title)
                self.assertNotIn("OTHER_STORY_SENTINEL", article["text"])
                self.assertIsNone(article["published_at"])
                self.assertIsNone(article["published_date"])

    def test_local_date_only_never_invents_a_time_zone_or_uses_related_news(self):
        day = datetime.now(timezone.utc).date().isoformat()
        localized = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        html = ('<html lang="fr"><main><div class="Article-header"><div class="Article-published">'
                + localized + ' 14:36</div><h1>Préparation du prochain match</h1></div>'
                '<article><p>' + "Only visible public text. " * 30 + '</p></article>'
                '<div class="LiveNews"><span class="publishedDate">01/01/2020</span></div></main></html>').encode()
        article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/comunicado-du-club", "Official")[0]
        self.assertIsNone(article["published_at"])
        self.assertEqual(article["published_date"], day)
        self.assertEqual(article["publication_precision"], "date")
        self.assertTrue(research.within_days(article, 7))
        self.assertFalse(research.within_days({**article, "published_date": "2020-01-01"}, 7))
        future = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        self.assertFalse(research.within_days({**article, "published_date": future}, 7))
        for value in ("unknown", "2026-02-30", "01/02/2026"):
            self.assertIsNone(ingestion.publication_metadata(value)["published_date"])

    async def test_source_advertised_feed_supplies_dated_root_slug_articles(self):
        calls = []
        async def fetch(url, source):
            calls.append(url)
            if url == self.source["url"]:
                body = b'<html><link rel="alternate" type="application/rss+xml" href="/feed/"><main>No static list</main></html>'
            elif url.endswith("/feed/"):
                body = ('<rss><channel><item><title>Comunicado oficial del primer equipo</title><link>'
                        'https://www.atleticodemadrid.com/comunicado-del-club</link><pubDate>' + self.recent
                        + '</pubDate></item></channel></rss>').encode()
            else:
                body = self.article()
            return body, url, "text/html"
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
            found = await research.collect_team(self.store, "atletico-madrid", limit=1, max_age_days=7)
        self.assertEqual(found["added"], 1)
        self.assertEqual(found["sources"][0]["discovery_method"], "html+advertised_rss")
        self.assertEqual(len(calls), 3)

    def test_advertised_feed_cannot_widen_source_host_scope(self):
        html = b'<link rel="alternate" type="application/rss+xml" href="https://other.invalid/feed/"><a href="http://127.0.0.1/news">News</a>'
        self.assertEqual(research._advertised_indexes(html, self.source["url"], self.source, include_landing=True), [])

    async def test_public_declared_json_index_is_inert_scoped_and_does_not_use_modified_dates(self):
        calls = []
        async def fetch(url, source):
            calls.append(url)
            if url == self.source["url"]:
                return b'<main data-url="/content/news/list.json">Public news list</main>', url, "text/html"
            if url.endswith("list.json"):
                payload = {"results": [{"title": "Comunicado oficial del primer equipo", "url": "/noticias/comunicado-del-club",
                                        "modifiedAt": self.recent},
                                       {"title": "Wrong publisher club announcement", "url": "https://other.invalid/noticias/article", "datePublished": self.recent}]}
                self.assertTrue(source["_index_json"])
                return json.dumps(payload).encode(), url, "application/json"
            return self.article(), url, "text/html"
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
            found = await research.collect_team(self.store, "atletico-madrid", limit=1, max_age_days=7)
        self.assertEqual(found["added"], 1)
        self.assertEqual(found["sources"][0]["discovery_method"], "html+advertised_json")
        self.assertEqual(found["sources"][0]["date_verify_attempted"], 1)
        self.assertEqual(len(calls), 3)

    def test_unlabelled_date_immediately_before_headline_is_day_precision(self):
        html = ('<html lang="it"><main><div class="page-intro-news">29 settembre 2026'
                '<h1>Le condizioni dei due attaccanti</h1><p>Only visible public text.</p></div></main></html>').encode()
        article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/notizie/comunicado", "Official")[0]
        self.assertEqual(article["published_date"], "2026-09-29")
        self.assertIsNone(article["published_at"])

    def test_related_datetime_before_primary_old_article_cannot_make_it_recent(self):
        html = ('<html><aside class="related-news"><time datetime="' + self.recent + '">Today</time></aside>'
                '<article><h1>Old first team announcement</h1><time datetime="2020-01-01T12:00:00Z">1 January 2020</time>'
                '<p>The first team made this old announcement and the dates must retain its actual publication time.</p></article></html>').encode()
        article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/noticias/old-announcement", "Official")[0]
        self.assertEqual(article["published_at"], "2020-01-01T12:00:00+00:00")
        self.assertFalse(research.within_days(article, 7))

    def test_related_datetime_cannot_supply_missing_primary_publication_date(self):
        localized = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        for heading_in_article in (True, False):
            with self.subTest(heading_in_article=heading_in_article):
                heading = '<h1>Undated first team announcement</h1>'
                html = ('<html lang="fr"><main><aside class="related-news"><time datetime="' + self.recent + '">'
                        + localized + '</time></aside>' + ("" if heading_in_article else heading)
                        + '<article>' + (heading if heading_in_article else "")
                        + '<p>The first team made this announcement, but this document does not declare a publication date.</p></article></main></html>').encode()
                article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/noticias/undated-announcement", "Official")[0]
                self.assertIsNone(article["published_at"])
                self.assertIsNone(article["published_date"])
                self.assertFalse(research.within_days(article, 7))

    def test_unlabelled_semantic_recommendations_cannot_supply_primary_date(self):
        for card in ("article", "section"):
            for primary_in_article in (False, True):
                for own_date in (None, "2020-01-01T12:00:00Z"):
                    with self.subTest(card=card, primary_in_article=primary_in_article, own_date=own_date):
                        primary = '<h1>First team announcement</h1>'
                        if own_date:
                            primary += '<time datetime="' + own_date + '">1 January 2020</time>'
                        primary += '<div class="article-body"><p>' + "Only the primary article's public text. " * 20 + '</p></div>'
                        related = ('<' + card + '><h2>Other recent announcement</h2><time datetime="'
                                   + self.recent + '">Recent</time></' + card + '>')
                        if primary_in_article:
                            primary = '<article>' + primary + related + '</article>'
                            related = ""
                        html = ('<html><main>' + primary + related + '</main></html>').encode()
                        article = ingestion.extract_html(html, "https://www.atleticodemadrid.com/noticias/announcement", "Official")[0]
                        self.assertEqual(article["published_at"], "2020-01-01T12:00:00+00:00" if own_date else None)
                        self.assertEqual(article["published_date"], "2020-01-01" if own_date else None)
                        self.assertFalse(research.within_days(article, 7))

    async def test_article_scope_metadata_cannot_turn_womens_news_into_mens_evidence(self):
        candidate = {"id": "candidate_womens", "team_id": "atletico-madrid", "mode": "live", "source_id": self.source["id"],
                     "source_name": self.source["name"], "url": "https://www.atleticodemadrid.com/noticias/announcement",
                     "title": "Club announcement", "published_at": self.recent}
        self.store.put("candidates", candidate)
        html = b'<html><meta property="article:section" content="Women"><h1>Club announcement</h1><article><p>The club has announced training plans for the next week.</p></article></html>'
        with self.discovery_scope(), patch.object(research, "_fetch", new=AsyncMock(return_value=(html, candidate["url"], "text/html"))):
            with self.assertRaises(ingestion.IngestionError):
                await research.read_candidate(self.store, "atletico-madrid", candidate["id"])
        self.assertEqual(self.store.list("evidence", "live", "atletico-madrid"), [])

    async def test_declared_persisted_news_query_keeps_delimiters_routes_and_budget(self):
        calls = []
        async def fetch(url, source):
            calls.append(url)
            if url == self.source["url"]:
                body = b'<html lang="en"><news-overview news-api-url="/graphql/execute.json/publisher/public-news;" news-detail-url="/news/detail.html" news-detail-suffix=".html" language-parameter="/content/dam/publisher/"></news-overview><div data-url="/news/unrelated-search.json"></div></html>'
                return body, url, "text/html"
            if "/graphql/execute.json/" in url:
                self.assertTrue(url.endswith(";"))
                self.assertIn(";offset=0;limit=21;", url)
                self.assertIn("language=/content/dam/publisher/en/news;", url)
                data = {"data": {"items": [{"headline": "Comunicado oficial del primer equipo", "url": "2026/10/club-announcement",
                                             "showDate": self.recent, "subCategories": ["publisher:football/men"]}]}}
                return json.dumps(data).encode(), url, "application/json"
            return self.article(), url, "text/html"
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
            found = await research.collect_team(self.store, "atletico-madrid", limit=1, max_age_days=7)
        self.assertEqual(found["added"], 1)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any("unrelated-search" in url for url in calls))
        evidence = self.store.get("evidence", found["evidence_ids"][0])
        self.assertTrue(evidence["article_body_bound"])
        self.assertTrue(evidence["url"].endswith("/news/detail.html/2026/10/club-announcement.html"))

    def test_json_parent_visibility_and_route_hints_cannot_grant_another_host(self):
        payload = {"private": True, "items": [{"@type": "NewsArticle", "headline": "Hidden club announcement",
                                               "url": "https://www.atleticodemadrid.com/noticias/hidden", "datePublished": self.recent}]}
        self.assertEqual(research._json_index_candidates(json.dumps(payload).encode(), self.source["url"], self.source), [])
        html = b'<html lang="en"><news-overview news-api-url="https://other.invalid/graphql/execute.json/public/news;" news-detail-url="/news/detail.html" language-parameter="/content/dam/public/"></news-overview></html>'
        self.assertEqual(research._declared_news_api(html, self.source["url"], self.source), [])

    async def test_bound_primary_intro_scope_rejection_is_not_a_network_failure(self):
        for intro in ("Our women’s team played in the league this week and will train again tomorrow.",
                      "The under-21 team is preparing for its next match and will train again tomorrow.",
                      "Our development squad play in Premier League 2 this week and will train again tomorrow."):
            with self.subTest(intro=intro):
                async def fetch(url, source):
                    if url == self.source["url"]:
                        html = ('<main><article><a href="/noticias/update"><h2>Club match update</h2></a><time datetime="'
                                + self.recent + '"></time></article></main>').encode()
                    else:
                        html = ('<html><h1>Club match update</h1><article><p>' + intro + '</p></article></html>').encode()
                    return html, url, "text/html"
                web, espn = self.extras()
                with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
                    found = await research.collect_team(self.store, "atletico-madrid", limit=1, max_age_days=7, force=True)
                self.assertEqual(found["scope_rejected_count"], 1)
                self.assertEqual(found["sources"][0]["scope_rejected_count"], 1)
                self.assertEqual(found["source_errors"], [])
                self.assertEqual(found["evidence_ids"], [])

    def test_primary_intro_keeps_first_team_news_about_youth_graduates(self):
        article = {"title": "Senior first team contract announcement",
                   "url": "https://www.atleticodemadrid.com/noticias/announcement",
                   "_scope_intro": "The club's youth team graduate has signed a senior first-team contract."}
        self.assertTrue(research._first_team_entry(article))

    async def test_unbound_site_text_is_not_collected_as_article_body(self):
        async def fetch(url, source):
            if url == self.source["url"]:
                return ('<main><article><a href="/noticias/update"><h2>Club match update</h2></a><time datetime="'
                        + self.recent + '"></time></article></main>').encode(), url, "text/html"
            return b'<html><title>Club match update</title><body>Site navigation and newsletter labels with no article headline or own story container.</body></html>', url, "text/html"
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
            found = await research.collect_team(self.store, "atletico-madrid", limit=1, max_age_days=7)
        self.assertEqual(found["evidence_ids"], [])
        self.assertEqual(found["source_errors"][0]["code"], "content_unavailable")

    async def test_owned_fallback_body_can_be_reused_without_erasing_binding_proof(self):
        candidate = {"id": "candidate_bound", "team_id": "atletico-madrid", "mode": "live", "source_id": self.source["id"],
                     "source_name": self.source["name"], "url": "https://www.atleticodemadrid.com/noticias/update", "title": "Club match update"}
        self.store.put("candidates", candidate)
        html = b'<html><h1>Club match update</h1><div><p>The senior team will train tomorrow and prepare for the next match, according to the public club announcement.</p></div></html>'
        with self.discovery_scope(), patch.object(research, "_fetch", new=AsyncMock(return_value=(html, candidate["url"], "text/html"))) as reader:
            first = await research.read_candidate(self.store, "atletico-madrid", candidate["id"])
            second = await research.read_candidate(self.store, "atletico-madrid", candidate["id"])
        self.assertTrue(first["evidence"]["article_body_bound"])
        self.assertTrue(second["cached"])
        self.assertEqual(reader.call_count, 1)

    async def test_undated_official_news_is_read_once_and_only_metadata_is_persisted(self):
        calls = []
        async def fetch(url, source):
            calls.append(url)
            body = b'<main><a href="/noticias/comunicado-del-club">Comunicado oficial del primer equipo</a></main>' if url == self.source["url"] else self.article()
            return body, url, "text/html"
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
            found = await research.collect_team(self.store, "atletico-madrid", limit=1, max_age_days=7)
        self.assertEqual(len(calls), 2)
        self.assertEqual(found["added"], 1)
        status = found["sources"][0]
        self.assertEqual((status["indexed_count"], status["relevant_count"], status["dated_count"],
                          status["window_count"], status["read_attempted"], status["readable_count"]), (1, 1, 1, 1, 1, 1))
        self.assertEqual(status["date_verified_count"], 1)
        self.assertIn("PUBLIC_ARTICLE_BODY_SENTINEL", self.store.get("evidence", found["evidence_ids"][0])["text"])
        persisted = self.store._db.execute("SELECT body FROM documents UNION ALL SELECT body FROM metadata").fetchall()
        self.assertFalse(any("PUBLIC_ARTICLE_BODY_SENTINEL" in row[0] for row in persisted))

    async def test_unknown_publication_date_is_explicit_and_cannot_become_recent_news(self):
        async def fetch(url, source):
            body = b'<main><a href="/noticias/comunicado-del-club">Comunicado oficial del primer equipo</a></main>' if url == self.source["url"] else self.article(dated=False)
            return body, url, "text/html"
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch) as reader:
            found = await research.discover_news(self.store, "atletico-madrid", max_age_days=7)
            again = await research.discover_news(self.store, "atletico-madrid", max_age_days=7)
        self.assertEqual(found["candidates"], [])
        self.assertEqual(found["sources"][0]["date_unknown_count"], 1)
        self.assertEqual(found["sources"][0]["window_count"], 0)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(again["sources"][0]["date_verify_attempted"], 0)

    async def test_date_verification_has_global_budget_and_reserves_official_capacity(self):
        sources = [{**self.source, "id": "media_" + str(index), "url": self.source["url"] + "/" + str(index)} for index in range(3)]
        sources.append(self.source)
        calls = []
        async def index(store, team_id, source, **kwargs):
            if source["id"].startswith("official_"):
                await asyncio.sleep(.03)
            entries = [{"url": "https://www.atleticodemadrid.com/noticias/" + source["id"] + "-" + str(i),
                        "title": "Comunicado oficial del primer equipo " + str(i), "published_at": None} for i in range(10)]
            return entries, [], {"state": "fresh", "checked_at": ingestion.now()}
        async def fetch(url, source):
            calls.append(source["id"])
            return self.article(), url, "text/html"
        web, espn = self.extras()
        with patch.object(research, "source_catalog", return_value=sources), web, espn, \
                patch.object(research, "_source_index", side_effect=index), patch.object(research, "_fetch", side_effect=fetch):
            found = await research.discover_news(self.store, "atletico-madrid", max_age_days=7, limit=40)
        self.assertEqual(len(calls), research.DATE_VERIFY_TOTAL)
        self.assertEqual(calls.count(self.source["id"]), research.DATE_VERIFY_PER_SOURCE)
        self.assertGreater(sum(row["date_pending_count"] for row in found["sources"]), 0)

    async def test_date_verification_cancel_does_not_leave_background_readers(self):
        begun = asyncio.Event()
        finished = asyncio.Event()
        async def fetch(url, source):
            if url == self.source["url"]:
                return b'<main><a href="/noticias/comunicado-del-club">Comunicado oficial del primer equipo</a></main>', url, "text/html"
            begun.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        web, espn = self.extras()
        with self.discovery_scope(), web, espn, patch.object(research, "_fetch", side_effect=fetch):
            task = asyncio.create_task(research.discover_news(self.store, "atletico-madrid", max_age_days=7))
            await asyncio.wait_for(begun.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(self.store.list("evidence", "live", "atletico-madrid"), [])

    async def test_fixed_official_sources_use_explicit_system_proxy_without_local_dns(self):
        client = httpx.AsyncClient
        options = []
        def factory(**kwargs):
            options.append(kwargs)
            return client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text="Public article")),
                          **{key: value for key, value in kwargs.items() if key != "proxy"})
        with patch.object(research, "getproxies", return_value={"https": "http://proxy.invalid:8080"}), \
                patch.object(research.httpx, "AsyncClient", side_effect=factory), \
                patch.object(ingestion, "fetch_document", side_effect=AssertionError("reviewed publisher must use explicit proxy")):
            for team_id in ("arsenal", "liverpool", "inter", "atletico-madrid"):
                source = next(row for row in research.source_catalog(self.store, team_id) if row["id"].startswith("official_"))
                await research._fetch(source["url"], source)
        self.assertEqual(len(options), 4)
        self.assertTrue(all(option["proxy"] == "http://proxy.invalid:8080" and option["trust_env"] is False for option in options))

    async def test_custom_source_cannot_use_proxy_or_follow_builtin_redirect_scope(self):
        custom = {**self.source, "builtin": False}
        with patch.object(research, "getproxies", return_value={"https": "http://proxy.invalid:8080"}), \
                patch.object(ingestion, "fetch_document", return_value=(b"public", custom["url"], "text/html")) as pinned, \
                patch.object(research.httpx, "AsyncClient", side_effect=AssertionError("custom source must stay pinned")):
            await research._fetch(custom["url"], custom)
        pinned.assert_called_once()
        client = httpx.AsyncClient
        called = []
        def factory(**kwargs):
            def redirect(request):
                called.append(str(request.url))
                return httpx.Response(302, headers={"location": "https://other.invalid/private"})
            return client(transport=httpx.MockTransport(redirect), **{key: value for key, value in kwargs.items() if key != "proxy"})
        with patch.object(research, "getproxies", return_value={"https": "http://proxy.invalid:8080"}), \
                patch.object(research.httpx, "AsyncClient", side_effect=factory):
            with self.assertRaises(ingestion.IngestionError):
                await research._fetch(self.source["url"], self.source)
        self.assertEqual(called, [self.source["url"]])
