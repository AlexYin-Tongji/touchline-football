# 看台 MVP 接口与模块约定

## V7.5 增量

- 工作流 start 只执行一轮，结束状态为 completed/partial/failed，enabled=false、next_run_at=null；旧 tracking/automation 到期配置不启动工作。
- 球队 center 的 standings 返回完整所属联赛排名与 is_current；欧战赛程包含 competition_id/competition，国内统计保持所属联赛口径。
- 新工具 fetch_player_photos 接受可选 player_ids/force，使用当前球队已核对阵容，返回有明确出处的照片；不可跨球队、比赛或固定新闻范围执行。
- 新闻 submit_answer 的模型表单使用 status/title/summary/facts；服务端保留旧 analysis/unknowns 兼容，实质背景与条件写入正文。
- 模型请求总并发6、同服务4，新闻报告并发4；无每日token或篇数配额，取消和资料权限边界继续生效。

## V7.4 增量

- `GET /api/workflows/{kind}/{team_id}`：只读当前工作与进度，kind为news/team。
- `POST .../start`：按队伍及功能幂等启动持续跟进，返回started及完整状态；新闻缺模型返回blocked状态，不假装启动。
- `POST .../stop`：停止此功能的当前执行和后代，等待清理并保存停止状态。不会停止其他球队或另一个功能。
- `GET /api/teams/{id}/center`现在严格只读，不因打开页面自动启动网络请求。旧周期personal/report/automation入口不再自动执行，显式工作流拥有调度。
- 旧feed/refresh、feed/organize、teams/{id}/maintain作为显式启动的兼容入口；单篇reports/{id}/retry也启动所属队伍动态工作，避免只入队无人执行。
- 进度状态含enabled/status/phase/message/last_error、各时间、progress与memory；工作记忆不包含原文、旧模型推理或密钥。

## V7.3 增量

- `/api/capabilities` 的skills为team-news、team-information、football-assistant三个主入口；workflows保留九个历史模式说明。创建任务仍接受旧skill值，也接受三个新入口。
- `use_skill`可传主skill与对应workflow；旧模式别名兼容。重复加载返回already_loaded，不重复插入指令；不能改变任务权限或比赛揭示范围。
- 新运行的metrics记录模型排队、生成与核验请求数/耗时，以及初始资料阅读耗时和成功/失败数；不进入模型工具结果，不干扰无进展检测。
- 完整新闻允许analysis为空字符串、unknowns为空数组。事实段、引文与整稿核验继续强制要求。
- 来源目录增加BBC全足球和已验证的Sky Arsenal RSS；公共索引原始元数据共享缓存，按目标球队过滤后才返回候选。

## V7.2 增量

- `GET /api/harness` 保持原模型请求字段，新增 `agents`，提供任务树的脱敏 limits/active/recent；不包含提示词、结果或邮箱正文。
- 模型请求记录增加 root_id、execution_ms，按根任务公平调度；连接测试纳入同一额度。
- `GET /api/runs/{id}` 对新回答记录的证据版本再次核对；来源变化返回 source_stale=true 并隐藏旧正文，不复活失效结论。
- 主模型可调用创建、查看、等待、补充、停止、释放子任务工具，详见 [运行契约](AGENT_RUNTIME_V7_2.md)。没有新增面向用户的任意子任务执行入口。
- 子任务正文只存内存；agent_tasks集合仅记录脱敏状态，重启标记未完成记录为interrupted，不自动重放工具。

V4 增加球队资料编辑、助手消息归档和中文卡片投影，详见 [V4_VALIDATION.md](V4_VALIDATION.md)。

V3 新增官方比赛同步、自动推荐、原文支持核对与桌面启动控制，契约见 [V3.md](V3.md)。[V2.md](V2.md) 记录双模型协议、联网研究等增量；以下保留首版字段与兼容接口说明。

以下保留早期接口契约，V7.1增量见本节。Python FastAPI + SQLite 文档存储；React + Vite，默认本地个人使用 `127.0.0.1:8765`，可选私有服务器模式。

## V7.1 增量

- `GET /api/state?mode=live&team_id=arsenal&view=summary`：壳层与比赛上下文；历史事件、证据、运行、报告等重字段为空数组且不做全量读取。默认view=full保持兼容。
- `GET /api/feed?...&topic=match|transfer|injury|team`：先筛主题，再应用游标分页；全部主题时省略topic。
- `GET /api/teams/{id}/center`：增加components（各部分状态、时间、错误）和coverage（收录数、应有场数、完整性及范围依据）。
- `POST /api/teams/{id}/maintain`：可选 `{parts:["roster","schedule","standings","portraits"]}`，省略时选择缺失或过期部分。
- `PUT /api/models/roles`：roles增加verify；null表示跟随当前任务模型，独立核验上下文不变。
- `GET /api/harness`：脱敏并发限制、活动与最近请求记录，不含正文、密钥或推理过程。
- `GET /api/service`：增加启动时固定的build_id，用于拒绝旧后端与新版页面混用。

