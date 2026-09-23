"""Publisher failures retain actionable reasons without widening report scope."""
import asyncio
import json
import ssl
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from server import agent, ingestion, news_reports
from server.agent_tools import LocalTools
from server.research_transport import shared_request
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


if __name__ == '__main__':
    unittest.main()
