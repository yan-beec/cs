"""坐席分时只请求本页身份，完整页和导出保持全量路径。"""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from ics_agent.statistics.periods import ReportPeriod
from ics_agent.statistics.repository import StatisticsRepository
from quality.query.config import QuerySettings


@pytest.mark.parametrize('page,page_size', [(1, 20), (2, 20), (1, 100),
    (1, QuerySettings(_env_file=None, reader_password='test').export_max_entities)])
def test_selected_page_passes_full_identity_or_none(page, page_size):
    period = ReportPeriod.build('month', date(2026, 10, 1))
    calls = []
    totals = {('s', 1, str(i)): {'inquiries': 100-i} for i in range(40)}

    def buckets(grain, period_buckets, *, seats='not-passed'):
        calls.append(seats)
        return {}

    repository = StatisticsRepository(None)
    repository.composer = lambda *a, **kw: SimpleNamespace(seat_totals=lambda: totals, seat_buckets=buckets)
    connection = SimpleNamespace(execute=lambda *a, **kw: SimpleNamespace(fetchall=lambda: []))
    result = repository._agents(connection, period, period.start, period.start, 's', 1,
                                page, page_size, period.end + timedelta(days=1))
    selected = [('s', 1, row['agent_id']) for row in result['agents']]
    assert calls == [None if len(selected) == len(totals) else selected]
    assert result['total'] == 40
    assert result['total_inquiries'] == sum(100-i for i in range(40))


def test_empty_seat_selection_has_no_database_work():
    from quality.processing.rollups import Composer
    composer = Composer.__new__(Composer)
    assert composer.seat_buckets('day', (), seats=[]) == {}
