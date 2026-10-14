# 看台 V8.9 待办

2026-10-13 记。这一版修掉了球队页的卡顿与三处数据缺陷，并把模型接进球队自己的资料链路。

**2026-10-13 续记**：本文最初的「下一步要做」（图像审查）与「已知问题」五条现已全部完成——图像审查扩展成通用复核回路（覆盖照片、球员信息、球队资料、队徽），中文名繁体边界、`decisions` 无上限、summary 全量返回、`should_continue` 死代码、`_apply_photo` 静默丢弃五项一并修掉；前端只修了核实成立的那一条，另两条经核实**不成立**，理由见 3.3。

**交付验证**：本轮 full 1432 / fast 516 / UI 45 全过，`npm run build` 通过。

---

## 一、已完成：通用复核回路（照片、球员信息、球队资料、队徽）

用户 2026-10-12 明确要求：**球员照片、队徽、球队信息，模型都要接入去了解状态、决定补什么、最后验收返工**。用户随后进一步说明：**不是让模型逐条看所有东西**——代码本来就有前置结论，模型要处理的是**模糊项**，并决定下一轮哪些 tool 该重试、如何重试，即「react」而不是「重新判一遍」。四块现已全部接入同一条回路。

### 落点：代码判定，模型取舍

| 问题 | 谁回答 |
| --- | --- |
| 这是谁、有没有字段、来源有没有回答过 | **代码**，规则判定 |
| 字段值写什么、写进哪一行 | **代码**，只由来源写入 |
| 哪个候选更可能、一次未命中是限流还是检索词错 | **模型** |
| 下一轮重试谁、怎么重试 | **模型**给动作，代码执行 |

实现分三层：

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 协议 | `provider.py`（`image_block`、`_wire_image`） | 三种协议的图像内容块构造 |
| 回路 | `review_loop.py` | 通用复核：候选板、动作词汇、解析与校验 |
| 领域 | `photo_review.py`、`review_records.py` | 建板与取字节（照片）；名字、球员事实、球队资料、队徽（记录） |

### 四块各自的「代码已给结论 / 模型仍需判断」

| 域 | 代码已给 | 模型判断 |
| --- | --- | --- |
| 照片画面 | 身份、许可、地址由来源核对完毕 | 画面是否可用（清晰、人像、无水印或错误裁切） |
| 球员事实 | 阵容源给了哪些字段、缺哪些 | 缺出生日期／国籍的人是否值得专项查找，先查谁 |
| 中文名 | `zh-hans` 标签、繁简归一化、身份证明 | 未命中是限流、检索词，还是来源确实没有 |
| 球队资料与队徽 | `missing_fields`、队徽 `status`（缺来源／站外／本地） | 缺口该补哪一项、队徽缺的署名能否从已有记录恢复 |

**动作是行动，不是形容词**：`keep` / `retry`（带 `retry_hint` 与 `effort`）/ `investigate`。旧的 `ok`/`needs_rework` 形容词命名已废弃——存一个"不好"没人能执行，存"换检索词再试"才是下一步。

### 关键边界（均有回归测试）

- **只有已有来源的照片进入复核**。带 `photo_url` 无 `photo_source_url` 的行被排除：否则模型的答案会顶替它从未做过的来源核对。
- **模型不写任何字段值**，不引入新来源，不撤回已核验照片。
- **读不到就当没读**。下载失败或格式不符记入 `unreadable`，不是 `retry`——把网络故障当成"图很糟"会误伤好照片。
- **缺口没有图也要能裁**。无图可读时本轮仍对缺口给出结论，否则一个坏图会取消整轮可回答的复核。
- **未指派角色 = 不发请求、不下载**。模型检查在下载之前。
- **队徽不做画面审查**，保持来源记录与哈希核验。队徽的字节已计入署名记录的哈希、权利属于俱乐部，没有"这张脸是谁"的等价问题；它开放的问题只有覆盖度，而那正是代码能算的。
- 字节按不可信输入处理：复用 `fetch_document` 的固定公网地址、体积上限、类型白名单，再按魔数判断真实类型。

### 两个被改正的设计错误（都是本轮自查发现）

1. **第一版把结论写成形容词**（`ok`/`needs_rework`），且是照片专用。按用户说明改为通用回路 + 动作词汇。
2. **`club_items` 曾完全空转**：签名收 `summary`，调用点只传 team，`missing` 永远为空，球队资料缺口从未进入复核。`club_profile` 把字段直接写在 team 记录上，读的是一个不存在的报告对象。改为从 team 记录读 `missing_fields`，并加回归测试锁住"必须读 team 行"。

