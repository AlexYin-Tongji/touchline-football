"""Publisher failures retain actionable reasons without widening report scope."""
import asyncio
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from server import agent, ingestion, news_reports
from server.agent_tools import LocalTools
from server.research_transport import fetch_reviewed_portrait_document, shared_request
from server.storage import Store
from tests.test_agent import FakeStore
from tests.test_news_reports import event, successful_runner


class SourceDiagnosticsTests(unittest.TestCase):
    def test_long_access_challenge_is_never_a_full_article(self):
        for heading in ("Just a moment", "Verify you are human", "Consent required", "Not available in your region"):
            with self.subTest(heading=heading), self.assertRaises(ingestion.IngestionError):
                ingestion.extract_html((f'<html><title>{heading}</title><article><h1>{heading}</h1>'
                                        + '<p>Checking access to this website. Please continue.</p>' * 60
                                        + '</article></html>').encode(), 'https://news.example/story', 'Publisher')

    def test_actual_article_discussing_captcha_is_not_rejected(self):
        body = ('<html><title>Arsenal explains ticket changes</title><article><h1>Arsenal explains ticket changes</h1>'
                + '<p>The club explained changes to its ticket system and member access.</p>' * 15
                + '<p>A supporter described seeing the words verify you are human on the ticket page.</p></article></html>')
        self.assertEqual(ingestion.extract_html(body.encode(), 'https://news.example/story', 'Publisher')[0]['content_status'], 'full_text')

    def test_pinned_transport_distinguishes_certificate_from_timeout_without_secrets(self):
        for error, phrase in ((ssl.SSLCertVerificationError('secret-token=https://wrong.example/private'), '证书'),
                              (TimeoutError('https://private.example/?token=secret'), '超时')):
            with self.subTest(error=type(error).__name__):
                connection = MagicMock()
                connection.request.side_effect = error
                with patch.object(ingestion, 'public_addresses', return_value=['93.184.216.34']), \
                     patch.object(ingestion, '_PinnedHTTP', return_value=connection), \
                     self.assertRaises(ingestion.SourceConnectionError) as caught:
                    ingestion.fetch_document('https://news.example/story')
                self.assertIn(phrase, str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))

    def test_challenge_response_header_blocks_even_article_shaped_body(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.side_effect = lambda name, default=None: {'cf-mitigated': 'challenge', 'Content-Type': 'text/html'}.get(name, default)
        response.read.side_effect = [b'<article>' + b'An apparently normal story. ' * 50 + b'</article>', b'']
        with patch.object(ingestion, 'public_addresses', return_value=['93.184.216.34']), \
             patch.object(ingestion, '_PinnedHTTP', return_value=connection), \
             self.assertRaisesRegex(ingestion.IngestionError, '验证'):
            ingestion.fetch_document('https://news.example/story')


class PortraitProxyTransportTests(unittest.TestCase):
    url = 'https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p=Gavi'
    hosts = {'www.thesportsdb.com', 'thesportsdb.com'}

    def request(self, handler, *, max_body=512 * 1024):
        from server import research_transport
        real_client = httpx.Client
        self.requests = []
        def response(request):
            self.requests.append(str(request.url))
            return handler(request)
        def client(**options):
            self.assertFalse(options['trust_env'])
            self.assertEqual(options['proxy'], 'http://127.0.0.1:7890')
            self.assertNotIn('verify', options)  # The default TLS verification stays enabled.
            return real_client(transport=httpx.MockTransport(response))
        with patch.object(research_transport, 'getproxies', return_value={'https': 'http://127.0.0.1:7890'}), \
             patch.object(research_transport.httpx, 'Client', side_effect=client), \
             patch.object(ingestion, 'public_addresses', side_effect=AssertionError('fixed proxy destination must not resolve through fake local DNS')):
            return fetch_reviewed_portrait_document(self.url, allowed_hosts=self.hosts, max_body=max_body,
                                                    content_types=('application/json',))

    def test_fixed_portrait_endpoint_can_use_system_proxy_despite_fake_dns(self):
        body, final, content_type = self.request(lambda request: httpx.Response(200, json={'player': []}))
        self.assertEqual(json.loads(body), {'player': []})
        self.assertEqual(final, self.url)
        self.assertIn('json', content_type)
        self.assertEqual(self.requests, [self.url])

    def test_unknown_hosts_private_ips_and_unreviewed_paths_cannot_use_proxy(self):
        from server import research_transport
        for url, hosts in (('https://unreviewed.example/search', {'unreviewed.example'}),
                           ('https://198.18.0.20/api/v1/json/3/searchplayers.php', {'198.18.0.20'}),
                           ('http://www.thesportsdb.com/api/v1/json/3/searchplayers.php', self.hosts),
                           ('https://www.thesportsdb.com:444/api/v1/json/3/searchplayers.php', self.hosts),
                           ('https://www.thesportsdb.com/admin', self.hosts)):
            with self.subTest(url=url), patch.object(research_transport.httpx, 'Client') as client, self.assertRaises(ingestion.IngestionError):
                fetch_reviewed_portrait_document(url, allowed_hosts=hosts, max_body=100)
            client.assert_not_called()

    def test_every_proxy_redirect_is_rechecked_before_a_second_request(self):
        for target in ('http://www.thesportsdb.com/api/v1/json/3/searchplayers.php',
                       'https://127.0.0.1/private', 'https://unreviewed.example/news',
                       'https://www.thesportsdb.com/admin'):
            with self.subTest(target=target), self.assertRaises(ingestion.IngestionError):
                self.request(lambda request: httpx.Response(302, headers={'location': target}))
            self.assertEqual(len(self.requests), 1)

    def test_proxy_bodies_keep_type_size_and_challenge_limits(self):
        for response in (httpx.Response(200, headers={'content-type': 'image/png'}, content=b'png'),
                         httpx.Response(200, headers={'content-type': 'application/json', 'content-length': '100'}, content=b'{}'),
                         httpx.Response(200, headers={'content-type': 'application/json'}, content=b'x' * 100),
                         httpx.Response(200, headers={'content-type': 'application/json', 'cf-mitigated': 'challenge'}, content=b'{}')):
            with self.subTest(response=response.headers), self.assertRaises(ingestion.IngestionError):
                self.request(lambda request: response, max_body=10)

    def test_absent_system_proxy_retains_original_pinned_security_gate(self):
        from server import research_transport
        with patch.object(research_transport, 'getproxies', return_value={}), \
             patch.object(ingestion.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('198.18.0.20', 443))]), \
             patch.object(ingestion, '_PinnedHTTP') as connection, self.assertRaisesRegex(ingestion.IngestionError, '私有网络'):
            fetch_reviewed_portrait_document(self.url, allowed_hosts=self.hosts, max_body=100)
        connection.assert_not_called()

    def test_proxy_transport_errors_never_expose_authenticated_proxy_url(self):
        def failed(request):
            raise httpx.ConnectError('http://private-user:secret@127.0.0.1:7890', request=request)
        with self.assertRaises(ingestion.SourceConnectionError) as caught:
            self.request(failed)
        self.assertNotIn('secret', str(caught.exception))


class SourceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_unreadable_sources_fail_before_model_and_keep_specific_reason(self):
        store = FakeStore()
        item = store.get('evidence', 'ev_injury')
        store.put('evidence', {**item, 'origin': 'web', 'url': 'https://news.example/story',
                              'body_storage': 'transient', 'body_hash': 'known-version', 'text': ''})
        run = store.new_run(skill='news_report', report_id='report_one', input_evidence_ids=['ev_injury'], allow_network=False)
        with patch('server.research.read_evidence_source', new=AsyncMock(side_effect=ingestion.SourceConnectionError('来源请求超时，请稍后重试'))), \
             patch.object(agent, 'request_completion', new=AsyncMock()) as model:
            await agent.run_agent(store, run['id'], asyncio.Event())
        final = store.get('runs', run['id'])
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final.get('source_errors', [{}])[0].get('code'), 'timeout')
        self.assertEqual(final['source_errors'][0]['host'], 'news.example')
        self.assertIn('超时', final['error'])
        self.assertEqual(final['read_evidence_ids'], [])
        self.assertEqual(final['metrics'].get('model_requests'), 0)
        model.assert_not_called()  # Same-team ev_transfer must not become a substitute.

    async def test_completed_report_exposes_gaps_but_only_cites_successful_same_event_source(self):
        store = FakeStore()
        second = {**store.get('evidence', 'ev_injury'), 'id': 'ev_failed', 'source_name': 'Unavailable publisher',
                  'url': 'https://news.example/story'}
        store.put('evidence', second)
        row_event = event(store)
        store.put('events', {**row_event, 'evidence_ids': ['ev_injury', 'ev_failed']})
        row = news_reports.enqueue_reports(store)[0]
        async def runner(store, run_id, cancel):
            await successful_runner(store, run_id, cancel)
            value = store.get('runs', run_id)
            value['source_errors'] = [{'evidence_id': 'ev_failed', 'message': '来源返回 HTTP 403'},
                                      {'evidence_id': 'ev_transfer', 'message': 'HTTP 503'}]
            store.put('runs', value)
        await news_reports.process_report_queue(store, runner=runner)
        public = news_reports.get_report(store, row['id'])
        self.assertEqual(public['status'], 'completed')
        self.assertEqual([s['id'] for s in public['report']['sources']], ['ev_injury'])
        self.assertEqual([s['evidence_id'] for s in public['report'].get('source_gaps', [])], ['ev_failed'])
        self.assertEqual(public['report']['source_gaps'][0]['code'], 'http_forbidden')
        store.put('evidence', {**second, 'private': True})
        hidden = news_reports.get_report(store, row['id'])
        self.assertIsNone(hidden['report'])
        self.assertNotIn('Unavailable publisher', json.dumps(hidden))

    async def test_partial_source_read_reaches_editorial_model_without_broadening_tools(self):
        store = FakeStore()
        store.put('evidence', {**store.get('evidence', 'ev_injury'), 'id': 'ev_failed', 'origin': 'web',
                              'url': 'https://news.example/story', 'body_storage': 'transient', 'text': ''})
        run = store.new_run(skill='news_report', report_id='report_one', input_evidence_ids=['ev_injury', 'ev_failed'], allow_network=False)
        tools_seen = []
        async def model(config, messages, **options):
            tools_seen.extend(t['function']['name'] for t in options.get('tools', []))
            raise agent.AgentFailure('stop after the editorial boundary')
        with patch('server.research.read_evidence_source', new=AsyncMock(side_effect=ingestion.IngestionError('来源返回 HTTP 403'))), \
             patch.object(agent, 'request_completion', new=model):
            await agent.run_agent(store, run['id'], asyncio.Event())
        final = store.get('runs', run['id'])
        self.assertIn('submit_answer', tools_seen)
        self.assertNotIn('discover_news', tools_seen)
        self.assertNotIn('read_article', tools_seen)
        self.assertEqual(final['read_evidence_ids'], ['ev_injury'])
        self.assertEqual(final['source_errors'][0].get('http_status'), 403)

    async def test_cached_failure_retains_safe_reason(self):
        store = FakeStore()
        async def fail():
            raise ingestion.SourceConnectionError('来源证书校验失败，请检查网络或系统代理')
        for _ in range(2):
            with self.assertRaises(ingestion.IngestionError) as caught:
                await shared_request(store, 'article', 'news.example', fail)
            self.assertIn('证书', str(caught.exception))


class RestartedDraftTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'reports.sqlite3'
        store = Store(self.path)
        fixtures = FakeStore()
        store.save_settings(fixtures.settings())
        for value in fixtures.list('teams'):
            store.put('teams', value)
        source = fixtures.get('evidence', 'ev_injury')
        source.update(origin='web', source_id='medical', source_name='Medical publisher',
                      url='https://example.com/news/medical', published_at=ingestion.published_time(source['published_at']))
        self.original = source['text']
        store.put('evidence', source)
        store.put('sources', {'id': 'medical', 'name': source['source_name'], 'url': 'https://example.com/news',
                              'kind': 'html', 'team_id': 'arsenal', 'mode': 'live', 'enabled': True})
        event(store)
        self.row = news_reports.enqueue_reports(store)[0]
        store.close()
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def document(self, text):
        source = self.store.get('evidence', 'ev_injury')
        return (f'<html><title>{source["title"]}</title><meta property="article:published_time" '
                f'content="{source["published_at"]}"><article><p>{text}</p></article></html>').encode()

    async def draft(self, text, *, before_adopt=None, after_read=None):
        from server import research
        self.adoption_errors = []
        async def runner(store, run_id, cancel):
            run = store.get('runs', run_id)
            local = LocalTools(store, run, store.settings())
            await local.acall('read_evidence', {'evidence_id': 'ev_injury'})
            if before_adopt:
                before_adopt()
            try:
                news_reports.adopt_initial_reads(store, run, local)
            except agent.AgentFailure as exc:
                self.adoption_errors.append(str(exc))
                raise
            if after_read:
                after_read()
            await successful_runner(store, run_id, cancel)
        source = self.store.get('evidence', 'ev_injury')
        with patch.object(research, '_fetch', new=AsyncMock(return_value=(self.document(text), source['url'], 'text/html'))):
            return await news_reports.process_report_queue(self.store, runner=runner)

    async def test_evicted_body_of_same_version_does_not_invalidate_report(self):
        self.assertFalse(self.store.get('evidence', 'ev_injury')['text'])
        result = await self.draft(self.original)
        self.assertEqual(result['completed'], 1)
        row = self.store.get('reports', self.row['id'])
        self.assertEqual(row['fingerprint'], self.row['fingerprint'])
        self.assertEqual(news_reports.get_report(self.store, row['id'])['status'], 'completed')

    async def test_new_draft_uses_corrected_initial_read_without_discarding_delivery(self):
        def concurrent_page_read():
            # HTTP presentation may show the pending material as stale during
            # its first re-read; it must not mutate the worker's durable claim.
            news_reports.get_report(self.store, self.row['id'])
            news_reports.list_reports(self.store, 'arsenal')
            self.assertEqual(self.store.get('reports', self.row['id'])['status'], 'generating')
        result = await self.draft(self.original + ' The club will publish another update after assessment.',
                                  before_adopt=concurrent_page_read)
        self.assertEqual(result['completed'], 1)
        row = self.store.get('reports', self.row['id'])
        self.assertNotEqual(row['fingerprint'], self.row['fingerprint'])
        self.assertTrue(row['initial_reads_at'])
        self.assertEqual(news_reports.get_report(self.store, row['id'])['status'], 'completed')
        self.assertEqual(news_reports.enqueue_reports(self.store)[0]['id'], row['id'])
        self.assertEqual(len(self.store.list('reports')), 1)
        self.assertNotIn(self.original, self.store._db.execute("SELECT body FROM documents WHERE collection='evidence'").fetchone()[0])

    async def test_correction_after_initial_read_still_invalidates_new_draft(self):
        def correct():
            source = self.store.get('evidence', 'ev_injury')
            self.store.put('evidence', {**source, 'text': 'The midfielder is now training normally.'})
        result = await self.draft(self.original, after_read=correct)
        self.assertEqual(result['completed'], 0)
        self.assertEqual(self.store.get('reports', self.row['id'])['status'], 'stale')
        self.assertIsNone(news_reports.get_report(self.store, self.row['id'])['report'])

    async def test_privacy_change_during_first_read_cannot_be_adopted(self):
        def restrict():
            source = self.store.get('evidence', 'ev_injury')
            self.store.put('evidence', {**source, 'private': True})
        result = await self.draft(self.original + ' Another medical update will follow.', before_adopt=restrict)
        self.assertEqual(result['completed'], 0)
        self.assertTrue(self.adoption_errors)
        row = self.store.get('reports', self.row['id'])
        self.assertEqual(row['fingerprint'], self.row['fingerprint'])
        self.assertNotIn('initial_reads_at', row)
        self.assertIsNone(news_reports.get_report(self.store, row['id'])['report'])

    async def test_changed_unread_source_cannot_be_adopted(self):
        source = self.store.get('evidence', 'ev_injury')
        self.store.put('evidence', {**source, 'id': 'ev_unread', 'url': 'https://example.com/news/other', 'text': self.original})
        current = self.store.get('events', 'event_one')
        self.store.put('events', {**current, 'evidence_ids': ['ev_injury', 'ev_unread']})
        self.row = news_reports.enqueue_reports(self.store)[0]
        def correct_unread():
            second = self.store.get('evidence', 'ev_unread')
            self.store.put('evidence', {**second, 'text': 'A different medical assessment was published.'})
        result = await self.draft(self.original, before_adopt=correct_unread)
        self.assertEqual(result['completed'], 0)
        self.assertIn('尚未读取', self.adoption_errors[0])
        row = self.store.get('reports', self.row['id'])
        self.assertEqual(row['fingerprint'], self.row['fingerprint'])
        self.assertIsNone(news_reports.get_report(self.store, row['id'])['report'])


if __name__ == '__main__':
    unittest.main()
