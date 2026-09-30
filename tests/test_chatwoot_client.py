import unittest
from unittest.mock import Mock, patch

from ics_agent.app.chatwoot.client import ChatwootAPIError, ChatwootClient
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

    def test_conversation_operations_use_user_token_when_agent_bot_token_exists(
        self,
    ) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_agent_bot_access_token="agent-bot-token",
            chatwoot_api_token="user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(client, "_request_json", return_value={}) as request_json:
            client.set_conversation_status(
                account_id="1",
                conversation_id="56",
                status="pending",
            )
            client.assign_conversation(
                account_id="1",
                conversation_id="56",
                assignee_id=11,
            )
            client.create_private_note(
                account_id="1",
                conversation_id="56",
                content="handoff",
            )
            client.toggle_typing_status(
                account_id="1",
                conversation_id="56",
                typing_status="off",
            )

        self.assertEqual(request_json.call_count, 4)
        for call in request_json.call_args_list:
            self.assertEqual(call.kwargs["access_token"], "user-token")

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

    def test_count_open_conversations_counts_nested_assignees_and_scope(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_contact_api_token="user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(
            client,
            "_request_json",
            side_effect=[
                {
                    "data": {
                        "payload": [
                            {"id": 1, "meta": {"assignee": {"id": 7}}},
                            {"id": 2, "meta": {"assignee": {"id": 8}}},
                        ]
                    }
                },
                {"data": {"payload": []}},
            ],
        ) as request_json:
            result = client.count_open_conversations(
                account_id="1",
                assignee_id=7,
                inbox_id=4,
                team_id=3,
            )

        self.assertEqual(result, 1)
        self.assertEqual(request_json.call_count, 2)
        self.assertIn(
            "/api/v1/accounts/1/conversations?",
            request_json.call_args_list[0].args[1],
        )
        self.assertIn("assignee_type=assigned", request_json.call_args_list[0].args[1])
        self.assertIn("status=open", request_json.call_args_list[0].args[1])
        self.assertIn("inbox_id=4", request_json.call_args_list[0].args[1])
        self.assertIn("team_id=3", request_json.call_args_list[0].args[1])

    def test_count_open_conversations_accepts_direct_data_list(self) -> None:
        settings = AppSettings(
            _env_file=None,
            chatwoot_base_url="http://chatwoot.test",
            chatwoot_contact_api_token="user-token",
        )
        client = ChatwootClient(settings)

        with patch.object(
            client,
            "_request_json",
            side_effect=[
                {
                    "data": [
                        {"id": 1, "meta": {"assignee": {"id": 7}}},
                        {"id": 2, "meta": {"assignee": {"id": 8}}},
                    ]
                },
                {"data": []},
            ],
        ) as request_json:
            result = client.count_open_conversations(
                account_id="1",
                assignee_id=7,
            )

        self.assertEqual(result, 1)
        self.assertEqual(request_json.call_count, 2)


class ChatwootConversationLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = ChatwootClient(
            AppSettings(
                _env_file=None,
                chatwoot_base_url="http://chatwoot.test",
                chatwoot_agent_bot_access_token="bot-token",
                chatwoot_contact_api_token="user-token",
                chatwoot_api_timeout=15,
            )
        )

    @staticmethod
    def conversation(conversation_id, assignee_id=7, **fields):
        return {
            "id": conversation_id,
            "display_id": conversation_id,
            "meta": {
                "assignee": None if assignee_id is None else {"id": assignee_id}
            },
            **fields,
        }

    def test_scans_all_inboxes_and_assignees_once_with_user_token(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[
                {"data": {"payload": [
                    self.conversation(1, 7, inbox_id=3),
                    self.conversation(2, 8, inbox_id=4),
                    self.conversation(3, 7, inbox_id=4),
                ]}},
                {"data": {"payload": []}},
            ],
        ) as request:
            result = self.client.list_open_conversation_assignments(account_id="1")

        self.assertEqual(result, {"7": {"1", "3"}, "8": {"2"}})
        self.assertEqual(request.call_count, 2)
        for page, call in enumerate(request.call_args_list, start=1):
            self.assertEqual(call.args, (
                "GET",
                f"/api/v1/accounts/1/conversations?assignee_type=all&status=open&page={page}",
            ))
            self.assertEqual(call.kwargs["access_token"], "user-token")
            self.assertEqual(call.kwargs["timeout_seconds"], 15)

    def test_accepts_response_envelopes(self) -> None:
        for wrap in (
            lambda items: {"payload": items},
            lambda items: {"data": items},
            lambda items: {"data": {"payload": items}},
        ):
            with self.subTest(wrap=wrap), patch.object(
                self.client,
                "_request_json",
                side_effect=[wrap([self.conversation(1)]), wrap([])],
            ):
                self.assertEqual(
                    self.client.list_open_conversation_assignments(account_id="1"),
                    {"7": {"1"}},
                )

    def test_deduplicates_pages_and_uses_display_ids(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[
                {"payload": [self.conversation(900, display_id=56)]},
                {"payload": [
                    self.conversation(900, display_id=56),
                    self.conversation(901, 8, display_id=57),
                    self.conversation(902, None, display_id=58),
                ], "meta": {"all_count": 3}},
                {"payload": []},
            ],
        ):
            self.assertEqual(
                self.client.list_open_conversation_assignments(account_id="1"),
                {"7": {"56"}, "8": {"57"}},
            )

    def test_missing_or_malformed_entries_never_return_partial_load(self) -> None:
        for bad in (
            {}, {"payload": None}, {"data": "invalid"},
            {"payload": [None]},
            {"payload": [{"meta": {"assignee": {"id": 7}}}]},
            {"payload": [self.conversation(True)]},
            {"payload": [{"id": 2}]},
            {"payload": [{"id": 2, "meta": {"assignee": {}}}]},
            {"payload": [self.conversation(2, 0)]},
        ):
            with self.subTest(response=bad), patch.object(
                self.client,
                "_request_json",
                side_effect=[{"payload": [self.conversation(1)]}, bad],
            ):
                with self.assertRaises(ChatwootAPIError):
                    self.client.list_open_conversation_assignments(account_id="1")

    def test_empty_account_is_zero_only_with_valid_terminal_page(self) -> None:
        with patch.object(self.client, "_request_json", return_value={"payload": []}):
            self.assertEqual(
                self.client.list_open_conversation_assignments(account_id="1"), {}
            )

    def test_advertised_total_prevents_silent_truncation(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[
                {"data": {"payload": [self.conversation(1)], "meta": {"all_count": 3}}},
                {"data": {"payload": []}},
            ],
        ):
            with self.assertRaisesRegex(ChatwootAPIError, "advertised total"):
                self.client.list_open_conversation_assignments(account_id="1")

    def test_repeated_page_does_not_look_like_complete_snapshot(self) -> None:
        with patch.object(
            self.client, "_request_json", return_value={"payload": [self.conversation(1)]}
        ) as request:
            with self.assertRaisesRegex(ChatwootAPIError, "did not advance"):
                self.client.list_open_conversation_assignments(account_id="1")
        self.assertEqual(request.call_count, 2)

    def test_changed_assignee_during_pagination_rejects_inconsistent_load(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[
                {"payload": [self.conversation(1, 7)]},
                {"payload": [self.conversation(1, 8), self.conversation(2, 8)]},
            ],
        ):
            with self.assertRaisesRegex(ChatwootAPIError, "changed during pagination"):
                self.client.list_open_conversation_assignments(account_id="1")

    def test_page_limit_never_returns_partial_snapshot(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[{"payload": [self.conversation(page)]} for page in range(1, 101)],
        ) as request:
            with self.assertRaisesRegex(ChatwootAPIError, "exceeded 100 pages"):
                self.client.list_open_conversation_assignments(account_id="1")
        self.assertEqual(request.call_count, 100)

    def test_overall_budget_bounds_request_timeouts_and_rejects_late_result(self) -> None:
        with (
            patch(
                "ics_agent.app.chatwoot.client.time.monotonic",
                side_effect=[0, 0, 10, 10, 44, 44, 45],
            ),
            patch.object(
                self.client,
                "_request_json",
                side_effect=[
                    {"payload": [self.conversation(1)]},
                    {"payload": [self.conversation(2)]},
                    {"payload": []},
                ],
            ) as request,
        ):
            with self.assertRaisesRegex(ChatwootAPIError, "timed out"):
                self.client.list_open_conversation_assignments(account_id="1")
        self.assertEqual(
            [call.kwargs["timeout_seconds"] for call in request.call_args_list],
            [15, 15, 1],
        )

    def test_http_transport_uses_remaining_budget(self) -> None:
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"payload": []}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("ics_agent.app.chatwoot.client.urlopen", return_value=response) as open_url:
            self.client._request_json("GET", "/test", timeout_seconds=0.5)
        self.assertEqual(open_url.call_args.kwargs["timeout"], 0.5)


if __name__ == "__main__":
    unittest.main()
