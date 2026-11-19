"""Map assignment identities to live Chatwoot names without rewriting history."""

from copy import deepcopy
import pytest

from quality.query.agents import agent_name_index, archived_agent_name, enrich_round_agents


def test_round_names_match_source_account_and_chatwoot_id_without_changing_evidence():
    names = agent_name_index([
        {"source_key": "local", "account_id": 1, "agent_id": "1", "agent_name": "beec", "agent_name_source": "chatwoot"},
        {"source_key": "local", "account_id": 1, "agent_id": "2", "agent_name": "QQ", "agent_name_source": "chatwoot"},
        {"source_key": "other", "account_id": 1, "agent_id": "1", "agent_name": "Other instance"},
        {"source_key": "local", "account_id": 2, "agent_id": "1", "agent_name": "Other account"},
    ])
    row = {"source_key": "local", "account_id": 1, "responsibility_basis": "ics_assignment",
        "responsible_agent_id": "1", "responsible_agent_name": "分配坐席1", "responsibility_event_id": 99,
        "current_assignee_id": "2", "last_public_agent_id": "2", "last_public_agent_name": "Old observed name",
        "agents": [{"agent_id": "1", "agent_name": None, "was_assigned": True},
            {"agent_id": "2", "agent_name": "Old observed name", "replied": True}]}
    original = deepcopy(row)
    result = enrich_round_agents(row, names)
    assert result["responsible_agent_name"] == "beec"
    assert result["responsible_agent_id"] == "1" and result["responsibility_event_id"] == 99
    assert result["current_assignee_name"] == result["last_public_agent_name"] == "QQ"
    assert [agent["agent_name"] for agent in result["agents"]] == ["beec", "QQ"]
    assert enrich_round_agents({**row, "source_key": "other"}, names)["responsible_agent_name"] == "Other instance"
    assert enrich_round_agents({**row, "account_id": 2}, names)["responsible_agent_name"] == "Other account"
    assert row == original


@pytest.mark.parametrize("name", ["", " ", "1", "#1", "坐席 1", "分配坐席 1"])
def test_generated_labels_are_never_presented_as_real_names(name):
    assert archived_agent_name(name, "1") is None
    assert agent_name_index([{"source_key": "local", "account_id": 1, "agent_id": "1", "agent_name": name}]) == {}


def test_authoritative_chatwoot_name_is_trusted_even_if_it_looks_like_a_generated_label():
    assert agent_name_index([{"source_key": "local", "account_id": 1, "agent_id": "1",
        "agent_name": "坐席1", "agent_name_source": "chatwoot"}]) == {("local", "1", "1"): "坐席1"}


def test_missing_directory_keeps_known_archived_names_and_leaves_missing_names_unknown():
    row = {"source_key": "local", "account_id": 1, "responsibility_basis": "ics_assignment",
        "responsible_agent_id": "1", "responsible_agent_name": "分配坐席1",
        "current_assignee_id": "2", "last_public_agent_id": "2", "last_public_agent_name": "Actual name",
        "agents": [{"agent_id": "2", "agent_name": "Actual name"}]}
    result = enrich_round_agents(row, {})
    assert result["responsible_agent_name"] is None
    assert result["current_assignee_name"] == result["last_public_agent_name"] == "Actual name"


def test_directory_cannot_create_responsibility_without_ics_assignment():
    row = {"source_key": "local", "account_id": 1, "responsibility_basis": "assignment_unobserved",
        "responsible_agent_id": None, "responsible_agent_name": None}
    result = enrich_round_agents(row, {("local", "1", "1"): "beec"})
    assert result["responsible_agent_id"] is None and result["responsible_agent_name"] is None
