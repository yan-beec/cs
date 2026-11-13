"""A statement timeout is a too-wide request, not an unavailable archive."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from psycopg import errors
import pytest

from quality.query.repository import ArchiveFilters
from quality.query.runtime import ArchiveQueryTimeout, QUERY_TIMEOUT_DETAIL, QueryRuntime


def test_statement_timeout_keeps_the_archive_ready_and_reports_a_narrower_range():
    async def check():
        runtime = QueryRuntime()
        runtime.repository = Mock()
        runtime.repository.list_rounds.side_effect = errors.QueryCanceled("canceling statement due to statement timeout")
        runtime.ready, runtime.status = True, "ready"

        async def execute(function, *args, **kwargs):
            return function(*args, **kwargs)

        with patch.object(runtime, "_execute", side_effect=execute):
            with pytest.raises(ArchiveQueryTimeout):
                await runtime.call("list_rounds", ArchiveFilters())
        assert runtime.ready and runtime.status == "ready"

    asyncio.run(check())


def test_other_database_failures_still_mark_the_archive_unavailable():
    async def check():
        runtime = QueryRuntime()
        runtime.repository = Mock()
        runtime.repository.list_rounds.side_effect = errors.OperationalError("server closed the connection")
        runtime.ready, runtime.status = True, "ready"

        async def execute(function, *args, **kwargs):
            return function(*args, **kwargs)

        with patch.object(runtime, "_execute", side_effect=execute):
            with pytest.raises(Exception):
                await runtime.call("list_rounds", ArchiveFilters())
        assert not runtime.ready and runtime.status == "unavailable"

    asyncio.run(check())


def test_statistics_timeout_returns_the_actionable_message():
    from ics_agent.statistics import routes

    runtime = Mock(ready=True, repository=Mock(pool=object()))

    async def execute(function, *args, **kwargs):
        raise errors.QueryCanceled("canceling statement due to statement timeout")

    runtime._execute = execute
    with patch.object(routes, "get_query_runtime", return_value=runtime):
        from fastapi import HTTPException
        from datetime import date
        from ics_agent.statistics.periods import ReportPeriod

        with pytest.raises(HTTPException) as raised:
            request = Mock(is_disconnected=AsyncMock(return_value=False))
            asyncio.run(routes.run_report(request, "agent_report", ReportPeriod.build("year", date(2026, 1, 1))))
    assert raised.value.status_code == 503 and raised.value.detail == QUERY_TIMEOUT_DETAIL
