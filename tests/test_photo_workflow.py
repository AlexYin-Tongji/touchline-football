"""Exercise the actual team start route through photo persistence and status."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from server import community_portraits, team_center, team_portraits, wikimedia_portraits
from server.ingestion import IngestionError
from server.main import create_app
from tests.test_team_center import roster_payload
from tests.test_wikimedia_portraits import page, commons
from tests.test_team_portraits import card, PHOTO, URL
from tests.test_community_portraits import record, search


class PhotoWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.espn = patch('server.espn_pages.fetch_player_portrait_records', new=AsyncMock(return_value=[]))
        self.espn.start()
        self.addCleanup(self.espn.stop)
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
        self.community = patch.object(community_portraits, 'resolve_portrait', new=AsyncMock(return_value={
            'status': 'unavailable', 'photo_reason': '社区照片来源暂不可达，稍后重试。',
            'retryable': True, 'reason_code': 'connection_error'}))
        self.community.start()

    async def asyncTearDown(self):
        await self.app.state.workflows.close()
        await self.client.aclose()
        self.core.stop()
        self.community.stop()
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
        leaves = {row['id']: row for row in state['leaves']}
        self.assertEqual(leaves['squad.images']['status'], 'filled')
        self.assertEqual(state['progress']['failed'], 0)
        self.assertGreaterEqual(state['progress']['completed'], 1)
        self.assertIn('completed', state['progress'])
        self.assertIn('remaining', state['progress'])
        self.assertFalse(self.store.list('runs'))  # Photo maintenance does not need a model.

    async def test_unavailable_photo_source_keeps_core_results_and_reports_partial(self):
        async def unavailable(*args, **kwargs):
            raise IngestionError('来源返回 HTTP 429')
        with patch.object(wikimedia_portraits, 'request_json', new=unavailable):
            state = await self.start_and_finish()
        self.assertEqual(state['status'], 'partial')
        self.assertGreaterEqual(state['progress']['failed'], 1)
        self.assertIn('照片', state['last_error'])
        images = {row['id']: row for row in state['leaves']}['squad.images']
        self.assertEqual(images['status'], 'failed')
        self.assertIn(images['reason_code'], {'SOURCE_UNAVAILABLE', 'RATE_LIMITED'})
        self.assertTrue(images['retryable'])
        roster = self.store.get('team_snapshots', self.snapshot_id)['roster']
        self.assertEqual(roster[0]['name'], 'Martin Ødegaard')
        self.assertFalse(roster[0]['photo_url'])
        # A throttle is transient, so it must be reported as retryable rather
        # than as a permanent verdict about this player.
        self.assertIn('重试', roster[0]['photo_reason'])
        self.assertTrue(roster[0].get('retryable'))
        self.assertEqual(roster[0].get('reason_code'), 'connection_error')
        self.assertIn('维基媒体', roster[0]['photo_reason'])
        self.assertIn('TheSportsDB', roster[0]['photo_reason'])

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

    async def official_retry(self, *, explicit_part):
        team, league, season, provider = team_center.team_season_context(self.store, 'barcelona')
        payload = roster_payload()
        payload['season']['year'] = season['season_start_year']
        payload['team'] = {'id': provider, 'seasonSummary': f"{season['season_start_year']}-{str(season['season_start_year'] + 1)[-2:]}"}
        payload['athletes'] = [{**payload['athletes'][0], 'displayName': 'Joan García', 'jersey': '1',
                                'dateOfBirth': '2001-05-04T00:00Z'}]
        snapshot = team_center.parse_roster(payload, team, league, season, provider)
        preserved = []
        if not explicit_part:
            # More than one fallback batch already has identity-checked photos.
            # A manual resume must not clear them before filling the one gap.
            self.community.stop()
            for index in range(team_portraits.COMMUNITY_PAGE_LIMIT + 1):
                player = {**snapshot['roster'][0], 'provider_id': str(200 + index), 'id': f'espn-player-{200 + index}',
                          'name': f'Saved Player {index}', 'number': str(index + 2), 'birth_date': '2000-01-10', 'photo_url': ''}
                with patch.object(community_portraits, '_search', new=AsyncMock(return_value=search(record(
                        player['name'], 'Spain', idPlayer=str(34001000 + index), dateBorn=player['birth_date'])))):
                    await community_portraits.resolve_portrait(self.store, player, 'barcelona')
                snapshot['roster'].append(player)
            self.community.start()
            snapshot = team_portraits.apply_cached_portraits(self.store, snapshot)
            preserved = [row['photo_url'] for row in snapshot['roster'][1:]]
            self.assertTrue(all(preserved))
        self.store.put('team_snapshots', snapshot)
        self.store.set_meta('team_portraits_v1_barcelona', {
            'status': 'unavailable', 'cards': [], 'last_attempt': team_center.now()})
        status_key = team_center._status_key('barcelona', season['id'])
        self.store.set_meta(status_key, {'components': {'portraits': {
            'status': 'failed', 'last_attempt': team_center.now(), 'errors': ['此前连接失败']}}})
        if explicit_part:
            # Exercise the real maintain_team operation rather than the core
            # stub used by the unrelated photo-phase integration scenarios.
            self.core.stop()
        with patch.object(team_portraits, 'fetch_document', return_value=(card().encode(), URL, 'text/html')) as fetch, \
             patch.object(wikimedia_portraits, 'resolve_portrait', new=AsyncMock(return_value={'status': 'missing'})):
            response = await self.client.post('/api/teams/barcelona/maintain', json={'parts': ['portraits']}) if explicit_part else \
                       await self.client.post('/api/workflows/team/barcelona/start')
            self.assertEqual(response.status_code, 202)
            await asyncio.wait_for(asyncio.shield(self.app.state.workflows.tasks[('team', 'barcelona')].task), 2)
        fetch.assert_called_once()
        result = self.store.get('team_snapshots', snapshot['id'])['roster'][0]
        self.assertEqual(result['photo_url'], PHOTO)
        self.assertEqual(result['photo_match'], 'exact_name_and_number')
        if preserved:
            saved = self.store.get('team_snapshots', snapshot['id'])['roster'][1:]
            self.assertEqual([row['photo_url'] for row in saved], preserved)
            community_portraits.resolve_portrait.assert_not_awaited()
        self.assertEqual(self.store.get_meta('team_portraits_v1_barcelona')['status'], 'completed')
        workflow = (await self.client.get('/api/workflows/team/barcelona')).json()
        if explicit_part:
            self.assertEqual(workflow['status'], 'completed')
        else:
            self.assertNotEqual(workflow['status'], 'failed')
            images = {row['id']: row for row in workflow['leaves']}
            self.assertEqual(images['squad.images']['status'], 'filled')

    async def test_a_pass_cut_short_keeps_the_photos_it_already_checked(self):
        """A bounded pass must bank what it earned instead of losing the batch.

        The pass runs under a wall-clock bound, so being stopped partway is
        normal. It used to write its results once at the end, which meant a
        timeout discarded every photo fetched in that run: the interface still
        showed the player as missing and the next start repeated the same work.
        """
        roster = self.store.get('team_snapshots', self.snapshot_id)['roster']
        checked_id = roster[0]['provider_id']
        second = {**roster[0], 'provider_id': '999', 'id': 'espn-player-999',
                  'name': 'Second Player', 'photo_url': ''}
        self.store.put('team_snapshots', {**self.store.get('team_snapshots', self.snapshot_id),
                                          'roster': [roster[0], second]})
        checked = []

        async def first_then_cancelled(store, player, team_id, **kwargs):
            if player['provider_id'] == '999':
                raise asyncio.CancelledError
            checked.append(player['provider_id'])
            return {'status': 'completed', 'photo_url': 'https://example.test/p.jpg',
                    'photo_credit': 'Wikimedia', 'photo_license': 'CC BY-SA 4.0',
                    'photo_source_url': 'https://en.wikipedia.org/wiki/A',
                    'photo_file_page': 'https://commons.wikimedia.org/wiki/File:A.jpg',
                    'photo_match': 'wikimedia_verified_club'}

        community = patch.object(community_portraits, 'resolve_portrait', new=AsyncMock(
            return_value={'status': 'missing', 'retryable': False, 'reason_code': 'community_missing'}))
        with community, patch.object(wikimedia_portraits, 'resolve_portrait', new=first_then_cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await team_center.fetch_player_photos(self.store, 'arsenal')
        # The first player's photo was checked and paid for, so it is on disk
        # even though the pass never reached its final write.
        self.assertEqual(checked, [checked_id])
        stored = self.store.get('team_snapshots', self.snapshot_id)['roster']
        self.assertTrue(next(row for row in stored if row['provider_id'] == checked_id).get('photo_url'))

    async def test_manual_start_retries_recent_official_failure_through_default_photo_phase(self):
        await self.official_retry(explicit_part=False)

    async def test_explicit_photo_part_retries_recent_failure_through_actual_maintain_operation(self):
        await self.official_retry(explicit_part=True)


    async def test_each_pass_advances_the_attempt_budget(self):
        """The budget in ``should_continue`` needs a count that actually advances.

        ``attempts`` was a parameter no production caller passed, so the only
        stop that ever fired was "no progress". A pass that gains exactly one
        photo each time therefore continued for as long as the reader kept
        opening the team. The count now lives with the stored verdict and moves
        once per pass, which is the unit the budget is written in.
        """
        from server.team_center import _photo_acceptance_key
        key = _photo_acceptance_key('arsenal', self.store.get('team_snapshots', self.snapshot_id)['season_id'])
        self.assertIsNone(self.store.get_meta(key, None))
        await team_center.fetch_player_photos(self.store, 'arsenal')
        first = self.store.get_meta(key, None)
        self.assertEqual(first['attempts'], 1)
        await team_center.fetch_player_photos(self.store, 'arsenal', force=True)
        second = self.store.get_meta(key, None)
        self.assertEqual(second['attempts'], 2)

    async def test_the_budget_eventually_stops_a_pass_that_keeps_gaining(self):
        # Every pass gains a photo, so the "no progress" stop never fires; only
        # the budget ends the loop.
        from server.team_center import _photo_acceptance_key
        from server import photo_acceptance
        roster = self.store.get('team_snapshots', self.snapshot_id)['roster']
        key = _photo_acceptance_key('arsenal', self.store.get('team_snapshots', self.snapshot_id)['season_id'])
        report = photo_acceptance.assess([{**roster[0], 'photo_url': 'https://example.test/p.jpg'}])
        self.assertFalse(photo_acceptance.should_continue(report, attempts=3, max_attempts=3))
        self.store.set_meta(key, {**report, 'attempts': 3})
        self.assertEqual(self.store.get_meta(key)['attempts'], 3)


if __name__ == '__main__':
    unittest.main()
