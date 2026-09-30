# V8 重构进度

> **下一个模型请先读 `docs/HANDOVER.md`**，那一份含完整交接（现状、待办、硬约束、踩过的坑、用户原话）。本文件只记逐项完成状态。

目标：按 `DESIGN_PRINCIPLES.md` 持续迭代，直到作为发布 demo 达到 95 分。用户最后统一验收，中途不汇报，只在需要用户改产品边界或提供资源（API key）时停下提问。

边界：五大联赛 96 队（英20/西20/意20/德18/法17+摩纳哥）。claim 模型覆盖球员/球队/比赛；ESPN 归 DATASET；阵容、积分、比分不改 claim 制。

回退标签：`v7.8.1-before-agent-backbone`（提交 6d1799e）。上一交付标签 `v8.0.0`（`4bf67fb`）。
测试：重构起点 full 735 → 交接时 full 959 → 收尾 full 1019 → **V8.1 交付 full 1040 / UI 28**，正式构建通过。

## 待办（详见 HANDOVER.md 第 3 节）

- [x] **TODO-1** 修复成功返回 `error:null` 的误报；真实 Gemini 验收还发现长 tool-call ID 超过内部限制，改用内部摘要键，协议 ID 不变。真实两子任务创建、完成、释放及父任务引用通过。
- [x] **TODO-2** 保留 `search_passages` / `compare_sources` 按需使用：样本中仅 5 场运行读过至少两篇原文，零调用不足以证明工具无用；它们分别提供长文定位和同源/日期线索，不裁决真伪。
- [x] **TODO-3** 界面显示名单实际赛季；新赛季目录不全时解释回退，不用请求年份冒充数据年份。
- [x] **TODO-4** 持久决策日志：原因、结构化观察、各自来源/双时钟、执行结果与前后变化；失败/缓存不更新确认时间，球队页与助手可读。
- [x] **TODO-5** API-Football 适配器、官方目录精确身份解析、持久滚动 24 小时配额、6 秒节流、请求共享、限流退避及加密凭据配置。代码与受控数据验证完成；本机尚无 key，带密钥业务联网验收待用户配置。
- [x] **TODO-6** 应用生命周期驱动常驻维护，仅关注球队、每轮最多两队、按时效更新、失败退避、取消清理；不自动调用付费模型。
- [x] **D-3** 删除无生产调用的 `conservative_delivery` 和对应旧删改行为断言，引用硬门保留。

## 已完成

- [x] **1. 溯源层** `server/provenance.py` + `tests/test_provenance.py`（45 项）
  - SourceTier(OFFICIAL/DATASET/PRESS/COMMUNITY/DERIVED)：层级决定措辞，不决定资格
  - Claim 字段级主张：两个时钟分开（fetched_at / observed_at），TTL 由字段名推导
  - Relation 枚举解决租借歧义：parent_club / current_club / former_club / youth_club
    - **关键修正**：母队与租借队不再被误报为冲突（合并键与 conflicts 都按 relation 分组）
  - TTL 锚定在 fetched_at 而非读取时刻；不可变字段（生日/国籍/身高）豁免转会窗加速
  - 复审队列按"超期倍数"排序，不是绝对时长
  - 登记表：域名层级由 register() 授予，tier_for_url 查表；URL 优先于 id，社区主机不能借用官方 id
  - 5 条不变量：无来源拒绝 / 未登记域名不得自称强层级 / DERIVED 记录算法输入 / tier 不取自输入 / 未知 tier 拒绝

- [x] **2. providers 层** `server/providers/` + `tests/test_providers.py`（17 项）
  - `provider(...)` 装饰器一次登记来源与能力；`sources(capability=, tier=)` 求解可用集
  - 统一信封 Answer(items/gaps/cached/spent/notices)；**失败是 gap 不是空成功**
  - 缺凭据的来源不对外提供（configure_credentials 注入密钥查询）
  - Paced 共享限速；merge_answers 保留全部归属

## 进行中

代码收尾与独立审查完成。API-Football 带密钥的现网可用性仍取决于用户账号，不能用离线结果替代现场验证。

## 待办

见上方待办清单与 `HANDOVER.md` 第 3 节。

## 完成记录

- [x] **9. V8.1 照片可恢复性与回答质量收尾（2026-09-30）**
  - full **1040 项通过（100.89 秒）**，UI 28 项通过，TypeScript/Vite 正式构建通过，`class-audit` 0 未样式化类。
  - 照片缓存：`_portrait_key` 支持代际，`cached_portrait` 回读上一代；`completed` 用 30 天窗口、`rejected/missing` 仍 24 小时、`unavailable` 仍 5 分钟。真实库 30 条 v1 孤儿条目已可被读回，Courtois 的 CC BY-SA 4.0 照片实测找回。
  - 社区兜底判据改为「维基媒体已给出结论（含拒绝）且社区未 settled」；`unavailable` 不算结论，避免维基媒体故障期间把整队推给限速的社区源。无结论时写 `not_yet_resolved` 并说明原因。
  - 失败报告由「永久跳过」改为有界退避（上限 5 次；15 分钟 / 1 小时 / 4 小时 / 12 小时）；来源分级导入失败回落「未分类来源」而非 500。
  - 回答质量：提问只强制 `status/answer/facts`；正文拒绝接口时间戳与内部字段名（引用原文豁免）；删除每条回答下的固定免责后缀；历史对话每轮只声明一次；六张卡预填提示压成一句话。
  - 新增 `server/player_names.py`（查表不音译，缺则保留源拼写）与 `tests/test_portrait_cache.py`、`tests/test_player_names.py`。
  - 样式表补回 `edaec35` 丢失的后代规则与失效的 `b`/`strong` 选择器（球员详情、号码徽标、位置角标、胜平负圆点、比赛日期、卡片标题、赛季数字）。桌面 1440px 与 390px 窄屏实机验收通过。
  - 交付标签 `v8.1.0`，上一交付标签 `v8.0.0`（`4bf67fb`）。按维护者要求本轮不重启本地服务。

