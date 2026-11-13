"""排队判断、成功计次与配置缓存；故障只记录告警并放行。"""
import asyncio
import logging
import secrets
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from .policy import DEFAULT_CONFIG, initial_count, remaining, estimate_minutes, success_counts, validate_config
logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class QueueDecision:
    kind: str
    count: int = 0
    minutes: int = 0

class HandoffQueueService:

    def __init__(self, repository):
        self.repository = repository
        self.cached = {'version': 0, 'config': deepcopy(DEFAULT_CONFIG)}
        self.loaded_at = float('-inf')

    async def current_config(self, force=False):
        if force or time.monotonic() - self.loaded_at >= 30:
            try:
                value = await asyncio.to_thread(self.repository.current_config)
                if value and value['version'] >= self.cached['version']:
                    validate_config(value['config'])
                    self.cached = value
                self.loaded_at = time.monotonic()
            except Exception:
                logger.warning('webhook.handoff.queue_config_failed', exc_info=True)
                self.loaded_at = time.monotonic()
        return deepcopy(self.cached)

    async def evaluate(self, account, contact, message_id, now=None):
        if not account or not contact:
            return QueueDecision('bypass')
        now = now or datetime.now(timezone.utc)
        try:
            current = await self.current_config()
            cfg = current['config']
            if not cfg['enabled']:
                return QueueDecision('bypass')
            state = await asyncio.to_thread(self.repository.get_state, account, contact)
            if state is None:
                counter = await asyncio.to_thread(self.repository.get_counter, account, contact)
                (daily, weekly) = success_counts(counter, now)
                seed = secrets.randbits(63)
                count = initial_count(cfg, daily, weekly, seed)
                if count == 0:
                    return QueueDecision('bypass')
                state = await asyncio.to_thread(self.repository.create_state, account, contact, dict(initial_count=count, entered_at=now, seed=seed, step_min_s=cfg['step_min_s'], step_max_s=cfg['step_max_s'], config_version=current['version']))
            count = remaining(state, now)
            if count == 0:
                if not state.get('redeem_ready'):
                    await asyncio.to_thread(self.repository.mark_redeem, account, contact, state)
                return QueueDecision('redeem')
            return QueueDecision('queued', count, estimate_minutes(count, cfg['estimate_per_person_s']))
        except Exception:
            logger.warning('webhook.handoff.queue_evaluate_failed', exc_info=True)
            return QueueDecision('bypass')

    async def record_success(self, account, contact, message_id, now=None):
        if not account or not contact or (not message_id):
            return
        try:
            await asyncio.to_thread(self.repository.record_success, account, contact, str(message_id), now or datetime.now(timezone.utc))
        except Exception:
            logger.warning('webhook.handoff.queue_record_failed', exc_info=True)

    async def saved_decision(self, account, conversation, message):
        return await asyncio.to_thread(self.repository.saved_decision, account, conversation, message)

    async def save_decision(self, account, conversation, message, contact, trigger_at):
        from quality.assignment.queue_evidence import queue_event_key
        row = dict(account_id=account, conversation_id=conversation, trigger_message_id=message,
            contact_id=contact, trigger_at=trigger_at, decided_at=datetime.now(timezone.utc),
            event_key=queue_event_key(account,conversation,message), payload_version=1, outcome='queued')
        return await asyncio.to_thread(self.repository.save_decision, row)
