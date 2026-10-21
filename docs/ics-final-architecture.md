# ICS（Intelligent Customer Service）最终版项目架构图

## 总体架构

如果 Markdown 预览中的 Mermaid 图显示过小，请打开[可缩放版架构图](ics-final-architecture.html)。该页面支持滚轮缩放、按钮缩放、拖拽平移，并提供总览、实时接待链路、归档统计链路三个视图。

```mermaid
flowchart TB
    subgraph CHANNEL["一、客户渠道层"]
        WX["微信"]
        APP["产品 APP<br/>Android / iOS"]
        WEB["业务网页 / H5"]
        ID["业务身份服务<br/>用户 ID / 昵称 / 渠道 / 签名"]
    end

    subgraph CHATWOOT["二、Chatwoot 会话与坐席底座"]
        WIDGET["Website Widget / 渠道 Inbox"]
        CONTACT["Contact / Conversation<br/>联系人、会话、附件"]
        DESK["坐席工作台<br/>人工回复、备注、标签、解决"]
        CWAPI["Chatwoot API"]
        CWSTATUS["坐席状态与基础分配<br/>online / busy / offline"]
        CWHOOK["Webhook 事件<br/>message_created / status / assignment"]
    end

    subgraph ICS["三、ICS 智能客服核心服务"]
        ENTRY["FastAPI 服务入口"]
        VERIFY["Webhook 验签、事件解析<br/>入站过滤、幂等登记"]
        QUEUE["异步处理队列<br/>当前单实例内存队列 + 会话串行锁"]
        CONTROL["会话轮次与控制状态<br/>AI 开关、人工接管、静音、解决重置"]
        HANDOFF["转人工策略<br/>关键词、业务意图、need_human、置信度、检索质量"]
        FAQ["FAQ 优先路由<br/>BM25 / 标准答案"]
        RAG["LangGraph RAG 编排"]
        RETRIEVE["意图识别<br/>向量召回、可选精排"]
        LLM["云端大模型<br/>主模型 + 备用模型"]
        REPLY["回复与动作编排<br/>公开回复、私密备注、标签、状态、分配"]
        CLIENT["Chatwoot API Client"]
    end

    subgraph CONTENT["四、知识与会话数据"]
        ADMIN["ICS 管理后台<br/>管理员登录、渠道配置、日志"]
        FAQDATA["渠道 FAQ 数据<br/>question / answer / need_human / enabled"]
        DOCS["业务知识库文档<br/>提示词、FAQ Excel、资料文件"]
        VECTOR["Chroma 向量库<br/>BGE-M3 文档切片"]
        MEMORY["PostgreSQL 会话记忆<br/>LangGraph checkpoint"]
        BUSINESSDB[("ICS 业务数据库<br/>会话控制、幂等、任务状态")]
    end

    subgraph HUMAN["五、人工接待与强提醒"]
        ASSIGN["ICS 坐席分配<br/>在线状态、容量、历史优先、轮询"]
        REMIND["Windows 提醒客户端"]
        EXT["浏览器插件<br/>复用已登录 Chatwoot 页面"]
        POPUP["置顶弹窗、声音、待处理提示<br/>确认、改派、解决状态同步"]
    end

    subgraph ARCHIVE["六、归档、质检与统计运营"]
        CAPTURE["独立归档接收器<br/>专用 Webhook、签名校验、原始事件入库"]
        RAW[("原始事件库<br/>完整 Chatwoot payload")]
        WORKER["归档解析 Worker<br/>消息、会话、轮次、评价、版本索引"]
        ARCHIVEDB[("独立归档数据库<br/>分级账号、只读查询")]
        PRESENCE["客服状态采样<br/>每分钟记录在线 / 忙碌 / 离线"]
        QUERY["对话归档查询与人工质检"]
        STATS["统计报表<br/>询盘、渠道、坐席质量、状态、质检"]
        EXPORT["Excel 导出"]
    end

    subgraph OPS["七、部署与运行保障"]
        DOCKER["Docker Compose / 发布脚本"]
        LOG["轮转日志、任务状态、健康检查"]
        MONITOR["运维与版本回归<br/>Chatwoot / ICS / 归档组件"]
    end

    WX --> WIDGET
    APP --> WEB
    WEB --> WIDGET
    ID --> WIDGET
    WIDGET --> CONTACT
    DESK <--> CONTACT
    CWSTATUS --> CWAPI
    CONTACT --> CWHOOK

    CWHOOK --> ENTRY
    ENTRY --> VERIFY
    VERIFY --> QUEUE
    QUEUE --> CONTROL
    CONTROL --> HANDOFF
    CONTROL --> FAQ
    FAQ -->|未命中| RAG
    RAG --> RETRIEVE
    RETRIEVE --> LLM
    LLM --> REPLY
    FAQ -->|命中| REPLY
    HANDOFF --> ASSIGN
    ASSIGN --> REPLY
    REPLY --> CLIENT
    CLIENT --> CWAPI
    CWAPI --> CONTACT

    ADMIN --> FAQDATA
    ADMIN --> DOCS
    FAQDATA --> FAQ
    DOCS --> VECTOR
    VECTOR --> RETRIEVE
    MEMORY <--> RAG
    CONTROL <--> BUSINESSDB
    VERIFY <--> BUSINESSDB
    QUEUE --> BUSINESSDB

    ASSIGN --> REMIND
    REMIND --> POPUP
    REMIND --> EXT
    EXT --> DESK
    POPUP --> REMIND

    CWHOOK --> CAPTURE
    CAPTURE --> RAW
    CAPTURE --> WORKER
    WORKER --> ARCHIVEDB
    CWAPI --> PRESENCE
    PRESENCE --> ARCHIVEDB
    ARCHIVEDB --> QUERY
    ARCHIVEDB --> STATS
    QUERY --> ADMIN
    STATS --> ADMIN
    STATS --> EXPORT

    DOCKER -.部署.-> ENTRY
    DOCKER -.部署.-> CAPTURE
    DOCKER -.部署.-> WORKER
    DOCKER -.部署.-> ARCHIVEDB
    ENTRY --> LOG
    CAPTURE --> LOG
    WORKER --> LOG
    STATS --> LOG
    MONITOR -.回归验证.-> CWAPI
    MONITOR -.回归验证.-> ENTRY
    MONITOR -.回归验证.-> CAPTURE

    classDef channel fill:#E8F1FF,stroke:#4A78C2,color:#17345F;
    classDef chatwoot fill:#FFF3D6,stroke:#C28A27,color:#5C3A00;
    classDef ics fill:#E7F7EE,stroke:#3A9B65,color:#174C30;
    classDef data fill:#F3EAFE,stroke:#8955C7,color:#45206B;
    classDef human fill:#FFE8EE,stroke:#C95773,color:#651E31;
    classDef archive fill:#E9F7F7,stroke:#2D9999,color:#124D4D;
    classDef ops fill:#F0F0F0,stroke:#777,color:#333;

    class WX,APP,WEB,ID channel;
    class WIDGET,CONTACT,DESK,CWAPI,CWSTATUS,CWHOOK chatwoot;
    class ENTRY,VERIFY,QUEUE,CONTROL,HANDOFF,FAQ,RAG,RETRIEVE,LLM,REPLY,CLIENT ics;
    class ADMIN,FAQDATA,DOCS,VECTOR,MEMORY,BUSINESSDB data;
    class ASSIGN,REMIND,EXT,POPUP human;
    class CAPTURE,RAW,WORKER,ARCHIVEDB,PRESENCE,QUERY,STATS,EXPORT archive;
    class DOCKER,LOG,MONITOR ops;
```

