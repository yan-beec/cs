"""真实数据库中验证本页三元组在日汇总、重复扣除与现场合并中的等价性。"""

from datetime import date, datetime, time, timedelta
import os
import uuid

import pytest

from ics_agent.statistics.periods import BEIJING, ReportPeriod
from quality.processing.rollups import Composer, ROLLUP_VERSION
from quality.processing.service_facts import FACT_VERSION
from quality.tests import test_query_database as query_fixture

pytestmark = pytest.mark.skipif(os.environ.get('PROCESSING_TEST_DATABASE') != '1',
                                reason='需要独立归档测试数据库')


@pytest.fixture
def bucket_data():
    case = query_fixture.QueryDatabaseTest()
    case.setUpClass()
    case.source = 'bucket-scope-' + uuid.uuid4().hex
    sources = [case.source, case.source + '-other']
    identities = [(sources[0], 1, '7'), (sources[1], 1, '7'), (sources[0], 2, '7'), (sources[0], 1, '8')]
    now = datetime(2026, 10, 5, tzinfo=BEIJING)
    try:
        # 所有样本和全局日状态在事务末尾回滚，避免影响其他数据库用例。
        with case.admin.transaction(force_rollback=True):
            for index, (source, account, agent) in enumerate(identities):
                rid = case.overview_round(str(index), source=source, account=account, responsible=agent)
                first = datetime(2026, 10, 1, 20, tzinfo=BEIJING)
                last = first + timedelta(days=2)
                case.admin.execute('''INSERT INTO ops.round_service_facts
                    (round_id,source_key,account_id,visitor_messages,busy_refused,human_replied,ai_replied,
                     fact_version,responsible_agent_id,messages_until,first_reply_at,last_reply_at,started_at)
                    VALUES(%s,%s,%s,1,false,true,false,%s,%s,%s,%s,%s,%s)''',
                    [rid, source, account, FACT_VERSION, agent, last, first, last, first])
                for day in range(3):
                    at = first + timedelta(days=day)
                    case.admin.execute('''INSERT INTO ops.round_reply_hours
                        (round_id,hour_at,any_replies,assessed_replies,prev_hour_at) VALUES(%s,%s,1,1,%s)''',
                        [rid, at, at-timedelta(days=1) if day else None])
                    if day < 2:
                        case.admin.execute('''INSERT INTO ops.stat_seat_days
                            (day,variant,source_key,account_id,agent_id,inquiries) VALUES(%s,'d',%s,%s,%s,1)''',
                            [at.date(), source, account, agent])
                case.admin.execute('''INSERT INTO ops.stat_seat_repeats
                    (day,source_key,account_id,agent_id,prev_hour_at,inquiries) VALUES(%s,%s,%s,%s,%s,1)''',
                    [date(2026,10,2), source, account, agent, first])
            for day in (date(2026,10,1), date(2026,10,2)):
                case.admin.execute('''INSERT INTO ops.stat_rollup_days(day,state,fact_version,rollup_version,built_at)
                    VALUES(%s,'built',%s,%s,%s) ON CONFLICT(day) DO UPDATE SET state='built',
                    fact_version=excluded.fact_version,rollup_version=excluded.rollup_version,built_at=excluded.built_at''',
                    [day, FACT_VERSION, ROLLUP_VERSION, now-timedelta(hours=1)])
                case.admin.execute('DELETE FROM ops.stat_dirty_days WHERE day=%s', [day])
            case.admin.execute("INSERT INTO ops.stat_dirty_days(day) VALUES('2026-10-03') ON CONFLICT DO NOTHING")
            yield case.admin, sources, identities, now
    finally:
        case.tearDownClass()


@pytest.mark.parametrize('mode', ['day', 'month', 'year', 'custom', 'refined'])
def test_page_buckets_equal_full_subset_across_sources_and_accounts(bucket_data, mode):
    connection, sources, identities, now = bucket_data
    period = ReportPeriod.build('custom' if mode == 'refined' else mode, date(2026,10,1),
                               date(2026,10,3) if mode in ('custom','refined') else None)
    if mode == 'refined':
        period = period.refine(time(19), time(20,59))
    composer = Composer(connection, period.start, period.end, now)
    composer.seat_scope = composer.round_scope = ('{a}.source_key = ANY(%s)', [sources])
    full = composer.seat_buckets(period.grain, period.buckets)
    for seats in ([identities[0]], [identities[1], identities[2]], identities):
        actual = composer.seat_buckets(period.grain, period.buckets, seats=seats)
        assert actual == {key: value for key, value in full.items() if key in seats}
        assert set(actual) == set(seats)
    assert len(full) == 4
    # 年报的跨日重复只计一次；按日分桶则三个日期各计一次。
    assert sum(full[identities[0]].values()) == (1 if mode in ('day','year') else 3)
