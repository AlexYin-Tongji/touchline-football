# 看台 V8.8：头像来源、同名遮挡与技能分层

2026-10-12 交付。这一轮修掉球员照片的四类缺陷、查清并绕开社区来源的免费额度上限，并把三个技能整理成分层结构。

## 1. 球员照片：四处「把缓存条件当成身份判定」

四个缺陷病根相同——**存储过期被读成了身份被否定**，于是已核验的照片被自己擦掉。

| 缺陷 | 表现 | 修法 |
| --- | --- | --- |
| 缓存过期即清空照片 | 社区窗口 7 天，到期就把行上的照片清掉再重取 | 新增 `current_proof()`：分开「能否复用」与「是否撤回」，证据仍成立就不管多久都保留 |
| 两个源对 `force` 理解相反 | 整队刷新时社区源跳过所有缓存、对每个未解决球员重发请求 | 统一为维基源的规则：只有点名球员才重新核验（`community_force_player`） |
| `status != "unavailable"` 当成否定 | 连"阵容缺出生日期""有多个候选"这类**没发请求就返回**的结论也会擦照片 | `_source_refutes_photo()` 排除这两类；传输故障一律不能撤销已证照片 |
| 社区 7 天 vs 维基 30 天 | 完成与判定共用一个窗口 | 拆成 `SUCCESS_SECONDS`（判定，7 天）与 `COMPLETED_SECONDS`（完成，30 天） |

**同名遮挡成为独立状态。** 整年出生日期不符的同名者（全库 53 人）一直拒绝得对，但一直报成普通的「多候选」，读起来像"重试一下"。现在带 `namesake_blocked: true`，验收模块归类为 `fix_namesake`：对同一来源重试无意义，但**不等于拿不到照片**——另一个来源仍可回答。这与「源里没有此人」是两件事。

## 2. 社区来源的免费额度上限（本轮查清）

实测免费 key（`/api/v1/json/3/`）**两个端点都被截断**：

| 端点 | 上限 | 行为 |
| --- | --- | --- |
| `searchplayers.php?p=` | **1 条** | 按名字匹配取第一条，**不按相关性排序** |
| `lookup_all_players.php?id=` | **10 条** | 字母序，从中间截断 |

六支球队实测全部 10 行（阿森纳 A→E、切尔西 A→J、皇马 A→D、拜仁 A→J）。所有绕法均无效：分页参数、通配符、旧版端点、API 版本、同主机其他前缀、大小写/空格/连字符/零宽字符变体、按 ESPN id 反查。

**这解释了"冷门球员能找到、里斯·詹姆斯找不到"这个反常现象**：规则是字母序位置，不是球员名气。`p=Palmer` 返回 Liam Palmer 而非 Cole Palmer，`p=White` 返回 Jamie Sendles-White 而非 Ben White。数据库本身完整（Salah、Haaland、van Dijk、Saka 均可查到）。`p="Reece James"` 只返回谢周三那位（1993-11-07），切尔西那位（1999-12-08）在该搜索下不可见。

**联合查找（新增）。** ESPN 的 roster 端点返回整队（含 `id`、球衣号、全名），TheSportsDB 的每条记录带 `idESPN`，两者同一套编号。于是走**姓氏搜索**（与全名搜索返回不同结果集），再用 `idESPN` 交叉确认。切尔西实测 **12/29 通过这条路确认身份**，包括全名搜索永远拿不到的人。已接入 `resolve_portrait`：姓氏分支现在对同名遮挡也开放，并在这组结果上做 id 交叉；用 `id_bound_settled` 避免重复查询。

**ESPN 不能作为图像源。** 178 名球员实测，roster 自带头像仅 **10 人 = 5.6%**（切尔西 3/30，PSG 0/24）；CDN 不能按 id 构造（未提供的就是 404）；球员页返回的是**别的球员**的头像。ESPN 的位置是身份锚点。

## 3. 技能分层

技能从"一个文件"变成"一棵树"：`SKILL.md` 是任务的视角，下面是按问题加载的方向。

```
team-information/SKILL.md          数据范围、维护按部分进行
  references/squad.md              → squad.info | squad.images
  references/team.md               俱乐部、赛程、积分、避剧透
  references/data.md               胜负、进失球、样本范围
  references/data-scope.md
team-news/SKILL.md
  references/directions.md         → briefing.md | news-writing.md
```

**加载一个方向会收窄工具面。** `tool_registry.py` 本来就按 capability 选工具，方向只是声明自己那部分：`TOPIC_CAPABILITIES` 与技能集合**取交集**，只能减不能加。实测加载 `squad.images` 后工具从 16 个降到 11 个，证据阅读器与文章发现被移除。

**唯一的例外**是 `exposes`：它把技能本来就持有、但默认不向对话提供的后台工具放出来。`squad.images` 是唯一使用者，放回 `fetch_player_photos`——图像方向的存在意义就是跑一批核对，扣住工具会让它的说明无法执行。**作用域仍然优先**：demo、无网、固定文章、固定比赛四种情况下，图像方向也拿不回这些工具（逐个验证）。

## 4. 验收后的行为

- 每个缺图球员带 `reason_code` 与 `retryable`；`namesake_blocked` 明确标注。
- `acceptance` 给出七类归因计数、`actionable_count`（只有为 0 才算没活可干）、中文结论 `acceptance_note`、以及是否值得再跑一轮的判断。
- `photo_queue` 把同名遮挡与普通多候选分开，方便先看清缺口再决定动作。

## 验证

1346 项 Python 测试、45 项 UI 回归、`npm run build` 全部通过。新增 `tests/test_joint_lookup.py`（联合查找与额度上限）、`tests/test_skill_tree.py`（分层、收窄与作用域优先级），并在 `test_community_portraits.py`、`test_team_portraits.py`、`test_photo_planning.py` 增加同名遮挡与保留逻辑用例。

**一处教训值得记住：** 为"保留照片"写的第一个测试是**空洞的**——删掉修复它仍通过，因为照片从另一条路径也活了下来。做变异测试（专门禁用保留分支）后它才失败。另有两个回归测试同样空洞，已一并修正。**给"清除某字段"写的测试，必须用变异测试确认它真的在测。**

## 尚未处理

- **付费 key**（约 $10/月）可解除 1/10 条上限，仍是单点最省事的改善；联合查找已覆盖其中一部分。
- 维基媒体在本轮验证期间从开发机不可达（限流），该路径的联网验收未完成。