- [x] **8. V8 交接收尾（2026-09-28）**
  - full 1019 项通过（109.34 秒），UI 28 项通过，TypeScript/Vite 正式构建通过。
  - 独立审查修复取消传播、旧任务污染下一轮、照片日志提前终结、缓存误确认、照片来源归属/替换检测、跨入口照片去重、HTTPS 凭据重定向与排队 429 等边界。
  - 真实配置模型用隔离虚构材料验证：9 次模型请求；两次 spawn、两子任务完成、两次 release、父任务实际阅读并引用两份材料；结束 active=0。真实验收另揭示并修复超长工具调用编号，不仅修正旧统计误报。
  - 桌面及 390px 窄屏已检查，页面无整体横向溢出，更新记录能展开且更新后重新读取。
  - 本机服务已切换 8.0.0，启动指纹与当前文件匹配，后台维护 running=true；已自动完成阿森纳、皇家马德里、拜仁和切尔西维护并生成记录。升级前数据库与 key 成对备份；459 条资料、85 个事件、206 次运行、93 份报告、279 条知识、1 份模型配置及 278 条旧任务记录的 ID、设置、加密凭据和 key 均核对保留。
  - 本轮前回退点 `v8-before-handover-completion-20260928`，交付标签 `v8.0.0`。来源配置与边界见 `DATA_SOURCES.md`，验收报告保存在 Git 忽略的 `output/`。

- [x] **1. 溯源层** `server/provenance.py`（45 项）：tier / field-level Claim / TTL 按场次与转会窗推导 / Relation 解决租借歧义 / 登记表 5 条不变量
- [x] **2. providers 层** `server/providers/`（65 项）：统一信封（失败是 gap 不是空成功）/ 能力注册表 / 缺凭据不对外提供 / ESPN(DATASET) + Wikimedia(DATASET) + TheSportsDB(COMMUNITY) + Reddit(COMMUNITY) + 球迷 RSS(COMMUNITY) + football-data.org(DATASET, 需 key)
- [x] **3. 阵容时效性**（13 项）：`roster_ttl` 按转会窗/临近开球/赛季推导，锚定 fetched_at；`transfer_window_note` 向用户解释当前时钟
- [x] **4. 核验放开**：删语义门/修订循环/恢复路径/覆盖率门；引用硬检查前移到 `submit_answer`（可重试）+ 交付兜底；`verification.annotations` 逐条标注；动态与问答都放开
- [x] **4b. 来源标注**（7 项）：`source_tier` + 前端 `.tier-badge`，未登记域名回落"未分类来源"不默认提级
- [x] **5. 采集调度**（19 项）：`collection.py` 按超期倍数排序（不是绝对时长）、无时间戳优先、预算闸门按最便宜可用来源计费、跳过的项要报告
- [x] **5b. 工具层重构**（15 项）：`tool_registry.py` 能力声明；三个只读工具合并为 `context_pack`；两个后台工具降级不对外宣传；旧名保留可调用（按 206 次真实调用记录决定）
- [x] **6. football-data.org 接入**（16 项）：第二个结构化来源；`fetch_document` 支持 headers（密钥不进 URL）；`Paced.disable()` 供测试使用
- [x] **6b. 采集调度接入执行**（11 项）：`TeamMaintenanceManager.review_team()` 按规划结果定向刷新；`team_view_claims` 每部分带自己的采集时间
- [x] **7. 提示词抽出**（18 项）：`server/prompts.py` + 漂移测试（提示词曾描述已删除的核验门）

## 用户明确要求

- 只想说清来源，真伪用户自己判断；核验不要严，严了会把信息卡死、阅读体验差
- 工具越丰富越好，但每次不一定全调用 → 需要一个 skill 专门管理这些工具
- 删掉太慢的、以前没写好的工具，重新规范格式
- 球员信息和图像要参考社区信息源（用户指出 TheSportsDB 的"维拉标成阿森纳"其实是租借，不是数据错误）
- 理想场景：软件由足球 AI Agent 自主维护信息，这对未来扩展很重要

## 命令

```
python scripts/check.py --suite fast
python scripts/check.py --suite full
python scripts/check.py --module test_xxx
npm run test:ui
npm run build
```
