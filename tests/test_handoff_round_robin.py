import unittest
from unittest.mock import AsyncMock, Mock, patch

from ics_agent.app.chatwoot import routes
from ics_agent.app.core.models import ChatwootWebhookEvent


class HandoffRoundRobinSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_selects_online_agents_and_passes_live_loads_to_repository(self):
        event = ChatwootWebhookEvent(
            event="message_created",
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "inbox_id": 4},
            content="转人工",
            message_type="incoming",
        )
        repository = Mock()
        repository.reserve_handoff_agent = Mock(return_value="11")
        client = Mock()
        client.list_open_conversation_assignments.return_value = {
            "10": {"old-1", "old-2"},
            "11": {"old-3", "old-4", "old-5", "old-6"},
        }

        with (
            patch.object(
                routes,
                "_load_handoff_agents",
                AsyncMock(
                    return_value=(
                        "1",
                        4,
                        None,
                        [{"id": 10}, {"id": 11}],
                    )
                ),
            ),
            patch.object(routes, "get_chatwoot_client", return_value=client),
            patch.object(routes, "conversation_control_repository", repository),
            patch.object(
                routes,
                "settings",
                routes.settings.model_copy(
                    update={"chatwoot_handoff_max_active_conversations": 5}
                ),
            ),
        ):
            result = await routes._select_handoff_agent(event)

        self.assertEqual(result, "11")
        callback = repository.reserve_handoff_agent.call_args.kwargs[
            "load_open_assignments"
        ]
        self.assertEqual(callback(), client.list_open_conversation_assignments.return_value)
        client.list_open_conversation_assignments.assert_called_once_with(
            account_id="1"
        )
        kwargs = repository.reserve_handoff_agent.call_args.kwargs
        self.assertEqual(
            {key: value for key, value in kwargs.items() if key != "load_open_assignments"},
            {
                "account_id": "1",
                "conversation_id": "56",
                "scope_key": "account=1:inbox=4:team=0",
                "candidate_agent_ids": ["10", "11"],
                "max_active_conversations": 5,
                "assigned_team_id": None,
            },
        )

    async def test_load_failure_fails_closed(self):
        event = ChatwootWebhookEvent(
            event="message_created",
            account={"id": 1},
            conversation={"id": 56, "account_id": 1, "inbox_id": 4},
            content="转人工",
            message_type="incoming",
        )
        client = Mock()
        client.list_open_conversation_assignments.side_effect = RuntimeError(
            "timeout"
        )
        repository = Mock()
        repository.reserve_handoff_agent.side_effect = lambda **kwargs: kwargs[
            "load_open_assignments"
        ]()
        with (
            patch.object(
                routes,
                "_load_handoff_agents",
                AsyncMock(
                    return_value=("1", 4, None, [{"id": 10}])
                ),
            ),
            patch.object(routes, "get_chatwoot_client", return_value=client),
            patch.object(routes, "conversation_control_repository", repository),
        ):
            result = await routes._select_handoff_agent(event)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
