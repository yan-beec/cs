"""Opt-in review persistence checks against the disposable query database."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest

from openpyxl import load_workbook
import psycopg

from quality.query.export import ExportRepository
from quality.query.repository import ArchiveFilters, QueryRepository, ReviewConflict
from quality.tests import test_query_database as fixture
from quality.tests.test_processing import event


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated query test database")
class RoundReviewDatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    receive = fixture.QueryDatabaseTest.receive
    drain = fixture.QueryDatabaseTest.drain
    rows = fixture.QueryDatabaseTest.rows

    def setUp(self):
        fixture.QueryDatabaseTest.setUp(self)
        self.receive(event(1, 0))
        self.drain()
        self.round = self.reader.list_rounds(ArchiveFilters(source_key=self.source))["items"][0]
        self.round_id = self.round["round_id"]

    def test_review_survives_projection_rebuild_and_reopening_connection(self):
        original = self.reader.round_review(self.round_id)
        self.assertEqual((original["ai_status"], original["human_status"], original["revision"], original["remark"]), ("pending", "pending", "0", ""))
        saved = self.reader.save_round_review(self.round_id, "failed", "failed", "永久备注\n<保留原文>", "admin", 0)
        self.assertEqual((saved["ai_status"], saved["human_status"]), ("failed", "failed"))
        self.receive(event(2, 2))
        self.drain()  # Deletes/recreates ops.round_index for this conversation.
        self.assertEqual(self.reader.round_review(self.round_id)["remark"], "永久备注\n<保留原文>")
        reopened = QueryRepository(self._export_settings())
        reopened.open()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.round_review(self.round_id)["revision"], saved["revision"])
        self.assertEqual(self.reader.round_detail(self.round_id)["round"]["review"]["ai_status"], "failed")
        self.assertEqual(self.reader.round_detail(self.round_id)["round"]["review"]["human_status"], "failed")
        directory = self.reader.customer_rounds(self.round["customer_id"])
        self.assertEqual(directory["items"][0]["review"]["revision"], saved["revision"])
        selected = ArchiveFilters(source_key=self.source)
        self.assertEqual(self.reader.list_rounds(selected)["items"][0]["review"]["revision"], saved["revision"])
        self.assertEqual(self.reader.list_customers(selected)["items"][0]["latest_round"]["review"]["revision"], saved["revision"])
        changed = self.reader.save_round_review(self.round_id, "failed", "passed", "已复核", "admin", int(saved["revision"]))
        self.assertNotEqual(saved["revision"], changed["revision"])
        self.assertEqual((changed["ai_status"], changed["human_status"]), ("failed", "passed"))
        self.assertEqual((self.reader.round_review(self.round_id)["ai_status"],
                          self.reader.round_review(self.round_id)["human_status"]), ("failed", "passed"))
        latest = self.reader.list_customers(selected)["items"][0]["latest_round"]["review"]
        self.assertEqual((latest["ai_status"], latest["human_status"], latest["revision"]),
                         ("failed", "passed", changed["revision"]))
        self.assertEqual(len(self.rows("quality.round_reviews")), 2)
        with self.reader.pool.connection() as connection:
            self.assertEqual(connection.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"], "on")
        self.assertIsNone(self.reader.save_round_review("missing-round", "pending", "pending", "", "admin", 0))

    def test_simultaneous_saves_reject_one_stale_revision(self):
        def save(status):
            try:
                return self.reader.save_round_review(self.round_id, "failed", status, "同一版本", "admin", 0)
            except ReviewConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, ["passed", "failed"]))
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(len(self.rows("quality.round_reviews")), 1)

    def test_runtime_roles_cannot_directly_overwrite_or_delete_reviews(self):
        self.reader.save_round_review(self.round_id, "pending", "pending", "留存", "admin", 0)
        with self.reader.pool.connection() as connection:
            for table in ("quality.round_reviews", "ops.round_index", "archive.events"):
                row = connection.execute("SELECT has_table_privilege(current_user,%s,'INSERT,UPDATE,DELETE,TRUNCATE') AS writes", (table,)).fetchone()
                self.assertFalse(row["writes"])
            self.assertFalse(connection.execute("SELECT has_function_privilege(current_user, 'quality.save_round_review(text,text,text,text,bigint)', 'EXECUTE') AS legacy").fetchone()["legacy"])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.reader.pool.connection() as connection:
                connection.execute("SET TRANSACTION READ WRITE")
                connection.execute("DELETE FROM quality.round_reviews WHERE round_id=%s", (self.round_id,))

    def test_all_chat_export_scopes_include_latest_review_in_same_sheet(self):
        first = self.reader.save_round_review(self.round_id, "failed", "failed", "旧备注", "admin", 0)
        self.reader.save_round_review(self.round_id, "failed", "passed", '=HYPERLINK("https://example.test","新备注")', "admin", int(first["revision"]))
        repository = ExportRepository(self._export_settings())
        repository.open()
        self.addCleanup(repository.close)
        for mode, scope in (("rounds", {}), ("customers", {}),
                            ("rounds", {"round_id": self.round_id}),
                            ("customers", {"customer_id": self.round["customer_id"]})):
            artifact = repository.create_export(mode, ArchiveFilters(source_key=self.source), **scope)
            self.addCleanup(artifact.cleanup)
            workbook = load_workbook(artifact.path)
            try:
                self.assertEqual(workbook.sheetnames, ["聊天记录"])
                values = list(workbook.active.values)
                row = dict(zip(values[0], values[1]))
                self.assertEqual(row["AI质检"], "不合格")
                self.assertEqual(row["人工质检"], "合格")
                self.assertEqual(row["质检备注"], '=HYPERLINK("https://example.test","新备注")')
                note_column = list(values[0]).index("质检备注") + 1
                self.assertEqual(workbook.active.cell(2, note_column).data_type, "s")
                self.assertTrue(workbook.active.auto_filter.ref.startswith("A1:M"))
            finally:
                workbook.close()

    def test_review_filters_require_both_verdicts_on_one_round_and_use_latest_revision(self):
        self.receive(event(2, 1, conversation_id=13))
        self.drain()
        other_round = next(row for row in self.rows("ops.round_index") if row["conversation_id"] == 13)
        first = self.reader.save_round_review(self.round_id, "failed", "passed", "第一轮", "admin", 0)
        self.reader.save_round_review(other_round["round_id"], "passed", "failed", "另一轮", "admin", 0)
        filters = ArchiveFilters(source_key=self.source, ai_review_status="failed", human_review_status="failed")
        # A customer must not match AI failure on one round and human failure on another.
        self.assertEqual(self.reader.list_rounds(filters)["total"], 0)
        self.assertEqual(self.reader.list_customers(filters)["total"], 0)
        self.assertEqual(self.reader.overview(filters)["total_rounds"], 0)
        self.reader.save_round_review(self.round_id, "failed", "failed", "两项不合格", "admin", int(first["revision"]))
        self.assertEqual(self.reader.list_rounds(filters, page_size=1)["items"][0]["round_id"], self.round_id)
        self.assertEqual(self.reader.list_rounds(filters)["total"], 1)
        self.assertEqual(self.reader.list_customers(filters)["total"], 1)
        self.assertEqual(self.reader.overview(filters)["total_rounds"], 1)
        obsolete = ArchiveFilters(source_key=self.source, ai_review_status="failed", human_review_status="passed")
        self.assertEqual(self.reader.list_rounds(obsolete)["total"], 0)
        self.assertEqual(self.reader.list_customers(obsolete)["total"], 0)
        repository = ExportRepository(self._export_settings())
        repository.open()
        self.addCleanup(repository.close)
        for mode, expected_messages in (("rounds", {"1"}), ("customers", {"1", "2"})):
            # Customer exports keep the complete history of matched customers.
            artifact = repository.create_export(mode, filters)
            self.addCleanup(artifact.cleanup)
            workbook = load_workbook(artifact.path)
            try:
                values = list(workbook.active.values)
                rows = [dict(zip(values[0], row)) for row in values[1:]]
                self.assertEqual({row["消息ID"] for row in rows}, expected_messages)
                target = next(row for row in rows if row["消息ID"] == "1")
                self.assertEqual((target["AI质检"], target["人工质检"]), ("不合格", "不合格"))
            finally:
                workbook.close()

    def test_pending_filters_include_unsaved_rounds_and_each_verdict_is_independent(self):
        pending = ArchiveFilters(source_key=self.source, ai_review_status="pending", human_review_status="pending")
        self.assertEqual(self.reader.list_rounds(pending)["total"], 1)
        self.assertEqual(self.reader.list_customers(pending)["total"], 1)
        saved = self.reader.save_round_review(self.round_id, "pending", "pending", "待复核", "admin", 0)
        self.assertEqual(self.reader.list_rounds(pending)["total"], 1)
        self.reader.save_round_review(self.round_id, "passed", "pending", "AI已复核", "admin", int(saved["revision"]))
        self.assertEqual(self.reader.list_rounds(pending)["total"], 0)
        self.assertEqual(self.reader.list_customers(pending)["total"], 0)
        for ai, human, expected in (("passed", None, 1), (None, "pending", 1),
                                    ("pending", None, 0), (None, "passed", 0), (None, None, 1)):
            with self.subTest(ai=ai, human=human):
                filters = ArchiveFilters(source_key=self.source, ai_review_status=ai, human_review_status=human)
                self.assertEqual(self.reader.list_rounds(filters)["total"], expected)
                self.assertEqual(self.reader.list_customers(filters)["total"], expected)

    def test_customer_light_verdicts_belong_to_latest_round_not_an_older_review(self):
        original = self.reader.save_round_review(self.round_id, "failed", "passed", "旧轮次", "admin", 0)
        self.receive(event(2, 1, event_type="conversation_status_changed", status="resolved"), event(3, 2))
        self.drain()
        filters = ArchiveFilters(source_key=self.source)
        customer = self.reader.list_customers(filters)["items"][0]
        latest_round = customer["latest_round"]
        self.assertNotEqual(latest_round["round_id"], self.round_id)
        self.assertIsNone(latest_round["review"])
        saved = self.reader.save_round_review(latest_round["round_id"], "passed", "failed", "最近一轮", "admin", 0)
        latest_review = self.reader.list_customers(filters)["items"][0]["latest_round"]["review"]
        self.assertEqual((latest_review["ai_status"], latest_review["human_status"], latest_review["revision"]),
                         ("passed", "failed", saved["revision"]))
        self.assertEqual(self.reader.round_review(self.round_id)["revision"], original["revision"])

    def _export_settings(self):
        from quality.query.config import QuerySettings
        return QuerySettings(_env_file=None, reader_password=os.environ["ARCHIVE_READER_PASSWORD"],
            postgres_host=self.settings.postgres_host, postgres_port=self.settings.postgres_port,
            postgres_db=self.settings.postgres_db, postgres_sslmode=self.settings.postgres_sslmode)
