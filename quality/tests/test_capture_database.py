"""Opt-in HTTP/database integration checks, restricted to disposable test databases."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import psycopg

from quality.capture.config import CaptureSettings
from quality.capture.envelope import CaptureEnvelope
from quality.capture.repository import CaptureRepository
from quality.tests.test_capture import signed_headers


@unittest.skipUnless(os.environ.get("CAPTURE_TEST_DATABASE") == "1", "Requires an isolated capture test database")
class CaptureDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = CaptureSettings()
        if not cls.settings.postgres_db.startswith("quality_capture_test_") or not cls.settings.source_key.startswith("capture-test-"):
            raise RuntimeError("Integration tests only operate on disposable capture test databases")
        kwargs = dict(cls.settings.connection_kwargs)
        kwargs.update(user="archive_admin", password=os.environ["CAPTURE_TEST_ADMIN_PASSWORD"])
        cls.admin = psycopg.connect(**kwargs, autocommit=True)
        cls.url = os.environ["CAPTURE_TEST_URL"].rstrip("/") + "/quality/webhooks/chatwoot"

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def raw(self, **overrides):
        payload = {
            "event": "message_created", "id": 100, "account": {"id": 1},
            "conversation": {"id": 12, "account_id": 1}, "message_type": "incoming",
            "content": "immutable original", "verification_key": str(uuid.uuid4()),
        }
        payload.update(overrides)
        return json.dumps(payload, ensure_ascii=False).encode()

    def post(self, raw):
        request = Request(self.url, data=raw, headers=signed_headers(raw), method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def test_captures_both_directions_and_private_messages_as_durable_work(self):
        for direction, role, private in [
            ("incoming", "contact", False), ("outgoing", "agent_bot", False),
            ("outgoing", "user", False), ("outgoing", "user", True),
        ]:
            raw = self.raw(message_type=direction, sender={"type": role}, private=private)
            status, receipt = self.post(raw)
            self.assertEqual(status, 202)
            row = self.admin.execute("""
                SELECT e.raw_body, e.body_sha256, j.status FROM archive.events e
                JOIN ops.event_jobs j ON j.event_id=e.id WHERE e.id=%s
            """, (receipt["event_id"],)).fetchone()
            self.assertEqual(bytes(row[0]), raw)
            self.assertEqual(row[1], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row[2], "queued")

    def test_concurrent_duplicates_create_exactly_one_event_and_job(self):
        raw = self.raw()
        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(self.post, [raw] * 12))
        self.assertEqual(sum(status == 202 for status, _ in responses), 1, responses)
        self.assertEqual(sum(status == 200 for status, _ in responses), 11, responses)
        event_ids = {receipt["event_id"] for _, receipt in responses}
        self.assertEqual(len(event_ids), 1)
        self.assertEqual(self.admin.execute("SELECT count(*) FROM ops.event_jobs WHERE event_id=%s", (event_ids.pop(),)).fetchone()[0], 1)

    def test_edit_and_delete_observations_leave_the_original_body_intact(self):
        marker = str(uuid.uuid4())
        bodies = [
            self.raw(verification_key=marker),
            self.raw(verification_key=marker, event="message_updated", content="changed"),
            self.raw(verification_key=marker, event="message_updated", content=None, content_attributes={"deleted": True}),
        ]
        ids = [self.post(raw)[1]["event_id"] for raw in bodies]
        self.assertEqual(len(set(ids)), 3)
        for event_id, raw in zip(ids, bodies):
            self.assertEqual(bytes(self.admin.execute("SELECT raw_body FROM archive.events WHERE id=%s", (event_id,)).fetchone()[0]), raw)

    def test_failed_job_insert_rolls_back_evidence_and_returns_503(self):
        raw = self.raw()
        self.admin.execute("ALTER TABLE ops.event_jobs ADD CONSTRAINT capture_test_reject_jobs CHECK (event_id < 0) NOT VALID")
        try:
            status, _ = self.post(raw)
            self.assertEqual(status, 503)
            count = self.admin.execute("SELECT count(*) FROM archive.events WHERE source_key=%s AND event_key=%s",
                                       (self.settings.source_key, CaptureEnvelope.parse(raw).event_key)).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            self.admin.execute("ALTER TABLE ops.event_jobs DROP CONSTRAINT capture_test_reject_jobs")
        self.assertEqual(self.post(raw)[0], 202)

    def test_ingest_role_cannot_modify_accepted_evidence(self):
        status, receipt = self.post(self.raw())
        self.assertEqual(status, 202)
        repository = CaptureRepository(self.settings)
        repository.open()
        try:
            with repository.pool.connection() as connection:
                for sql in ["UPDATE archive.events SET event_type='changed' WHERE id=%s", "DELETE FROM archive.events WHERE id=%s"]:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute(sql, (receipt["event_id"],))
        finally:
            repository.close()

    def test_raw_numeric_precision_survives_python_json_parsing(self):
        raw = self.raw()[:-1] + b',"precise":12345678901234567890.12345678901234567890}'
        status, receipt = self.post(raw)
        self.assertEqual(status, 202)
        value = self.admin.execute("SELECT payload->>'precise' FROM archive.events WHERE id=%s", (receipt["event_id"],)).fetchone()[0]
        self.assertEqual(value, "12345678901234567890.12345678901234567890")
