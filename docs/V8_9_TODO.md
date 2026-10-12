# 看台 V8.9 待办

2026-10-13 记。这一版修掉了球队页的卡顿与三处数据缺陷，并把模型接进球队自己的资料链路。下面按「下一步要做的事」和「已知问题」分开记，每条都带**已经查到的证据**，避免下一个人重新调查一遍。

**交付验证**：full 1364 / fast 407 / UI 45 全过，`npm run build` 通过。

---

## 一、下一步要做：图像审查（唯一未动的一块）

用户 2026-10-12 明确要求：**球员照片、队徽、球队信息，模型都要接入去了解状态、决定补什么、最后验收返工**。其中球员信息、队徽、球队信息三块已在 V8.9 完成（见下节），**图像审查尚未开始**。

### 现状（已核实，不是推测）

- **没有多模态请求路径。** `server/provider.py:90-147` 的 `make_payload` 是唯一请求构造点，Anthropic 分支只产出 `text` / `tool_use` / `tool_result`，**没有 `{"type": "image"}` 分支**；Chat Completions 分支原样透传，没有把 `content` 改写成数组的地方。
- **全仓库不下载照片字节。** grep `image/png`、`image/jpeg`、`base64` 的命中全部是 data: URL 黑名单、光标编码、队徽 SVG 与 Fernet 密钥，**没有一处取回球员照片内容**。
- **现有判定 100% 是元数据规则**：ESPN 编号、全名+球衣号、出生日期、国籍、Commons 许可字段（见 `team_portraits.py:192-232` 的源强弱表与各 `resolve_portrait`）。
- `server/photo_acceptance.py:9-23` 的注释写明这是**刻意的**：验收判断由代码做，不让模型决定，否则"付费且非确定性的答案"会挡在一个本可由测试钉死的布尔值前面。

### 设计冲突要先解决

用户要的「模型看图判断」与上述"判定归代码"是张力的两端。**建议的落点是：事实判定仍归代码，模型只决定"要不要补、补谁"和"验收哪些需要返工"**——即模型在代码给出的候选集上做取舍，不推翻身份核验。若确实要模型看图，需要：

1. `provider.py` 增加 image 内容块构造（三种协议都要）。
2. 照片下载与体积/类型限制（不可信输入，需按不可信数据处理）。
3. 与 `AGENTS.md` 的「照片必须来自明确来源」对齐：模型只能对**已有明确来源**的照片做二次判断，不能引入新来源。
4. 队徽**建议不做**看图判断——队徽是商标图形，没有"这张脸是谁"的等价问题；保持 V8.9 的来源记录 + 哈希核验。

---

## 二、已知问题（本轮发现，未修）

### 2.1 Wikidata 中文名可能是繁体 —— **新引入的边界**

修中文名短路（见下节）后实测发现：`_wikidata_label` 按 `zh-hans|zh` 取标签，实体没有 `zh-hans` 时回落到 `zh`，而 `zh` **可能是繁体**。`player_names.py:438` 的 `cjk_label` 只校验"是 CJK 字符"，不区分简繁，于是繁体被当成有效中文名发布。

实测样本（2026-10-12，真实联网）：

| 球员 | 拿到 | 问题 |
| --- | --- | --- |
| Jamal Musiala | 贾迈勒·穆西亚拉 | 简体，正确 |
| Michael Olise | 米高·奧利斯 | 繁体，`奧` 应为 `奥` |
| Konrad Laimer | 孔拉德·萊梅爾 | 繁体，`萊`/`爾` 应为 `莱`/`尔` |

**方向**：优先 `zh-hans`；只拿到 `zh` 时判断是否含繁体专用字再决定采用或留空。改 `_wikidata_label` 与 `cjk_label` 的调用点，先用离线断言再做限量联网复核。

### 2.2 `decisions` 集合没有保留上限

`decisions.py:96-108` 的 `begin()` 把 `before`/`after` 两套完整观测写进每条记录，单条约 18 KB。实测 **167 行 / 5.31 MB**，全量反序列化 57–60 ms，**无 TTL、无上限**。

其余路径已用带 LIMIT 的定向查询（`decisions.py:144-149`，0.72 ms），但 `recover()`（`:171-174`）仍会全量。**方向**：按 team 保留最近 N 条（如 50），或在 `finish()` 后清理最老记录。

V8.9 已把 `json.loads` 移出锁，锁竞争从 69 ms 降到 38 ms，但**那 38 ms 仍是这把全局锁**，上限问题本身没解决。

### 2.3 `/state?view=summary` 返回全量数据

`main.py:708-726`：`store.list("matches", mode, team_id)` 无数量限制，`store.list("teams")` 固定 96 行。而 `sync_roster` 的球衣号筛选注释也提到过 summary 语义只需要"当前队最近几场+已关注球队"。`main.py:779` 的 full 视图还会带上 `news_reports.list_reports`，其内部走 `_public` → `_snapshot` → 逐个 `store.get("evidence")`，**又是一轮 N+1**。

### 2.4 前端渲染与轮询

- `App.tsx:465-484` 的 `TeamPicker` 过滤：`filter` 里跑 `clubCatalog.find()`（O(96) 线性查找）→ 每次渲染约 9216 次比较；`followed.includes()` 也是线性；**无 `useMemo`**（全项目 `useMemo` 只在 `v7Shared.tsx` 出现 2 次）。`clubCatalog.ts` 已有 `clubsByName` Map，但没有按 id 查的接口。
- `player-grid`（`v7-app.css:2002`）与 `picker-grid`（`:3667`）无虚拟化；96 个卡片节点全量挂载，卡片带 `box-shadow` + `transform` transition。
- `WorkflowControls.tsx:147` 的 4 秒轮询在 `enabled: true` 的长任务上会**永久轮询**。
- V8.9 已修：`/decisions` 的 5 秒重发、`Crest` 缺懒加载、轮询定时器被反复重建。

