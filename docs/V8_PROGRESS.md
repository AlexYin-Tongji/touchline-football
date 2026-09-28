# V8 重构进度

> **下一个模型请先读 `docs/HANDOVER.md`**，那一份含完整交接（现状、待办、硬约束、踩过的坑、用户原话）。本文件只记逐项完成状态。

目标：按 `DESIGN_PRINCIPLES.md` 持续迭代，直到作为发布 demo 达到 95 分。用户最后统一验收，中途不汇报，只在需要用户改产品边界或提供资源（API key）时停下提问。

边界：五大联赛 96 队（英20/西20/意20/德18/法17+摩纳哥）。claim 模型覆盖球员/球队/比赛；ESPN 归 DATASET；阵容、积分、比分不改 claim 制。

回退标签：`v7.8.1-before-agent-backbone`（提交 6d1799e）。
测试：重构起点 full 735 → 当前 **full 959 / fast 314 / UI 24**。

## 待办（详见 HANDOVER.md 第 3 节）

- [ ] **TODO-1** 查清 `spawn_agent` 失败原因（206 次运行里 2 次调用 2 次失败）
- [ ] **TODO-2** 评估删除 `search_passages` / `compare_sources`（零调用）
- [ ] **TODO-3** 阵容赛季标注：新赛季目录不全时显示"当前为上一赛季名单"
- [ ] **TODO-4** 决策日志（设计原则第八节，事后补不回来）
- [ ] **TODO-5** API-Football 接入（需配额计数）
- [ ] **TODO-6** 常驻循环：collection 与 review_team 都无定时器驱动
- [ ] **D-3** 评估删除 `verification.conservative_delivery`（已无交付路径调用）

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

（无。本次收尾时手上工作已全部完成并提交。）

## 待办

见上方待办清单与 `HANDOVER.md` 第 3 节。

## 完成记录

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
