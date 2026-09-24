"""Exercise the actual team start route through photo persistence and status."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from server import team_center, wikimedia_portraits
from server.ingestion import IngestionError
from server.main import create_app
from tests.test_team_center import roster_payload
from tests.test_wikimedia_portraits import page, commons


class PhotoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.temp.name) / 'photos.sqlite3')
        self.store = self.app.state.store
        team, league, season, provider = team_center.team_season_context(self.store, 'arsenal')
        payload = roster_payload()
        payload['season']['year'] = season['season_start_year']
        payload['team']['seasonSummary'] = f"{season['season_start_year']}-{str(season['season_start_year'] + 1)[-2:]}"
        player = payload['athletes'][0]
        player.update(displayName='Martin Ødegaard', dateOfBirth='1998-12-17T00:00Z')
        player.pop('headshot', None)
        payload['athletes'] = [player]
        snapshot = team_center.parse_roster(payload, team, league, season, provider)
        self.snapshot_id = snapshot['id']
        self.store.put('team_snapshots', snapshot)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')
        self.core = patch.object(team_center, 'maintain_team', new=AsyncMock(return_value={
            'status': 'completed', 'components': {part: {'status': 'completed'} for part in team_center.CORE_PARTS}}))
        self.core.start()

    async def asyncTearDown(self):
        await self.app.state.workflows.close()
        await self.client.aclose()
        self.core.stop()
        self.store.close()
        self.temp.cleanup()

    async def wikipedia(self, store, host, params):
        if params.get('list') == 'search':
            return {'query': {'search': [{'title': 'Martin Ødegaard'}]}}
        return commons() if host == 'commons.wikimedia.org' else page()

    async def start_and_finish(self):
        result = await self.client.post('/api/workflows/team/arsenal/start')
        self.assertEqual(result.status_code, 202)
        task = self.app.state.workflows.tasks[('team', 'arsenal')].task
        await asyncio.wait_for(asyncio.shield(task), 2)
        return (await self.client.get('/api/workflows/team/arsenal')).json()

    async def test_normal_team_start_persists_verified_photo_for_non_directory_club(self):
        with patch.object(wikimedia_portraits, 'request_json', new=self.wikipedia):
            state = await self.start_and_finish()
        roster = (await self.client.get('/api/teams/arsenal/center')).json()['roster']
        self.assertTrue(roster[0]['photo_url'])
        self.assertEqual(roster[0]['photo_license'], 'CC BY-SA 4.0')
        self.assertEqual(roster[0]['photo_match'], 'wikimedia_verified_club')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['progress'], {'completed': 4, 'failed': 0, 'remaining': 0})
        self.assertFalse(self.store.list('runs'))  # Photo maintenance does not need a model.

    async def test_unavailable_photo_source_keeps_core_results_and_reports_partial(self):
        async def unavailable(*args, **kwargs):
            raise IngestionError('来源返回 HTTP 429')
        with patch.object(wikimedia_portraits, 'request_json', new=unavailable):
            state = await self.start_and_finish()
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['progress']['completed'], 3)
        self.assertEqual(state['progress']['failed'], 1)
        self.assertIn('照片', state['last_error'])
        roster = self.store.get('team_snapshots', self.snapshot_id)['roster']
        self.assertEqual(roster[0]['name'], 'Martin Ødegaard')
        self.assertFalse(roster[0]['photo_url'])
        # A throttle is transient, so it must be reported as retryable rather
        # than as a permanent verdict about this player.
        self.assertIn('重试', roster[0]['photo_reason'])
        self.assertTrue(roster[0].get('retryable'))
        self.assertEqual(roster[0].get('reason_code'), 'http_rate_limited')

    async def test_stop_during_photo_fetch_keeps_roster_and_cleans_up_work(self):
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(wikimedia_portraits, 'request_json', new=pending):
            await self.client.post('/api/workflows/team/arsenal/start')
            try:
                await asyncio.wait_for(entered.wait(), .5)
            except TimeoutError:
                self.fail('Normal team start skipped its photo phase')
            stopped = await self.client.post('/api/workflows/team/arsenal/stop')
        self.assertEqual(stopped.json()['status'], 'stopped')
        self.assertFalse(self.app.state.workflows.tasks)
        self.assertEqual(self.store.get('team_snapshots', self.snapshot_id)['roster'][0]['name'], 'Martin Ødegaard')


if __name__ == '__main__':
    unittest.main()
