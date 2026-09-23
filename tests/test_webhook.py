import hashlib
import hmac
import time
import unittest
from unittest.mock import AsyncMock, patch

from ics_agent.app.models import ChatwootWebhookEvent
from ics_agent.app.webhook import (
    is_emoji_only_text,
    parse_event,
    verify_signature,
)


class VerifySignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "webhook-secret"
        self.body = b'{"event":"message_created","content":"hello"}'
        self.timestamp = 1_700_000_000

    def signature(self, timestamp: int | None = None) -> str:
        timestamp_text = str(timestamp or self.timestamp)
        signed_payload = timestamp_text.encode("ascii") + b"." + self.body
        digest = hmac.new(
            self.secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"

    def test_accepts_chatwoot_timestamp_signature(self) -> None:
        self.assertTrue(
            verify_signature(
                self.body,
                self.signature(),
                str(self.timestamp),
                self.secret,
                now=self.timestamp,
            )
        )

    def test_rejects_modified_body(self) -> None:
        self.assertFalse(
            verify_signature(
                b'{"event":"message_created","content":"changed"}',
                self.signature(),
                str(self.timestamp),
                self.secret,
                now=self.timestamp,
            )
        )

    def test_rejects_missing_timestamp(self) -> None:
        self.assertFalse(
            verify_signature(
                self.body,
                self.signature(),
                None,
                self.secret,
                now=self.timestamp,
            )
        )

    def test_rejects_expired_timestamp(self) -> None:
        self.assertFalse(
            verify_signature(
                self.body,
                self.signature(),
                str(self.timestamp),
                self.secret,
                tolerance_seconds=300,
                now=self.timestamp + 301,
            )
        )

    def test_unconfigured_secret_keeps_development_bypass(self) -> None:
        self.assertTrue(
            verify_signature(
                self.body,
                None,
                None,
                "",
                now=time.time(),
            )
        )

    def test_parse_event_preserves_emoji_content(self) -> None:
        event = parse_event(
            '{"event":"message_created","message_type":"incoming",'
            '"content":"你好 😊"}'.encode("utf-8")
        )
        self.assertEqual("你好 😊", event.content)

    def test_detects_emoji_only_message(self) -> None:
        self.assertTrue(is_emoji_only_text("  👋🏽❤️‍🔥  "))
        self.assertTrue(is_emoji_only_text("1️⃣"))

    def test_does_not_treat_text_with_emoji_as_emoji_only(self) -> None:
        self.assertFalse(is_emoji_only_text("你好 😊"))
        self.assertFalse(is_emoji_only_text("😊?"))


class WebhookEmojiProcessingTests(unittest.IsolatedAsyncioTestCase):
    async def test_emoji_only_bypasses_rag_and_returns_ack(self) -> None:
        from ics_agent.app import main as app_main
        from ics_agent.app.webhook_queue import WebhookJob

        event = ChatwootWebhookEvent(
            event="message_created",
            id=9001,
            message_type="incoming",
            content="👋🏽",
            account={"id": 1},
            conversation={"id": 2},
        )
        with (
            patch.object(app_main.settings, "chatwoot_auto_reply_enabled", False),
            patch.object(
                app_main,
                "_run_rag_query",
                new=AsyncMock(side_effect=AssertionError("emoji entered RAG")),
            ),
        ):
            result = await app_main._process_webhook_job(
                WebhookJob(event_key="test:emoji", event=event)
            )

        self.assertEqual("emoji_ack", result["route"])
        self.assertTrue(str(result["answer"]).strip())


if __name__ == "__main__":
    unittest.main()
