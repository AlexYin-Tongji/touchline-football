# 通用主子智能体运行实施计划

> **For agentic workers:** 使用 superpowers:subagent-driven-development 分工实施并交叉审阅。用户已批准上一轮设计并明确要求开始完成，无需再次等待设计批准。

**Goal:** 主模型按需求动态创建、查看、等待、补充和停止子智能体；并行新闻整理与任务拆分使用相同运行机制。

**Architecture:** 增加不依赖足球、模型角色、技能或数据库的进程内任务树运行器。现有助手作为执行回调，主子共用 run_agent；业务适配器限定子任务范围、临时上下文和引用有效性。模型请求调度继续与智能体生命周期分离。

**Tech Stack:** Python 3.11+ / asyncio / existing SQLite Store / existing provider adapters；不增加运行依赖。

**Spec:** 本文记录已经批准的对话设计及实现契约。

## 约束与决定

- 保留 main 主线，在干净工作区实现；开始前保存回退标签，不操作另一个仓库，不自动推送。
- 不迁移个人数据库、不调用真实模型、不持久保存子任务提示词、原文快照或模型私有上下文。
- 角色名只是业务模型选择与日志标签，不参与通用任务树调度分支。
- 子任务继承根截止时间、球队/模式/比赛/文章范围、联网权限及配置快照；工具集合只能收窄。
- 子结果是候选材料，不自动授予父任务引用资格；父任务须读取实际来源并完成既有交付核验。
- 暂不引入多进程、分布式队列或自动模型价格路由。

## Task 1: 通用任务树及请求调度

Files: server/agent_runtime.py, server/model_harness.py, tests/test_agent_runtime.py, tests/test_model_harness.py。

接口：AgentRuntime(observer=None, max_active_per_root=12, max_depth=3)。register_root(task_id, *, cancel_event, deadline)；spawn(parent_id, *, execute, payload, request_id=None)；send(parent_id, task_id, message, request_id=None)；inspect(parent_id, task_ids=None, after=0)；async wait(parent_id, task_ids=None, after=0, timeout=30)；drain_inbox(task_id)；async cancel(parent_id, task_id)；async finish_root(task_id, status)；async shutdown()；snapshot()。spawn/send 返回任务公开快照。execute(context) 接收 id、parent_id、root_id、generation、deadline、cancel_event、payload、previous_result；返回 JSON 结果。inspect/wait 返回 agents、events、cursor、timed_out。公开结果含 id/status/generation/result/error；observer 仅接收无 payload/result/message 的状态元数据。

- [x] 先写并运行失败测试：真实重叠、局部失败、跨父访问拒绝、消息到达顺序、完成后追加新代次、丢通知、取消整树、过期根不可创建、并发容量回收。
- [x] 实现无工作者槽等待死锁的非阻塞 spawn、单实例执行循环、稳定 request_id 去重、事件序号与子树取消。等待超时不取消子任务，重试继承原截止时间。
- [x] ModelHarness.request 增加可选 root_id 并实现按根公平的可执行请求调度，保留原调用兼容及总3/同服务2；等待不占请求名额。
- [x] 局部测试与根代理接口审查。

## Task 2: 助手业务适配与端到端运行

Files: server/agent_delegation.py, server/agent.py, server/agent_tools.py, server/main.py, tests/test_agent_delegation.py。

- [x] 先以 mock 模型验证主模型创建两子任务、等待结果、读取来源、统一交付；补充新一轮与取消场景。
- [x] 添加 spawn_agent / list_agents / wait_agents / send_agent / cancel_agent 工具。创建只传明确任务和必要背景，不复制父完整会话；复用同一 run_agent。
- [x] 子运行使用内存 runs 覆盖层，不成为用户会话、不归档独立球队知识、不发布独立报告。元数据记录保存在 agent_tasks，结果只保留在当前任务树内。
- [x] 新补充消息在安全节点读取，终稿不得忽略核验/模型等待期间新到消息；完成任务可再次启动，旧结果不得覆盖新代次。
- [x] 主任务退出、取消与服务关闭清理所有子任务，重启将未结束元数据标记 interrupted；/api/harness 输出脱敏任务树状态。
- [x] 核验前后检查引用版本，来源变化不得交付旧 supported 结论；保护既有隐私/比赛/文章限制。

## Task 3: 修复已复现的并发边界

Files: server/news_reports.py, server/research.py, server/research_transport.py, tests/test_report_concurrency.py, tests/test_research_concurrency.py。

- [x] 复现新版先完成旧版后结束，修复列表按当前来源版本选报告。
- [x] 复现同步下载取消后仍占底层网络但提前释放名额，确保物理下载结束前不释放共享调度容量，保留最后读取者取消后无晚写入。
- [x] 局部回归，检查正常共享读取取消不影响其他读取者。

## Task 4: 文档、审阅和交付

- [x] 记录官方开源仓库固定提交、许可证、借鉴点；仅参考设计，不复制大段代码。
- [x] 更新运行说明与版本为 7.2.0，说明自主分派、能力边界、恢复语义和验证结果。
- [x] 独立代码审阅运行、权限与并发交付边界；修复确认的问题。
- [x] python scripts/check.py --suite full；npm run test:ui；npm run build；git diff --check。
- [x] 保存本地提交，保持 main，报告实际测试与回退点，不推送。

## 工作记录

- 初始 main: f3b4be9，工作区干净。原调度只限制模型请求；run_agent 可复用但仍是足球业务执行器。
- 已决定保持当前工作区而非另开分支，遵守项目只保留 main 的明确约定。

- 最终核验：549项Python测试通过（69.723秒）；6项前端回归通过；生产构建通过；git diff --check通过。独立审查无剩余阻塞项。
- 根上下文预算采用显式release回收，不自动删除未读结果。内部证据版本表只留服务端，所有API出口剔除。
- 为元数据增加agent_tasks集合白名单，无SQL结构迁移；连接检查加入统一模型额度。
