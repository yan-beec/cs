"""隔离 PostgreSQL 与真实 Node 使用同一只读快照比较完整筛选集合。"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from quality.offline.bundle import build_bundle
from quality.offline.config import OfflineSettings
from quality.offline.media import MediaDownloader
from quality.offline import repository as offline
from quality.processing.config import ProcessingSettings
from quality.processing.repository import ProcessingRepository
from quality.processing.service_facts import FACT_VERSION, store
from quality.query.repository import (ArchiveFilters, customer_conditions,
    customer_unrounded_message_predicate, load_facts, round_conditions)
from quality.tests.test_processing import START, agent_reply, event, ics_assignment
from quality.query.export import ExportRepository
from quality.query.routes import archive_filters, BLACKLIST_STATE

FILTERS = Path(__file__).parents[1] / "offline/web/filters.js"
pytestmark = pytest.mark.skipif(os.environ.get("PROCESSING_TEST_DATABASE") != "1",
                               reason="仅允许显式启用的隔离数据库")


class BorrowedRepository:
    """把真实导出入口绑定到测试已有事务，避免另开一个数据库快照。"""
    _selection = staticmethod(ExportRepository._selection)
    def __init__(self, connection):
        self.pool = self
        self.connection_value = connection

    def open(self):
        pass

    def close(self):
        pass

    @contextmanager
    def connection(self):
        yield self.connection_value


def _read_bundle(path):
    """按实际 HTML 数据块及字符偏移组装观察正文，不用 Python 导出中间对象代替。"""
    blocks = {key: json.loads(value) for key, value in re.findall(
        r'<script type="application/json" id="([^"]+)">(.*?)</script>',
        path.read_text(encoding="utf-8"), re.S)}
    catalog = blocks["catalog"]
    versions = defaultdict(list)
    for key, value in blocks.items():
        if re.fullmatch(r"v\d+", key):
            identity = (value["source_key"], str(value["account_id"]), str(value["message_id"]), value["version"])
            versions[identity].append(value)
    contents = defaultdict(list)
    for identity, chunks in versions.items():
        chunks.sort(key=lambda item: item["offset"])
        offset = 0
        for chunk in chunks:
            assert chunk["offset"] == offset
            offset += len(chunk["content"])
        assert chunks[-1]["last"]
        contents[identity[:3]].append("".join(item["content"] for item in chunks))
    messages = {}
    for entry in catalog["entries"]:
        rows = [row for part in range(entry["parts"]) for row in blocks[f'g{entry["index"]}.{part}']]
        for row in rows:
            key = (entry["source_key"], str(entry["account_id"]), str(row["message_id"]))
            row["search_contents"] = contents[key]
        messages[str(entry["index"])] = rows
    return catalog, messages


def _online(connection, filters, exported):
    """直接执行后台原函数生成的 SQL，并与本文件的真实导出集合求交。"""
    facts = load_facts(connection, filters)
    where, params = round_conditions(filters, facts)
    rounds = connection.execute(f"SELECT r.round_id FROM ops.round_index r JOIN ops.customer_index c "
                                f"ON c.customer_id=r.customer_id WHERE {where}", params).fetchall()
    round_ids = {row["round_id"] for row in rounds} & exported["round_ids"]
    message_ids = set()
    unrounded = customer_unrounded_message_predicate(filters, "c.customer_id")
    if unrounded is not None:
        # 用户级搜索和黑名单与后台客户详情/导出的调用方式一致。
        customer_where, customer_params = customer_conditions(ArchiveFilters(source_key=filters.source_key,
            account_id=filters.account_id, customer_query=filters.customer_query, blacklist=filters.blacklist))
        rows = connection.execute(f"SELECT m.source_key,m.account_id,m.message_id FROM ops.message_index m "
            f"JOIN ops.customer_index c ON c.customer_id=m.customer_id WHERE ({unrounded[0]}) "
            f"AND ({customer_where})", [*unrounded[1], *customer_params]).fetchall()
        message_ids = {(row["source_key"], str(row["account_id"]), str(row["message_id"])) for row in rows} & exported["message_ids"]
    customer_ids = {exported["round_customers"][key] for key in round_ids}
    customer_ids.update(exported["message_customers"][key] for key in message_ids)
    return {"round_ids": sorted(round_ids), "message_ids": sorted(map(list, message_ids)),
            "customer_ids": sorted(customer_ids)}


@pytest.fixture(scope="module")
def parity_snapshot(tmp_path_factory):
    settings = ProcessingSettings()
    if not settings.postgres_db.startswith(("quality_query_test_", "quality_capture_test_")):
        pytest.fail("必须使用一次性隔离数据库")
    admin = psycopg.connect(**{**settings.connection_kwargs, "user": "archive_admin",
        "password": os.environ["PROCESSING_TEST_ADMIN_PASSWORD"], "row_factory": dict_row}, autocommit=True)
    source = "offline-parity-" + uuid.uuid4().hex
    processor = ProcessingRepository(settings, {("1", "3"): "xingliao", ("2", "3"): "xingliao"})
    processor.open()
    try:
        events = []
        for account in (1, 2):
            # 人工主责、协助、纯 AI、无回复流单、未归轮及跨月轮次各自有独立会话。
            identities = [(12, 42), (22, 43), (32, 44), (42, 45), (52, 46), (62, 47), (72, 48),
                          *[(80+i, 80+i) for i in range(8)], (99, 99)]
            for conversation, contact in identities:
                base = conversation * 100
                if 80 <= conversation < 88:
                    # 每个 Unicode 正文独占轮次，不能用同一正文同时放大小写掩盖差异。
                    bodies = ["ΟΣ", "İ", "ß", "SS", "é", "é", "\U00010400", "x"*65535+"😀boundary-seam"]
                    rows = [event(base, content=bodies[conversation-80])]
                elif conversation == 99:
                    rows = [event(base, content="microsecond", created_at=START.replace(microsecond=100).isoformat())]
                elif conversation == 72:
                    rows = [event(base, content="转人工")]
                    queue = event(base+1, event_type="ics_handoff_queue_decided")
                    queue["ingestion_kind"] = "backfill"
                    queue["payload"] = {"event": "ics_handoff_queue_decided", "account": {"id": account},
                        "conversation": {"id": conversation, "account_id": account}, "updated_at": START.isoformat(),
                        "queue_decision": {"schema_version": 1, "origin": "ics", "outcome": "queued",
                        "trigger_message_id": str(base), "contact_id": str(contact),
                        "trigger_at": START.isoformat(), "decided_at": START.isoformat()}}
                    rows.append(queue)
                elif conversation == 52:
                    rows = [agent_reply(base, content="orphan NEEDLE", conversation_id=conversation),
                            agent_reply(base+1, 1, content="orphan other", conversation_id=conversation),
                            event(base+2, 2, event_type="conversation_updated", assignee={"id": 9, "type": "user"})]
                elif conversation == 62:
                    rows = [event(base, content="cross month", created_at="2026-09-30T23:59:59.999+08:00"),
                            event(base+1, event_type="conversation_status_changed", status="resolved",
                                  updated_at="2026-10-01T00:00:00.001+08:00")]
                else:
                    rows = [event(base, content="NEEDLE 100%_\\字 İ I ı Σ σ ς É é 😀", attachments=[
                        {"id": base, "file_name": "filename-only-needle.pdf", "data_url": "https://invalid.test/a.pdf"}]),
                        event(base+1, 1, content="NEEDLE no attachment"),
                        event(base+2, 2, event_type="message_updated", message_id=base,
                              created_at=START.isoformat(), content="current changed", attachments=[
                        {"id": base, "file_name": "filename-only-needle.pdf", "data_url": "https://invalid.test/a.pdf"}]),
                        agent_reply(base+3, 3, agent_id=7, content="agent separate", private=True),
                        agent_reply(base+4, 4, agent_id=7, content="   "),
                        agent_reply(base+5, 5, agent_id=7, content="deleted", content_attributes={"deleted": True})]
                    if conversation in (12, 22, 42):
                        assignment = ics_assignment(base+10, agent_id=7, trigger_message_id=base,
                            business_round_id=f"parity-{account}-{conversation}")
                        rows.append(assignment)
                    if conversation in (12, 22):
                        rows.append(agent_reply(base+6, 6, agent_id=7 if conversation == 12 else 8, content="public reply"))
                    if conversation == 32:
                        rows.append(event(base+6, 6, sender="agent_bot", direction="outgoing", content="bot body"))
                    rows.extend([event(base+7, 7, event_type="conversation_updated", assignee={"id": 9, "type": "user"}),
                                 event(base+8, 8, event_type="conversation_status_changed", status="resolved")])
                    if conversation in (12, 22, 32):
                        rows.append(event(base+9, 9, sender="agent_bot", direction="outgoing", content_type="input_csat",
                            content_attributes={"ics_closed_at": (START+timedelta(minutes=8)).isoformat()},
                            csat_survey_response={"message_id": base+9, "rating": {12: 2, 22: 3, 32: 5}[conversation]}))
                for row in rows:
                    payload = row["payload"]
                    payload["account"] = {"id": account}
                    if row["event_type"].startswith("message_"):
                        payload["conversation"]["id"] = conversation
                        payload["conversation"]["account_id"] = account
                        payload["conversation"]["meta"]["sender"].update(id=contact, name=f"客户{contact}")
                        if payload["sender"]["type"] == "contact":
                            payload["sender"]["id"] = contact
                    elif row["event_type"] in ("ics_assignment_recorded", "ics_handoff_queue_decided"):
                        payload["conversation"].update(id=conversation, account_id=account)
                    else:
                        payload["id"] = conversation
                        payload["meta"]["sender"]["id"] = contact
                    events.append((account, conversation, row))
        for number, (account, conversation, row) in enumerate(events):
            payload = row["payload"]
            record = admin.execute("""INSERT INTO archive.events
                (source_key,account_id,event_key,event_type,ingestion_kind,conversation_id,message_id,raw_body,payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (source, account, str(number), row["event_type"], row["ingestion_kind"], conversation,
                 row["message_id"], json.dumps(payload).encode(), Jsonb(payload))).fetchone()
            admin.execute("INSERT INTO ops.event_jobs(event_id) VALUES(%s)", (record["id"],))
        # 只领取本 fixture 的任务；不能消费并行测试或遗留来源的全局队列。
        for _ in range(100):
            claim = admin.execute("""UPDATE ops.event_jobs SET status='processing',locked_by='offline-parity',
                locked_until=now()+interval '60 seconds',attempts=attempts+1 WHERE event_id=(
                SELECT j.event_id FROM ops.event_jobs j JOIN archive.events e ON e.id=j.event_id
                WHERE e.source_key=%s AND j.status IN ('queued','retry') ORDER BY j.event_id LIMIT 1)
                RETURNING event_id,locked_by,attempts""", (source,)).fetchone()
            if claim is None:
                break
            processor.process_claim(claim)
        else:
            pytest.fail("fixture 队列未收敛")
        admin.execute("UPDATE ops.customer_index SET business_user_id='biz-'||contact_id, "
                      "data=data||jsonb_build_object('h5_user_id','H5-'||contact_id) WHERE source_key=%s", (source,))
        # 清除未归轮的消息缓存身份，实际验证后台与快照共同使用会话索引的 NULL 回退。
        admin.execute("UPDATE ops.message_index SET data=data-'inbox_id'-'channel_key' "
                      "WHERE source_key=%s AND conversation_id=52", (source,))
        for seq, contact, action in ((1, "42", "block"), (2, "42", "unblock"), (3, "43", "block")):
            admin.execute("""INSERT INTO archive.contact_block_events
                (source_key,account_id,event_key,source_seq,contact_id,action,source,permanent,occurred_at,payload)
                VALUES(%s,1,%s,%s,%s,%s,'client',true,now(),'{}')""", (source, str(seq), str(seq), contact, action))
        ids = [row["round_id"] for row in admin.execute("SELECT round_id FROM ops.round_index WHERE source_key=%s", (source,))]
        seeded_rounds = admin.execute("SELECT round_id,conversation_id,reception_type FROM ops.round_index WHERE source_key=%s", (source,)).fetchall()
        for row in seeded_rounds:
            if row["conversation_id"] in (12, 22, 32):
                verdict = "failed" if row["conversation_id"] == 22 else "passed"
                human = verdict if row["reception_type"] == "human" else None
                saved = admin.execute("SELECT quality.save_round_review(%s,%s,%s,'对照质检','parity',0) AS review",
                    (row["round_id"], verdict, human)).fetchone()["review"]
                assert saved["ai_status"] == verdict
                if row["conversation_id"] == 32:
                    revision = admin.execute("SELECT quality.get_rating_review(%s) AS review", (row["round_id"],)).fetchone()["review"]
                    invalid = admin.execute("SELECT quality.save_rating_review(%s,'invalid','parity',0,%s) AS review",
                        (row["round_id"], revision["content_revision"])).fetchone()["review"]
                    assert invalid["status"] == "invalid"
        with admin.transaction():
            store(admin, ids)
        assert FACT_VERSION == 11
        assert {row["fact_version"] for row in admin.execute("SELECT fact_version FROM ops.round_service_facts WHERE source_key=%s", (source,))} == {11}
        facts_rows = admin.execute("SELECT r.conversation_id,r.reception_type,f.* FROM ops.round_service_facts f "
            "JOIN ops.round_index r ON r.round_id=f.round_id WHERE r.source_key=%s", (source,)).fetchall()
        for row in facts_rows:
            if row["conversation_id"] == 72:
                assert row["reception_type"] == "ai_only" and not row["is_handoff"] and not row["is_lost"]
            if row["conversation_id"] == 22:
                assert row["assisted"]
            if row["conversation_id"] == 42:
                assert row["is_lost"]
        with admin.transaction():
            admin.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            metadata = {}
            request = {"mode": "customers", "view": "customers", "filters": {"source_key": source}}
            class SnapshotRepository(BorrowedRepository):
                def __init__(self, _):
                    super().__init__(admin)
            with patch.object(offline, "ExportRepository", SnapshotRepository), patch.object(offline, "_agent_names", return_value={}):
                rows = list(offline.messages(request, lambda: None, metadata=metadata))
            root = tmp_path_factory.mktemp("parity")
            settings_offline = OfflineSettings(_env_file=None, directory=str(root), min_free_bytes=0)
            path = build_bundle(root / "bundle", iter(rows), settings_offline, MediaDownloader([]),
                lambda **_: None, lambda: None, scope=request, metadata=metadata)
            catalog, messages = _read_bundle(path)
            exported = {"round_ids": set(), "message_ids": set(), "round_customers": {}, "message_customers": {}}
            for entry in catalog["entries"]:
                customer = catalog["customers"][entry["customer"]]["id"]
                if entry["round_id"]:
                    exported["round_ids"].add(entry["round_id"])
                    exported["round_customers"][entry["round_id"]] = customer
                else:
                    for message in messages[str(entry["index"])]:
                        key = (entry["source_key"], str(entry["account_id"]), str(message["message_id"]))
                        exported["message_ids"].add(key)
                        exported["message_customers"][key] = customer
            assert len(exported["round_ids"]) >= 10
            assert len(exported["message_ids"]) == 4
            yield admin, source, catalog, messages, exported
    finally:
        processor.close()
        admin.close()


