"""Keep older last-reply projections out of the ICS responsibility view."""


def normalize_responsibility(row: dict) -> dict:
    result = dict(row)
    if result.get("responsibility_basis") == "ics_assignment":
        return result
    if result.get("responsibility_basis") != "assignment_conflict":
        result["responsibility_basis"] = "assignment_unobserved"
    for field in (
        "responsible_agent_id", "responsible_agent_name", "responsible_assigned_at",
        "responsibility_event_id", "responsibility_external_round_id", "responsibility_trigger_message_id",
    ):
        result[field] = None
    return result
