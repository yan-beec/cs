import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from psycopg import OperationalError
from pydantic import ValidationError

from quality.capture.app import create_app
from quality.capture.config import CaptureSettings
from quality.capture.envelope import CaptureEnvelope
from quality.capture.repository import Receipt


SIGNING_SECRET = "test-signing-secret-0123456789abcdef"


def settings(**overrides):
    return CaptureSettings(
        _env_file=None, source_key="test-source", allowed_account_ids="1,2",
        webhook_secret=SIGNING_SECRET, ingest_password="test-ingest-password", **overrides,
    )


def body(**overrides):
    payload = {
        "event": "message_created", "id": 100, "account": {"id": 1},
        "conversation": {"id": 12, "account_id": 1},
        "message_type": "incoming", "sender": {"type": "contact"}, "content": "原始消息",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False).encode()


def signed_headers(raw_body, timestamp=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(SIGNING_SECRET.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Chatwoot-Signature": "sha256=" + signature, "X-Chatwoot-Timestamp": timestamp}


class CaptureHTTPTest(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.receive.return_value = Receipt(event_id=7, duplicate=False)
        self.client = TestClient(create_app(settings(), self.repository))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def post(self, raw_body=None, **kwargs):
        raw_body = body() if raw_body is None else raw_body
        return self.client.post("/quality/webhooks/chatwoot", content=raw_body, headers=signed_headers(raw_body), **kwargs)

    def test_captures_customer_bot_agent_and_private_messages_without_filtering(self):
        for message_type, sender, private in [
            ("incoming", "contact", False), ("outgoing", "agent_bot", False),
            ("outgoing", "user", False), ("outgoing", "user", True), ("activity", "user", False),
        ]:
            with self.subTest(sender=sender, message_type=message_type, private=private):
                raw = body(message_type=message_type, sender={"type": sender}, private=private)
                response = self.post(raw)
                self.assertEqual(response.status_code, 202)
                self.assertEqual(self.repository.receive.call_args.args[0].raw_body, raw)

    def test_duplicate_success_and_database_failure_are_distinct(self):
        self.repository.receive.return_value = Receipt(7, True)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["duplicate"])
        self.repository.receive.side_effect = OperationalError("private row content")
        with self.assertLogs("quality.capture.app", level="ERROR") as logs:
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private row content", "".join(logs.output) + response.text)

    def test_invalid_missing_stale_or_tampered_signatures_never_write(self):
        raw = body()
        cases = [
            {"Content-Type": "application/json"}, signed_headers(raw, int(time.time()) - 900),
            signed_headers(raw + b" "),
            {**signed_headers(raw), "X-Chatwoot-Signature": "\u00e9"},
            {**signed_headers(raw), "X-Chatwoot-Timestamp": "\u00e9"},
        ]
        for headers in cases:
            # Raw header bytes also cover malformed input that HTTP clients won't encode as text.
            encoded_headers = [(key.encode("ascii"), value.encode("latin-1")) for key, value in headers.items()]
            self.assertEqual(self.client.post("/quality/webhooks/chatwoot", content=raw, headers=encoded_headers).status_code, 401)
        self.repository.receive.assert_not_called()

    def test_invalid_envelopes_and_wrong_accounts_never_write(self):
        for raw in [b"[]", b"null", b"{", b'{"event":"message_created","account":{"id":true}}',
                    body(account={"id": 1}, conversation={"account_id": 2}),
                    b'{"event":"message_created","account":{"id":1},"value":NaN}']:
            self.assertEqual(self.post(raw).status_code, 400)
        self.assertEqual(self.post(body(account={"id": 3}, conversation={"account_id": 3})).status_code, 403)
        self.repository.receive.assert_not_called()

    def test_body_limits_apply_to_content_length_and_streamed_data(self):
        with TestClient(create_app(settings(receiver_max_body_bytes=1024), self.repository)) as client:
            raw = body(content="x" * 2000)
            self.assertEqual(client.post("/quality/webhooks/chatwoot", content=raw, headers=signed_headers(raw)).status_code, 413)
            self.assertEqual(client.post("/quality/webhooks/chatwoot", content=iter([raw[:500], raw[500:]]), headers=signed_headers(raw)).status_code, 413)
        self.repository.receive.assert_not_called()

    def test_health_and_request_method(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/quality/webhooks/chatwoot").status_code, 405)
        self.repository.ping.side_effect = OperationalError("db offline")
        self.assertEqual(self.client.get("/health").status_code, 503)


class EnvelopeTest(unittest.TestCase):
    def test_same_raw_body_deduplicates_but_edit_and_deletion_are_distinct(self):
        original = CaptureEnvelope.parse(body())
        self.assertEqual(original.event_key, CaptureEnvelope.parse(body()).event_key)
        changed = CaptureEnvelope.parse(body(event="message_updated", content="edited"))
        deleted = CaptureEnvelope.parse(body(event="message_updated", content=None, content_attributes={"deleted": True}))
        self.assertEqual(len({original.event_key, changed.event_key, deleted.event_key}), 3)
        self.assertEqual((original.message_id, original.conversation_id), (100, 12))

    def test_missing_optional_metadata_does_not_discard_signed_evidence(self):
        event = CaptureEnvelope.parse(body(id=None, conversation={}))
        self.assertIsNone(event.message_id)
        self.assertIsNone(event.conversation_id)

    def test_configuration_requires_explicit_credentials_and_account_scope(self):
        with self.assertRaises(ValidationError):
            CaptureSettings(_env_file=None, source_key="test", allowed_account_ids="", webhook_secret="", ingest_password="")
