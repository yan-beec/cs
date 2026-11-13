import unittest
from datetime import datetime, timedelta, timezone
from copy import deepcopy
from importlib.util import find_spec

class QueuePolicyTest(unittest.TestCase):
    def policy(self):
        self.assertIsNotNone(find_spec('ics_agent.app.handoff_queue'), '缺少排队模块')
        from ics_agent.app.handoff_queue import policy
        return policy

    def test_week_boundary_and_rolling_window(self):
        p = self.policy()
        now = datetime(2026, 11, 13, 1, tzinfo=timezone.utc)
        self.assertEqual(p.week_anchor(now), now)
        self.assertEqual(p.week_anchor(now-timedelta(seconds=1)), now-timedelta(days=7))
        self.assertEqual(p.success_counts({'success_times':[now-timedelta(days=1),now-timedelta(seconds=1)],'week_anchor':now-timedelta(days=7),'week_count':40}, now), (1,0))

    def test_dynamic_tiers_and_maximum_queue(self):
        p = self.policy()
        cfg = deepcopy(p.DEFAULT_CONFIG)
        cfg.update(daily_tiers=[{'start':1,'min_count':200,'max_count':200}], weekly_tiers=[{'start':1,'min_count':200,'max_count':200}])
        p.validate_config(cfg)
        self.assertEqual(p.initial_count(cfg, 0, 0, 123), 400)
        now = datetime.now(timezone.utc)
        state = dict(initial_count=400, entered_at=now, seed=123, step_min_s=5, step_max_s=5)
        self.assertEqual(p.remaining(state,now+timedelta(seconds=5)),399)
        self.assertEqual(p.remaining(state,now+timedelta(seconds=2000)),0)
        self.assertEqual(p.estimate_minutes(2,40),2)

    def test_nondefault_highest_tier_and_validation(self):
        p = self.policy()
        cfg = deepcopy(p.DEFAULT_CONFIG)
        cfg.update(daily_tiers=[],weekly_tiers=[{'start':2,'min_count':3,'max_count':3},{'start':4,'min_count':7,'max_count':7}])
        self.assertEqual(p.initial_count(cfg,0,3,1),7)
        cfg['weekly_tiers'][1]['start']=2
        with self.assertRaises(p.ConfigError): p.validate_config(cfg)

class QueueServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_bypasses_and_redeem_survives(self):
        from unittest.mock import Mock
        from types import SimpleNamespace
        self.assertIsNotNone(find_spec('ics_agent.app.handoff_queue.service'))
        from ics_agent.app.handoff_queue.service import HandoffQueueService
        from ics_agent.app.handoff_queue.policy import DEFAULT_CONFIG
        repo=Mock()
        repo.current_config.return_value={'version':1,'config':{**deepcopy(DEFAULT_CONFIG),'enabled':True}}
        repo.get_state.side_effect=RuntimeError('库不可用')
        service=HandoffQueueService(repo)
        self.assertEqual((await service.evaluate('1','2','3')).kind,'bypass')
        repo.get_state.side_effect=None
        repo.get_state.return_value={'redeem_ready':True}
        self.assertEqual((await service.evaluate('1','2','4')).kind,'redeem')

    async def test_queue_frozen_and_disabled_success_count(self):
        from unittest.mock import Mock
        self.assertIsNotNone(find_spec('ics_agent.app.handoff_queue.service'))
        from ics_agent.app.handoff_queue.service import HandoffQueueService
        from ics_agent.app.handoff_queue.policy import DEFAULT_CONFIG
        now=datetime.now(timezone.utc)
        repo=Mock()
        cfg={**deepcopy(DEFAULT_CONFIG),'enabled':True,'step_min_s':600,'step_max_s':600}
        repo.current_config.return_value={'version':2,'config':cfg}
        repo.get_state.return_value=dict(initial_count=2,entered_at=now-timedelta(seconds=30),seed=1,step_min_s=30,step_max_s=30,redeem_ready=False)
        service=HandoffQueueService(repo)
        result=await service.evaluate('1','2','3',now)
        self.assertEqual((result.kind,result.count),('queued',1))
        cfg['enabled']=False
        await service.record_success('1','2','success',now)
        self.assertEqual(repo.record_success.call_args.args,('1','2','success',now))

class QueueIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_images_are_queued_before_seat_selection(self):
        from unittest.mock import Mock, AsyncMock, patch
        from types import SimpleNamespace
        from tests.test_handoff import _control
        from ics_agent.app.chatwoot import routes as r
        from ics_agent.app.core.models import ChatwootWebhookEvent
        repo=Mock()
        repo.record_user_message.return_value=_control(handoff_required=False)
        runtime=SimpleNamespace(ready=True,service=SimpleNamespace(saved_decision=AsyncMock(return_value=None),save_decision=AsyncMock(return_value=({},True)),evaluate=AsyncMock(return_value=SimpleNamespace(kind='queued',count=2,minutes=2))))
        self.assertTrue(hasattr(r,'get_handoff_queue_runtime'),'链路未接入排队')
        client=Mock()
        client.send_message.return_value={'id':99}
        with patch.object(r,'conversation_control_repository',repo), patch.object(r,'_refresh_handoff_from_chatwoot_status',AsyncMock(return_value=_control(handoff_required=False))), patch.object(r,'get_handoff_queue_runtime',return_value=runtime), patch.object(r,'get_chatwoot_client',return_value=client), patch.object(r,'_set_ai_chatwoot_status',AsyncMock(return_value=True)) as pending, patch.object(r,'_has_available_handoff_agent',AsyncMock()) as seats:
            for i in range(3):
                event=ChatwootWebhookEvent(event='message_created',id=900+i,account={'id':1},conversation={'id':56,'account_id':1,'status':'pending'},sender={'id':42},message_type='incoming',content='',raw={'attachments':[{'file_type':'image'}]})
                result=await r._process_webhook_job_unlocked(r.WebhookJob(event_key=f'queue-{i}',event=event))
                self.assertEqual(result['route'],'handoff_queued')
        self.assertEqual(client.send_message.call_count,3)
        self.assertEqual(pending.await_count,3)
        seats.assert_not_awaited()
        repo.request_handoff.assert_not_called()

