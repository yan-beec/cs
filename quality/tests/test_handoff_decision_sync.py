import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import pytest
from quality.tests.test_assignment_sync import settings

NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)

def evidence(**changes):
    from quality.assignment.queue_evidence import queue_event_key
    row = dict(account_id='1', conversation_id='12', trigger_message_id='101', contact_id='42',
               trigger_at=NOW, decided_at=NOW, payload_version=1, outcome='queued')
    row.update(changes)
    row['event_key'] = queue_event_key(row['account_id'],row['conversation_id'],row['trigger_message_id'])
    return row

def test_queue_event_stable_and_explicit_type():
    from quality.assignment.queue_evidence import queue_event
    first = queue_event(settings(), evidence())
    assert first.event_type == 'ics_handoff_queue_decided'
    assert first == queue_event(settings(), evidence())
    assert json.loads(first.raw_body)['queue_decision']['contact_id'] == '42'
    with pytest.raises(ValueError): queue_event(settings(), evidence(contact_id=None))

def test_queued_redelivery_precedes_queue_evaluation_and_all_control_effects():
    asyncio.run(_redelivery())

async def _redelivery():
    from ics_agent.app.chatwoot import routes as r
    from ics_agent.app.core.models import ChatwootWebhookEvent
    service = Mock()
    service.saved_decision = AsyncMock(return_value=evidence())
    service.evaluate = AsyncMock()
    event = ChatwootWebhookEvent(event='message_created', id=101, account={'id':1},
        conversation={'id':12,'account_id':1}, content='转人工',message_type='incoming')
    with patch.object(r,'get_handoff_queue_runtime',return_value=SimpleNamespace(ready=True,service=service)), \
         patch.object(r,'conversation_control_repository') as control, \
         patch.object(r,'_resolve_handoff_contact',AsyncMock()) as contact, \
         patch.object(r,'_set_ai_chatwoot_status',AsyncMock()) as ai:
        result = await r._process_webhook_job(r.WebhookJob(event_key='expired',event=event))
    assert result['route'] == 'handoff_queued' and result['reused'] is True
    control.record_user_message.assert_not_called()
    contact.assert_not_awaited()
    service.evaluate.assert_not_awaited()
    ai.assert_not_awaited()

def test_fair_batch_and_spare_capacity():
    from quality.assignment.repository import AssignmentSyncRepository
    for a,q in ((30,30),(3,30),(30,2)):
        repo=object.__new__(AssignmentSyncRepository)
        repo.pending=Mock(return_value=[{'business_round_id':str(i)} for i in range(a)])
        repo.pending_queue=Mock(return_value=[evidence(trigger_message_id=str(i+1)) for i in range(q)])
        repo.append=Mock();repo.acknowledge=Mock();repo.quarantine=Mock()
        repo.sync_once()
        assert repo.append.call_count == min(20,a+q)
        rows=[c.args[0] for c in repo.append.call_args_list]
        assert sum('business_round_id' in r for r in rows) >= min(10,a)
        assert sum('event_key' in r for r in rows) >= min(10,q)

def test_runtime_reports_queue_counts_separately():
    from quality.assignment.runtime import AssignmentSyncRuntime
    runtime=AssignmentSyncRuntime()
    runtime.repository=Mock(last_batch_blocked=0,last_batch_delivered=0,
        last_batch_queue_delivered=0,last_batch_queue_blocked=0)
    runtime.repository.sync_once.return_value=0
    runtime.repository.queue_counts.return_value={'pending_count':2,'blocked_count':1}
    runtime.repository.queue_evidence_counts.return_value={'pending_count':3,'blocked_count':4}
    runtime._work(settings())
    assert runtime.queue_pending_count == 3
    assert runtime.queue_blocked_count == 4
    assert runtime.pending_count == 2

@pytest.mark.parametrize('failure', ['notice','read','write','no_contact','conflict'])
def test_queue_persistence_and_failure_boundaries(failure):
    asyncio.run(_failure_boundary(failure))