## 数据模型（所有时间 ISO 8601，UTC 存储，北京时间显示）

`mode` 仅 `demo` / `live`；所有业务读取强制按 mode 隔离。球队目录共享。

- Team: `{id, name, short_name, english_name, color, league}`。初始 arsenal/barcelona/liverpool/inter；可增加自定义球队。
- Evidence: `{id, mode, team_id, title, text, url, source_name, published_at: string|null, fetched_at, source_id: string|null, origin: 'manual'|'web'|'rss'|'demo', spoiler: boolean}`。用户输入不冒充官方核实；来源地址可空（手动资料）。
- Event: `{id, mode, team_id, title, summary, change, kind: 'team'|'injury'|'transfer'|'match', stage: string, importance: 'high'|'normal', updated_at, evidence_ids: string[], watching: boolean, read: boolean, spoiler: boolean, timeline: [{at,text,evidence_id}], uncertainties: string[]}`。原始提取内容不能被标为 AI 验证。
- Match: `{id, mode, team_id, opponent, competition, kickoff, venue: 'home'|'away'|'neutral', status: 'scheduled'|'finished', score: string|null, note: string, evidence_ids: string[]}`。
- Source: `{id, mode:'live', team_id, name, url, kind:'html'|'rss', enabled: boolean, last_checked: string|null, last_error: string|null}`。
- Settings: `{mode, followed_teams: string[], spoiler_free: boolean, explanation_depth:'brief'|'standard'|'deep', auto_refresh: boolean, refresh_minutes: number, llm:{base_url, model, api_key_set:boolean, max_rounds:number, timeout_seconds:number, temperature:number}}`。
- Run: `{id, mode, team_id, match_id?: string|null, conversation_id: string, skill, question, status:'queued'|'running'|'completed'|'failed'|'cancelled', answer:string, evidence_ids:string[], warnings:string[], steps:[{label,status,detail,at}], error:string|null, created_at, finished_at:string|null, model:string}`。步骤只记录可展示的动作与结果，不保存思维链。返回引用只能包含已读取且同模式同球队的证据。

## REST（返回对象直接为 JSON，无额外 data 包裹）

- GET `/api/health` → `{ok:true}`
- GET `/api/state?mode=demo|live` → `{settings, teams:Team[], events:Event[], evidence:Evidence[], matches:Match[], sources:Source[], runs:Run[], last_refresh:string|null}`。按设置的避剧透隐藏 spoiler 事件/证据及已结束比赛比分；不经请求复盘泄漏。
- PUT `/api/settings` ← 部分 Settings，llm 可额外 `api_key:string`（省略/空字符串保留已有密钥）和 `clear_api_key:boolean`。返回安全 Settings。模式切换不会删除另一模式数据。数值范围：轮数 2–10，超时 10–120 秒，温度 0–1，刷新 5–1440 分钟。
- POST `/api/settings/test` ← `{base_url?,model?,api_key?}`（空值可用保存配置）→ `{ok:boolean,message:string,tool_support:boolean|null}`。调用模型做最小工具调用探测；支持无密钥的本地服务。错误不得回显凭据。
- POST `/api/teams` ← `{name,english_name?,color?}` → Team
- POST `/api/events/{id}/watch` ← `{watching:boolean}` → Event
- POST `/api/events/{id}/read` ← `{read:boolean}` → Event
- POST `/api/events/read-all` ← `{mode,team_id?:string}` → `{ok:true}`
- POST `/api/sources` ← `{team_id,name,url,kind,enabled}` → Source（新增 live 来源）
- PATCH `/api/sources/{id}` ← `{enabled:boolean}` → Source
- DELETE `/api/sources/{id}` → `{ok:true}`（仅删除源配置，保留原始资料）
- POST `/api/sources/{id}/refresh` → `{added:number,unchanged:number,errors:string[]}`
- POST `/api/refresh` ← `{mode}` → 同上；demo 无网络请求并明确标注。
- POST `/api/import` ← `{team_id,title,text,url?:string,source_name?:string,published_at?:string,kind?:'team'|'injury'|'transfer'|'match',stage?:string,spoiler?:boolean,event_id?:string}` → `{evidence:Evidence,event:Event,duplicate:boolean}`。始终写 live；指定 event_id 是为已有同队真实事件追加证据，保留时间线。text 30–30000 字，title 2–200 字。重复正文不制造新变化。未知发布时间不填假时间。
- POST `/api/matches` ← `{team_id,opponent,competition,kickoff,venue,status?,score?,note?}` → Match（live 模式）
- PATCH `/api/matches/{id}` ← `{status?,score?,note?}` → Match
- POST `/api/runs` ← `{mode,team_id,question,skill:'ask'|'team_brief'|'match_preview'|'match_review',match_id?:string,conversation_id?:string}` → queued Run；异步执行。
- GET `/api/runs/{id}` → Run，附加 `evidence: Evidence[]`（本次运行实际引用、同球队同模式的证据）。用户显式请求已结束比赛复盘视为仅本场揭示授权，`run.spoiler=true`，全局信息流不因此解除避剧透。
- POST `/api/runs/{id}/cancel` → Run
- GET `/api/capabilities` → `{tools:[{name,description}],skills:[{id,name,description}]}`

