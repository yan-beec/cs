"""坐席客户端黑名单管理：拉黑时长、原因、解除备注与操作历史。

黑名单状态保存在 ICS 自己的 chatwoot_contact_block 表，转人工拒接只看这张表；
与 Chatwoot 网页端静音（contact.blocked）互不影响。到期解除由 webhook 模块的
reaper 执行。这里负责按选定时长写入到期时间、记录操作历史，并把历史交给归档同步。
"""