### 2.5 `should_continue` 的 `max_attempts` 是死代码

`photo_acceptance.py:220-241` 有第三个停止条件 `attempts >= max_attempts`（默认 3），但**生产代码从不传 `attempts`**（`team_center.py` 的调用点只传 acceptance/previous），全仓库唯一传的是测试。实际只有"无进展即停"那条生效。

### 2.6 `_apply_photo` 返回值被忽略

`team_portraits.py:173-174` 在 `photo_outranks` 拒绝时返回 `False`，但多数调用点忽略返回值（`:1034`、`:1083`、`:1109`），更强源已有图时较弱源的成功结果被静默丢弃，`updated_count` 不计入。

### 2.7 静态中文名表覆盖 8/96 队

`player_names.py:58-405` 只覆盖英超 6 队 + 皇马 + 巴萨（西甲其余 19 队为 0，曼联 0/30）。V8.9 修好动态链路后动态来源成为主力，静态表可继续保留为"作者手录的高置信补充"，**不必强行扩表**——扩表需要逐条核准来源，收益低于动态链路。

---

## 三、V8.9 已完成（供对照）

### 3.1 卡顿

| 改动 | 位置 | 效果 |
| --- | --- | --- |
| `json.loads` 移出全局锁 | `storage.py` | `/decisions` 锁竞争 **69 ms → 38 ms** |
| `_ReadView`：一次渲染只扫一遍 | `team_center.py` | 消 2 次 `league_matches` + N+1 `store.get` |
| `ReadView`：`/leagues` 共享视图 | `league_data.py`、`main.py` | store 调用 **209 → 19 次**（`get:teams` 96 → 0） |
| `/decisions` 不再 5 秒重发 | `v7TeamCenter.tsx` | 去掉约 1/3 的轮询请求 |
| 轮询定时器不再被反复重建 | `v7TeamCenter.tsx` | 依赖从 `components` 对象改为稳定布尔值 |
| `Crest` 补 `loading="lazy" decoding="async"` | `components.tsx` | 96 张队徽不再全部立即解码 |

### 3.2 球员照片：三处「要启动几次才行」

| 改动 | 位置 |
| --- | --- |
| 超时不再丢弃整批——每完成一个球员就落库 | `team_center.py`、`team_portraits.py`（`on_progress`） |
| `photos` 角色可通过设置页指派（原先必然 422） | `model_config.py`（`RoleAssignments` 缺 `photos` 字段） |

**试过但回退的一个改动**：曾把"首个传输失败即中止本来源"改成"连续 3 次失败才停"，理由是"一次失败不该放弃整批"。回退原因——它不解决任何真实问题：

- 协调器对主机级失败有 30 秒冷却（`research_transport.py:214-216`），所以停下之后，整批剩下的球员在冷却期内**根本不会发请求**，继续遍历只是把 1 个真实故障变成 N 个上报故障，既省不下网络也不会误伤。
- 你抱怨的"要启动几次"，真正的解法是**增量保存**（保留），与容忍度无关。回退后新增的取消测试仍然通过，正好证明这一点。
- 两个既有测试（`test_wikimedia_portraits.py:317`、`:442`）明确锁定了旧的 `break` 行为，注释写明了理由。

增量写入的守卫必须是「**自上次保存以来有没有别人写过**」：记录 `store.put` 返回的副本而不是工作对象引用，工作对象会被就地块修改。第一版比较「存储是否等于起始快照」是错的，会让每次保存都误判为"阵容被替换"。回归测试见 `tests/test_photo_workflow.py` 的 `test_a_pass_cut_short_keeps_the_photos_it_already_checked`。

### 3.3 中文名短路（西甲球员无中文名的根因）

`wikimedia_portraits.py` 原第 549 行：

```python
if club != "confirmed" and not await _wikidata_club(...):
```

`_wikidata_club` 一次调用同时做**核对俱乐部**和**取中文标签**。文章首句已写明"现效力于 X"时整个调用被跳过，标签一并丢失——**越是名队、越完整，越拿不到中文名**。

实测本地库：**16 条完成的照片里 15 条 `name_status=unknown`**；曼联 0/30 有中文名（却有 29 张照片）。

**修法**：新增 `_wikidata_label`，把「取标签」与「核验俱乐部」解耦。标签请求推迟到确定只有一个候选人之后（避免在最终被拒的文章上白花请求）。安全依据是**已经跑过的身份检查**（完整姓名 + 出生日期 + 运动员身份），不是俱乐部声明——俱乐部声明是第二重独立确认，不是读标签的前提。

真实联网实测（拜仁三人，原均为 `unknown`）：Musiala、Olise、Laimer 全部拿到中文名，并顺带解开缓存死锁（原本 `unknown` 会让 `cached_portrait` 早退、永不再查 Wikidata）。

### 3.4 球队自身资料接入模型（`server/club_profile.py` 新增）

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

## 四、硬约束（改动时必须守住）

- 模型可以解释，**不能编造或直接改写事实字段**。补数据只能通过带来源的采集，不能由模型写入。
- 照片与队徽**必须来自明确来源**；跨源匹配需明确身份依据；缺图留空。私有、删除、避剧透来源不得从旧图或引用入口泄漏。
- 测试用临时数据库和本地/mock 模型，**不调用真实付费模型、不修改用户数据**。真实联网验收要限量，并区分测试资料与真实资料。
- 数据升级只补缺失字段，保留用户修改、明确留空、自建球队与关注选择。
