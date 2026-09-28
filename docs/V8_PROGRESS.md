# V8 重构进度

目标：按 `DESIGN_PRINCIPLES.md` 持续迭代，直到作为发布 demo 达到 95 分。用户最后统一验收，中途不汇报，只在需要用户改产品边界或提供资源（API key）时停下提问。

边界：五大联赛 96 队（英20/西20/意20/德18/法17+摩纳哥）。claim 模型覆盖球员/球队/比赛；ESPN 归 DATASET；阵容、积分、比分不改 claim 制。

回退标签：`v7.8.1-before-agent-backbone`（提交 6d1799e）。
测试基线：fast 303 / full 735 → 当前 full 783。

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

- [ ] **3. 搬迁现有来源到 providers**（行为不变）
  - ESPN（DATASET, FIXTURE/SQUAD/STANDINGS）← team_center.py + league_data.py
  - Wikimedia（OFFICIAL? no → 自由许可来源，按现有判定，IM… 定为 DATASET）← wikimedia_portraits.py
  - TheSportsDB（COMMUNITY, PERSON/IMAGE）← community_portraits.py
  - 现有多家媒体源（PRESS, NEWS）← news_sources.py + research.py
  - 注意：搬迁必须行为不变，跑全量对比

## 待办

- [ ] **4. 核验全部放开**：删 verify_submission 语义门 / conservative_delivery / revise_news / restore_*_checkpoint / news_coverage 覆盖率门；保留 prepare_claims 引用硬检查（本地确定性）；改为每条主张带 source_support 标注 + UI tier 标签。动态页与问答都放开。
- [ ] **5. 阵容时效性修复**：ROSTER_CACHE_SECONDS 一刀切 → 按转会窗/赛季阶段/临近开球推导 TTL；赛季目录不全时明确显示"当前为 2025/26 名单"
- [ ] **6. collection/ 常驻调度**：采集与入库解耦（claim 直接入库，新闻稿是视图）；优先级队列 + 配额闸门 + 新鲜度驱动重查
- [ ] **7. 工具层重构**：注册表 + 能力标签 + 统一信封；合并冗余工具（context.pack / news.search / doc.read / entity.view / search+locate+conflicts）；先扫 runs.steps 统计再砍（maintain_team_data / fetch_player_photos / conversation_history 是删除候选）
- [ ] **8. 新来源接入**：football-data.org、API-Football（命名密钥表）、Reddit JSON 端点（COMMUNITY，corroboration=upvote）、球迷博客/论坛 RSS
- [ ] **9. 拆 agent.py** → harness/loop.py + policy.py + delivery.py；修 agent_progress.py 交替循环判定（按是否产生新证据判断）
- [ ] **10. 决策日志**

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
