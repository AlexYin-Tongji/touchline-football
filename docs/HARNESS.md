# 看台 V2 Harness

这是围绕足球证据研究实现的轻量循环，未依赖第三方 Agent/搜索分析服务。模型推理由用户配置的服务提供；来源采集、范围过滤、工具执行、技能文件、预算和任务记录由本项目维护。

## 两种接口

设置明确选择 Chat Completions 或 Responses。基础地址支持版本路径，也可填写完整端点；只有域名时补 `/v1`。完整端点与协议矛盾会报错，不会自动改协议重试。

- Chat Completions：`messages`、嵌套 `tools.function`、`tool_calls`、`tool_call_id`。普通模型发送 `max_tokens`，已知 OpenAI 推理模型名使用 `max_completion_tokens`。默认不发送 temperature；用户可明确启用。
- Responses：`input`、平铺函数定义、`function_call`/`function_call_output.call_id`、`max_output_tokens`，`store=false`。工具续答和引用修复会原样回传所有 `output` items，包括 reasoning/encrypted_content；这些只留在单次运行内存，永不存入运行记录、页面或日志。
- DeepSeek：默认关闭深度思考以减少短任务延迟；可在设置中启用。`reasoning_content` 同样仅在内存中续接。客户端声明真实 `Touchline/2.0` User-Agent，并按会话给出稳定 `x-opencode-session`。
- 环境代理可开关。HTTP 错误只展示状态码、允许列表中的错误分类和参数，不展示响应体、密钥或完整配置。输出截断不会被当作成功。

依据：[OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)。

设置测试发送三个受输出预算限制的请求：普通文本、指定函数调用、随机工具结果续答。只有三层全部成功才标为可用于 Agent；测试不会保存设置。模拟器测试证明协议处理，不能代替真实服务联调或足球分析质量验收。

## 10 个自研工具与 5 个工作技能

本地工具：`team_context`、`search_evidence`、`read_evidence`、`event_timeline`、`match_context`。检索使用可检查的双语足球词表与关键词；未声称实现语义向量检索。

网络工具：`discover_news` 返回已知来源候选；`read_article(candidate_id)` 获取原文、入库并返回通过范围校验的可引用证据；`refresh_team_sources` 批量收集少量相关文章，返回 ID 后仍需 `read_evidence` 阅读。

`use_skill` 从仓库本地白名单加载附加工作规程：ask、team_brief、match_preview、match_review、event_watch。`auto` 先按明确问题/比赛/跟进上下文选择起始技能，之后模型可组合技能。技能加载不会增加比赛揭示授权、改变赛前截止线、球队、资料模式或联网权限。持续调度由跟进任务模块负责，普通问答本身不承诺后台持续工作。

`submit_answer` 是自研最终交付协议：模型提交简短 summary、最多三条有已读 evidence_ids 的事实、可能影响和关键未知。服务校验嵌套字段、数量、长度、引用范围及明显过程叙述，通过后直接排版并结束，不再请求模型复述。没有原文时可显式提交 insufficient，但不能包含无依据的事实或分析。相同 URL 的抓取版本仍保留证据编号，原文篇数统计按 URL 合并，不将其宣称为独立交叉核实。

旧的普通文本输出保留兼容。明显的过程前言、重复段落或过长内容最多要求一次结构化重交，不使用正则删除事实来制造“干净”答案。该层校验交付质量与引用资格，不声称自动证明每项足球结论。

## 范围与预算

每个工具调用重新校验球队和资料模式。演示模式、`allow_network=false` 禁止联网；历史赛前任务不能新抓取事后编辑页面。原文阅读再次检查避剧透、比赛关联和时间范围。索引、候选、旧回答不授予引用资格。

每次运行默认 10 轮，可设置 2–16 轮；每轮最多 5 个工具；联网发现/收集合计最多 3 次、候选合计最多 16 条、原文尝试最多 8 篇（失败也计预算）。HTTP 请求和网络工具都受取消与总时限约束，总时限最多 10 分钟。一个小任务可以读多个来源并给出回答，不能无限循环。

继续任务创建新的运行并保留 parent_run_id。只恢复同球队、同模式和相同比赛范围；重新读取原文并复核当前避剧透范围，恢复公开任务背景。无需持久化或恢复模型私有推理。原任务及历史赛前观察保持不可覆盖。

引用检查只证明 ID 属于本次读取的资料，并检查引用是否缺失；失败最多让模型修复一次。引用语义与结论是否充分仍需对照原文验收，不能把结构检查宣传为事实核实。

## 验证

`python -m unittest tests.test_agent tests.test_agent_v2 tests.test_provider tests.test_agent_delivery -v`

覆盖双协议实际 HTTP payload、函数续接、reasoning/encrypted_content 内存保留及无持久化、分级错误、密钥脱敏、工具结果真实性、取消、引用、网络预算、避剧透、比赛范围、技能组合和任务继续。
