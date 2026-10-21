"""Read-only service metrics from ordered logical messages, without message bodies."""

from datetime import datetime, timezone


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


class RoundServiceMetrics:
    """Consume one round's messages once, ordered by creation time and message ID.

    A customer burst waits from its first unanswered message until the next
    public human reply. Consecutive agent replies add no response samples. The
    first wait starts at an observed handoff, never in the earlier bot phase.
    """

    def __init__(self, round_item):
        self.round = round_item
        started = _time(round_item.get("started_at"))
        self.handoff_at = None
        self.phase_basis = None
        self.handoff_trigger_message_id = (
            str(round_item.get("responsibility_trigger_message_id"))
            if round_item.get("responsibility_trigger_message_id") not in (None, "") else None
        )
        if round_item.get("responsibility_basis") == "ics_assignment":
            # A round may contain one or more refused requests before the
            # successful assignment.  The assignment timestamp is the first
            # reliable successful-transfer boundary.
            candidates = [(round_item.get("responsible_assigned_at"), "ics_assignment")]
        else:
            candidates = [(round_item.get("handoff_requested_at"), "handoff_request")]
        if round_item.get("takeover_inherited"):
            candidates.append((started, "inherited_takeover"))
        for value, basis in candidates:
            at = _time(value)
            if at is not None and started is not None and at >= started:
                self.handoff_at, self.phase_basis = at, basis
                break
        # handoff_completed_at can be inferred from the first reply itself;
        # treating that value as a request would invent a zero-second response.
        self.phase_at = self.handoff_at
        self.pending_at = self.handoff_at
        self.first_reply_at = None
        self.last_role = None
        self.last_is_agent = None
        self.unknown_message_time = False
        self.unknown_customer_time = False
        self.unknown_agent_time = False
        self.customer_count = self.agent_count = 0
        self.response_seconds = 0.0
        self.response_count = 0

    def observe(self, message):
        data = message.get("data") or {}
        if (message.get("is_private") or message.get("is_rating", data.get("is_rating"))
                or message.get("direction") == "activity" or message.get("sender_role") == "system"):
            return
        role = message.get("sender_role")
        contact = role == "contact" and message.get("direction") == "incoming"
        agent = role == "agent" and message.get("direction") == "outgoing"
        at = _time(message.get("sent_at"))
        if at is None:
            self.unknown_message_time = True
            self.unknown_customer_time |= contact
            self.unknown_agent_time |= agent
            return
        self.last_role = role
        self.last_is_agent = agent if role in {"agent", "contact", "bot"} else None
        if (contact and self.handoff_trigger_message_id is not None
                and at == self.phase_at
                and str(message.get("message_id")) == self.handoff_trigger_message_id):
            # The message that caused a successful ICS assignment is the
            # transfer request, not the first post-transfer customer reply.
            return
        if self.phase_at is None and agent:
            # Without an observed transfer, only the first actual human reply
            # proves when a human phase was underway. Its first wait is unknown.
            self.phase_at, self.phase_basis = at, "first_agent_reply"
        if self.phase_at is None or at < self.phase_at:
            return
        if contact:
            self.customer_count += 1
            if self.pending_at is None:
                self.pending_at = at
        elif agent:
            self.agent_count += 1
            if self.first_reply_at is None:
                self.first_reply_at = at
            if self.pending_at is not None:
                self.response_seconds += (at - self.pending_at).total_seconds()
                self.response_count += 1
                self.pending_at = None

    def result(self):
        first = None
        if self.unknown_agent_time:
            first_status = "unknown_time"
        elif self.handoff_at is None:
            human = self.phase_at is not None or self.round.get("reception_type") == "human"
            first_status = "missing_handoff_time" if human else "not_handed_off"
        elif self.first_reply_at is None:
            first_status = "awaiting_agent"
        else:
            first = (self.first_reply_at - self.handoff_at).total_seconds()
            first_status = "recorded"
        average = None
        if self.response_count and not (self.unknown_customer_time or self.unknown_agent_time):
            average = self.response_seconds / self.response_count
        last_role = None if self.unknown_message_time else self.last_role
        last_by_agent = None if self.unknown_message_time else self.last_is_agent
        started = _time(self.round.get("started_at"))
        ended = None
        if self.round.get("status") == "resolved":
            ended = _time(self.round.get("resolved_at")) or _time(self.round.get("closed_at"))
        ended = ended or _time(self.round.get("last_message_at"))
        duration = (ended - started).total_seconds() if started is not None and ended is not None and ended >= started else None
        customers = None if self.unknown_customer_time else self.customer_count
        agents = None if self.unknown_agent_time else self.agent_count
        return {
            "first_response_seconds": first,
            "first_response_status": first_status,
            "average_response_seconds": average,
            "response_sample_count": self.response_count,
            "last_message_by_agent": last_by_agent,
            "last_message_sender_role": last_role,
            "duration_seconds": duration,
            "human_phase_started_at": self.phase_at.isoformat() if self.phase_at else None,
            "human_phase_basis": self.phase_basis,
            "human_phase_customer_count": customers,
            "human_phase_agent_count": agents,
            "human_phase_message_count": customers + agents if customers is not None and agents is not None else None,
        }