## 主链路说明

```text
客户从微信、APP 或 H5 进入
  -> Chatwoot 创建或复用 Contact / Conversation
  -> Chatwoot Webhook 投递用户消息
  -> ICS 完成验签、过滤、幂等登记并入队
  -> 判断当前会话是否已人工接管
  -> 判断是否需要转人工
  -> FAQ 命中则直接回复
  -> FAQ 未命中则进入 LangGraph RAG
  -> ICS 通过 Chatwoot API 回复客户
  -> 需要人工时执行分配、标签、备注、状态和提醒
  -> 坐席在 Chatwoot 工作台继续接待
  -> 会话解决后结束当前轮次
```

## 数据与运营链路

```text
Chatwoot Webhook
  -> 独立归档接收器
  -> 原始事件库
  -> 异步解析 Worker
  -> 消息 / 会话 / 轮次 / 评价 / 分配 / 状态索引
  -> 对话查询、人工质检、坐席状态监管、统计报表和 Excel 导出
```

## 组件职责边界

| 组件 | 主要职责 | 不承担的职责 |
| --- | --- | --- |
| Chatwoot | 联系人、会话、Inbox、坐席工作台、人工消息、基础状态和会话展示 | AI 问答策略、企业统计口径、独立归档和业务知识维护 |
| ICS 核心服务 | Webhook 接入、AI 首接、FAQ/RAG、会话状态、转人工和 Chatwoot API 编排 | 重新实现完整坐席工作台 |
| 管理后台 | FAQ、知识库、渠道资料、日志、归档、质检和统计入口 | 直接暴露 Chatwoot 管理凭据 |
| 提醒客户端 | 转人工强提醒、声音、置顶弹窗、浏览器会话跳转和确认 | 修改 Chatwoot 分配结果或替代坐席登录 |
| 归档接收器与 Worker | 保存原始事件、异步解析、生成可查询索引 | 参与实时 AI 回复决策 |
| 归档/统计模块 | 历史查询、质检、状态监管、询盘和坐席指标 | 影响在线会话处理结果 |

## 当前部署形态

- `ics-agent`：FastAPI、Webhook、FAQ/RAG、会话控制、管理后台和状态采样。
- `quality-receiver`：接收 Chatwoot 账户级归档 Webhook，只写原始事件和持久化任务。
- `quality-worker`：异步解析原始事件，生成消息、轮次、评价和查询索引。
- `postgres`：ICS 业务数据库，保存会话控制、幂等状态、任务状态和会话记忆。
- `archive-postgres`：独立归档数据库，按接收、解析、查询、状态采样和质检职责分级授权。
- Chatwoot：独立部署，向 ICS 提供业务 Webhook，向归档接收器提供专用账户级 Webhook。
- Windows 提醒客户端与浏览器插件：部署在客服电脑，连接 ICS 并复用客服浏览器中的 Chatwoot 登录态。

## 关键设计原则

1. Chatwoot 保留为会话与坐席底座，差异化能力放在 ICS 外部服务。
2. 转人工是一个包含状态、分配、标签、备注、提醒和公开提示的幂等动作。
3. 实时 AI 处理链路与历史归档、质检、统计链路隔离。
4. 前端和提醒客户端不持有 Chatwoot 管理端 Token。
5. 所有统计指标以归档证据、成功分配时间、公开回复和分钟状态采样为依据。
