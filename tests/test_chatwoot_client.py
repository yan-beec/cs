import unittest
from unittest.mock import Mock, patch

from ics_agent.app.chatwoot_client import ChatwootClient
from ics_agent.app.config import AppSettings


class ChatwootContactTokenTest(unittest.TestCase):
    def test_contact_operations_use_dedicated_user_token(self) -> None:
        settings = AppSettings(
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


if __name__ == "__main__":
    unittest.main()