### `should_continue` 的 `max_attempts`（见 3.4）

复核回路当初**没有**顺手修它：`attempts` 无人传，那个预算从未生效。已在 3.4 接上真实轮次计数。

### 新增模型角色 `review`

`MODEL_ROLES` 增加 `review`，与 `verify`/`photos` 同属 `NEVER_INHERITED_ROLES`——它在刷新时运行，不能被活动聊天模型继承。`RoleAssignments` 与设置页角色表同步增加该字段（`photos` 曾因缺字段导致必然 422，此处按同一教训一并补齐）。

工具 `review_player_photos` 已更名为 `review_maintenance`——它现在覆盖四块，旧名字是误导。

### 涉及文件

`server/review_loop.py`（新）、`server/review_records.py`（新）、`server/photo_review.py`（重写）、`server/provider.py`、`server/model_config.py`、`server/tool_registry.py`、`server/skill_catalog.py`、`server/agent_tools.py`、`server/team_center.py`、`server/workflows.py`、`server/player_names.py`、`server/wikimedia_portraits.py`、`server/wikidata_zh_hans.json`（新）、`agent_skills/team-information/references/squad/review.md`（新）、`agent_skills/team-information/{SKILL.md,references/squad.md,references/squad/images.md}`、`src/v7ModelSettings.tsx`、`src/v7TeamCenter.tsx`、`tests/test_photo_review.py`（新）。

### 交付验证

full 1432 / fast 516 / UI 45 全过，`npm run build` 通过（已确认 `review_note` 进入产物）。

---

## 二、已修：Wikidata 中文名的繁体边界（原 2.1）

`_wikidata_label` 按 `zh-hans|zh` 取标签，实体没有 `zh-hans` 时回落到 `zh`，而 `zh` 可能是繁体；`cjk_label` 只校验"是 CJK 字符"，不区分简繁。

**修法**：新增 `server/wikidata_zh_hans.json`，由 Unicode Unihan 的 `kSimplifiedVariant` **一次性生成后随仓库发布**（运行时不联网、不依赖外部库）。只保留单目标映射，并剔除自身是简化目标的字（`著`/`裡`/`復` 等简繁通用字，转换会破坏名字而非规范它）。

- `to_simplified()` 逐字转换；`_wikidata_label` 读取时归一化，并把**来源发布的原文**存进证明的 `label`。
- `checked_wikidata_reading` 用同一映射重算必对：行里存简体、证明里存原文，`to_simplified(proof.label) == name_zh` 才算通过。改过的读数无法借别人的转换蒙混过关。
- 未能完全归一化的标签**留空而非半转换**。

实测（真实联网，2026-10-13）：`米高·奧利斯` → `米高·奥利斯`，`孔拉德·萊梅爾` → `孔拉德·莱梅尔`，`贾马尔·穆西亚拉` 不变。注意 `米高` 是香港译名而非错字，所以只做确定性转换，不靠"看起来不对"来拒绝。

**踩过的坑**：`_apply_photo`/`with_reading` 的静态表优先级会把 Wikidata 标签压回去（Ødegaard 表内有"厄德高"），所以端到端测试要用静态表里没有的球员，否则断言测的不是转换。

---

## 三、已修：性能与既有缺陷（原 3.1–3.5）

五条全部修复。每条都先核实断言是否成立，再动手——其中一条（3.3 的轮询）核实后发现**原描述不成立**，见下。

### 3.1 `decisions` 集合无上限 → 按队保留 50 条

实测比原记录更严重：**82 行 / 2.17 MB，单条约 26 KB**，且 liverpool 一队占了 64 行。

- `RETAIN_PER_TEAM = 50`，在 `finish()` **同一事务内**裁掉该队最老的终态记录。事务内是刻意的：两个并发 finisher 不能各自看到"满 50"而留下 51 条。
- **只裁终态**。运行中的行是 `finish()` 正要更新的那一行，删掉它等于抹掉一条尚未记录的决策。
- 裁剪**按队**，因为这是读者提问的轴：球队页的更新记录就是该队的历史，一个繁忙的俱乐部不该挤掉别的队。
- `recover()` 改为 SQL 只取未完成的行。原先走 `store.list("decisions")`，为找几行运行中的记录解码整个集合。
- 上限取 **API 页上限**（`le=DECISION_HISTORY_MAX`），不再各写一个字面量：上限小于 API 声明的页大小，那个声明就成了谎。

真实数据实测（一致快照）：**82 行 / 2.17 MB → 38 行 / 1.07 MB**，liverpool 64 → 20（当时上限 20），其余队完整保留。上限改为 50 后 liverpool 保留最近 50 条。