class QueueAdminTest(unittest.TestCase):
    def test_api_conflict_validation_and_auth(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import Mock,patch
        from types import SimpleNamespace
        self.assertIsNotNone(find_spec('ics_agent.app.handoff_queue.routes'))
        from ics_agent.app.handoff_queue.routes import router
        from ics_agent.app.handoff_queue.policy import DEFAULT_CONFIG
        from ics_agent.app.handoff_queue.repository import ConfigConflict
        from ics_agent.app.handoff_queue.service import HandoffQueueService
        from ics_agent.app.core.auth import require_admin
        app=FastAPI()
        app.include_router(router)
        repo=Mock()
        repo.current_config.return_value={'version':1,'config':deepcopy(DEFAULT_CONFIG)}
        repo.save_config.side_effect=ConfigConflict()
        runtime=SimpleNamespace(ready=True,service=HandoffQueueService(repo),repository=repo)
        with TestClient(app) as client, patch('ics_agent.app.handoff_queue.routes.get_handoff_queue_runtime',return_value=runtime):
            self.assertIn(client.get('/admin/api/handoff-queue/config').status_code,(401,403,503))
            app.dependency_overrides[require_admin]=lambda:SimpleNamespace(username='admin')
            self.assertEqual(client.get('/admin/api/handoff-queue/config').json()['version'],1)
            cfg=deepcopy(DEFAULT_CONFIG)
            cfg['step_min_s']=1
            response=client.put('/admin/api/handoff-queue/config',json={'expected_version':1,'config':cfg})
            self.assertEqual(response.status_code,422)
            self.assertEqual(response.json()['detail'][0]['loc'],['config','step_min_s'])
            self.assertEqual(client.put('/admin/api/handoff-queue/config',json={'expected_version':1,'config':DEFAULT_CONFIG}).status_code,409)

class QueueCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_cache_refresh_error_preserves_last_valid_configuration(self):
        from unittest.mock import Mock,patch
        from ics_agent.app.handoff_queue.service import HandoffQueueService
        from ics_agent.app.handoff_queue.policy import DEFAULT_CONFIG
        repo=Mock()
        repo.current_config.side_effect=[{'version':4,'config':{**deepcopy(DEFAULT_CONFIG),'enabled':True}},RuntimeError('离线'),{'version':5,'config':deepcopy(DEFAULT_CONFIG)}]
        service=HandoffQueueService(repo)
        with patch('ics_agent.app.handoff_queue.service.time.monotonic',return_value=100):
            self.assertEqual((await service.current_config())['version'],4)
            self.assertEqual((await service.current_config())['version'],4)
        with patch('ics_agent.app.handoff_queue.service.time.monotonic',return_value=131):
            self.assertEqual((await service.current_config())['version'],4)
        with patch('ics_agent.app.handoff_queue.service.time.monotonic',return_value=162):
            self.assertEqual((await service.current_config())['version'],5)

    async def test_assignment_records_success_once_with_queue_disabled(self):
        from dataclasses import replace
        from unittest.mock import Mock,AsyncMock,patch
        from types import SimpleNamespace
        from tests.test_handoff import _control
        from ics_agent.app.chatwoot import routes as r
        from ics_agent.app.core.models import ChatwootWebhookEvent
        service=SimpleNamespace(record_success=AsyncMock())
        runtime=SimpleNamespace(ready=True,service=service)
        control=replace(_control(handoff_required=True),assigned_agent_id='11',handoff_trigger_message_id='700',handoff_assignment_applied=False)
        event=ChatwootWebhookEvent(event='message_created',id=700,account={'id':1},sender={'id':42},conversation={'id':56,'account_id':1},content='转人工',message_type='incoming')
        repo=Mock()
        repo.mark_handoff_action.return_value=True
        with patch.object(r,'ensure_notification',return_value=True),patch.object(r,'get_handoff_queue_runtime',return_value=runtime),patch.object(r,'get_chatwoot_client',return_value=Mock()),patch.object(r,'conversation_control_repository',repo),patch.object(r,'_handoff_session_is_current',AsyncMock(return_value=True)),patch.object(r,'settings',r.settings.model_copy(update={'chatwoot_handoff_enabled':True,'chatwoot_handoff_notice_enabled':False})):
            await r._complete_handoff(event=event,job=r.WebhookJob(event_key='count-success',event=event),control=control,content='转人工')
            await r._complete_handoff(event=event,job=r.WebhookJob(event_key='count-success',event=event),control=replace(control,handoff_assignment_applied=True),content='转人工')
        service.record_success.assert_awaited_once_with('1','42','700')

class QueueCacheOrderingTest(unittest.IsolatedAsyncioTestCase):
    async def test_late_config_read_cannot_overwrite_newer_saved_version(self):
        from unittest.mock import Mock
        from ics_agent.app.handoff_queue.service import HandoffQueueService
        from ics_agent.app.handoff_queue.policy import DEFAULT_CONFIG
        repo=Mock()
        repo.current_config.return_value={'version':4,'config':deepcopy(DEFAULT_CONFIG)}
        service=HandoffQueueService(repo)
        service.cached={'version':5,'config':{**deepcopy(DEFAULT_CONFIG),'enabled':True}}
        current=await service.current_config(force=True)
        self.assertEqual(current['version'],5)
        self.assertTrue(current['config']['enabled'])

class QueueLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_failure_does_not_interrupt_other_modules_shutdown(self):
        from unittest.mock import Mock
        from ics_agent.app.handoff_queue.runtime import HandoffQueueRuntime
        runtime=HandoffQueueRuntime()
        runtime.repository=Mock()
        runtime.repository.close.side_effect=RuntimeError('关闭失败')
        runtime.service=object()
        runtime.ready=True
        await runtime.close()
        self.assertFalse(runtime.ready)
        self.assertIsNone(runtime.repository)
        self.assertIsNone(runtime.service)

class QueueRandomProgressTest(unittest.TestCase):
    def test_random_progress_is_repeatable_monotonic_and_bounded(self):
        from ics_agent.app.handoff_queue.policy import remaining
        now=datetime.now(timezone.utc)
        state=dict(initial_count=400,entered_at=now,seed=781,step_min_s=5,step_max_s=600)
        counts=[remaining(state,now+timedelta(seconds=i)) for i in range(0,240001,200)]
        self.assertEqual(counts[0],400)
        self.assertEqual(counts[-1],0)
        self.assertEqual(counts,sorted(counts,reverse=True))
        self.assertEqual(remaining(state,now+timedelta(seconds=1000)),remaining(state,now+timedelta(seconds=1000)))