async def _failure_boundary(failure):
    from tests.test_handoff import _control
    from ics_agent.app.chatwoot import routes as r
    from ics_agent.app.core.models import ChatwootWebhookEvent
    from ics_agent.app.handoff_queue.service import QueueDecision
    event=ChatwootWebhookEvent(event='message_created',id=101,account={'id':1},
        conversation={'id':12,'account_id':1},content='转人工',message_type='incoming')
    service=SimpleNamespace(saved_decision=AsyncMock(return_value=None),
        evaluate=AsyncMock(return_value=QueueDecision('queued',2,2)),
        save_decision=AsyncMock(return_value=(evidence(),failure!='conflict')))
    if failure=='read': service.saved_decision.side_effect=OSError('read')
    if failure=='write': service.save_decision.side_effect=OSError('write')
    repo=Mock();repo.record_user_message.return_value=_control(handoff_required=False)
    client=Mock();client.send_message.side_effect=OSError('notice')
    with patch.object(r,'get_handoff_queue_runtime',return_value=SimpleNamespace(ready=True,service=service)), \
         patch.object(r,'conversation_control_repository',repo), \
         patch.object(r,'_refresh_handoff_from_chatwoot_status',AsyncMock(return_value=_control(handoff_required=False))), \
         patch.object(r,'_resolve_handoff_contact',AsyncMock(return_value=None if failure=='no_contact' else '42')), \
         patch.object(r,'_set_ai_chatwoot_status',AsyncMock()) as ai, \
         patch.object(r,'get_chatwoot_client',return_value=client), \
         patch.object(r,'_has_available_handoff_agent',AsyncMock(return_value=False)) as available, \
         patch.object(r,'_suppress_no_agent_handoff',AsyncMock(return_value={'route':'no_agent'})):
        result=await r._process_webhook_job(r.WebhookJob(event_key='fresh',event=event))
    if failure in ('read','write','no_contact'):
        assert result['route']=='no_agent'
        available.assert_awaited_once()
        ai.assert_not_awaited()
    elif failure=='notice':
        assert result['route']=='handoff_queued' and not result['notice_sent']
        service.save_decision.assert_awaited_once()
        available.assert_not_awaited()
    else:
        assert result['reused'] is True
        ai.assert_not_awaited()
        available.assert_not_awaited()
    if failure in ('read','no_contact'): service.evaluate.assert_not_awaited()


def test_same_message_is_serialized_across_evaluation_and_persistence():
    asyncio.run(_serial())

async def _serial():
    from tests.test_handoff import _control
    from ics_agent.app.chatwoot import routes as r
    from ics_agent.app.core.models import ChatwootWebhookEvent
    from ics_agent.app.handoff_queue.service import QueueDecision
    stored={}
    async def lookup(*args): return stored.get('row')
    async def save(*args):
        await asyncio.sleep(0)
        stored['row']=evidence()
        return stored['row'],True
    service=SimpleNamespace(saved_decision=AsyncMock(side_effect=lookup),evaluate=AsyncMock(return_value=QueueDecision('queued',2,2)),save_decision=AsyncMock(side_effect=save))
    event=ChatwootWebhookEvent(event='message_created',id=101,account={'id':1},conversation={'id':12,'account_id':1},content='转人工',message_type='incoming')
    repo=Mock();repo.record_user_message.return_value=_control(handoff_required=False)
    with patch.object(r,'get_handoff_queue_runtime',return_value=SimpleNamespace(ready=True,service=service)), \
         patch.object(r,'conversation_control_repository',repo), \
         patch.object(r,'_refresh_handoff_from_chatwoot_status',AsyncMock(return_value=_control(handoff_required=False))), \
         patch.object(r,'_resolve_handoff_contact',AsyncMock(return_value='42')), \
         patch.object(r,'_reply_handoff_queued',AsyncMock(return_value={'route':'handoff_queued'})):
        results=await asyncio.gather(*(r._process_webhook_job(r.WebhookJob(event_key='repeat',event=event)) for _ in range(2)))
    assert sum(row.get('reused',False) for row in results)==1
    service.evaluate.assert_awaited_once()
    service.save_decision.assert_awaited_once()
