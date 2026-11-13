"""评分复核接口和统计口径回归。"""
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ics_agent.app.core.auth import require_admin
from quality.query import routes
from quality.query.repository import ReviewConflict
from quality.processing.stat_metrics import METRICS


@pytest.mark.parametrize("name", ["rated_inquiries", "rating_sum", "low_rating", "low_rating_rounds"])
def test_rating_metrics_exclude_invalid(name):
    assert "round_rating_review_current" in METRICS[name].predicate
    assert "invalid" in METRICS[name].predicate
    assert "f.rating" in METRICS[name].predicate


def test_rating_review_api(monkeypatch):
    runtime = Mock(call=AsyncMock(return_value={"status": "invalid", "revision": "12"}))
    monkeypatch.setattr(routes, "get_query_runtime", lambda: runtime)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: Mock(username="admin")
    client = TestClient(app)
    payload = {"status": "invalid", "revision": "0", "content_revision": "abc"}
    assert client.put("/admin/api/quality/rounds/r/rating-review", json=payload).status_code == 200
    assert runtime.call.call_args.args[-3:] == ("admin", 0, "abc")
    runtime.call.side_effect = ReviewConflict()
    assert client.put("/admin/api/quality/rounds/r/rating-review", json=payload).status_code == 409
    payload["reviewer"] = "forged"
    assert client.put("/admin/api/quality/rounds/r/rating-review", json=payload).status_code == 422
    app.dependency_overrides.clear()
    assert client.put("/admin/api/quality/rounds/r/rating-review", json=payload).status_code in (401, 403)


def test_offline_snapshot_preserves_original_rating_and_review(tmp_path):
    from quality.tests.test_offline_bundle import build, message, parse
    review = {"status": "invalid", "revision": "9007199254740993", "round_id": "r1"}
    result = build(tmp_path, [message(1, round_rating_status="rated", round_rating=1,
                                     round_rating_feedback="原始差评", rating_review=review)])
    _, catalog, _, _ = parse(result)
    entry = catalog["entries"][0]
    assert entry["rating"] == 1
    assert entry["rating_feedback"] == "原始差评"
    assert entry["rating_review"] == review
