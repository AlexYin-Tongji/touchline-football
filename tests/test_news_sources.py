import asyncio
import tempfile
import json
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx

from server import ingestion, research
from server.seed import seed
from server.storage import Store


BBC_FEED = "https://feeds.bbci.co.uk/sport/football/rss.xml"
SKY_FEED = "https://www.skysports.com/rss/11670"
SKY_FOOTBALL_FEED = "https://www.skysports.com/rss/11095"
RMC_FOOTBALL_FEED = "https://rmcsport.bfmtv.com/rss/football/"


def feed(items):
    return ("<rss><channel>" + "".join(
        "<item><title>" + escape(title) + "</title><link>" + escape(url)
        + "</link><description>" + escape(description) + "</description><pubDate>"
        + escape(stamp) + "</pubDate></item>"
        for title, url, description, stamp in items
    ) + "</channel></rss>").encode()


class NewsSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "sources.db")
        self.addCleanup(self.store.close)
        seed(self.store)

    def test_reviewed_feeds_are_available_without_guessing_sky_team_ids(self):
        arsenal = research.source_catalog(self.store, "arsenal")
        self.assertIn(BBC_FEED, {item["url"] for item in arsenal})
        for team_id in research.CATALOG:
            with self.subTest(team=team_id):
                sources = research.source_catalog(self.store, team_id)
                self.assertIn(BBC_FEED, {item["url"] for item in sources})
                # Club subscriptions and the shared feeds are both present; the
                # club one is no longer privileged over any other publisher.
                sky = [item for item in sources if "skysports.com" in item["url"]]
                self.assertEqual([item["url"] for item in sky], [SKY_FOOTBALL_FEED])
                self.assertIn(RMC_FOOTBALL_FEED, {item["url"] for item in sources})

    def test_new_global_feeds_keep_club_and_article_scope(self):
        for team_id, url, title, expected in (
            ("arsenal", "https://www.skysports.com/football/news/11095/12345/arteta-arsenal", "Arsenal agree Arteta contract", True),
            ("arsenal", "https://www.skysports.com/football/live-blog/11095/12345/arsenal", "Arsenal latest", False),
            ("arsenal", "https://www.skysports.com/football/arsenal-women/report/12345", "Arsenal Women win", False),
            ("arsenal", "https://www.skysports.com/football/news/11095/12345/chelsea", "Chelsea announce deal", False),
            ("marseille", "https://rmcsport.bfmtv.com/football/ligue-1/om-psg_AV-202609220638.html", "OM-PSG: Marseille prepare", True),
            ("marseille", "https://rmcsport.bfmtv.com/football/ligue-1/om-psg_AV-202609220639.html", "OM: prépare le match", True),
            ("marseille", "https://rmcsport.bfmtv.com/football/ligue-1/om-psg_VN-202609220638.html", "Marseille video", False),
            ("marseille", "https://rmcsport.bfmtv.com/football/ligue-1/om-psg_LN-202609220638.html", "Marseille direct", False),
            ("marseille", "https://rmcsport.bfmtv.com/football/ligue-1/om-psg_DN-202609220638.html", "Marseille club page", False),
            ("marseille", "https://rmcsport.bfmtv.com/football/ligue-1/lyon_AV-202609220638.html", "Lyon prepare", False),
        ):
            with self.subTest(team_id=team_id, url=url):
                entry = {"url": url, "title": title, "description": ""}
                self.assertEqual(research._first_team_entry(entry)
                                 and research.global_feed_mentions_team(team_id, entry), expected)

    def test_rmc_historical_club_reference_is_not_current_club_news(self):
        entry = {"url": "https://rmcsport.bfmtv.com/football/major-league-soccer/higuain_AV-202609230063.html",
                 "title": "Le frère de Gonzalo Higuain viré de sa franchise de MLS",
                 "description": "Federico Higuain, frère de l'ancien buteur du Real Madrid, a été viré par Columbus."}
        self.assertFalse(research.global_feed_mentions_team("real-madrid", entry))

    def test_french_publisher_topic_and_womens_scope(self):
        for title, category in (
            ("OM: une blessure inquiète l'entraîneur", "injury"),
            ("PSG: entraînement collectif avant le match", "training"),
            ("Marseille: transfert et prolongation du contrat", "transfer"),
            ("OM: conférence de l'entraîneur", "interview"),
        ):
            with self.subTest(title=title):
                entry = {"url": "https://rmcsport.bfmtv.com/football/ligue-1/om_AV-202609220638.html",
                         "title": title, "description": ""}
                self.assertIn(category, research.article_categories(entry))
        for title in ("PSG féminin: une blessure", "Marseille féminine prépare la saison",
                      "L'équipe féminine de l'OM à l'entraînement"):
            with self.subTest(title=title):
                entry = {"url": "https://rmcsport.bfmtv.com/football/feminin/om_AV-202609220638.html",
                         "title": title, "description": ""}
                self.assertFalse(research._first_team_entry(entry))

    async def test_unavailable_shared_host_index_cools_down_across_teams_then_recovers(self):
        # Arsenal's and Liverpool's club subscriptions, plus BBC's global
        # football feed, all sit on feeds.bbci.co.uk. A transport fault there is
        # a property of the host, not of one feed path, so one observed failure
        # must cover its siblings without a second request.
        arsenal = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == "bbc_arsenal")
        liverpool = next(s for s in research.source_catalog(self.store, "liverpool") if s["id"] == "bbc_liverpool")
        football = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == "bbc_football")
        self.assertEqual({urlsplit(s["url"]).hostname for s in (arsenal, liverpool, football)},
                         {"feeds.bbci.co.uk"})
        with patch.object(research, "_fetch", new=AsyncMock(side_effect=ingestion.SourceConnectionError("来源请求超时，请稍后重试"))) as fetch:
            first = await research._source_index(self.store, "arsenal", arsenal)
            second = await research._source_index(self.store, "liverpool", liverpool)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first[2]["state"], "unavailable")
        self.assertEqual(second[2]["state"], "unavailable")
        health = research.source_health(self.store, arsenal)
        self.assertTrue(health["backoff_until"])
        self.assertEqual(health["consecutive_failures"], 1)
        health["backoff_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.set_meta(research._host_health_key(arsenal), health)
        self.store._research_requests.failures.clear()
        self.store._research_requests.host_failures.clear()
        cached = self.store.get_meta(research._index_key("arsenal", arsenal))
        cached["last_checked"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.store.set_meta(research._index_key("arsenal", arsenal), cached)
        article = "https://www.bbc.co.uk/sport/football/articles/arsenal-training"
        body = feed([("Arsenal training", article, "", "Tue, 22 Sep 2026 12:00:00 GMT")])
        with patch("server.research_transport.FAILURE_COOLDOWN_SECONDS", 0), \
             patch.object(research, "_fetch", new=AsyncMock(return_value=(body, arsenal["url"], "application/rss+xml"))) as fetch:
            recovered = await research._source_index(self.store, "arsenal", arsenal)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(recovered[2]["state"], "fresh")
        self.assertEqual(research.source_health(self.store, arsenal)["consecutive_failures"], 0)

    async def test_article_specific_failure_does_not_cool_other_feed(self):
        arsenal = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == "bbc_arsenal")
        liverpool = next(s for s in research.source_catalog(self.store, "liverpool") if s["id"] == "bbc_liverpool")
        with patch.object(research, "_fetch", new=AsyncMock(side_effect=ingestion.IngestionError("来源返回 HTTP 404"))) as fetch:
            await research._source_index(self.store, "arsenal", arsenal)
            self.assertFalse(research._source_in_cooldown(self.store, liverpool))
            await research._source_index(self.store, "liverpool", liverpool)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(research.source_health(self.store, arsenal)["last_error_kind"], "http_error")

    async def test_one_network_failure_wave_counts_once(self):
        teams = ("arsenal", "liverpool")
        sources = [(team, next(s for s in research.source_catalog(self.store, team)
                               if s["id"] == "bbc_" + team)) for team in teams]
        async def outage(url, source):
            await asyncio.sleep(0.03)
            raise ingestion.SourceConnectionError("来源请求超时，请稍后重试")
        with patch.object(research, "_fetch", side_effect=outage) as fetch:
            # Concurrent, so both readers are in flight inside one wave. A
            # request already past its cooldown check is not interrupted; the
            # point is that the wave records ONE failure for the host, not one
            # per reader, and the next reader is short-circuited by it.
            await asyncio.gather(*(research._source_index(self.store, team, source)
                                   for team, source in sources))
            wave_calls = fetch.call_count
            followup = await research._source_index(
                self.store, "arsenal", next(s for s in research.source_catalog(self.store, "arsenal")
                                            if s["id"] == "bbc_football"))
        self.assertLessEqual(wave_calls, len(teams))
        self.assertEqual(fetch.call_count, wave_calls)
        self.assertEqual(followup[2]["state"], "unavailable")
        health = research.source_health(self.store, sources[0][1])
        self.assertEqual(health["consecutive_failures"], 1)

    async def test_forced_failure_wave_counts_once_then_later_retry_counts_again(self):
        teams = ("arsenal", "liverpool")
        sources = [(team, next(s for s in research.source_catalog(self.store, team)
                               if s["id"] == "bbc_" + team)) for team in teams]
        async def outage(url, source):
            await asyncio.sleep(0.03)
            raise ingestion.SourceConnectionError("来源请求超时，请稍后重试")
        with patch.object(research, "_fetch", side_effect=outage) as fetch:
            await asyncio.gather(*(research._source_index(self.store, team, source, force=True)
                                   for team, source in sources))
            physical_attempts = fetch.call_count
            first_health = research.source_health(self.store, sources[0][1])
            self.assertEqual(first_health["consecutive_failures"], 1)
            # force=True starts a new retry boundary, so this one does go out.
            await research._source_index(self.store, "arsenal", sources[0][1], force=True)
        self.assertLessEqual(physical_attempts, len(teams))
        self.assertEqual(fetch.call_count, physical_attempts + 1)
        self.assertEqual(research.source_health(self.store, sources[0][1])["consecutive_failures"], 2)

    async def test_expired_access_review_is_retried(self):
        source = {"id": "official_arsenal", "name": "Arsenal", "url": "https://www.arsenal.com/news", "kind": "html",
                  "access_review": {"status": "restricted", "checked_at": "2026-01-01"}}
        with patch.dict(research.CATALOG, {"arsenal": [source]}):
            selected = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == source["id"])
        self.assertFalse(selected.get("access_review_active"))
        with patch.object(research, "_fetch", new=AsyncMock(side_effect=ingestion.IngestionError("来源返回 HTTP 403"))) as fetch:
            await research._source_index(self.store, "arsenal", selected)
        self.assertEqual(fetch.call_count, 1)

    async def test_recent_access_review_waits_for_expiry_but_force_rechecks(self):
        source = {"id": "official_arsenal", "name": "Arsenal", "url": "https://www.arsenal.com/news", "kind": "html",
                  "access_review": {"status": "restricted", "checked_at": datetime.now(timezone.utc).date().isoformat()}}
        with patch.dict(research.CATALOG, {"arsenal": [source]}):
            selected = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == source["id"])
        self.assertTrue(selected.get("access_review_active"))
        with patch.object(research, "_fetch", new=AsyncMock(side_effect=ingestion.IngestionError("来源返回 HTTP 403"))) as fetch:
            skipped = await research._source_index(self.store, "arsenal", selected)
            await research._source_index(self.store, "arsenal", selected, force=True)
        self.assertEqual(skipped[2]["state"], "unavailable")
        self.assertEqual(fetch.call_count, 1)

    async def test_successful_force_review_reopens_source_after_index_expires(self):
        source = {"id": "official_arsenal", "name": "Arsenal", "url": "https://www.arsenal.com/news", "kind": "rss",
                  "access_review": {"status": "restricted", "checked_at": datetime.now(timezone.utc).date().isoformat()}}
        article = "https://www.arsenal.com/news/arsenal-training"
        body = feed([("Arsenal training", article, "", "Tue, 22 Sep 2026 12:00:00 GMT")])
        with patch.dict(research.CATALOG, {"arsenal": [source]}):
            reviewed = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == source["id"])
            self.assertTrue(reviewed["access_review_active"])
            with patch.object(research, "_fetch", new=AsyncMock(return_value=(body, source["url"], "application/rss+xml"))) as fetch:
                fresh = await research._source_index(self.store, "arsenal", reviewed, force=True)
                self.assertEqual(fresh[2]["state"], "fresh")
                cache_key = research._index_key("arsenal", reviewed)
                cached = self.store.get_meta(cache_key)
                cached["fetched_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
                self.store.set_meta(cache_key, cached)
                updated = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == source["id"])
                self.assertFalse(updated["access_review_active"])
                refreshed = await research._source_index(self.store, "arsenal", updated)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(refreshed[2]["state"], "fresh")

    async def test_failed_espn_index_is_cached_briefly_then_force_rechecks(self):
        with patch("server.league_data.fetch_json", new=AsyncMock(side_effect=OSError("blocked"))) as fetch:
            first = await research._espn_news_index(self.store, "arsenal")
            second = await research._espn_news_index(self.store, "arsenal")
            await research._espn_news_index(self.store, "arsenal", force=True)
        self.assertEqual(first[2]["state"], "unavailable")
        self.assertEqual(second[2]["state"], "unavailable")
        self.assertEqual(fetch.call_count, 2)
        cached = self.store.get_meta("espn_news:arsenal")
        self.assertEqual(cached["last_error"], "索引暂不可用")
        self.assertFalse(cached["entries"])

    async def test_proxy_branch_rejects_challenge_header(self):
        source = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == "sky_football")
        original_client = httpx.AsyncClient
        def client_with_challenge(**kwargs):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={
                "content-type": "text/html", "cf-mitigated": "challenge"}, text="Checking your browser"))
            return original_client(transport=transport, **{key: value for key, value in kwargs.items() if key != "proxy"})
        with patch.object(research, "getproxies", return_value={"https": "http://proxy.invalid:8080"}), \
             patch.object(research.httpx, "AsyncClient", side_effect=client_with_challenge):
            with self.assertRaises(ingestion.IngestionError) as caught:
                await research._fetch(source["url"], source)
        self.assertEqual(ingestion.source_error_details(caught.exception)["code"], "access_challenge")

    async def test_proxy_connect_failure_keeps_safe_connection_category(self):
        source = next(s for s in research.source_catalog(self.store, "arsenal") if s["id"] == "sky_football")
        original_client = httpx.AsyncClient
        def client_with_failure(**kwargs):
            def fail(request):
                raise httpx.ConnectError("private proxy diagnostic: token=secret", request=request)
            return original_client(transport=httpx.MockTransport(fail), **{key: value for key, value in kwargs.items() if key != "proxy"})
        with patch.object(research, "getproxies", return_value={"https": "http://proxy.invalid:8080"}), \
             patch.object(research.httpx, "AsyncClient", side_effect=client_with_failure):
            with self.assertRaises(ingestion.SourceConnectionError) as caught:
                await research._fetch(source["url"], source)
        details = ingestion.source_error_details(caught.exception)
        self.assertEqual(details["code"], "connection_error")
        self.assertIn("连接", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_catalog_works_with_read_only_store_without_health_metadata(self):
        class SourceOnlyStore:
            def get(self, collection, key):
                return {"id": key, "name": "Arsenal"} if collection == "teams" and key == "arsenal" else None
            def list(self, collection, mode, team_id):
                return []
        sources = research.source_catalog(SourceOnlyStore(), "arsenal")
        self.assertTrue(any(item["id"] == "sky_football" for item in sources))

    def test_explicit_publisher_name_is_not_replaced_by_bbc(self):
        source = {"id": "other_publisher", "name": "另一家媒体", "url": "https://www.skysports.com/rss/11670", "kind": "rss"}
        with patch.dict(research.CATALOG, {"arsenal": [source]}):
            selected = next(item for item in research.source_catalog(self.store, "arsenal") if item["id"] == source["id"])
        self.assertEqual(selected["name"], "另一家媒体")

    def test_new_defaults_preserve_custom_sources_and_disable_choices(self):
        for index in range(9):
            self.store.put("sources", {"id": f"custom_{index}", "name": f"Custom {index}",
                "url": f"https://example.com/feed/{index}", "kind": "rss", "enabled": True,
                "mode": "live", "team_id": "arsenal"})
        self.store.put("sources", {"id": "disable_bbc", "name": "BBC", "url": BBC_FEED,
            "kind": "rss", "enabled": False, "mode": "live", "team_id": "arsenal"})
        sources = research.source_catalog(self.store, "arsenal")
        self.assertTrue({f"custom_{index}" for index in range(9)} <= {item["id"] for item in sources})
        self.assertNotIn(BBC_FEED, {item["url"] for item in sources})
        self.store.put("sources", {"id": "enable_bbc", "name": "BBC", "url": BBC_FEED,
            "kind": "rss", "enabled": True, "mode": "live", "team_id": "liverpool"})
        self.assertEqual(sum(item["url"] == BBC_FEED for item in research.source_catalog(self.store, "liverpool")), 1)

    def test_sky_bst_dates_are_normalized_without_assuming_other_timezones(self):
        for url, stamp, expected in (
            (SKY_FEED, "Mon, 21 Sep 2026 15:20:00 BST", "2026-09-21T14:20:00+00:00"),
            (SKY_FEED, "Mon, 21 Dec 2026 15:20:00 GMT", "2026-12-21T15:20:00+00:00"),
            (SKY_FEED, "Mon, 21 Sep 2026 15:20:00", None),
            (BBC_FEED, "Mon, 21 Sep 2026 15:20:00 BST", None),
            ("https://example.com/feed", "Mon, 21 Sep 2026 15:20:00 BST", None),
            ("https://example.com/feed", "2026-09-21T15:20:00", None),
        ):
            with self.subTest(url=url, stamp=stamp):
                entries = research._feed_candidates(feed([("Arsenal update", "https://www.skysports.com/football/news/1/2", "", stamp)]), url)
                self.assertEqual(entries[0]["published_at"], expected)

    def test_publisher_media_and_other_squads_are_not_article_candidates(self):
        for url, title, description, expected in (
            ("https://www.bbc.com/sport/football/articles/arsenal", "Arsenal training update", "", True),
            ("https://www.bbc.com/sounds/play/arsenal", "Arsenal transfer latest", "", False),
            ("https://www.bbc.com/sport/av/football/arsenal", "Arsenal training update", "", False),
            ("https://www.bbc.com/sport/football/videos/arsenal", "Arsenal training update", "", False),
            ("https://www.bbc.com/sport/football/live/arsenal", "Arsenal latest", "", False),
            ("https://www.bbc.com/sport/football/articles/wsl", "Arsenal prepare for weekend", "The WSL side are training.", False),
            ("https://www.bbc.com/sport/football/articles/women", "Arsenal Women training update", "", False),
            ("https://www.bbc.com/sport/football/articles/youth", "Arsenal U-21 training update", "", False),
            ("https://www.bbc.com/sport/football/articles/academy", "Arsenal youth team prepare for weekend", "", False),
            ("https://www.skysports.com/watch/video/sports/football/arsenal", "Arsenal training update", "", False),
            ("https://www.skysports.com/football/live-blog/1/2", "Arsenal latest", "", False),
            ("https://www.skysports.com/football/news/11670/12345/arsenal", "Arsenal training update", "", True),
            ("https://www.bbc.com/sport/football/articles/a", "Arsenal train at academy ground", "Senior squad relocated for pitch maintenance.", True),
        ):
            with self.subTest(url=url, title=title):
                self.assertEqual(research._first_team_entry({"url": url, "title": title, "description": description}), expected)

    def test_first_team_youth_background_is_kept_but_youth_squads_are_filtered(self):
        for title, description, expected in (
            ("Arsenal youth product signs new first-team contract", "", True),
            ("Arsenal senior players return to training", "The men's first-team squad included several youth graduates.", True),
            ("Arsenal first-team contract announced", "The club's youth team graduate has signed a senior contract.", True),
            ("Arsenal youth team prepare for weekend", "", False),
            ("Arsenal prepare for European fixture", "The youth squad will play in the UEFA Youth League.", False),
            ("Arsenal U-21 prepare for weekend", "", False),
            ("Arsenal Women prepare for weekend", "", False),
            ("Arsenal prepare for weekend", "The WSL squad return to training.", False),
        ):
            with self.subTest(title=title, description=description):
                self.assertEqual(research._first_team_entry({
                    "url": "https://www.bbc.com/sport/football/articles/example",
                    "title": title, "description": description,
                }), expected)

    async def test_shared_global_feed_is_filtered_for_each_team_after_caching(self):
        sources = research.source_catalog(self.store, "arsenal")
        self.assertTrue(any(item["url"] == BBC_FEED for item in sources), "BBC global feed is missing")
        source = next(item for item in sources if item["url"] == BBC_FEED)
        body = feed([
            ("Arsenal training update", "https://www.bbc.com/sport/football/articles/arsenal", "First-team session", "Mon, 21 Sep 2026 15:20:00 GMT"),
            ("Liverpool training update", "https://www.bbc.com/sport/football/articles/liverpool", "First-team session", "Mon, 21 Sep 2026 15:20:00 GMT"),
            ("Chelsea training update", "https://www.bbc.com/sport/football/articles/chelsea", "First-team session", "Mon, 21 Sep 2026 15:20:00 GMT"),
            ("Arsenal Women training", "https://www.bbc.com/sport/football/articles/arsenal-women", "WSL team", "Mon, 21 Sep 2026 15:20:00 GMT"),
        ])
        empty = ([], [], {"id": "external", "name": "External", "state": "unavailable", "checked_at": ingestion.now()})
        with patch("server.research.source_catalog", side_effect=lambda store, team_id: [{**source, "team_id": team_id}]), \
                patch("server.research._fetch", new=AsyncMock(return_value=(body, BBC_FEED, "application/rss+xml"))) as fetch_index, \
                patch("server.research._web_news_index", new=AsyncMock(return_value=empty)), \
                patch("server.research._espn_news_index", new=AsyncMock(return_value=empty)):
            arsenal = await research.discover_news(self.store, "arsenal", "最近消息", 10)
            liverpool = await research.discover_news(self.store, "liverpool", "最近消息", 10)
        self.assertEqual([item["title"] for item in arsenal["candidates"]], ["Arsenal training update"])
        self.assertEqual([item["title"] for item in liverpool["candidates"]], ["Liverpool training update"])
        self.assertEqual(fetch_index.call_count, 1)
        cached = self.store.get_meta(research._index_key("arsenal", source))
        self.assertEqual(len(cached["entries"]), 4)
        self.assertTrue(all("team_id" not in item and "text" not in item for item in cached["entries"]))

    def test_custom_shared_flag_cannot_cross_team_cache_boundaries(self):
        source = {"id": "custom", "url": "https://example.com/feed", "builtin": False, "shared_index": True}
        self.assertNotEqual(research._index_key("arsenal", source), research._index_key("liverpool", source))

    async def test_reviewed_spanish_and_argentine_sources_discover_only_matching_club(self):
        sources = research.source_catalog(self.store, "barcelona")
        selected = [source for source in sources if source["id"] in {"as_football", "tyc_international"}]
        self.assertEqual(len(selected), 2, "Both reviewed regional publishers must be discoverable")
        as_url = "https://as.com/futbol/primera/barcelona-vuelve-al-entrenamiento-f202609-n/"
        tyc_url = "https://www.tycsports.com/futbol-internacional/barcelona-y-su-nueva-lesion-id762400.html"
        as_feed = feed([
            ("Barcelona vuelve al entrenamiento", as_url, "El primer equipo prepara el partido", "Tue, 22 Sep 2026 12:00:00 GMT"),
            ("Barcelona femenino prepara el partido", "https://as.com/futbol/femenino/barcelona-f202609-n/", "", "Tue, 22 Sep 2026 12:00:00 GMT"),
            ("Barcelona noticia externa", "https://unreviewed.example/article", "", "Tue, 22 Sep 2026 12:00:00 GMT"),
        ])
        tyc_html = ('<main><h2><a href="' + tyc_url + '">Barcelona y su nueva lesión</a></h2>'
                    '<a href="/futbol-internacional.html">Fútbol internacional</a>'
                    '<a href="/futbol-internacional/arsenal-de-sarandi-id762401.html">Arsenal de Sarandí prepara el partido</a></main>').encode()
        async def fetch(url, source):
            return (as_feed, url, "application/xml") if source["id"] == "as_football" else (tyc_html, url, "text/html")
        empty = ([], [], {"id": "external", "name": "External", "state": "unavailable", "checked_at": ingestion.now()})
        with patch.object(research, "source_catalog", return_value=selected), patch.object(research, "_fetch", side_effect=fetch), \
                patch.object(research, "_web_news_index", new=AsyncMock(return_value=empty)), \
                patch.object(research, "_espn_news_index", new=AsyncMock(return_value=empty)):
            result = await research.discover_news(self.store, "barcelona", "最近消息", 10)
        self.assertEqual({row["url"] for row in result["candidates"]}, {as_url, tyc_url})
        self.assertIsNone(next(row for row in result["candidates"] if row["url"] == tyc_url)["published_at"])

    def test_spanish_topics_and_other_squads_keep_existing_scope(self):
        for title, category in (("Barcelona confirma una lesión", "injury"), ("Barcelona vuelve al entrenamiento", "training"),
                                ("Inter de Milán anuncia un fichaje", "transfer")):
            with self.subTest(title=title):
                row = {"title": title, "url": "https://as.com/futbol/primera/noticia-n/"}
                self.assertIn(category, research.article_categories(row))
        for title in ("Barcelona femenino prepara el partido", "Barcelona sub-20 prepara el partido", "Barcelona feminino volta aos treinos"):
            with self.subTest(title=title):
                self.assertFalse(research._first_team_entry({"title": title, "url": "https://as.com/futbol/noticia-n/"}))
        for team, title, expected in (("inter", "Inter de Milán anuncia un fichaje", True),
                                      ("inter", "Inter Miami anuncia un fichaje", False),
                                      ("arsenal", "Arsenal de Sarandí prepara el partido", False),
                                      ("barcelona", "Barcelona SC anuncia un fichaje", False)):
            with self.subTest(team=team, title=title):
                self.assertEqual(research.global_feed_mentions_team(team, {"title": title}), expected)

    def test_argentine_article_keeps_all_paragraphs_and_excludes_recommendations(self):
        opening = "El entrenador explicó la preparación del equipo y confirmó las novedades. " * 5
        ending = "La decisión definitiva dependerá del entrenamiento del viernes, no está confirmada. " * 5
        body = ('<html><title>Barcelona prepara el partido</title><article class="col8"><p>' + opening + '</p>'
                '<div class="alsoReadContainer">Real Madrid unrelated transfer</div><p>' + ending + '</p>'
                '<div class="articleComments">Readers speculate about a transfer</div>'
                '<section class="containerGrid"><h2>Te puede interesar</h2><div class="card">Unrelated injury</div></section>'
                '</article></html>').encode()
        row = ingestion.extract_html(body, "https://www.tycsports.com/futbol-internacional/barcelona-id762400.html", "TyC Sports")[0]
        self.assertIn(opening.strip(), row["text"])
        self.assertIn(ending.strip(), row["text"])
        self.assertNotIn("unrelated", row["text"].lower())
        self.assertNotIn("Readers speculate", row["text"])
        self.assertEqual(row["content_status"], "full_text")

    def test_subscriber_metadata_does_not_expose_hidden_structured_body(self):
        url = "https://as.com/futbol/primera/reportaje-f202609-n/"
        node = {"@type": "NewsArticle", "url": url, "headline": "Reportaje de Barcelona",
                "isAccessibleForFree": False, "articleBody": "Subscriber-only tactical details. " * 40}
        visible = "Este es el adelanto público de la noticia. " * 20
        body = ('<html><script type="application/ld+json">' + json.dumps(node) + '</script>'
                '<article><p>' + visible + '</p><p>Suscríbete para seguir leyendo</p></article></html>').encode()
        row = ingestion.extract_html(body, url, "AS")[0]
        self.assertEqual(row["content_status"], "partial")
        self.assertIn(visible.strip(), row["text"])
        self.assertNotIn("Subscriber-only", row["text"])

    def test_explicitly_hidden_body_and_images_are_not_public_article_content(self):
        url = "https://as.com/futbol/primera/reportaje-f202609-n/"
        for attribute in ('hidden', 'style="display: none !important"', 'style="visibility: hidden;"', 'aria-hidden="true"'):
            with self.subTest(attribute=attribute):
                body = ('<html><title>Reportaje de Barcelona</title>'
                        f'<div itemprop="articleBody" {attribute}><p>PRIVATE_SUBSCRIBER_BODY</p>'
                        '<figure><img src="https://as.com/hidden-photo.jpg"><figcaption>Reportaje de Barcelona</figcaption></figure></div>'
                        '<article><p>' + "Este es el adelanto público de la noticia. " * 20 + '</p></article></html>').encode()
                row = ingestion.extract_html(body, url, "AS")[0]
                self.assertNotIn("PRIVATE_SUBSCRIBER_BODY", row["text"])
                self.assertIn("adelanto público", row["text"])
                self.assertFalse(row["images"])

    async def test_recent_discovery_verifies_bounded_undated_publisher_articles(self):
        source = next(s for s in research.source_catalog(self.store, "barcelona") if s["id"] == "tyc_international")
        urls = [f"https://www.tycsports.com/futbol-internacional/barcelona-news-id76240{number}.html" for number in range(4)]
        index = ("<main>" + "".join(f'<a href="{url}">Barcelona update number {number}</a>' for number, url in enumerate(urls)) + "</main>").encode()
        stamps = [ingestion.now(), (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), None, ingestion.now()]
        reads = []
        async def fetch(url, selected):
            if url == source["url"]:
                return index, url, "text/html"
            reads.append(url)
            number = urls.index(url)
            meta = f'<meta property="article:published_time" content="{stamps[number]}">' if stamps[number] else ""
            return (f'<html><title>Barcelona update number {number}</title>{meta}<article>'
                    + "Barcelona prepara su próximo partido. " * 30 + "</article></html>").encode(), url, "text/html"
        empty = ([], [], {"id": "external", "name": "External", "state": "unavailable", "checked_at": ingestion.now()})
        with patch.object(research, "source_catalog", return_value=[source]), patch.object(research, "_fetch", side_effect=fetch), \
                patch.object(research, "_web_news_index", new=AsyncMock(return_value=empty)), \
                patch.object(research, "_espn_news_index", new=AsyncMock(return_value=empty)):
            result = await research.discover_news(self.store, "barcelona", limit=10, max_age_days=7)
        self.assertEqual([row["url"] for row in result["candidates"]], [urls[0]])
        self.assertEqual(set(reads), set(urls[:3]))
        self.assertFalse(self.store.list("evidence", "live"), "Date verification must not publish read evidence")
        cached = self.store.get_meta(research._index_key("barcelona", source))
        self.assertTrue(next(row for row in cached["entries"] if row["url"] == urls[0])["published_at"])
        self.assertNotIn("Barcelona prepara su próximo partido", json.dumps(cached))

    def test_global_feed_requires_unambiguous_club_identity(self):
        for team_id, title, expected in (
            ("inter", "Inter Milan prepare for weekend fixture", True),
            ("inter", "Internazionale confirm training plans", True),
            ("inter", "Inter Miami prepare for weekend fixture", False),
            ("ac-milan", "AC Milan confirm training plans", True),
            ("ac-milan", "Inter Milan confirm training plans", False),
            ("ac-milan", "国际米兰确认训练安排", False),
            ("nice", "Arsenal make a nice gesture to supporters", False),
            ("nice", "OGC Nice prepare for weekend fixture", True),
            ("lens", "Liverpool's training seen through the lens of a camera", False),
            ("lens", "RC Lens prepare for weekend fixture", True),
            ("angers", "Chelsea transfer decision angers supporters", False),
            ("angers", "Angers SCO prepare for weekend fixture", True),
            ("angers", "SCO Angers prepare for weekend fixture", True),
        ):
            with self.subTest(team=team_id, title=title):
                self.assertEqual(research.global_feed_mentions_team(team_id, {"title": title}), expected)

    def test_bbc_feed_redirects_stay_in_the_reviewed_publisher_hosts(self):
        source = {"id": "bbc_football", "url": BBC_FEED, "builtin": True, "kind": "rss"}
        hosts = research._source_hosts(source)
        self.assertEqual(research._checked_url("https://www.bbc.com/sport/football/articles/example", hosts), "https://www.bbc.com/sport/football/articles/example")
        for url in ("https://www.skysports.com/football/news/1", "https://fake.bbc.com/article", "http://127.0.0.1/private"):
            with self.subTest(url=url), self.assertRaises(ingestion.IngestionError):
                research._checked_url(url, hosts)


class InterPublisherExtractionTests(unittest.TestCase):
    def page(self, *, slug="ronaldo-50", access="Free", published=True, hidden=False):
        # Reduced from Inter's public Next.js page: text/quote components are
        # siblings of podcast embeds/cards; the article also wraps UI chrome.
        self.opening = "In an interview on Daily Podcast, Max Pezzali recalled Ronaldo's first match. " * 4
        self.quote = "Remember our players and teach the Academy respect, hard work and belonging. " * 4
        self.ending = "He scored 59 goals in 99 appearances. Ronaldo's team is, and will always be, Inter."
        rich = lambda text: {"json": {"nodeType": "document", "content": [{"nodeType": "paragraph", "content": [{"nodeType": "text", "value": text, "marks": []}]}]}}
        data = {"props": {"pageProps": {"page": {"slug": slug, "title": "Ronaldo - 50", "pageType": "article",
                    "published": published, "accessLevel": access, "subtitle": None},
                "components": [{"__typename": "TextComponent", "text": rich(self.opening)},
                    {"__typename": "QuoteComponent", "quote": self.quote, "author": "Giuseppe Marotta"},
                    {"__typename": "InlineArticleLinkComponent", "title": "International Morning Club", "tag": "Daily Podcast"},
                    {"__typename": "TextComponent", "text": rich(self.ending)},
                    {"__typename": "VideoPlayerComponent", "videoEntryId": "video"}],
                "relatedNews": [{"title": "Unrelated transfer story"}]}}}
        content = (f'<div class="css-v4u6zg" {"hidden" if hidden else ""}><div class="chakra-container css-y5jgc7">'
                   f'<div class="css-13pau54"><div class="css-efeopg"><p>{self.opening}</p></div></div></div>'
                   f'<div class="chakra-container css-1259f0x"><div class="css-1ncqdei"><p>{self.quote}</p><span>Giuseppe Marotta</span></div></div>'
                   '<div class="chakra-container css-y5jgc7"><div>Daily Podcast</div><h2>International Morning Club</h2><a>TUTTE LE PUNTATE</a></div>'
                   f'<div class="chakra-container css-y5jgc7"><p>{self.ending}</p></div></div>')
        return ('<html><title>Ronaldo - 50</title><script id="__NEXT_DATA__" type="application/json">'
                + json.dumps(data) + '</script><article><div class="css-utb8f8"><h1>Ronaldo - 50</h1>'
                '<span>Legends — 16 hours ago</span><div>2 MIN READING</div></div>' + content
                + ('<p>A public preview is available for this football story.</p>' if hidden else '')
                + '<div class="chakra-container"><a href="/it/notizie/ronaldo-50">Italian version</a><a>Legends</a></div></article></html>').encode()

    def test_text_and_quote_components_preserve_body_and_author_without_ui_cards(self):
        body = self.page()
        row = ingestion.extract_html(body, "https://www.inter.it/en/news/ronaldo-50", "Inter")[0]
        self.assertEqual(row["text"], "\n".join((self.opening.strip(), self.quote.strip(), "Giuseppe Marotta", self.ending)))
        self.assertEqual(row["content_status"], "full_text")
        self.assertIn("Daily Podcast", row["text"], "A medium actually discussed in prose remains part of the story")
        self.assertNotIn("MIN READING", row["text"])
        self.assertNotIn("Italian version", row["text"])

    def test_publisher_components_cannot_restore_hidden_or_unbound_page_text(self):
        for changes in ({"hidden": True}, {"access": "Paid", "hidden": True},
                        {"slug": "another-story", "hidden": True}, {"published": False, "hidden": True}):
            with self.subTest(changes=changes):
                row = ingestion.extract_html(self.page(**changes), "https://www.inter.it/en/news/ronaldo-50", "Inter")[0]
                self.assertNotIn("59 goals", row["text"])
                self.assertNotIn("Academy respect", row["text"])
                self.assertIn("public preview", row["text"])

    def test_unrelated_malformed_page_data_does_not_break_public_article(self):
        body = self.page().replace(b"<html>", b'<html><script type="application/json">{"props":[]}</script>')
        row = ingestion.extract_html(body, "https://www.inter.it/en/news/ronaldo-50", "Inter")[0]
        self.assertIn(self.ending, row["text"])
        self.assertNotIn("MIN READING", row["text"])


if __name__ == "__main__":
    unittest.main()
