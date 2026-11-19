"""Resolve display names by Chatwoot identity without changing message evidence."""


def archived_agent_name(value, agent_id):
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    compact = "".join(name.split())
    if compact in {str(agent_id), f"#{agent_id}", f"坐席{agent_id}", f"分配坐席{agent_id}"}:
        return None
    return name


def agent_name_index(agents):
    names = {}
    for agent in agents:
        if any(agent.get(key) is None for key in ("source_key", "account_id", "agent_id")):
            continue
        name = agent.get("agent_name")
        if agent.get("agent_name_source") == "chatwoot":
            name = name.strip() if isinstance(name, str) and name.strip() else None
        else:
            name = archived_agent_name(name, agent["agent_id"])
        if name:
            names[(str(agent["source_key"]), str(agent["account_id"]), str(agent["agent_id"]))] = name
    return names


def enrich_round_agents(row, names):
    result = dict(row)
    scope = str(row.get("source_key")), str(row.get("account_id"))

    def resolve(agent_id, observed=None):
        if agent_id is None or str(agent_id) == "":
            return None
        return names.get((*scope, str(agent_id))) or archived_agent_name(observed, agent_id)

    result["agents"] = [{**agent, "agent_name": resolve(agent.get("agent_id"), agent.get("agent_name"))}
        for agent in row.get("agents", [])]
    if row.get("responsibility_basis") == "ics_assignment":
        result["responsible_agent_name"] = resolve(row.get("responsible_agent_id"), row.get("responsible_agent_name"))
    result["last_public_agent_name"] = resolve(row.get("last_public_agent_id"), row.get("last_public_agent_name"))
    current = row.get("current_assignee_id")
    observed_current = next((agent.get("agent_name") for agent in result["agents"]
        if str(agent.get("agent_id")) == str(current)), row.get("current_assignee_name"))
    result["current_assignee_name"] = resolve(current, observed_current)
    return result
