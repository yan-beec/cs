import unittest
from unittest.mock import Mock, patch

from ics_agent.app.chatwoot.client import ChatwootClient
from ics_agent.app.core.config import AppSettings


class ChatwootContactTokenTest(unittest.TestCase):
    def test_contact_operations_use_dedicated_user_token(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_agent_bot_access_token="agent-bot-token",
            chatwoot_api_token="fallback-user-token",
            chatwoot_contact_api_token="dedicated-user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(
            client,
            "_request_json",
            return_value={},
        ) as request_json:
            client.set_contact_blocked(
                account_id="1",
                contact_id="42",
                blocked=False,
            )

        request_json.assert_called_once_with(
            "PUT",
            "/api/v1/accounts/1/contacts/42",
            {"blocked": False},
            access_token="dedicated-user-token",
        )

    def test_contact_operations_fall_back_to_user_api_token(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_agent_bot_access_token="agent-bot-token",
            chatwoot_api_token="fallback-user-token",
        )
        client = ChatwootClient(settings)
        request_json = Mock(return_value={})

        with patch.object(client, "_request_json", request_json):
            client.get_contact(account_id="1", contact_id="42")

        self.assertEqual(
            request_json.call_args.kwargs["access_token"],
            "fallback-user-token",
        )

    def test_list_agents_uses_user_token_and_normalizes_items(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_api_token="user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(
            client,
            "_request_json",
            return_value={
                "data": [
                    {"id": 1, "availability_status": "online"},
                    {"id": 2, "availability_status": "busy"},
                    "invalid",
                ]
            },
        ) as request_json:
            result = client.list_agents(account_id="1")

        self.assertEqual(
            result,
            [
                {"id": 1, "availability_status": "online"},
                {"id": 2, "availability_status": "busy"},
            ],
        )
        request_json.assert_called_once_with(
            "GET",
            "/api/v1/accounts/1/agents",
            access_token="user-token",
        )

    def test_list_team_members_uses_team_endpoint(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_contact_api_token="user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(
            client,
            "_request_json",
            return_value={"payload": [{"id": 7, "availability_status": "online"}]},
        ) as request_json:
            result = client.list_team_members(account_id="1", team_id=3)

        self.assertEqual(
            result,
            [{"id": 7, "availability_status": "online"}],
        )
        request_json.assert_called_once_with(
            "GET",
            "/api/v1/accounts/1/teams/3/team_members",
            access_token="user-token",
        )

    def test_list_inbox_members_uses_inbox_endpoint(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_api_token="user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(
            client,
            "_request_json",
            return_value={"data": [{"id": 9, "availability_status": "online"}]},
        ) as request_json:
            result = client.list_inbox_members(account_id="1", inbox_id=4)

        self.assertEqual(
            result,
            [{"id": 9, "availability_status": "online"}],
        )
        request_json.assert_called_once_with(
            "GET",
            "/api/v1/accounts/1/inbox_members/4",
            access_token="user-token",
        )


if __name__ == "__main__":
    unittest.main()