def _cases():
    cases = [("all", {})]
    for field in ("started_at", "message_at", "resolved_at", "rating_at", "handoff_at", "reply_at", "assigned_at"):
        cases.extend((f"time-{field}-{minute}", {"time_field": field, "start": START+timedelta(minutes=minute),
            "end": START+timedelta(minutes=minute+2)}) for minute in (0, 5, 8, 20))
    for scope in ("responsible", "replied", "assigned", "current"):
        for agent in ("7", "8", "9"):
            for account in (1, 2):
                cases.append((f"seat-{scope}-{agent}-{account}", {"account_id": account, "agent_id": agent, "agent_scope": scope}))
    for name, values in {"reception_type": ["ai_only", "human"], "rating_status": ["rated", "not_invited", "pending"],
        "blacklist": [None, "ever", "active"], "assisted": [True, False], "unassigned": [True, False],
        "lost": [True, False], "outcome": ["ai_only", "handoff", "refused", "effective_human", "lost"],
        "dialogue": ["dialogue", "high_quality"], "ai_review_status": ["passed", "failed", "pending"],
        "human_review_status": ["passed", "failed", "pending"], "conversation_id": [12, 52, 999],
        "round_no": [1, 2], "inbox_id": [3, 999], "channel_key": ["xingliao", "unmapped"],
        "status": ["resolved", "active"]}.items():
        for value in values:
            cases.append((f"{name}-{value}", {name: value}))
    for operator in ("eq", "gt", "lt"):
        for score in (0, 3, 5):
            cases.append((f"rating-{operator}-{score}", {"rating": score, "rating_op": operator}))
    for keyword in ("NEEDLE", "current changed", "100%_\\字", "filename-only-needle", "orphan NEEDLE", "İ", "i", "i̇", "ı", "Σ", "σ", "ς", "é", "é", "ß", "SS", "οσ", "ος", "\U00010428", "😀boundary-seam", "😀", "absent"):
        cases.append((f"keyword-{keyword}", {"keyword": keyword}))
    for scope in ("all", "contact", "agent", "bot", "private"):
        for attachment in (None, True, False):
            cases.append((f"message-{scope}-{attachment}", {"keyword": "NEEDLE", "message_scope": scope, "has_attachment": attachment}))
        cases.append((f"scope-{scope}", {"message_scope": scope}))
    for attachment in (True, False):
        cases.append((f"attachment-only-{attachment}", {"has_attachment": attachment}))
    for query in ("客户42", "42", "H5-42", "biz-42", "__exact_customer__", "absent"):
        cases.append((f"customer-{query}", {"customer_query": query}))
    for delta in (-1, 0, 1):
        cases.append((f"millisecond-{delta}", {"time_field": "message_at", "start": START+timedelta(milliseconds=delta),
            "end": START+timedelta(milliseconds=delta+1)}))
    cases.extend([("same-message-time", {"keyword": "NEEDLE", "time_field": "message_at", "start": START+timedelta(minutes=3), "end": START+timedelta(minutes=7)}),
        ("reply-independent", {"keyword": "NEEDLE", "time_field": "reply_at", "start": START+timedelta(minutes=6), "end": START+timedelta(minutes=7)}),
        ("as-of", {"as_of": START+timedelta(minutes=5)}),
        ("microsecond-as-of", {"conversation_id": 99, "as_of": START.replace(microsecond=900)}),
        ("microsecond-message-window", {"conversation_id": 99, "time_field": "message_at", "start": START,
            "end": START.replace(microsecond=900)}),
        ("microsecond-message-end-exclusive", {"conversation_id": 99, "time_field": "message_at", "start": START,
            "end": START.replace(microsecond=100)}),
        ("microsecond-started-window", {"conversation_id": 99, "start": START.replace(microsecond=100),
            "end": START.replace(microsecond=900)}),
        ("cross-month", {"time_field": "resolved_at", "start": START-timedelta(hours=16), "end": START-timedelta(hours=15)})])
    return cases


