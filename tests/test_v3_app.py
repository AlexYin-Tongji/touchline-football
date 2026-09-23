import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from server.main import create_app
from server.storage import Store
from server.seed import seed
from server.automation import AutomationManager
from server.recommendations import for_team


class V3AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name) / 'app.sqlite3')
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.put("/api/settings", json={"spoiler_free": True})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_nested_automation_settings_merge_and_verification_defaults(self):
        current = self.client.get('/api/state').json()
        self.assertTrue(current['settings']['verification_enabled'])
        self.assertFalse(current['settings']['automation']['enabled'])
        self.client.put('/api/settings',json={'automation':{'interval_minutes':120,'generate_brief':True}})
        changed = self.client.put('/api/settings',json={'automation':{'enabled':False}}).json()
        self.assertEqual(changed['automation']['interval_minutes'],120)
        self.assertTrue(changed['automation']['generate_brief'])
        self.assertTrue(changed['automation']['sync_matches'])
        self.assertEqual(self.client.put('/api/settings',json={'automation':{'interval_minutes':0}}).status_code,422)

    def test_manual_fixture_sync_forces_source_refresh(self):
        sync = AsyncMock(return_value={'matches_updated':2,'evidence_added':2,'lineups_updated':0,'errors':[],'synced_at':'2026-09-09T00:00:00+00:00'})
        with patch('server.fixtures.sync_team',sync):
            response = self.client.post('/api/teams/arsenal/sync')
        self.assertEqual(response.status_code,200)
        self.assertTrue(sync.call_args.kwargs['force'])

    def test_recommendations_have_scope_reason_and_persistent_dismissal(self):
        kickoff = (datetime.now(timezone.utc)+timedelta(hours=3)).isoformat()
        match = self.client.post('/api/matches',json={'team_id':'arsenal','opponent':'测试对手','competition':'测试联赛','venue':'home','kickoff':kickoff}).json()
        first = self.client.get('/api/state?mode=live').json()['recommendations']
        preview = next(item for item in first if item['kind']=='preview')
        self.assertEqual(preview['match_id'],match['id'])
        self.assertEqual(preview['mode'],'live')
        self.assertTrue(preview['reason'])
        self.assertLessEqual(len(first),3)
        self.client.post('/api/recommendations/'+preview['id']+'/dismiss',json={'dismissed':True})
        second = self.client.get('/api/state?mode=live').json()['recommendations']
        self.assertNotIn(preview['id'],[item['id'] for item in second])

    def test_finished_match_numeric_fields_and_raw_snapshot_are_protected(self):
        self.app.state.store.put('matches',{'id':'fixture_secret','mode':'live','team_id':'arsenal','opponent':'Opponent',
            'status':'finished','kickoff':'2026-09-08T18:00:00+00:00','score':'2-1','home_score':2,'away_score':1,
            'goals_for':2,'goals_against':1,'result':'W','source_raw':{'result':'2-1'},'source_versions':[{'result':'2-1'}], 'evidence_ids':[]})
        state=self.client.get('/api/state?mode=live').json()
        match=next(item for item in state['matches'] if item['id']=='fixture_secret')
        for key in ('score','home_score','away_score','goals_for','goals_against','result'):
            self.assertIsNone(match[key])
        self.assertNotIn('source_raw',match)
        self.assertEqual(match['source_versions'],[])
        self.assertNotIn('2-1',str(state['recommendations']))

    def test_historical_verification_does_not_leak_when_spoilers_hidden(self):
        self.app.state.store.put('runs',{'id':'r_v3_spoiler','mode':'live','team_id':'arsenal','status':'completed','skill':'ask',
            'answer':'2-1','evidence_ids':[],'spoiler':True,'structured_answer':{'summary':'2-1'},
            'verification':{'status':'supported','claims':[{'text':'2-1','quotes':[{'evidence_id':'x','quote':'2-1'}]}]}})
        response=self.client.get('/api/runs/r_v3_spoiler').json()
        self.assertNotIn('2-1',str(response))
        self.assertIsNone(response['structured_answer'])
        self.assertEqual(response['verification']['claims'],[])

    def test_source_match_metadata_does_not_break_note_editing(self):
        kickoff=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        self.app.state.store.put('matches',{'id':'auto_match','mode':'live','team_id':'arsenal','opponent':'Opponent',
            'status':'scheduled','kickoff':kickoff,'competition':'League','venue':'away','score':None,'note':'',
            'source_match_id':'official_1','source_url':'https://www.arsenal.com/fixtures','home_team':'Opponent','away_team':'Arsenal','evidence_ids':[]})
        response=self.client.patch('/api/matches/auto_match',json={'note':'用户保留的观赛问题'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['source_match_id'],'official_1')
        self.assertEqual(response.json()['note'],'用户保留的观赛问题')
        rejected=self.client.patch('/api/matches/auto_match',json={'status':'finished','score':'2-1'})
        self.assertEqual(rejected.status_code,400)

    def test_successful_recheck_retires_previous_verification_recommendation(self):
        common={'mode':'live','team_id':'arsenal','skill':'ask','question':'同一个待查问题','status':'completed','evidence_ids':[]}
        self.app.state.store.put('runs',{**common,'id':'older','created_at':'2026-09-09T08:00:00+00:00','verification':{'status':'partial'}})
        self.app.state.store.put('runs',{**common,'id':'newer','created_at':'2026-09-09T09:00:00+00:00','verification':{'status':'supported'}})
        items=for_team(self.app.state.store,'arsenal','live')
        self.assertFalse(any(item['kind']=='verify' for item in items))


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.directory.name)/'auto.sqlite3')
        seed(self.store)
        self.start_run=AsyncMock(return_value={'id':'r_auto'})
        self.manager=AutomationManager(self.store,self.start_run)

    async def asyncTearDown(self):
        await self.manager.close()
        self.store.close()
        self.directory.cleanup()

    async def test_legacy_enabled_setting_never_schedules_an_update(self):
        self.store.save_settings({'automation': {'enabled': True, 'interval_minutes': 15}})
        self.store.set_meta('automation_status', {'last_checked': '2000-01-01T00:00:00+00:00'})
        self.assertFalse(self.manager.due())
        self.assertIsNone(self.manager.status()['next_check'])
        self.assertFalse(self.manager.status()['running'])

    async def test_manual_updates_work_when_schedule_off_and_unchanged_skip_model(self):
        self.store.save_settings({'automation':{'enabled':False,'generate_brief':True},'llm':{'base_url':'http://127.0.0.1:9/v1','model':'fixture'}})
        self.store.put('evidence',{'id':'e1','mode':'live','team_id':'arsenal','title':'Training','text':'Training note','spoiler':False})
        with patch('server.fixtures.sync_team',AsyncMock(return_value={'errors':[]})), patch('server.research.collect_team',AsyncMock(return_value={'added':0,'errors':[]})):
            await self.manager.run_once('arsenal')
            self.store.put('runs',{'id':'r_auto','mode':'live','team_id':'arsenal','status':'completed','skill':'team_brief'})
            await self.manager.run_once('arsenal')
        self.assertEqual(self.start_run.await_count,1)
        self.assertIsNotNone(self.manager.status()['last_checked'])
        self.assertFalse(self.manager.due())

    async def test_cancellation_stops_update_cycle(self):
        async def slow(*args,**kwargs): await asyncio.Event().wait()
        with patch('server.fixtures.sync_team',AsyncMock(side_effect=slow)):
            self.manager.start('arsenal')
            await asyncio.sleep(.01)
            self.assertTrue(self.manager.status()['running'])
            await self.manager.close()
        self.assertFalse(self.manager.status()['running'])
        self.start_run.assert_not_called()

    async def test_enabling_generation_after_collection_can_create_first_brief(self):
        self.store.save_settings({'automation':{'generate_brief':True},'llm':{'base_url':'http://127.0.0.1:9/v1','model':'fixture'}})
        self.store.put('evidence',{'id':'existing','mode':'live','team_id':'arsenal','title':'Training','text':'Training note','spoiler':False})
        self.store.set_meta('automation_baseline_arsenal',['existing'])
        with patch('server.fixtures.sync_team',AsyncMock(return_value={'errors':[]})), patch('server.research.collect_team',AsyncMock(return_value={'added':0,'errors':[]})):
            await self.manager.run_once('arsenal')
        self.assertEqual(self.start_run.await_count,1)


if __name__=='__main__': unittest.main()
