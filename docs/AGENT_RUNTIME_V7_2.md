# V7.2：通用主子智能体运行

## 能力与分层

主模型按需求创建子智能体，用于独立资料研究、并行消息整理和任务拆分。主子实例复用同一个助手执行流程。运行器不识别 feed/chat/team/verify：这些名称保留为业务模板、模型选择和诊断标签，不参与任务树调度。

当前为本地进程内实现，无新增运行依赖。后台新闻队列仍是业务入口，具体任务也能自主分派；简单问题不强制拆分。

- `agent_runtime.py`：通用任务树、代次、邮箱、事件游标、去重、等待、取消与释放；不导入足球工具、技能、模型服务或数据库。
- `model_harness.py`：按根任务轮转的模型请求调度，跳过服务商已满的请求，区分等待与执行耗时。等待工具或子任务不持有模型请求名额。
- `agent_delegation.py`：足球业务适配器、子任务工具、范围继承、临时运行存储、候选结果版本复验。
- `agent.py`：主子共用的业务执行器，接收独立上下文、工具集合与配置快照，既有交付核验仍由业务层执行。

## 模型工具

| 工具 | 契约 |
| --- | --- |
| spawn_agent | task必填；context、skill、evidence_ids、tools可选。立即返回编号，可同轮创建多个。 |
| list_agents | 查看直接子任务的状态、结果与事件游标；不能跨父节点访问。 |
| wait_agents | 按事件等待，默认30秒、最大60秒；after_cursor等待新增进展；超时不取消任务，无模型轮询。 |
| send_agent | 忙时按序加入邮箱，在安全节点处理；完成或失败后开始新代次。返回送达确认，不携带旧结果。 |
| cancel_agent | 停止直接子任务及后代，等待清理，重复调用安全。 |
| release_agent | 释放终态子树上下文与结果，保留身份墓碑；释放后需新建，不能继续原实例。 |

父实例、代次、模型轮次和工具调用编号构成去重键。相同键与参数不会重复启动，不同参数会被拒绝。完成事件使用递增游标；历史截断时返回events_truncated与当前状态，避免漏掉完成通知。

## 隔离、交付与恢复

子任务不复制父完整对话，只接收明确目标、必要背景与资料编号，拥有独立消息历史、已读范围和工具状态。主子默认使用同一份已解析模型配置快照；本次不增加自动模型切换或价格路由。

球队、模式、比赛、赛前截止线、避剧透规则、固定报告来源、联网权限逐层继承。技能改变工作方式，不能改变边界。工具集合只能收窄；选定来源及依赖阅读的复合工具必须保留read_evidence。子任务不能读取父会话，也不独立发布报告。

子结果是候选材料，不增加父任务的已读证据资格。主任务须读取实际来源并遵守自身交付规则。来源变化后list/wait不再返回旧候选，控制命令只返回确认。核验返回后和交付前再次检查引用版本；新完成回答在API展示时也检查版本，避免旧结论配上更正原文。

一个分支失败不取消兄弟，允许原实例接收补充后重试。所有代次继承根截止时间，重试不重置时限。新要求会阻止旧终稿直接结束。父正常结束清理遗留后代；父取消立即禁止新增并向下传播，等待清理。

子运行、提示词、原文快照、邮箱和结果仅存本次任务树内。数据库agent_tasks只保存编号、父子关系、代次、状态、时间、错误代码等元数据。重启将未结束记录标记interrupted，不自动重放有副作用的工具，用户重新发起主任务。现有个人数据库无需迁移。

## 容量与优化

- 模型请求仍为全局3、同服务地址2；连接测试也使用同一调度。
- 每个根最多12个活动子实例，最大子层级3；这是驻留与嵌套保护，不是每日次数或总研究条数配额。
- 运行器载荷、结果、待收消息每根合计16MB，单项JSON最多1MB，单实例待收消息128条。容量满可等待或显式释放已消费结果，不静默删除未读结果。此预算不包括模型完整消息历史及整进程内存。
- 同根子请求作为一组参加轮转，防止大量分支挤占其他任务。并发暂不自适应，不中断已发出的正常模型请求抢占名额。
- 下载继续在途共享。同步线程无法强停时，取消读取者立即返回，但真实下载结束前仍占网络名额，且不会继续写来源。
- 报告列表按当前资料版本选择，旧任务晚结束不再遮住完成的新版本。

## 开源参考

以下固定源码于2026-09-22检查，仅借鉴设计，没有复制实现或引入依赖。

- **OpenAI Codex**：`94174e44cbc54cece45f6052328ca0c2cd7a8a2a`，[Apache-2.0](https://github.com/openai/codex/blob/94174e44cbc54cece45f6052328ca0c2cd7a8a2a/LICENSE)。参考[注册与容量预留](https://github.com/openai/codex/blob/94174e44cbc54cece45f6052328ca0c2cd7a8a2a/codex-rs/core/src/agent/registry.rs)、[事件等待](https://github.com/openai/codex/blob/94174e44cbc54cece45f6052328ca0c2cd7a8a2a/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs)、[消息投递](https://github.com/openai/codex/blob/94174e44cbc54cece45f6052328ca0c2cd7a8a2a/codex-rs/core/src/agent/control/delivery.rs)。
- **DeepSeek Harness**：`c36a83ff6bb95e3f82cf79f9be7c724270a8aa61`，[MIT](https://github.com/deepseek-ai/deepseek-harness/blob/c36a83ff6bb95e3f82cf79f9be7c724270a8aa61/LICENSE)。参考[独立子会话](https://github.com/deepseek-ai/deepseek-harness/blob/c36a83ff6bb95e3f82cf79f9be7c724270a8aa61/packages/subagent/subagent-spawn-in-process/src/index.ts)、[收件箱](https://github.com/deepseek-ai/deepseek-harness/blob/c36a83ff6bb95e3f82cf79f9be7c724270a8aa61/packages/subagent/subagent/src/inbox.ts)、[控制工具](https://github.com/deepseek-ai/deepseek-harness/blob/c36a83ff6bb95e3f82cf79f9be7c724270a8aa61/packages/subagent/tool-subagent-control/src/index.ts)。

本项目选择“忙时加入消息、空闲时启动新代次”。模型请求额度与实例驻留额度独立，不把上游整回合运行名额当作模型请求额度。

## 验证与回退

局部：`python scripts/check.py --module test_agent_runtime test_agent_delegation test_model_harness`。交付：完整Python回归、前端回归与构建。

测试覆盖任务重叠、单请求名额父等子、失败续跑、补充消息、父权限、父子孙范围继承、取消清理竞态、去重、释放、资料变化及物理下载容量。模型与网络使用合成响应，验证运行机制，不代表真实模型自主拆分质量已验收。

2026-09-22最终验证：549项Python测试全部通过（69.723秒），6项前端回归与生产构建通过；独立代码审查没有剩余阻塞项。完整测试记录保存在本机Git忽略的`output/agent-runtime-tests.json`。

改动前回退标签：`v7.1.0-before-agent-runtime-20260922`，对应`f3b4be9`。标签仅含代码，不含个人数据。