### 3.2 `/state?view=summary` 返回全量 → 只取 shell 能显示的

- `matches` 收窄到**当前队 + 关注队**，每队上限 `SUMMARY_MATCHES_PER_TEAM = 24`。两个消费方（`FeedView` 的下一个未开赛、助手助手的最近一场已结束）都是按当前队过滤，所以范围外的行本来就是无人可达的行。
- `teams` **保持全量 96 行**：它被球队选择器、`favoriteTeams` 和按 id 查队徽使用，裁掉会坏界面。
- **未改**：full 视图的 `list_reports` → `_public` → `_snapshot` 逐个 `store.get("evidence")` 那条 N+1。它每次页面加载只发生一次（不是轮询），且改动会牵动报告可见性边界，风险大于收益。保留在此说明。

### 3.3 前端渲染与轮询 → 只修成立的一条

**核实结果：原描述有两条不成立。**

| 原描述 | 核实结论 |
| --- | --- |
| `TeamPicker` 过滤无 `useMemo`、O(96) 线性查找 | **成立**，已修 |
| grid 无虚拟化，96 卡片全量挂载 | **不成立**：`player-grid` 渲染的是阵容（约 30 人），不是 96 |
| 4 秒轮询会**永久轮询** | **不成立**：`enabled` 在完成路径会被清掉（`workflows.py` 的 `finally` 与 `update(..., enabled=False)`），`isActive` 随之转假，定时器被清除 |

修的是唯一成立的那条：

- `clubCatalog.ts` 新增 `findClubById`。`findClub` 走 `normalizeClubName`（去标点与空格），这是名字的正确规则、对 id 是错的；id 查表需要哈希而不是每次线性扫目录。
- `TeamPicker` 的过滤加 `useMemo`，`followed.includes` 换成 `Set`。原先每次渲染（含每次按键）约 9216 次比较。

**未做**：虚拟化。核实后没有依据——grid 挂载的是阵容人数而非目录人数。

### 3.4 `should_continue` 的 `max_attempts` 是死代码 → 接上真实计数

`attempts` 是没有任何生产调用点传的参数，所以唯一起作用的停止条件是"无进展"。这意味着一轮只多一张照片的传球会一直跑到读者不再打开球队页为止。

- 轮次计数与上次验收结果存在一起（`_photo_acceptance_key`），每轮递增一次，正是预算的计量单位。
- 写入与读路径（`center_view`）都传入该计数。
- 回归测试分别锁住"每轮递增"与"预算到点即停"，并已验证去掉修复后测试会失败。

### 3.5 `_apply_photo` 返回值被忽略 → 上报而不是静默

核实后修正了原描述的一处错误：`updated_count` 是**行内容与 before 快照的比较**，不受返回值影响，不存在"不计入"的问题。真正的缺陷是**静默**——调用方把 `False` 丢掉，于是"来源答了一张、被更强的源压回去"与"来源什么都没答"在结果里完全一样。

- `_record_kept_source` 把拒绝记成 `kept_stronger_source_count` 与来源对，**不记录球员编号**：读者需要知道这轮行为是否正确，不需要知道跳过了哪几行，而球员 id 在这里会是一句未经核验的身份断言。
- 排序方向与 `photo_outranks` 一致（**小 rank 更强**）。第一版我写反了，测试当场抓住。
- 只在来源确实新答了一张且被拒时上报；同源重跑、更强源写入、未登记来源都不算拒绝。

## 四、V8.9 前序已完成（供对照）

### 4.1 卡顿

| 改动 | 位置 | 效果 |
| --- | --- | --- |
| `json.loads` 移出全局锁 | `storage.py` | `/decisions` 锁竞争 **69 ms → 38 ms** |
| `_ReadView`：一次渲染只扫一遍 | `team_center.py` | 消 2 次 `league_matches` + N+1 `store.get` |
| `ReadView`：`/leagues` 共享视图 | `league_data.py`、`main.py` | store 调用 **209 → 19 次**（`get:teams` 96 → 0） |
| `/decisions` 不再 5 秒重发 | `v7TeamCenter.tsx` | 去掉约 1/3 的轮询请求 |
| 轮询定时器不再被反复重建 | `v7TeamCenter.tsx` | 依赖从 `components` 对象改为稳定布尔值 |
| `Crest` 补 `loading="lazy" decoding="async"` | `components.tsx` | 96 张队徽不再全部立即解码 |

### 4.2 球员照片：三处「要启动几次才行」

