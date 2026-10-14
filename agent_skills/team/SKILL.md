---
name: team
description: 球队中心面板「更新/补全」的编排：读取缺口快照，只派发缺失、过期或可重试的叶子，汇总拿满与失败后再计算图表。不是对话问答，也不替代 team_refresh。
---

# 球队中心更新

这是球队中心面板「更新/补全」的编排，不是对话，也不是问答。不要用它回答用户提问，不要写新闻。`team_refresh` / `team-maintenance` / `team-information` 仍服务其他入口，不要改走那几条，也不要把本流程并进它们。

父任务只加载本技能。叶子说明由子任务自己加载，不要把叶子全文抄进 `context`。

## 先读缺口，再决定派谁

用 `get_team_data` 读当前快照：俱乐部资料与队徽状态、阵容、照片验收、赛程、积分榜。快照是现状，不是待办清单。

只派下面这六种里**缺失、过期，或原因码 `retryable` 为真**的叶子。已经拿满、以及不可重试的终态，不要再派。

| 顺序 | 叶子 | 子任务加载 |
| --- | --- | --- |
| 1 | `club.crest` | `use_skill(skill="team", topic="club.crest")` |
| 2 | `squad.info` | `use_skill(skill="team", topic="squad.info")` |
| 3 | `squad.names` | `use_skill(skill="team", topic="squad.names")` |
| 4 | `squad.images` | `use_skill(skill="team", topic="squad.images")` |
| 5 | `schedule.standings` | `use_skill(skill="team", topic="schedule.standings")` |
| 6 | `stats.charts` | `use_skill(skill="team", topic="stats.charts")` |

`club.crest`、`squad.info`、`squad.images`、`schedule.standings` 可以并行。`squad.names` 不得早于阵容：本轮要等 `squad.info` 写出带 `provider_id` 的名单，或确认快照里身份键已经齐。条件满足后可以和 `squad.images` 并行。`stats.charts` 必须等本轮赛程结果，或确认已有仍有效的赛程缓存之后再派。没有可用样本时，图表叶子返回 `SAMPLE_INSUFFICIENT`，禁止编造胜平负、进失球或任何图表数字。

## 怎么派

每个叶子一个 `spawn_agent`。`skill` 传 `team`。`task` 只写这一个叶子要交付什么。`context` 只放该叶子的缺口与原因码。`tools` 只给该叶子要用的工具：

- `club.crest`、`squad.info`、`schedule.standings`：`get_team_data`、`maintain_team_data`、`read_evidence`、`use_skill`、`submit_answer`
- `squad.names`：只有 `get_team_data`、`use_skill`、`submit_answer`。不要给 `maintain_team_data` 或 `fetch_player_photos`。中文名由这一片的模型翻译，不查姓名来源。
- `squad.images`：再加上 `photo_queue`、`plan_photo_batch`、`fetch_player_photos`、`review_maintenance`
- `stats.charts`：只有 `get_team_data`、`use_skill`、`submit_answer`。不要给它 `maintain_team_data` 或 `fetch_player_photos`

子任务加载 topic 之后只做那一片，不要再派子任务。

## 汇总与停止

收回各叶子的结果后再交付。汇总两件事：哪些叶子拿满了，哪些带着原因码失败。原因码只用这组，并带上 `retryable`：`MISSING`、`STALE`、`SOURCE_UNAVAILABLE`、`RATE_LIMITED`、`MODEL_FAILED`、`NAMESAKE_BLOCKED`、`CREST_MISSING`、`INCOMPLETE_SCHEDULE`、`SAMPLE_INSUFFICIENT`、`SCOPE_DENIED`、`INTERRUPTED`。

`squad.names` 在球员身上的 `MISSING` 表示没有英文名可译，`retryable=false`。不要按共享表里「叶子还没有快照」的 `true` 再派这些人。`MODEL_FAILED` 才再派。译名可疑不拦写回。

本轮在下列任一条件停止，不要为了凑齐再开一轮：

- **成功**：范围内该拿满的叶子都已拿满。图表若是 `SAMPLE_INSUFFICIENT`，这不是拿满。
- **`actionable_count == 0`**：没有仍为缺失、过期或 `retryable=true` 的叶子。
- **`INTERRUPTED`**：本轮被打断。停止派发，不把中断记成可重试。

用 `submit_answer` 交付操作结果：拿满了哪几片、失败原因码、图表是算出来的还是样本不足。事实字段留空。模型不改球员归属、比分、排名或统计数值。维护不解除避剧透。
