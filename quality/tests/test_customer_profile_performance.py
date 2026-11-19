"""验证资料读取避免重复回查，同时保留缺失资料的恢复能力。"""

from contextlib import nullcontext
from unittest.mock import Mock

from quality.query.repository import QueryRepository


def history_repository(profile):
    """提供客户行和空消息页，记录资料补查访问。"""
    connection = Mock()
    connection.transaction.side_effect = lambda: nullcontext()
    statements = []

    def execute(sql, params=()):
        statements.append(sql)
        result = Mock()
        result.fetchall.return_value = []
        if "FROM ops.customer_index c WHERE c.customer_id" in sql:
            result.fetchone.return_value = {"customer_id": "c1", "contact_id": "42", "data": profile}
        elif "count(*)" in sql:
            result.fetchone.return_value = {"total": 0}
        if "archive.conversation_snapshots" in sql:
            result.fetchall.return_value = [{"data": {"meta": {"sender": {
                "id": 42, "phone_number": "archived-phone", "description": "历史备注"}}}}]
        return result

    connection.execute.side_effect = execute
    repository = QueryRepository.__new__(QueryRepository)
    repository.pool = Mock()
    repository.pool.connection.return_value = nullcontext(connection)
    repository._blacklist_history = lambda *_: None
    return repository, statements


def test_complete_customer_profile_does_not_read_historical_evidence_on_any_page():
    profile = {"phone_number": "known-phone", "contact_note": "现有备注", "h5_user_id": "h5-42"}
    repository, statements = history_repository(profile)
    for page in (1, 2):
        customer = repository.customer_history("c1", page=page)["customer"]
        assert customer["phone_number"] == "known-phone"
        assert customer["contact_note"] == "现有备注"
    assert not any("archive.conversation_snapshots" in sql or "archive.events" in sql for sql in statements)


def test_null_profile_fields_still_recover_earlier_contact_details():
    repository, statements = history_repository({"phone_number": None, "contact_note": None})
    customer = repository.customer_history("c1")["customer"]
    assert customer["phone_number"] == "archived-phone"
    assert customer["contact_note"] == "历史备注"
    assert any("archive.conversation_snapshots" in sql for sql in statements)