| 改动 | 位置 |
| --- | --- |
| 超时不再丢弃整批——每完成一个球员就落库 | `team_center.py`、`team_portraits.py`（`on_progress`） |
| `photos` 角色可通过设置页指派（原先必然 422） | `model_config.py`（`RoleAssignments` 缺 `photos` 字段） |

**试过但回退的一个改动**：曾把"首个传输失败即中止本来源"改成"连续 3 次失败才停"，理由是"一次失败不该放弃整批"。回退原因——它不解决任何真实问题：

- 协调器对主机级失败有 30 秒冷却（`research_transport.py:214-216`），所以停下之后，整批剩下的球员在冷却期内**根本不会发请求**，继续遍历只是把 1 个真实故障变成 N 个上报故障，既省不下网络也不会误伤。
- 你抱怨的"要启动几次"，真正的解法是**增量保存**（保留），与容忍度无关。回退后新增的取消测试仍然通过，正好证明这一点。
- 两个既有测试（`test_wikimedia_portraits.py:317`、`:442`）明确锁定了旧的 `break` 行为，注释写明了理由。

增量写入的守卫必须是「**自上次保存以来有没有别人写过**」：记录 `store.put` 返回的副本而不是工作对象引用，工作对象会被就地块修改。第一版比较「存储是否等于起始快照」是错的，会让每次保存都误判为"阵容被替换"。回归测试见 `tests/test_photo_workflow.py` 的 `test_a_pass_cut_short_keeps_the_photos_it_already_checked`。

### 4.3 中文名短路（西甲球员无中文名的根因）

`wikimedia_portraits.py` 原第 549 行：

```python
if club != "confirmed" and not await _wikidata_club(...):
```

`_wikidata_club` 一次调用同时做**核对俱乐部**和**取中文标签**。文章首句已写明"现效力于 X"时整个调用被跳过，标签一并丢失——**越是名队、越完整，越拿不到中文名**。

实测本地库：**16 条完成的照片里 15 条 `name_status=unknown`**；曼联 0/30 有中文名（却有 29 张照片）。

**修法**：新增 `_wikidata_label`，把「取标签」与「核验俱乐部」解耦。标签请求推迟到确定只有一个候选人之后（避免在最终被拒的文章上白花请求）。安全依据是**已经跑过的身份检查**（完整姓名 + 出生日期 + 运动员身份），不是俱乐部声明——俱乐部声明是第二重独立确认，不是读标签的前提。

真实联网实测（拜仁三人，原均为 `unknown`）：Musiala、Olise、Laimer 全部拿到中文名，并顺带解开缓存死锁（原本 `unknown` 会让 `cached_portrait` 早退、永不再查 Wikidata）。

### 4.4 球队自身资料接入模型（`server/club_profile.py` 新增）

| 用户要求 | 实现 |
| --- | --- |
| 模型**了解状态** | `get_team_data` 新增 `club_profile`：字段值、缺项、来源实体、队徽状态 |
| 模型**决定补哪些** | `maintain_team_data(parts=[...])` 新增 `profile`，描述写明"先看状态再决定" |
| 模型**验收返工** | 组件给 `missing_fields` + `note`，界面显示「球队资料」一项 |

**来源与规则**：只取维基数据（CC0）。**只填空、永不覆盖、不写空值**，来源与值同一次写入。队徽不进照片链路、不下载图片，只报告"本地资产是否带来源与署名"。

**测试覆盖的陷阱**（每个都是真实 Wikidata 返回过的）：青训队（`Q2669653`）、二队、女队、`U21`；博洛尼亚是城市（`Q1891` vs 俱乐部 `Q1893`）、尼斯是地名、摩纳哥是国家；无字段可采的实体一律拒绝；非拉丁昵称留空（阿斯顿维拉的昵称真是希腊语）。

**两个曾写错的规则**（已修，见 `tests/test_club_profile.py`）：
1. `_latin` 用 `< 0x2E80` 判拉丁文会**放行希腊字母/西里尔**——改为逐字母判定 `a-z`。
2. `identity_score` 不能对"任何有类别的实体"给 1 分——必须落在 `FOOTBALL_CLUB_CLASSES` 内。

---

## 五、硬约束（改动时必须守住）

- 模型可以解释，**不能编造或直接改写事实字段**。补数据只能通过带来源的采集，不能由模型写入。
- 照片与队徽**必须来自明确来源**；跨源匹配需明确身份依据；缺图留空。私有、删除、避剧透来源不得从旧图或引用入口泄漏。
- 测试用临时数据库和本地/mock 模型，**不调用真实付费模型、不修改用户数据**。真实联网验收要限量，并区分测试资料与真实资料。
- 数据升级只补缺失字段，保留用户修改、明确留空、自建球队与关注选择。