CASES = _cases()


@pytest.mark.parametrize("name,changes", CASES, ids=[case[0] for case in CASES])
def test_database_node_filter_parity(parity_snapshot, name, changes):
    connection, source, catalog, messages, exported = parity_snapshot
    changes = dict(changes)
    if changes.get("customer_query") == "__exact_customer__":
        changes["customer_query"] = next(c["id"] for c in catalog["customers"] if c["contact_id"] == "42" and str(c["account_id"]) == "1")
    raw_filters = ArchiveFilters(source_key=source, **{"account_id": 1, **changes})
    with patch.dict(BLACKLIST_STATE, ready=True):
        filters = archive_filters(**raw_filters.__dict__)
    expected = _online(connection, filters, exported)
    if name in ("ai_review_status-passed", "ai_review_status-failed", "human_review_status-passed", "human_review_status-failed",
                "rating-eq-5", "assisted-True", "lost-True", "microsecond-as-of", "microsecond-message-window", "microsecond-started-window", "cross-month"):
        assert expected["round_ids"], "正样本必须存在：" + name
    if name in ("time-started_at-0", "time-message_at-0", "time-resolved_at-8", "time-rating_at-8",
                "time-handoff_at-0", "time-reply_at-5", "time-assigned_at-0"):
        assert expected["round_ids"], "七种时间均必须有命中样本：" + name
    if name.startswith("time-") and name.endswith("-20"):
        assert expected["round_ids"] == [], "不命中的时间窗口不能回退到其他字段"
    if name in ("seat-current-9-1", "seat-current-9-2", "inbox_id-3", "channel_key-xingliao"):
        assert len(expected["message_ids"]) == 2, "未归轮的当前分配/收件箱/渠道回退必须实际命中"
    if name == "message-contact-False":
        assert expected["round_ids"] == [], "同关键词有附件/无附件共存时不能命中无附件轮次"
    if name == "keyword-orphan NEEDLE":
        assert len(expected["message_ids"]) == 1, "只能保留命中那条未归轮消息"
    payload = {"catalog": catalog, "messages": messages, "filters": filters.__dict__}
    script = """const fs=require('fs');const engine=require(process.argv[1]);
    const p=JSON.parse(fs.readFileSync(0,'utf8'));const r=engine.select(p.catalog.entries,p.catalog.customers,
      e=>p.messages[String(e.index)],p.filters,p.catalog.meta.filter_contract);
    const assert=require('assert');for(const e of r.entries){
      if(e.round_id)assert.strictEqual(e.messages,p.messages[String(e.index)].length);
      else assert.strictEqual(e.messages,e.matched_message_ids.length);
    }
    process.stdout.write(JSON.stringify({round_ids:r.round_ids.sort(),message_ids:r.message_ids.sort(),customer_ids:r.customer_ids.sort()}));"""
    result = subprocess.run(["node", "-e", script, str(FILTERS)], input=json.dumps(payload, default=str),
                            text=True, capture_output=True, check=False, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    actual["message_ids"] = sorted(actual["message_ids"])
    assert actual == expected, name
