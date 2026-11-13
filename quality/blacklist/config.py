"""黑名单归档同步复用转人工分配同步的受限写入账号，不新增数据库凭据。"""

from pydantic import model_validator

from quality.assignment.config import AssignmentSyncSettings


class BlacklistSyncSettings(AssignmentSyncSettings):
    # 未单独配置时跟随 ARCHIVE_ASSIGNMENT_SYNC_ENABLED。
    blacklist_sync_enabled: bool | None = None

    @model_validator(mode="after")
    def follow_assignment_switch(self):
        if self.blacklist_sync_enabled is None:
            self.blacklist_sync_enabled = self.assignment_sync_enabled
        elif self.blacklist_sync_enabled and not self.assignment_sync_enabled:
            # 父类只在 assignment 启用时校验凭据；单独启用黑名单同步时同样要求完整配置。
            AssignmentSyncSettings(**{**self.model_dump(exclude={"blacklist_sync_enabled"}),
                                      "assignment_sync_enabled": True})
        return self

    @property
    def connection_kwargs(self) -> dict:
        return {**super().connection_kwargs, "application_name": "ics-quality-blacklist-target"}