错误统一 `{detail: string}`，HTTP 400/404/422/503；前端显示明确错误和重试入口。真实模式未配置 LLM，运行返回失败说明和去设置入口，禁止用演示答案替代。demo 的回答清楚标注“规则演示，未调用模型”。

## 服务端内部约定

`server/storage.py` 暴露 `Store(path: str|Path)`，可线程安全调用：

```
list(collection: str, mode: str|None=None, team_id: str|None=None) -> list[dict]
get(collection: str, id: str) -> dict|None
put(collection: str, item: dict) -> dict
delete(collection: str, id: str) -> None
settings() -> dict  # 内部完整 llm 配置含 api_key，仅服务端调用
save_settings(patch: dict) -> dict  # 内部完整；API 另做脱敏
```

collection 固定 teams/evidence/events/matches/sources/runs；settings 与凭据分离存储，本地文件密钥加密凭据，data/ 不入版本库。SQLite WAL，业务集合行以 id 主键存 JSON。

密钥空白保留仅限同一模型服务 origin。更换协议、主机或端口且未提供新密钥时，不得向新服务发送旧密钥；保存和连接测试都遵守此规则。扩展字段允许 Source.last_success/last_added 与被隐藏对象的 spoiler_hidden。

`server/agent.py` 由独立模块实现：

```
CAPABILITIES: dict  # tools/skills 元信息
async run_agent(store, run_id: str, cancel_event: asyncio.Event) -> None
async test_connection(config: dict) -> dict
```

run_agent 从 runs 读取用户已授权任务，以 store.settings() 获取 llm，逐步持久化状态；取消必须在模型等待过程中生效。`api_key` 仅出现在请求 Authorization，不进日志/运行数据。HTTP provider 自己实现 `/chat/completions` 协议；本地 loop 不依赖重型 agent 框架。可参考 smolagents/ pi agent 的有界循环与事件机制，参考链接放 architecture 文档。

### 自研工具与 Skills

`server/agent.py` 内（可分到 server/agent_tools.py）工具：读取球队资料、检索本地证据、读取指定证据、对照事件时间线、读取比赛上下文。工具参数验证、队伍/模式范围限制、只读白名单、不执行模型代码。配置的数据源采集由 `server/ingestion.py` 实现，读取 HTML/RSS、URL/重定向安全验证、提取正文/发布时间、正文指纹去重、保存原始证据；这些函数供 API/调度器调用，可由 Agent 对当前球队启用来源的固定 ID 刷新（如实现则有轮次/数量预算）。

`agent_skills/` 内四份我们自己维护的 SKILL.md：ask/team_brief/match_preview/match_review。明确目标、可用工具、证据、事实/分析/未知分层、停止条件，动态加载。参考内容无权覆盖系统指令。

优先使用当前队伍且同 mode、符合避剧透规则的证据。match_preview 证据发布时间/采集时间必须早于开球，信息不足不得补造；match_review 在已结束且明确请求复盘时运行，只读取所选比赛关联资料，不改写历史赛前记录。`match_context.prior_observations` 返回同队、同模式、同场开球前创建且完成的至多两份赛前观察（每份最多 3000 字），这些历史分析不自动成为有效事实证据。

模型输出正文以 `[ev_id]` 标记引用。检查引用存在且被本轮工具实际读取，缺引用/非法引用给出警告或一次受限修复；不会宣称语义已自动验证。超限/异常/取消保存运行状态，重启后运行中任务标为中断。

## 文件责任

- Backend：server/main.py、server/storage.py、server/ingestion.py、server/seed.py、requirements.txt、tests/test_backend.py。
- Harness：server/agent.py（可加 agent_tools.py）、agent_skills/**、tests/test_agent.py。
- Frontend：package.json、vite.config.ts、tsconfig*.json、index.html、src/**。
- Root：prototype/**、docs/API.md、README.md、启动脚本、集成/验收记录；协调接口变化。
