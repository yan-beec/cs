"""Opt-in XLSX checks against the same disposable database as query tests.

Run with PROCESSING_TEST_DATABASE=1 and the isolated query-test credentials.
The existing fixture rejects production database names before connecting.
"""

from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
import psycopg

from quality.query import excel
from quality.query.config import QuerySettings
from quality.query.export import ExportNotFound, ExportRepository, ExportTooLarge
from quality.query.repository import ArchiveFilters
from quality.tests import test_query_database as database_fixture
from quality.tests.test_processing import START, agent_reply, event


def sheet_rows(workbook, name):
    values = workbook[name].iter_rows(values_only=True)
    headers = next(values)
    return [dict(zip(headers, row)) for row in values]


@unittest.skipUnless(os.environ.get("PROCESSING_TEST_DATABASE") == "1", "Requires an isolated query test database")
class ExportDatabaseTest(unittest.TestCase):
    # Reuse fixture methods without inheriting or importing its TestCase into
    # this module's globals, which would collect every query test a second time.
    @classmethod
    def setUpClass(cls):
        database_fixture.QueryDatabaseTest.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        database_fixture.QueryDatabaseTest.tearDownClass.__func__(cls)

    receive = database_fixture.QueryDatabaseTest.receive
    drain = database_fixture.QueryDatabaseTest.drain
    rows = database_fixture.QueryDatabaseTest.rows
    receive_assignment = database_fixture.QueryDatabaseTest.receive_assignment

    def setUp(self):
        database_fixture.QueryDatabaseTest.setUp(self)
        self.export_settings = QuerySettings(
            _env_file=None, query_enabled=True,
            reader_password=os.environ["ARCHIVE_READER_PASSWORD"],
            postgres_host=self.settings.postgres_host,
            postgres_port=self.settings.postgres_port,
            postgres_db=self.settings.postgres_db,
            postgres_sslmode=self.settings.postgres_sslmode,
        )
        self.exporter = self.new_exporter()

    def new_exporter(self, **limits):
        repository = ExportRepository(self.export_settings.model_copy(update=limits))
        repository.open()
        self.addCleanup(repository.close)
        return repository

    def filters(self, **values):
        return ArchiveFilters(source_key=self.source, account_id=1, **values)

    def export_workbook(self, mode="rounds", filters=None, **scope):
        artifact = self.exporter.create_export(mode, filters or self.filters(), **scope)
        self.addCleanup(artifact.cleanup)
        workbook = load_workbook(artifact.path, read_only=True, data_only=False)
        self.addCleanup(workbook.close)
        return workbook

    def test_filtered_round_export_contains_all_pages(self):
        self.receive(*(event(index + 1, index, conversation_id=100 + index)
                       for index in range(35)))
        self.drain()
        first_page = self.reader.list_rounds(self.filters())
        self.assertEqual((len(first_page["items"]), first_page["total"]), (30, 35))

        workbook = self.export_workbook()
        rounds = sheet_rows(workbook, "轮次概览")
        messages = sheet_rows(workbook, "消息明细")
        self.assertEqual(len(rounds), 35)
        self.assertEqual(len({row["轮次ID"] for row in rounds}), 35)
        self.assertEqual(len(messages), 35)
        self.assertEqual({row["会话ID"] for row in messages}, {str(100 + index) for index in range(35)})
        self.assertEqual({row["实例来源"] for row in rounds}, {self.source})

    def test_keyword_selects_round_but_keeps_its_replies_and_private_notes(self):
        self.receive(
            event(1, content="target 100%_literal"),
            event(2, 1, sender="agent_bot", direction="outgoing", content="bot reply"),
            event(3, 2, sender="user", direction="outgoing", private=True, content="internal note"),
            event(4, 3, conversation_id=22, content="another conversation"),
        )
        self.drain()
        workbook = self.export_workbook(filters=self.filters(keyword="100%_literal"))
        self.assertEqual(len(sheet_rows(workbook, "轮次概览")), 1)
        messages = sheet_rows(workbook, "消息明细")
        self.assertEqual({row["正文分段"] for row in messages}, {"target 100%_literal", "bot reply", "internal note"})
        self.assertEqual({row["会话ID"] for row in messages}, {"12"})
        note = next(row for row in messages if row["正文分段"] == "internal note")
        self.assertEqual((note["私密消息"], note["发送人类型"]), ("是", "客服"))

    def test_default_seat_export_uses_original_ics_assignment_and_keeps_actual_repliers(self):
        self.receive(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "assigned"}),
            agent_reply(3, 2, agent_id=7, agent_name="early reply"),
            agent_reply(4, 3, agent_id=9, agent_name="final reply"),
            agent_reply(5, 4, agent_id=8, private=True),
            event(6, 5, sender="agent_bot", direction="outgoing"),
            event(7, 6, event_type="conversation_status_changed", status="resolved"),
            event(8, 7))
        assignment_id = self.receive_assignment()
        self.drain()
        workbook = self.export_workbook(filters=self.filters(agent_id="8"))
        rounds = sheet_rows(workbook, "轮次概览")
        self.assertEqual(len(rounds), 1)
        self.assertEqual((rounds[0]["责任坐席ID"], rounds[0]["ICS分配事件ID"]), ("8", str(assignment_id)))
        self.assertEqual((rounds[0]["最后实际人工回复人ID（非责任）"], rounds[0]["最后实际人工回复人名称（非责任）"], rounds[0]["最后实际人工回复消息ID"]), ("9", "final reply", "4"))
        self.assertEqual(rounds[0]["轮内最后分配坐席ID（非责任）"], "8")
        self.assertEqual({row["消息ID"] for row in sheet_rows(workbook, "消息明细")}, {"1", "3", "4", "5", "6"})
        messages = {row["消息ID"]: row for row in sheet_rows(workbook, "消息明细")}
        self.assertEqual((messages["4"]["发送人ID"], messages["4"]["发送人名称"], messages["4"]["正文分段"]), ("9", "final reply", "message 4"))
        for agent_id in ("7", "9"):
            excluded = self.export_workbook(filters=self.filters(agent_id=agent_id))
            self.assertEqual(sheet_rows(excluded, "轮次概览"), [])
        assigned = self.export_workbook(filters=self.filters(agent_id="8", agent_scope="current"))
        assigned_rounds = sorted(sheet_rows(assigned, "轮次概览"), key=lambda row: row["轮次序号"])
        self.assertEqual(len(assigned_rounds), 2)
        self.assertIsNone(assigned_rounds[1]["责任坐席ID"])
        self.assertEqual(assigned_rounds[1]["责任依据"], "未记录 ICS 分配")
        # Selecting a responsible agent chooses a user; the user export still
        # contains that user's whole history, including the next unowned round.
        history = self.export_workbook("customers", self.filters(agent_id="8"))
        self.assertEqual(len(sheet_rows(history, "用户概览")), 1)
        self.assertEqual(len(sheet_rows(history, "轮次概览")), 2)

    def test_live_directory_names_enrich_export_summary_without_rewriting_message_history(self):
        self.receive(event(1),
            event(2, 1, event_type="conversation_updated", assignee={"id": 8, "name": "分配坐席8"}),
            agent_reply(3, 2, agent_id=9, agent_name="Original responder name"))
        self.receive_assignment()
        self.drain()
        original_events, original_snapshots = self.rows("archive.events"), self.rows("archive.message_snapshots")
        names = {(self.source, "1", "8"): "Real assigned agent", (self.source, "1", "9"): "Current responder name",
            (self.source, "2", "8"): "Other account agent", ("other", "1", "8"): "Other instance agent"}
        workbook = self.export_workbook(agent_names=names)
        summary = sheet_rows(workbook, "轮次概览")[0]
        self.assertEqual((summary["责任坐席ID"], summary["责任坐席名称"]), ("8", "Real assigned agent"))
        self.assertEqual(summary["轮内最后分配坐席名称（非责任）"], "Real assigned agent")
        self.assertEqual(summary["最后实际人工回复人名称（非责任）"], "Current responder name")
        actual = next(row for row in sheet_rows(workbook, "消息明细") if row["消息ID"] == "3")
        self.assertEqual((actual["发送人ID"], actual["发送人名称"], actual["正文分段"]), ("9", "Original responder name", "message 3"))
        self.assertEqual(self.rows("archive.events"), original_events)
        self.assertEqual(self.rows("archive.message_snapshots"), original_snapshots)

    def test_live_reception_export_moves_the_same_round_from_ai_to_human_on_request(self):
        self.receive(event(1))
        self.drain()
        workbook = self.export_workbook(filters=self.filters(reception_type="ai_only"))
        initial = sheet_rows(workbook, "轮次概览")
        self.assertEqual(len(initial), 1)
        self.assertEqual((initial[0]["接待类型"], initial[0]["轮次已结束"], initial[0]["回复构成"]),
                         ("纯 AI 接待", "否", "无回复"))
        round_id = initial[0]["轮次ID"]

        self.receive(event(2, 1, event_type="conversation_updated", custom_attributes={"handoff_requested": True}))
        self.drain()
        workbook = self.export_workbook(filters=self.filters(reception_type="ai_only"))
        self.assertEqual(sheet_rows(workbook, "轮次概览"), [])
        workbook = self.export_workbook(filters=self.filters(reception_type="human"))
        changed = sheet_rows(workbook, "轮次概览")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["轮次ID"], round_id)
        self.assertEqual((changed[0]["接待类型"], changed[0]["转人工结果"], changed[0]["轮次状态"]),
                         ("人工介入", "已请求转人工", "未结束"))
        self.assertIsNone(changed[0]["接入人工时间（北京时间）"])
        self.assertEqual(len(self.rows("ops.round_index")), 1)

    def test_customer_selection_exports_complete_cross_conversation_and_unassigned_history(self):
        missing_time = event(1, message_id=100, content="partial history")
        missing_time["payload"].pop("created_at")
        self.receive(missing_time, event(2, 1, message_id=101, content="select this customer"),
                     event(3, 2, message_id=102, conversation_id=22, content="outside conversation filter"))
        self.drain()
        workbook = self.export_workbook("customers", self.filters(conversation_id=12, keyword="select this customer"))
        customers = sheet_rows(workbook, "用户概览")
        self.assertEqual(len(customers), 1)
        self.assertEqual((customers[0]["轮次数"], customers[0]["逻辑消息数"]), (2, 3))
        self.assertEqual(len(sheet_rows(workbook, "轮次概览")), 2)
        messages = sheet_rows(workbook, "消息明细")
        self.assertEqual({row["消息ID"] for row in messages}, {"100", "101", "102"})
        self.assertEqual({row["会话ID"] for row in messages}, {"12", "22"})
        unassigned = next(row for row in messages if row["消息ID"] == "100")
        self.assertIsNone(unassigned["轮次ID"])
        self.assertIsNone(unassigned["发送时间（北京时间）"])
        self.assertEqual(unassigned["时间状态"], "未知")

    def test_detail_scope_ignores_list_filters_and_unknown_scope_is_not_found(self):
        self.receive(event(1), event(2, 1, conversation_id=22))
        self.drain()
        selected_round = self.rows("ops.round_index")[0]
        customer_id = self.rows("ops.customer_index")[0]["customer_id"]
        excluded = self.filters(keyword="no matching message", conversation_id=999)

        workbook = self.export_workbook("rounds", excluded, round_id=selected_round["round_id"])
        self.assertEqual([row["轮次ID"] for row in sheet_rows(workbook, "轮次概览")], [selected_round["round_id"]])
        self.assertEqual(len(sheet_rows(workbook, "消息明细")), 1)
        workbook = self.export_workbook("customers", excluded, customer_id=customer_id)
        self.assertEqual(len(sheet_rows(workbook, "消息明细")), 2)
        for mode, scope in (("rounds", {"round_id": "round:does-not-exist"}),
                            ("customers", {"customer_id": "customer:does-not-exist"})):
            with self.subTest(mode=mode), self.assertRaises(ExportNotFound):
                self.exporter.create_export(mode, self.filters(), **scope)

    def test_entity_message_and_byte_limits_create_no_partial_file(self):
        self.receive(event(1, content="x" * 9000), event(2, 1, sender="agent_bot", direction="outgoing"),
                     event(3, 2, conversation_id=22))
        self.drain()
        for limits in ({"export_max_entities": 1}, {"export_max_messages": 1}, {"export_max_bytes": 1024}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory(prefix="quality-export-limit-test-") as directory:
                repository = self.new_exporter(**limits)
                with patch("quality.query.export.tempfile.mkstemp") as make_file:
                    with self.assertRaises(ExportTooLarge):
                        repository.create_export("rounds", self.filters())
                    make_file.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_repeatable_read_keeps_all_sheets_at_one_projection_snapshot(self):
        self.receive(event(1, content="before snapshot"))
        self.drain()
        build = excel.build_workbook
        changed = []

        def change_projection_between_sheets(destination, dataset, **metadata):
            customers = list(dataset["customers"])
            rounds = list(dataset["rounds"])
            self.receive(event(2, 1, sender="user", direction="outgoing", content="after snapshot reply"),
                         event(3, 2, conversation_id=22, content="after snapshot conversation"))
            self.drain()
            changed.append(True)
            return build(destination, {**dataset, "customers": customers, "rounds": rounds}, **metadata)

        with patch("quality.query.excel.build_workbook", side_effect=change_projection_between_sheets):
            workbook = self.export_workbook("customers")
        self.assertEqual(changed, [True])
        self.assertEqual(len(self.rows("ops.round_index")), 2)
        self.assertEqual(len(self.rows("ops.message_index")), 3)
        customers = sheet_rows(workbook, "用户概览")
        self.assertEqual((customers[0]["轮次数"], customers[0]["逻辑消息数"]), (1, 1))
        self.assertEqual(len(sheet_rows(workbook, "轮次概览")), 1)
        self.assertEqual([row["正文分段"] for row in sheet_rows(workbook, "消息明细")], ["before snapshot"])

    def test_byte_budget_includes_seat_records_before_loading_them(self):
        self.receive(event(1), event(2, 1, sender="user", direction="outgoing"))
        self.drain()
        self.admin.execute("""UPDATE ops.round_agent_index SET agent_name=%s
            WHERE round_id IN (SELECT round_id FROM ops.round_index WHERE source_key=%s)""",
            ("seat-name-" * 3000, self.source))
        baseline = sum(self.admin.execute(
            f"SELECT sum(octet_length(to_jsonb(t)::text)) AS bytes FROM ops.{table} t WHERE source_key=%s",
            (self.source,)).fetchone()["bytes"]
            for table in ("round_index", "customer_index", "message_index"))
        repository = self.new_exporter(export_max_bytes=max(1024, baseline + 1))
        with patch.object(repository, "_agents") as load_seats, patch("quality.query.export.tempfile.mkstemp") as make_file:
            with self.assertRaises(ExportTooLarge):
                repository.create_export("rounds", self.filters())
            load_seats.assert_not_called()
            make_file.assert_not_called()

    def test_export_pool_is_independent_and_database_enforces_read_only(self):
        self.assertIsNot(self.exporter.pool, self.reader.pool)
        self.assertIsNot(self.exporter.pool, self.processor.pool)
        with self.exporter.pool.connection() as connection, self.reader.pool.connection() as interactive:
            exported = connection.execute("""SELECT current_user AS role, pg_backend_pid() AS pid,
                current_setting('application_name') AS application,
                current_setting('default_transaction_read_only') AS read_only""").fetchone()
            queried = interactive.execute("SELECT pg_backend_pid() AS pid").fetchone()
            self.assertEqual((exported["role"], exported["application"], exported["read_only"]),
                             ("archive_reader", "ics-quality-export", "on"))
            self.assertNotEqual(exported["pid"], queried["pid"])
            with self.assertRaises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
                with connection.transaction():
                    connection.execute("UPDATE ops.message_index SET content='cannot write' WHERE source_key=%s", (self.source,))

    def test_workbook_failure_removes_started_output_and_export_can_retry(self):
        self.receive(event(1))
        self.drain()
        real_mkstemp = tempfile.mkstemp
        created = []
        with tempfile.TemporaryDirectory(prefix="quality-export-failure-test-") as directory:
            def make_file(*args, **kwargs):
                kwargs["dir"] = directory
                descriptor, path = real_mkstemp(*args, **kwargs)
                created.append(Path(path))
                return descriptor, path

            def fail_workbook(destination, dataset, **metadata):
                destination.write(b"incomplete workbook")
                raise RuntimeError("injected workbook failure")

            with patch("quality.query.export.tempfile.mkstemp", side_effect=make_file), \
                    patch("quality.query.excel.build_workbook", side_effect=fail_workbook):
                with self.assertRaisesRegex(RuntimeError, "injected workbook failure"):
                    self.exporter.create_export("rounds", self.filters())
            self.assertEqual(len(created), 1)
            self.assertFalse(created[0].exists())
            self.assertEqual(list(Path(directory).iterdir()), [])
        workbook = self.export_workbook()
        self.assertEqual(len(sheet_rows(workbook, "消息明细")), 1)

    def test_excel_limit_is_reported_as_a_clear_export_limit(self):
        self.receive(event(1))
        self.drain()
        with patch("quality.query.excel.build_workbook", side_effect=excel.ExcelCellLimitExceeded("非正文文本超过 Excel 单元格长度上限")):
            with self.assertRaisesRegex(ExportTooLarge, "Excel 单元格长度上限"):
                self.exporter.create_export("rounds", self.filters())

    def test_unlinked_rating_export_retains_precise_snapshot_and_evidence_fields(self):
        closed_at = START - timedelta(minutes=5)
        receipts = self.receive(event(1), event(2, 1, message_id=500, sender="agent_bot", direction="outgoing",
            content_type="input_csat", content_attributes={"ics_round_id": "unobserved-ics-round",
                "ics_closed_at": closed_at.isoformat()},
            csat_survey_response={"rating": 3, "message_id": 500, "feedback_message": "=exact feedback"}))
        self.drain()
        stored = self.rows("ops.rating_index")[0]
        self.assertEqual(stored["status"], "unlinked")
        workbook = self.export_workbook("unlinked-ratings")
        self.assertEqual(workbook.sheetnames, ["导出说明", "待关联评价"])
        own = [row for row in sheet_rows(workbook, "待关联评价") if row["实例来源"] == self.source]
        self.assertEqual(len(own), 1)
        row = own[0]
        self.assertEqual((row["账号ID"], row["会话ID"], row["邀请消息ID"]), ("1", "12", "500"))
        self.assertEqual((row["评分"], row["评价备注"], row["评价状态"]), (3, "=exact feedback", "待关联"))
        self.assertEqual(row["评价键"], stored["rating_key"])
        self.assertEqual(row["外部轮次ID"], "unobserved-ics-round")
        self.assertEqual(row["快照ID"], str(stored["snapshot_id"]))
        self.assertEqual(row["事件ID"], str(receipts[1].event_id))
        self.assertIsNone(row["轮次ID"])
        self.assertEqual(row["评价时间（北京时间）"], "2026-10-01 16:01:00+08:00")
        self.assertEqual(row["轮次关闭时间（北京时间）"], "2026-10-01 15:55:00+08:00")
        self.assertEqual(self.rows("ops.round_index")[0]["rating_status"], "not_invited")
        self.assertTrue(all(cell.data_type != "f" for cells in workbook["待关联评价"] for cell in cells))

    def test_submitted_rating_filter_exports_feedback_on_its_original_round(self):
        attrs = {"ics_round_id": "ics-business-round", "ics_closed_at": (START + timedelta(minutes=1, microseconds=1)).isoformat()}
        self.receive(event(1), event(2, 1, event_type="conversation_status_changed", status="resolved"),
                     event(3, 2),
                     event(4, 3, message_id=500, sender="agent_bot", direction="outgoing", content_type="input_csat", content_attributes=attrs),
                     event(5, 4, event_type="message_updated", message_id=500, content_type="input_csat",
                           updated_at=(START + timedelta(minutes=4)).isoformat(),
                           content_attributes={**attrs, "submitted_values": {"csat_survey_response": {
                               "rating": 5, "feedback_message": "=feedback from previous round"}}}))
        self.drain()
        workbook = self.export_workbook(filters=self.filters(rating_status="rated", rating=5))
        rows = sheet_rows(workbook, "轮次概览")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["轮次序号"], rows[0]["评分"], rows[0]["评价反馈"]),
                         (1, 5, "=feedback from previous round"))
        self.assertEqual({row["轮次序号"] for row in sheet_rows(workbook, "消息明细")}, {1})

    def test_unlinked_rating_time_does_not_become_later_card_lifecycle_time(self):
        for index, known in enumerate((False, True)):
            with self.subTest(known_rating_time=known):
                identifier = 100 + index * 10
                conversation_id = 1000 + index
                invitation_id = 500 + index
                self.receive(event(identifier, conversation_id=conversation_id),
                             event(identifier+1, 1, conversation_id=conversation_id, message_id=invitation_id,
                                   sender="agent_bot", direction="outgoing", content_type="input_csat",
                                   content_attributes={"ics_round_id": "unproven-round-" + str(index)}))
                update = event(identifier+2, 2, conversation_id=conversation_id, event_type="message_updated",
                               message_id=invitation_id, content_type="input_csat",
                               content_attributes={"submitted_values": {"csat_survey_response": {"rating": 4}}})
                if known:
                    update["payload"]["updated_at"] = (START + timedelta(minutes=2)).isoformat()
                self.receive(update,
                             event(identifier+3, 3, conversation_id=conversation_id, event_type="message_updated",
                                   message_id=invitation_id, updated_at=(START + timedelta(minutes=3)).isoformat(), content="card lifecycle"))
                self.drain()
                queried = next(row for row in self.reader.unlinked_ratings(page_size=1000)["items"]
                               if row["source_key"] == self.source and row["conversation_id"] == conversation_id)
                self.assertEqual(queried["rating_at"], START + timedelta(minutes=2) if known else None)
                workbook = self.export_workbook("unlinked-ratings")
                exported = next(row for row in sheet_rows(workbook, "待关联评价")
                                if row["实例来源"] == self.source and row["会话ID"] == str(conversation_id))
                self.assertEqual(exported["评价时间（北京时间）"], "2026-10-01 16:02:00+08:00" if known else None)


if __name__ == "__main__":
    unittest.main()
