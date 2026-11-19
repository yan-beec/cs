"""界面之外的通知去重与离线确认状态。"""


def key(item: dict) -> tuple[str, int]:
    return item["id"], item["version"]


class ReminderState:
    def __init__(self) -> None:
        self.items: dict[tuple[str, int], dict] = {}
        self.confirmations: set[tuple[str, int]] = set()

    def replace(self, items: list[dict]) -> bool:
        updated = {key(item): item for item in items}
        added = bool(updated.keys() - self.items.keys())
        self.items = updated
        self.confirmations.intersection_update(updated)
        return added

    def confirm(self, item: dict) -> None:
        self.confirmations.add(key(item))

    def visible(self) -> list[dict]:
        return [item for identity, item in self.items.items() if identity not in self.confirmations]
